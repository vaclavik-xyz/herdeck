//! The desktop Maintenance section's shell side.
//!
//! - `maintenance_call`: a token-injecting proxy for the runtime's
//!   `/maintenance*` routes (deckapp/maintenance.py + bridge_update.py), built
//!   like `agent_card::agent_call` — the token never enters JS — but with an
//!   allow-list of EXACT paths rather than a prefix. `GET /maintenance` is
//!   stamped with what only the shell knows (`app`: its version, bundle, the
//!   bundled runtime binary and whether this shell spawned the runtime).
//! - `open_log`: opens the runtime log `/maintenance` reports or the app's own
//!   log, and nothing else (`validated_log_path`).
//! - `runtime_service`: runs the bundled frozen runtime's `service` subcommand
//!   (`herdeck-service install runtime --from-app <this bundle>`, restart,
//!   uninstall, status) with a timeout and a structured result.
//! - `restart_deck_from_shell`: the tray item / `[hotkeys].restart_deck` path.

use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use tauri::Manager;

use crate::proxy::{current_discovery, run_blocking};
use crate::sidecar::Discovery;
use crate::sync_util::LockExt;
use crate::{app_log, build_channel, http, AppState, HDR_TOKEN};

/// Longest bridge-update wait the runtime honours (bridge_update.UPDATE_WAIT_MAX_S).
pub const UPDATE_WAIT_MAX_MS: u64 = 25_000;
/// `GET /maintenance` may ask uhubctl for the D200's hub (10 s timeout there).
pub const STATUS_TIMEOUT: Duration = Duration::from_secs(15);
/// The runtime waits up to 15 s for the D200 to reopen (DECK_RESTART_TIMEOUT_S).
pub const DECK_RESTART_TIMEOUT: Duration = Duration::from_secs(20);
/// A uhubctl listing (10 s) plus the cycle itself (30 s), with headroom.
pub const POWER_CYCLE_TIMEOUT: Duration = Duration::from_secs(45);
/// The runtime's default bridge-update wait (15 s) when the body names none.
const UPDATE_DEFAULT_WAIT_MS: u64 = 15_000;

/// One allow-listed maintenance route.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MaintRoute {
    Status,
    DeckRestart,
    PowerCycle,
    /// POST: start (or join) a bridge update.
    UpdateStart,
    /// GET: long-poll a bridge update; carries the query's `wait_ms`.
    UpdatePoll { wait_ms: u64 },
}

/// A server id as a URL path segment: non-empty, URL-safe (percent-encoded by
/// the caller), and never a dot segment.
fn server_segment_ok(seg: &str) -> bool {
    !seg.is_empty()
        && seg != "."
        && seg != ".."
        && seg.bytes().all(|b| b.is_ascii_alphanumeric() || b"-_.~%".contains(&b))
}

/// The update long-poll query: exactly `after=<digits>` and `wait_ms=<digits>`
/// (each at most once, any order). Returns the wait.
fn update_poll_query(query: &str) -> Option<u64> {
    let mut after = false;
    let mut wait: Option<u64> = None;
    for kv in query.split('&') {
        let (k, v) = kv.split_once('=')?;
        if v.is_empty() || v.len() > 12 || !v.bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
        match k {
            "after" if !after => after = true,
            "wait_ms" if wait.is_none() => wait = Some(v.parse().ok()?),
            _ => return None,
        }
    }
    Some(wait.unwrap_or(0))
}

/// Classify `method` + `path` against the allow-list; `None` = refused.
pub fn maintenance_route(method: &str, path: &str) -> Option<MaintRoute> {
    let (route, query) = match path.split_once('?') {
        Some((r, q)) => (r, Some(q)),
        None => (path, None),
    };
    match (method, route, query) {
        ("GET", "/maintenance", None) => return Some(MaintRoute::Status),
        ("POST", "/maintenance/deck/restart", None) => return Some(MaintRoute::DeckRestart),
        ("POST", "/maintenance/deck/power-cycle", None) => return Some(MaintRoute::PowerCycle),
        _ => {}
    }
    let seg = route
        .strip_prefix("/maintenance/servers/")?
        .strip_suffix("/update")?;
    if !server_segment_ok(seg) {
        return None;
    }
    match (method, query) {
        ("POST", None) => Some(MaintRoute::UpdateStart),
        ("GET", Some(q)) => update_poll_query(q).map(|wait_ms| MaintRoute::UpdatePoll { wait_ms }),
        ("GET", None) => Some(MaintRoute::UpdatePoll { wait_ms: 0 }),
        _ => None,
    }
}

