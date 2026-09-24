//! Which runtime this shell talks to, and keeping it that way: the startup
//! plan (env override, attach to a healthy launchd runtime via runtime.json,
//! or spawn our own sidecar), the supervisor generations of that sidecar,
//! re-discovery after a runtime restart, hand-over to a launchd runtime that
//! appears later, and falling back to our own sidecar when it is lost.

use std::env;
use std::path::{Path, PathBuf};
use std::process::Child;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tauri::{Emitter, Manager};

use crate::notify_pump::discovery_key;
use crate::shortcuts::register_toggle_hotkey_logged;
use crate::sidecar::{self, supervise, CommandSpec, Discovery, SupervisorConfig};
use crate::{build_channel, deck_prefs, http, AppState, DiscoveryView, SIDECAR_TIMEOUT};

/// One supervisor generation: its stop flag and its child slot.
#[derive(Clone, Default)]
pub(crate) struct Supervisor {
    pub(crate) stop: Arc<AtomicBool>,
    pub(crate) child: Arc<Mutex<Option<Child>>>,
}

/// Failed re-discoveries before a switched shell gives up on the launchd
/// runtime and spawns its own sidecar again. BOTH thresholds must be met, so a
/// runtime restart (new port within seconds) never triggers it, and a runtime
/// that answers one probe and dies cannot make the shell flap faster than
/// once per `ATTACH_LOST_AFTER` (+ `REATTACH_CHECK_INTERVAL` to switch back).
pub(crate) const ATTACH_LOST_FAILURES: u32 = 3;
pub(crate) const ATTACH_LOST_AFTER: Duration = Duration::from_secs(30);

/// Streak of failed re-discoveries of the attached runtime. Any successful
/// poll or re-discovery resets it.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub(crate) struct AttachLoss {
    failures: u32,
    since: Option<std::time::Instant>,
}

impl AttachLoss {
    pub(crate) fn record_ok(&mut self) {
        *self = AttachLoss::default();
    }

    /// Record one failure at `now`; true once the runtime counts as lost.
    pub(crate) fn record_failure(&mut self, now: std::time::Instant) -> bool {
        self.failures = self.failures.saturating_add(1);
        let since = *self.since.get_or_insert(now);
        self.failures >= ATTACH_LOST_FAILURES && now.duration_since(since) >= ATTACH_LOST_AFTER
    }
}

/// Minimum interval between on-disk re-discovery attempts.
pub(crate) const REDISCOVER_MIN_INTERVAL: Duration = Duration::from_secs(5);

/// How often a shell running on its own spawned sidecar re-reads runtime.json
/// for a healthy launchd runtime to hand over to (one small file read; a
/// `/health` probe only when the file names a runtime other than ours).
pub(crate) const REATTACH_CHECK_INTERVAL: Duration = Duration::from_secs(12);

/// The one log line recording which runtime this shell is on and why. Lands in
/// the app log (`app_log`), so "why did two runtimes run / why did a banner go
/// through osascript" is answerable afterwards. Never includes the token.
pub(crate) fn plan_log_line(plan: &str, reason: &str, url: Option<&str>) -> String {
    match url {
        Some(url) => format!("herdeck: runtime plan={plan} reason={reason} url={url}"),
        None => format!("herdeck: runtime plan={plan} reason={reason}"),
    }
}

/// Should a shell running on its own spawned sidecar switch to `candidate`
/// (the runtime named by runtime.json)? Only when it is a DIFFERENT runtime
/// than the current one and its `/health` answers.
pub(crate) fn reattach_target<F>(
    eligible: bool,
    current: Option<&Discovery>,
    candidate: Option<Discovery>,
    healthy: F,
) -> Option<Discovery>
where
    F: Fn(&Discovery) -> bool,
{
    if !eligible {
        return None;
    }
    let candidate = candidate?;
    if current.map(discovery_key) == Some(discovery_key(&candidate)) {
        return None;
    }
    sidecar::decide_runtime_attach(Some(candidate), healthy)
}