/// The read timeout for one route: every long wait outlasts the runtime's by
/// 6 s, so the proxy never cuts off an answer that is on its way.
pub fn maintenance_timeout(route: &MaintRoute, body: Option<&serde_json::Value>) -> Duration {
    let wait = |ms: u64| Duration::from_millis(ms.min(UPDATE_WAIT_MAX_MS)) + Duration::from_secs(6);
    match route {
        MaintRoute::Status => STATUS_TIMEOUT,
        MaintRoute::DeckRestart => DECK_RESTART_TIMEOUT,
        MaintRoute::PowerCycle => POWER_CYCLE_TIMEOUT,
        MaintRoute::UpdateStart => {
            let ms = body
                .and_then(|b| b.get("wait_ms"))
                .and_then(serde_json::Value::as_u64)
                .unwrap_or(UPDATE_DEFAULT_WAIT_MS);
            wait(ms).max(Duration::from_secs(10))
        }
        MaintRoute::UpdatePoll { wait_ms } => wait(*wait_ms).max(Duration::from_secs(10)),
    }
}

/// The GET path with the access token appended.
pub fn with_token(path: &str, token: &str) -> String {
    let sep = if path.contains('?') { '&' } else { '?' };
    format!("{path}{sep}token={token}")
}

/// What only the shell knows, for the Maintenance overview.
#[derive(Debug, Clone, serde::Serialize, PartialEq)]
pub struct AppFacts {
    pub version: String,
    pub channel: String,
    /// This .app (None in a dev build run from the checkout).
    pub bundle: Option<String>,
    /// The frozen runtime inside it, when present.
    pub bundled_runtime: Option<String>,
    /// True while the runtime is this shell's own supervised child.
    pub spawned_runtime: bool,
}

/// Stamp `app` onto the `/maintenance` object (non-objects pass through).
pub fn with_app_facts(mut status: serde_json::Value, app: &AppFacts) -> serde_json::Value {
    if let Some(map) = status.as_object_mut() {
        map.insert(
            "app".into(),
            serde_json::to_value(app).unwrap_or(serde_json::Value::Null),
        );
    }
    status
}

fn bundled_runtime_binary(app: &tauri::AppHandle) -> Option<PathBuf> {
    let primary = app.path().resource_dir().ok();
    let exe = std::env::current_exe().ok();
    let dir = crate::sidecar::resolve_resource_dir(primary.as_deref(), exe.as_deref())?;
    crate::sidecar::resolve_frozen_sidecar(&dir).map(|spec| PathBuf::from(spec.program))
}

fn app_facts(app: &tauri::AppHandle, state: &AppState) -> AppFacts {
    let bundle = std::env::current_exe()
        .ok()
        .and_then(|exe| crate::runtime_service::bundle_of_exe(&exe));
    let spawned_runtime = {
        let sup = state.supervisor.lock_or_recover().clone();
        let child = sup.child.lock_or_recover();
        child.is_some()
    };
    AppFacts {
        version: env!("CARGO_PKG_VERSION").to_string(),
        channel: build_channel::current().to_string(),
        bundle: bundle.map(|b| b.to_string_lossy().into_owned()),
        bundled_runtime: bundled_runtime_binary(app).map(|p| p.to_string_lossy().into_owned()),
        spawned_runtime,
    }
}

/// Relay one maintenance call → `{status, body}` (the body parsed as JSON,
/// null when empty). `Err` only when the route is refused or the runtime is
/// unreachable.
#[tauri::command]
pub(crate) async fn maintenance_call(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    method: String,
    path: String,
    body: Option<serde_json::Value>,
) -> Result<serde_json::Value, String> {
    let route = maintenance_route(&method, &path)
        .ok_or_else(|| format!("maintenance_call: route not allowed: {method} {path}"))?;
    let d = current_discovery(&state)?;
    let timeout = maintenance_timeout(&route, body.as_ref());
    let facts = (route == MaintRoute::Status).then(|| app_facts(&app, &state));
    run_blocking(move || {
        let (code, text) = if method == "GET" {
            let (code, bytes) = http::http_get_bytes(&d.host, d.port, &with_token(&path, &d.token), timeout)?;
            (code, String::from_utf8_lossy(&bytes).into_owned())
        } else {
            let payload = body.unwrap_or_else(|| serde_json::json!({})).to_string();
            http::http_post_json(&d.host, d.port, &path, (HDR_TOKEN, &d.token), &payload, timeout)?
        };
        let mut response = crate::agent_card::agent_response(code, &text);
        if let (Some(facts), Some(body)) = (facts, response.get_mut("body")) {
            *body = with_app_facts(body.take(), &facts);
        }
        Ok(response)
    })
    .await
}

// --- logs ---------------------------------------------------------------------

/// This app's own log (`app_log`), as `start_app_log` writes it.
pub fn app_log_file(home: &Path, xdg_state_home: Option<&str>, dev: bool) -> PathBuf {
    app_log::log_dir(home, xdg_state_home).join(app_log::log_file_name(dev))
}

/// Directories a log `open_log` may open can live in.
pub fn log_roots(home: &Path, xdg_state_home: Option<&str>) -> Vec<PathBuf> {
    let mut roots = vec![home.join("Library/Logs"), home.join(".local/state/herdeck")];
    if let Some(dir) = xdg_state_home.filter(|d| Path::new(d).is_absolute()) {
        roots.push(Path::new(dir).join("herdeck"));
    }
    roots
}

/// A reported log path is opened only when it is an absolute `*.log` regular
/// file that — after resolving symlinks — lives under one of `roots`.
pub fn validated_log_path(candidate: &str, roots: &[PathBuf]) -> Result<PathBuf, String> {
    let path = Path::new(candidate);
    if !path.is_absolute() || candidate.contains('\0') {
        return Err(format!("not an absolute path: {candidate}"));
    }
    if path.extension().and_then(|e| e.to_str()) != Some("log") {
        return Err(format!("not a .log file: {candidate}"));
    }
    let real = std::fs::canonicalize(path).map_err(|e| format!("{candidate}: {e}"))?;
    if !real.is_file() {
        return Err(format!("not a file: {candidate}"));
    }
    let inside = roots.iter().any(|root| {
        std::fs::canonicalize(root)
            .map(|root| real.starts_with(root))
            .unwrap_or(false)
    });
    if !inside {
        return Err(format!("not a herdeck log location: {candidate}"));
    }
    Ok(real)
}

fn open_with_system(path: &Path) -> Result<(), String> {
    let opener = if cfg!(target_os = "macos") { "/usr/bin/open" } else { "xdg-open" };
    Command::new(opener)
        .arg(path)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map(|_| ())
        .map_err(|e| format!("could not run {opener}: {e}"))
}

/// Open a log in the system viewer: `kind` "app" (this app's log) or
/// "runtime" (the path the runtime's `/maintenance` reports). Returns the path.
#[tauri::command]
pub(crate) async fn open_log(
    state: tauri::State<'_, AppState>,
    kind: String,
) -> Result<String, String> {
    let home = std::env::var("HOME").map_err(|_| "HOME is not set".to_string())?;
    let xdg = std::env::var("XDG_STATE_HOME").ok();
    let roots = log_roots(Path::new(&home), xdg.as_deref());
    let candidate = match kind.as_str() {
        "app" => app_log_file(Path::new(&home), xdg.as_deref(), build_channel::is_dev())
            .to_string_lossy()
            .into_owned(),
        "runtime" => {
            let d = current_discovery(&state)?;
            run_blocking(move || runtime_log_path(&d)).await?
        }
        other => return Err(format!("open_log: unknown log kind {other}")),
    };
    run_blocking(move || {
        let path = validated_log_path(&candidate, &roots)?;
        open_with_system(&path)?;
        Ok(path.to_string_lossy().into_owned())
    })
    .await
}

fn runtime_log_path(d: &Discovery) -> Result<String, String> {
    let (code, bytes) =
        http::http_get_bytes(&d.host, d.port, &with_token("/maintenance", &d.token), STATUS_TIMEOUT)?;
    if code != 200 {
        return Err(format!("GET /maintenance returned HTTP {code}"));
    }
    let status: serde_json::Value =
        serde_json::from_slice(&bytes).map_err(|e| format!("invalid /maintenance JSON: {e}"))?;
    status
        .pointer("/logs/runtime")
        .and_then(serde_json::Value::as_str)
        .map(str::to_string)
        .ok_or_else(|| "the runtime reports no log file".to_string())
}

// --- runtime service -------------------------------------------------------------