/// Move the shell from its own sidecar onto the launchd runtime `d`: adopt
/// the discovery, then stop the supervisor and our sidecar for good (it would
/// otherwise keep a second Orchestrator + bridge alive next to the runtime).
/// The discovery write and the stop flag change together under the discovery
/// lock, which the supervisor's callback also takes — a sidecar reporting in
/// mid-switch cannot repoint us back.
pub(crate) fn switch_to_runtime(app: &tauri::AppHandle, d: Discovery, reason: &str) -> bool {
    let state = app.state::<AppState>();
    {
        let mut current = state.discovery.lock().unwrap();
        if !state.reattach_eligible.swap(false, Ordering::SeqCst) {
            return false; // another caller switched first
        }
        state.supervisor.lock().unwrap().stop.store(true, Ordering::SeqCst);
        state.attached_from_runtime_json.store(true, Ordering::Relaxed);
        state.switched_from_spawn.store(true, Ordering::SeqCst);
        state.attach_loss.lock().unwrap().record_ok();
        *current = Some(d.clone());
    }
    eprintln!("{}", plan_log_line("attach", reason, Some(&d.url)));
    register_toggle_hotkey_logged(app, &d);
    let _ = app.emit("discovery", DiscoveryView::from(&d)); // token-free
    let child = state.supervisor.lock().unwrap().child.clone();
    std::thread::spawn(move || {
        let taken = child.lock().unwrap().take();
        if let Some(mut c) = taken {
            eprintln!("herdeck: stopping own sidecar pid={}", c.id());
            sidecar::stop_child(&mut c, sidecar::SIDECAR_STOP_GRACE);
        }
    });
    true
}

/// Switch to a healthy launchd runtime if this shell is on its own sidecar
/// and runtime.json names one. Returns the adopted discovery.
pub(crate) fn try_reattach(app: &tauri::AppHandle, reason: &str) -> Option<Discovery> {
    let state = app.state::<AppState>();
    let eligible = state.reattach_eligible.load(Ordering::SeqCst);
    if !eligible {
        return None;
    }
    let current = state.discovery.lock().unwrap().clone();
    let candidate = sidecar::read_runtime_discovery(&sidecar::runtime_file_path());
    let d = reattach_target(eligible, current.as_ref(), candidate, probe_runtime_health)?;
    switch_to_runtime(app, d.clone(), reason).then_some(d)
}

/// Periodically hand a self-spawned shell over to the launchd runtime once it
/// is healthy. The startup `/health` probe can miss it (e.g. right after an
/// auto-update relaunch), which used to leave two runtimes running until the
/// app was restarted.
pub(crate) fn start_reattach_watch(app: tauri::AppHandle) {
    std::thread::spawn(move || loop {
        std::thread::sleep(REATTACH_CHECK_INTERVAL);
        let state = app.state::<AppState>();
        if !state.reattach_eligible.load(Ordering::SeqCst)
            || state.supervisor.lock().unwrap().stop.load(Ordering::SeqCst)
        {
            return; // switched already, or quitting
        }
        try_reattach(&app, "launchd_runtime_appeared");
    });
}

/// A proxied poll of the current runtime succeeded: the attached runtime is
/// alive, so any failure streak is over.
pub(crate) fn note_runtime_ok(state: &AppState) {
    let mut loss = state.attach_loss.lock().unwrap();
    if loss.failures != 0 {
        loss.record_ok();
    }
}