/// How long one `service` subcommand may run before it is killed.
pub fn service_timeout(action: &str) -> Duration {
    match action {
        "install" | "uninstall" => Duration::from_secs(60),
        _ => Duration::from_secs(20),
    }
}

/// Mirrors `service.validate_extra_env`: a plain variable name, never a
/// credential-looking one or one herdeck-service sets itself, no newlines.
pub fn validate_env_pair(pair: &str) -> Result<(), String> {
    let (key, value) = pair
        .split_once('=')
        .ok_or_else(|| format!("--env expects KEY=VALUE: {pair}"))?;
    let mut bytes = key.bytes();
    let first_ok = bytes.next().is_some_and(|b| b.is_ascii_alphabetic() || b == b'_');
    if !first_ok || !key.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_') {
        return Err(format!("--env name is not a valid variable name: {key}"));
    }
    let upper = key.to_ascii_uppercase();
    if ["TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL"]
        .iter()
        .any(|word| upper.contains(word))
    {
        return Err(format!("--env {key}: secrets never go into a service unit"));
    }
    if key == "HERDECK_RUNTIME_MANAGED" {
        return Err(format!("--env {key}: set by herdeck-service itself"));
    }
    if value.contains(['\n', '\r', '\0']) {
        return Err(format!("--env {key}: the value may not contain a newline"));
    }
    Ok(())
}

/// The argv (after the binary) for one runtime-service action.
pub fn service_argv(action: &str, bundle: Option<&Path>, env: &[String]) -> Result<Vec<String>, String> {
    let mut argv: Vec<String> = vec!["service".into(), action.into(), "runtime".into()];
    match action {
        "install" => {
            if !cfg!(target_os = "macos") {
                return Err("installing the runtime from the app is macOS only".into());
            }
            let bundle = bundle.ok_or("this app is not running from an .app bundle")?;
            argv.push("--from-app".into());
            argv.push(bundle.to_string_lossy().into_owned());
            for pair in env {
                validate_env_pair(pair)?;
                argv.push("--env".into());
                argv.push(pair.clone());
            }
            return Ok(argv);
        }
        "status" => argv.push("--json".into()),
        "restart" | "uninstall" => {}
        other => return Err(format!("runtime_service: unknown action {other}")),
    }
    if !env.is_empty() {
        return Err(format!("runtime_service: --env only applies to install, not {action}"));
    }
    Ok(argv)
}

/// The last `limit` bytes of `text`, on a char boundary.
fn tail(text: &str, limit: usize) -> String {
    let text = text.trim();
    if text.len() <= limit {
        return text.to_string();
    }
    let mut start = text.len() - limit;
    while !text.is_char_boundary(start) {
        start += 1;
    }
    text[start..].to_string()
}

/// The structured answer of one service run.
pub fn service_result(
    action: &str,
    exit_code: Option<i32>,
    timed_out: bool,
    stdout: &str,
    stderr: &str,
) -> serde_json::Value {
    let ok = !timed_out && exit_code == Some(0);
    let status = if action == "status" && ok {
        serde_json::from_str::<serde_json::Value>(stdout.trim()).ok()
    } else {
        None
    };
    serde_json::json!({
        "ok": ok,
        "action": action,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "stdout": tail(stdout, 2000),
        "stderr": tail(stderr, 2000),
        "status": status,
    })
}

/// Run `program argv…` with a timeout (killed when it expires).
fn run_with_timeout(program: &Path, argv: &[String], timeout: Duration) -> Result<(Option<i32>, bool, String, String), String> {
    let mut child = Command::new(program)
        .args(argv)
        .env_remove("HERDECK_RUNTIME_MANAGED")
        .env_remove(crate::sidecar::PARENT_WATCH_ENV)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("could not run {}: {e}", program.display()))?;
    let mut out = child.stdout.take();
    let mut err = child.stderr.take();
    let out_reader = std::thread::spawn(move || {
        let mut s = String::new();
        if let Some(o) = out.as_mut() {
            let _ = o.read_to_string(&mut s);
        }
        s
    });
    let err_reader = std::thread::spawn(move || {
        let mut s = String::new();
        if let Some(e) = err.as_mut() {
            let _ = e.read_to_string(&mut s);
        }
        s
    });
    let deadline = Instant::now() + timeout;
    let (code, timed_out) = loop {
        match child.try_wait() {
            Ok(Some(status)) => break (status.code(), false),
            Ok(None) if Instant::now() >= deadline => {
                let _ = child.kill();
                let _ = child.wait();
                break (None, true);
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(100)),
            Err(e) => return Err(format!("waiting for {}: {e}", program.display())),
        }
    };
    let stdout = out_reader.join().unwrap_or_default();
    let stderr = err_reader.join().unwrap_or_default();
    Ok((code, timed_out, stdout, stderr))
}

/// `herdeck-deckapp service <action> runtime …` from this app's bundle →
/// `{ok, action, exit_code, timed_out, stdout, stderr, status}`. `Err` when the
/// action cannot run at all (unknown action, bad env, dev build without a
/// bundled runtime).
#[tauri::command]
pub(crate) async fn runtime_service(
    app: tauri::AppHandle,
    action: String,
    env: Option<Vec<String>>,
) -> Result<serde_json::Value, String> {
    let binary = bundled_runtime_binary(&app)
        .ok_or("no bundled runtime in this build (dev build): use herdeck-service from the checkout")?;
    let bundle = std::env::current_exe()
        .ok()
        .and_then(|exe| crate::runtime_service::bundle_of_exe(&exe));
    let argv = service_argv(&action, bundle.as_deref(), &env.unwrap_or_default())?;
    let timeout = service_timeout(&action);
    run_blocking(move || {
        let (code, timed_out, stdout, stderr) = run_with_timeout(&binary, &argv, timeout)?;
        let result = service_result(&action, code, timed_out, &stdout, &stderr);
        eprintln!(
            "herdeck: runtime service {action}: ok={} exit={code:?} timed_out={timed_out}",
            result["ok"]
        );
        Ok(result)
    })
    .await
}

// --- tray / hotkey / navigation ----------------------------------------------------

/// Told to the app window: switch the settings to this section.
pub(crate) const OPEN_SECTION_EVENT: &str = "open-section";

/// HealthNotice's "Open Maintenance" (from either window): bring the settings
/// window forward on the Maintenance section.
#[tauri::command]
pub(crate) fn open_maintenance(app: tauri::AppHandle) {
    use tauri::Emitter;
    let _ = app.emit_to(crate::window_roles::APP_WINDOW, "open-settings", ());
    let _ = app.emit_to(crate::window_roles::APP_WINDOW, OPEN_SECTION_EVENT, "maintenance");
    crate::window_roles::show_role_window(&app, crate::window_roles::APP_WINDOW);
}