/// The launchd runtime we switched to is gone for good (uninstalled, booted
/// out, crash-looping): without this the window would have no runtime at all
/// until the app restarts, since we stopped our own sidecar to switch. Start a
/// fresh supervisor (new stop flag + child slot), and make the shell eligible
/// to re-adopt a launchd runtime that becomes healthy later.
pub(crate) fn fall_back_to_spawn(app: &tauri::AppHandle) {
    let state = app.state::<AppState>();
    let Some(spec) = state.spawn_spec.lock().unwrap().clone() else {
        return;
    };
    let fresh = Supervisor::default();
    {
        let mut current = state.discovery.lock().unwrap();
        if !state.switched_from_spawn.swap(false, Ordering::SeqCst) {
            return; // not switched, or another caller already fell back
        }
        if state.quitting.load(Ordering::SeqCst) {
            return; // the app is quitting: spawn nothing
        }
        *state.supervisor.lock().unwrap() = fresh.clone();
        state.attached_from_runtime_json.store(false, Ordering::Relaxed);
        *current = None; // "sidecar not ready" until the new one reports in
    }
    state.attach_loss.lock().unwrap().record_ok();
    eprintln!("{}", plan_log_line("spawn", "attached_runtime_lost", None));
    start_supervisor(app.clone(), spec, fresh);
    state.reattach_eligible.store(true, Ordering::SeqCst);
    start_reattach_watch(app.clone());
}

/// Re-read `runtime.json` from disk and, when it describes a LIVE runtime
/// (`/health` probe passes), adopt it as the current discovery. An external
/// sidecar (launchd) picks a fresh port on every restart; a shell holding the
/// stale port would silently drift into osascript fallbacks (the user sees
/// duplicated alerts) while the deck keeps rendering from cached state.
/// Called on `/state` and `/notifications` failures. A shell on its OWN
/// sidecar takes this chance to switch to a healthy launchd runtime
/// (`try_reattach`) instead of waiting for the periodic watch. Returns the
/// adopted discovery, or `None` when the file is absent/unhealthy or the shell
/// was pointed at a runtime by env override. Rate-limited to one attempt per
/// `REDISCOVER_MIN_INTERVAL`.
pub(crate) fn rediscover_runtime(app: &tauri::AppHandle) -> Option<Discovery> {
    let state = app.state::<AppState>();
    let attached = state.attached_from_runtime_json.load(Ordering::Relaxed);
    let eligible = state.reattach_eligible.load(Ordering::SeqCst);
    if !attached && !eligible {
        // Env-override (or a channel that never attaches): on-disk
        // re-discovery must not silently repoint the shell.
        return None;
    }
    {
        let mut last = state.rediscover_last.lock().unwrap();
        if let Some(t) = *last {
            if t.elapsed() < REDISCOVER_MIN_INTERVAL {
                return None;
            }
        }
        *last = Some(std::time::Instant::now());
    }
    if eligible {
        return try_reattach(app, "own_sidecar_unreachable");
    }
    let healthy = sidecar::read_runtime_discovery(&sidecar::runtime_file_path())
        .and_then(|d| sidecar::decide_runtime_attach(Some(d), probe_runtime_health));
    let Some(d) = healthy else {
        let lost = state
            .attach_loss
            .lock()
            .unwrap()
            .record_failure(std::time::Instant::now());
        if lost && state.switched_from_spawn.load(Ordering::SeqCst) {
            fall_back_to_spawn(app);
        }
        return None;
    };
    state.attach_loss.lock().unwrap().record_ok();
    let changed = {
        let mut current = state.discovery.lock().unwrap();
        let changed = current.as_ref().map(discovery_key) != Some(discovery_key(&d));
        *current = Some(d.clone());
        changed
    };
    if changed {
        eprintln!("{}", plan_log_line("attach", "runtime_restarted", Some(&d.url)));
    }
    Some(d)
}

/// Probe an already-running headless runtime's token-authed `GET /health`
/// (Rust-side, so the token never enters JS). `true` iff it responds — the
/// signal that a `runtime.json` we found is live (not stale) and we should
/// ATTACH to it rather than spawn our own sidecar.
pub(crate) fn probe_runtime_health(d: &Discovery) -> bool {
    http::http_get(
        &d.host,
        d.port,
        &format!("/health?token={}", d.token),
        SIDECAR_TIMEOUT,
    )
    .is_ok()
}