/// "Restart deck" from the tray or `[hotkeys].restart_deck`: nobody is looking
/// at a result, so it is logged.
pub(crate) fn restart_deck_from_shell(app: &tauri::AppHandle) {
    let discovery = {
        let state = app.state::<AppState>();
        current_discovery(&state)
    };
    let Ok(d) = discovery else {
        eprintln!("restart deck: runtime not ready");
        return;
    };
    tauri::async_runtime::spawn_blocking(move || {
        match http::http_post_json(
            &d.host,
            d.port,
            "/maintenance/deck/restart",
            (HDR_TOKEN, &d.token),
            "{}",
            DECK_RESTART_TIMEOUT,
        ) {
            Ok((code, body)) => eprintln!("restart deck: HTTP {code} {}", tail(&body, 300)),
            Err(e) => eprintln!("restart deck: {e}"),
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_the_exact_maintenance_routes_are_allowed() {
        assert_eq!(maintenance_route("GET", "/maintenance"), Some(MaintRoute::Status));
        assert_eq!(maintenance_route("POST", "/maintenance/deck/restart"), Some(MaintRoute::DeckRestart));
        assert_eq!(maintenance_route("POST", "/maintenance/deck/power-cycle"), Some(MaintRoute::PowerCycle));
        assert_eq!(maintenance_route("POST", "/maintenance/servers/m4/update"), Some(MaintRoute::UpdateStart));
        assert_eq!(
            maintenance_route("GET", "/maintenance/servers/m4/update?after=3&wait_ms=20000"),
            Some(MaintRoute::UpdatePoll { wait_ms: 20_000 })
        );
        assert_eq!(
            maintenance_route("GET", "/maintenance/servers/local%3Apersonal/update?wait_ms=5&after=0"),
            Some(MaintRoute::UpdatePoll { wait_ms: 5 })
        );
        // wrong method
        assert_eq!(maintenance_route("POST", "/maintenance"), None);
        assert_eq!(maintenance_route("GET", "/maintenance/deck/restart"), None);
        assert_eq!(maintenance_route("DELETE", "/maintenance/servers/m4/update"), None);
        // extra query / token smuggling
        assert_eq!(maintenance_route("GET", "/maintenance?token=evil"), None);
        assert_eq!(maintenance_route("POST", "/maintenance/deck/restart?x=1"), None);
        assert_eq!(maintenance_route("GET", "/maintenance/servers/m4/update?after=1&token=x"), None);
        assert_eq!(maintenance_route("GET", "/maintenance/servers/m4/update?after=1&after=2"), None);
        assert_eq!(maintenance_route("GET", "/maintenance/servers/m4/update?after=-1"), None);
        assert_eq!(maintenance_route("POST", "/maintenance/servers/m4/update?wait_ms=1"), None);
        // path games
        assert_eq!(maintenance_route("POST", "/maintenance/servers/../update"), None);
        assert_eq!(maintenance_route("POST", "/maintenance/servers//update"), None);
        assert_eq!(maintenance_route("POST", "/maintenance/servers/a/b/update"), None);
        assert_eq!(maintenance_route("POST", "/maintenance/servers/a b/update"), None);
        assert_eq!(maintenance_route("POST", "/maintenance/servers/m4/update\r\nX: y"), None);
        assert_eq!(maintenance_route("GET", "/config"), None);
        assert_eq!(maintenance_route("GET", "/maintenance/"), None);
        assert_eq!(maintenance_route("GET", "/agent/detail"), None);
    }

    #[test]
    fn update_waits_get_a_longer_read_timeout_than_the_wait() {
        let poll = MaintRoute::UpdatePoll { wait_ms: 20_000 };
        assert_eq!(maintenance_timeout(&poll, None), Duration::from_secs(26));
        let clamped = MaintRoute::UpdatePoll { wait_ms: 999_999 };
        assert_eq!(maintenance_timeout(&clamped, None), Duration::from_secs(31));
        let body = serde_json::json!({"wait_ms": 25_000});
        assert_eq!(maintenance_timeout(&MaintRoute::UpdateStart, Some(&body)), Duration::from_secs(31));
        // no wait named: the runtime's 15 s default
        assert_eq!(maintenance_timeout(&MaintRoute::UpdateStart, None), Duration::from_secs(21));
        assert_eq!(maintenance_timeout(&MaintRoute::UpdatePoll { wait_ms: 0 }, None), Duration::from_secs(10));
        assert!(maintenance_timeout(&MaintRoute::DeckRestart, None) > Duration::from_secs(15));
        assert!(maintenance_timeout(&MaintRoute::PowerCycle, None) > Duration::from_secs(40));
    }

    #[test]
    fn status_is_stamped_with_the_app_facts() {
        let facts = AppFacts {
            version: "0.9.0".into(),
            channel: "stable".into(),
            bundle: Some("/Applications/herdeck.app".into()),
            bundled_runtime: None,
            spawned_runtime: true,
        };
        let v = with_app_facts(serde_json::json!({"version": "0.9.0"}), &facts);
        assert_eq!(v["app"]["bundle"], "/Applications/herdeck.app");
        assert_eq!(v["app"]["spawned_runtime"], true);
        assert_eq!(with_app_facts(serde_json::Value::Null, &facts), serde_json::Value::Null);
    }

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("herdeck-maint-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn only_log_files_under_the_log_roots_open() {
        let home = scratch("logs");
        let logs = home.join("Library/Logs");
        std::fs::create_dir_all(logs.join("herdeck")).unwrap();
        let runtime = logs.join("herdeck-runtime.log");
        std::fs::write(&runtime, "x").unwrap();
        std::fs::write(logs.join("notes.txt"), "x").unwrap();
        std::fs::write(home.join("elsewhere.log"), "x").unwrap();
        let roots = log_roots(&home, None);

        let ok = validated_log_path(&runtime.to_string_lossy(), &roots).unwrap();
        assert!(ok.ends_with("herdeck-runtime.log"));
        assert!(validated_log_path("relative.log", &roots).is_err());
        assert!(validated_log_path(&logs.join("notes.txt").to_string_lossy(), &roots).is_err());
        assert!(validated_log_path(&home.join("elsewhere.log").to_string_lossy(), &roots).is_err());
        assert!(validated_log_path(&logs.join("missing.log").to_string_lossy(), &roots).is_err());
        assert!(validated_log_path(&logs.join("herdeck").to_string_lossy(), &roots).is_err());
        // a dot-dot escape resolves outside the roots
        let escape = logs.join("../../elsewhere.log");
        assert!(validated_log_path(&escape.to_string_lossy(), &roots).is_err());
        // a symlink into a root that points outside it is refused too
        #[cfg(unix)]
        {
            let link = logs.join("sneaky.log");
            std::os::unix::fs::symlink(home.join("elsewhere.log"), &link).unwrap();
            assert!(validated_log_path(&link.to_string_lossy(), &roots).is_err());
        }
        let _ = std::fs::remove_dir_all(&home);
    }

    #[test]
    fn app_log_file_matches_app_log() {
        let home = Path::new("/Users/me");
        let path = app_log_file(home, None, false);
        assert!(path.ends_with(app_log::log_file_name(false)));
        assert!(path.starts_with(app_log::log_dir(home, None)));
    }

    #[test]
    fn service_argv_per_action() {
        assert_eq!(service_argv("restart", None, &[]).unwrap(), ["service", "restart", "runtime"]);
        assert_eq!(service_argv("uninstall", None, &[]).unwrap(), ["service", "uninstall", "runtime"]);
        assert_eq!(service_argv("status", None, &[]).unwrap(), ["service", "status", "runtime", "--json"]);
        assert!(service_argv("bootstrap", None, &[]).is_err());
        assert!(service_argv("restart", None, &["A=1".into()]).is_err());
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn service_install_names_this_bundle_and_the_env() {
        let bundle = Path::new("/Applications/herdeck.app");
        let argv = service_argv(
            "install",
            Some(bundle),
            &["HERDECK_D200_STANDARD_WRITER=1".into()],
        )
        .unwrap();
        assert_eq!(
            argv,
            [
                "service", "install", "runtime", "--from-app", "/Applications/herdeck.app",
                "--env", "HERDECK_D200_STANDARD_WRITER=1",
            ]
        );
        assert!(service_argv("install", None, &[]).is_err());
        assert!(service_argv("install", Some(bundle), &["HERDECK_TOKEN=x".into()]).is_err());
    }

    #[test]
    fn env_pairs_are_validated_like_herdeck_service() {
        assert!(validate_env_pair("HERDECK_T3_DESKTOP_READ_STATE=1").is_ok());
        assert!(validate_env_pair("_X=").is_ok());
        assert!(validate_env_pair("NOEQUALS").is_err());
        assert!(validate_env_pair("1BAD=1").is_err());
        assert!(validate_env_pair("BAD-NAME=1").is_err());
        assert!(validate_env_pair("MY_api_token=1").is_err());
        assert!(validate_env_pair("DB_PASSWORD=1").is_err());
        assert!(validate_env_pair("HERDECK_RUNTIME_MANAGED=1").is_err());
        assert!(validate_env_pair("A=line\nbreak").is_err());
    }

    #[test]
    fn service_result_is_structured() {
        let status = service_result("status", Some(0), false, "{\"installed\":true}\n", "");
        assert_eq!(status["ok"], true);
        assert_eq!(status["status"]["installed"], true);
        let failed = service_result("restart", Some(1), false, "", "boom");
        assert_eq!(failed["ok"], false);
        assert_eq!(failed["stderr"], "boom");
        assert_eq!(failed["status"], serde_json::Value::Null);
        let timeout = service_result("install", None, true, "", "");
        assert_eq!(timeout["ok"], false);
        assert_eq!(timeout["timed_out"], true);
        assert_eq!(tail("ééé", 3), "é");
    }

    #[test]
    fn service_timeouts_are_bounded() {
        assert_eq!(service_timeout("install"), Duration::from_secs(60));
        assert_eq!(service_timeout("status"), Duration::from_secs(20));
    }

    #[cfg(unix)]
    #[test]
    fn run_with_timeout_kills_a_hung_child() {
        let (code, timed_out, _, _) =
            run_with_timeout(Path::new("/bin/sleep"), &["5".into()], Duration::from_millis(200)).unwrap();
        assert!(timed_out);
        assert_eq!(code, None);
        let (code, timed_out, out, _) =
            run_with_timeout(Path::new("/bin/echo"), &["hi".into()], Duration::from_secs(5)).unwrap();
        assert!(!timed_out);
        assert_eq!(code, Some(0));
        assert_eq!(out.trim(), "hi");
    }
}