/// How the sidecar is obtained: either an externally-managed one (dev override
/// via env, no spawn) or a child process we spawn and supervise.
pub(crate) enum SidecarPlan {
    External(Discovery, bool),
    Spawn(CommandSpec),
}

/// `<repo>/desktop/src-tauri` -> `<repo>`. Used to locate the dev `.venv`.
/// (Dev-mode only; the frozen/bundled sidecar is a later phase.)
pub(crate) fn repo_root_from_manifest() -> PathBuf {
    let manifest = Path::new(env!("CARGO_MANIFEST_DIR"));
    manifest
        .parent()
        .and_then(|p| p.parent())
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| manifest.to_path_buf())
}

/// Resolve `config.toml`'s path exactly as `run()` does at startup: the
/// dev-channel/`HERDECK_CONFIG` override, then the sidecar's own
/// existence-check order. Callable again later (see `reload_deck_always_on_top`)
/// so a live re-read can never disagree with what the config editor's own
/// `/config` write just persisted.
pub(crate) fn default_config_path() -> PathBuf {
    let home = PathBuf::from(env::var("HOME").unwrap_or_default());
    let repo_root = repo_root_from_manifest();
    let explicit_config = env::var("HERDECK_CONFIG").ok();
    let config_override = build_channel::config_override(explicit_config.as_deref(), &home);
    deck_prefs::resolve_config_path(
        config_override.as_ref().and_then(|path| path.to_str()),
        &home,
        &repo_root,
    )
}

/// Best-effort `http://host:port/...` split (informational fields for the
/// external-override path; the WebView only needs url+token).
pub(crate) fn parse_host_port(url: &str) -> (String, u16) {
    let after_scheme = url.split_once("://").map(|(_, rest)| rest).unwrap_or(url);
    let authority = after_scheme.split('/').next().unwrap_or(after_scheme);
    match authority.rsplit_once(':') {
        Some((h, p)) => (h.to_string(), p.parse::<u16>().unwrap_or(0)),
        None => (authority.to_string(), 0),
    }
}

/// The automatic plan plus the reason logged for it (`plan_log_line`).
pub(crate) fn resolve_automatic_plan<F>(
    channel: &str,
    resource_dir: Option<&Path>,
    repo_root: &Path,
    runtime_discovery: Option<Discovery>,
    healthy: F,
) -> (SidecarPlan, &'static str)
where
    F: Fn(&Discovery) -> bool,
{
    let reason = if !build_channel::shared_runtime_attach_enabled_for(channel) {
        "attach_disabled_for_channel"
    } else if runtime_discovery.is_none() {
        "no_runtime_json"
    } else if let Some(discovery) = sidecar::decide_runtime_attach(runtime_discovery, healthy) {
        return (SidecarPlan::External(discovery, true), "runtime_json_healthy");
    } else {
        "runtime_unhealthy"
    };
    (
        SidecarPlan::Spawn(sidecar::choose_spawn(resource_dir, repo_root)),
        reason,
    )
}

/// Decide how to obtain the sidecar. If `HERDECK_DECKAPP_URL` +
/// `HERDECK_DECKAPP_TOKEN` are set, trust that externally-started sidecar (handy
/// for manual `tauri dev` smoke without a `.venv`); otherwise spawn the dev venv.
pub(crate) fn resolve_plan(resource_dir: Option<PathBuf>) -> (SidecarPlan, &'static str) {
    if let (Ok(url), Ok(token)) = (
        env::var("HERDECK_DECKAPP_URL"),
        env::var("HERDECK_DECKAPP_TOKEN"),
    ) {
        if !url.is_empty() && !token.is_empty() {
            let (host, port) = parse_host_port(&url);
            let source =
                env::var("HERDECK_DECKAPP_SOURCE").unwrap_or_else(|_| "external".to_string());
            return (
                SidecarPlan::External(
                    Discovery {
                        url,
                        host,
                        port,
                        token,
                        source,
                    },
                    false,
                ),
                "env_override",
            );
        }
    }
    // Attach to an already-running headless runtime (herdeck.runtime) when its
    // discovery file is present AND /health responds: the window then shares the
    // runtime's Orchestrator + bridge + clock (D200 and window in lockstep) instead
    // of spawning its own sidecar. External == "we don't own it": quitting the
    // window never kills the launchd runtime. A missing/stale file falls through.
    let channel = build_channel::current();
    let runtime_discovery = build_channel::shared_runtime_attach_enabled()
        .then(|| sidecar::read_runtime_discovery(&sidecar::runtime_file_path()))
        .flatten();
    resolve_automatic_plan(
        channel,
        resource_dir.as_deref(),
        &repo_root_from_manifest(),
        runtime_discovery,
        probe_runtime_health,
    )
}

/// Start the sidecar supervisor (or record the external discovery). `config_path`
/// is exported as `HERDECK_CONFIG` so the spawned sidecar reads the SAME config
/// file Rust resolved the deck preferences from (mooting the sidecar's CWD-relative
/// branch — important for the frozen `.app`, where CWD is nondeterministic).
pub(crate) fn start_sidecar(app: &tauri::App, discovery: Arc<Mutex<Option<Discovery>>>, config_path: &Path) {
    let primary_resource_dir = app.path().resource_dir().ok();
    let executable = env::current_exe().ok();
    let resource_dir = sidecar::resolve_resource_dir(
        primary_resource_dir.as_deref(),
        executable.as_deref(),
    );
    let (plan, reason) = resolve_plan(resource_dir);
    match plan {
        SidecarPlan::External(d, from_runtime_json) => {
            eprintln!("{}", plan_log_line("attach", reason, Some(&d.url)));
            let view = DiscoveryView::from(&d);
            register_toggle_hotkey_logged(app.handle(), &d);
            if let Some(state) = app.try_state::<AppState>() {
                state
                    .attached_from_runtime_json
                    .store(from_runtime_json, Ordering::Relaxed);
            }
            *discovery.lock().unwrap() = Some(d);
            let _ = app.handle().emit("discovery", view); // token-free
        }
        SidecarPlan::Spawn(mut spec) => {
            eprintln!("{}", plan_log_line("spawn", reason, None));
            eprintln!("herdeck sidecar: spawning {}", spec.program);
            spec.envs.push((
                "HERDECK_CONFIG".to_string(),
                config_path.to_string_lossy().into_owned(),
            ));
            if let Some(service) = build_channel::keyring_service_override() {
                spec.envs
                    .push(("HERDECK_KEYRING_SERVICE".to_string(), service.to_string()));
            }
            let Some(state) = app.try_state::<AppState>() else {
                return;
            };
            *state.spawn_spec.lock().unwrap() = Some(spec.clone());
            let sup = state.supervisor.lock().unwrap().clone();
            start_supervisor(app.handle().clone(), spec, sup);
            if build_channel::shared_runtime_attach_enabled() {
                state.reattach_eligible.store(true, Ordering::SeqCst);
                start_reattach_watch(app.handle().clone());
            }
        }
    }
}

/// Run one supervisor generation on its own thread. Its discovery callback
/// checks ITS stop flag under the discovery lock, so once this generation is
/// stopped (switch to the launchd runtime, quit) a late report never repoints
/// the shell.
pub(crate) fn start_supervisor(handle: tauri::AppHandle, spec: CommandSpec, sup: Supervisor) {
    let callback_stop = sup.stop.clone();
    std::thread::spawn(move || {
        supervise(SupervisorConfig::new(spec), sup.child, sup.stop, move |d| {
            if let Some(state) = handle.try_state::<AppState>() {
                let mut current = state.discovery.lock().unwrap();
                if callback_stop.load(Ordering::SeqCst) {
                    return;
                }
                *current = Some(d.clone());
            }
            let view = DiscoveryView::from(&d);
            register_toggle_hotkey_logged(&handle, &d);
            let _ = handle.emit("discovery", view); // token-free
        });
    });
}
