//! herdeck desktop shell (phase 1, slice 3).
//!
//! Two windows with fixed roles — the borderless `main` deck overlay and the
//! decorated `config` settings window — plus a tray icon (show/hide/quit).
//! Neither ever changes shape, so nothing here needs a restart. On startup it
//! spawns and supervises the Python sidecar
//! (`python -m herdeck.deckapp`), reads its first stdout line (the discovery JSON
//! `{url, host, port, token, source}`), and hands the url+token to the WebView so
//! the frontend can reach the sidecar over loopback. The sidecar is restarted on
//! crash and killed on quit.

pub mod app_log;
pub mod build_channel;
pub mod deck_prefs;
pub mod hotkey;
pub mod http;
pub mod runtime_service;
pub mod sidecar;
pub mod window_state;

use std::env;
use std::path::{Path, PathBuf};
use std::process::Child;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tauri::menu::{CheckMenuItem, ContextMenu, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{
    Emitter, LogicalPosition, Manager, PhysicalPosition, WebviewUrl, WebviewWindowBuilder,
};
use tauri_plugin_updater::UpdaterExt;

use sidecar::{supervise, CommandSpec, Discovery, SupervisorConfig};
use window_state::WindowState;

/// The two fixed window roles. The labels are historical — `main` is the
/// borderless deck overlay, `config` the decorated settings window — and are
/// kept deliberately: `capabilities/default.json` scopes permissions to exactly
/// these two strings, and renaming would buy nothing a user can see.
const DECK_WINDOW: &str = "main";
const APP_WINDOW: &str = "config";

/// Told to the app window whenever the DECK's visibility changes, so its own
/// toggle button stays honest even when the tray, the hotkey, or the deck's
/// own close (⌘W) changed it from somewhere else. Emitted beside the tray
/// label sync in `show_role_window`/`hide_role_window` — same trigger, same
/// `deck_label_refresh` gate.
const DECK_VISIBILITY_EVENT: &str = "deck-visibility-changed";

/// Told to the DECK window (never the app window) when a zoom item is picked
/// from the deck's own right-click context menu. The zoom level itself lives
/// entirely in the deck's WebView (`floatingScale.ts`'s `localStorage` value
/// plus a CSS variable), so Rust cannot change it directly — it just forwards
/// the chosen command ("in" | "out" | "reset") and `App.svelte` applies it via
/// the same `applyFloatingScale` the ⌘+/⌘-/⌘0 keydown handler already uses.
/// Mirrors the `DECK_VISIBILITY_EVENT` emit_to pattern above.
const FLOATING_ZOOM_EVENT: &str = "floating-zoom-command";

/// Every tray/context-menu item id: built once in `build_tray` (or, for
/// "show_app"/"deck_aot", also in `build_deck_context_menu`, which reuses
/// them so the two menus share one `on_menu_event` handler instead of each
/// carrying its own) and matched again in `on_menu_event`. A plain string
/// literal repeated across those sites has no compiler check tying them
/// together — a typo in any one copy (e.g. `MenuItem::with_id(app,
/// "zoom_ni", …)`) builds fine and produces a menu item that silently never
/// fires, which no test can catch either. These consts are the single
/// spelling every site refers to instead, so the whole handler reads one way.
const MENU_ID_SHOW_APP: &str = "show_app";
const MENU_ID_DECK_AOT: &str = "deck_aot";
const MENU_ID_HIDE_DECK: &str = "hide_deck";
const MENU_ID_ZOOM_IN: &str = "zoom_in";
const MENU_ID_ZOOM_OUT: &str = "zoom_out";
const MENU_ID_ZOOM_RESET: &str = "zoom_reset";
const MENU_ID_TOGGLE_DECK: &str = "toggle_deck";
const MENU_ID_RECONNECT: &str = "reconnect";
const MENU_ID_AUTOSTART: &str = "autostart";
const MENU_ID_CHECK_UPDATE: &str = "check_update";
const MENU_ID_QUIT: &str = "quit";

/// Managed state read by the `get_discovery` command and by the supervisor
/// callback. The live child handle and stop flag are held as separate `Arc`s
/// owned by the supervisor + exit-handler closures (not routed through here).
struct AppState {
    discovery: Arc<Mutex<Option<Discovery>>>,
    /// Which windows are open and where the deck sits. Mirrored to disk on every
    /// show/hide and on exit, so the next launch reopens the same layout.
    window_state: Arc<Mutex<WindowState>>,
    /// The live value of `[desktop].deck_always_on_top`, applied to the deck
    /// window and flipped by the tray's `deck_aot` checkbox. Kept in memory
    /// (rather than re-read from config) so a failed persist can revert the
    /// checkbox to what is ACTUALLY in effect, not to stale config text.
    deck_always_on_top: Arc<Mutex<bool>>,
    /// Generation-scoped cursor acknowledged only after native delivery.
    notify_cursor: Arc<Mutex<NotifyCursor>>,
    /// True once macOS notification permission is GRANTED. Until then the
    /// dedicated pump does not claim delivery, so the runtime keeps using its
    /// osascript fallback instead of swallowing both banner and sound.
    notify_permission: Arc<AtomicBool>,
    /// Rate-limiter for `rediscover_runtime` (last attempt timestamp).
    rediscover_last: Arc<Mutex<Option<std::time::Instant>>>,
    /// True only when this shell ATTACHED to an external runtime discovered
    /// through runtime.json (not env override, not self-spawned) — the only
    /// case where on-disk re-discovery may repoint the shell.
    attached_from_runtime_json: Arc<AtomicBool>,
    /// True while this shell runs on its OWN spawned sidecar in a channel that
    /// may attach the shared runtime: a healthy launchd runtime appearing in
    /// runtime.json then takes over (see `try_reattach`). Cleared by the switch.
    reattach_eligible: Arc<AtomicBool>,
    /// The CURRENT supervisor (stop flag + child slot). A switch to the
    /// launchd runtime stops it; losing that runtime again starts a fresh one
    /// with its own flag + slot, so a late-waking old supervisor can never
    /// restart itself or reap the new child. The exit handler stops whichever
    /// is current.
    supervisor: Arc<Mutex<Supervisor>>,
    /// The spawn recipe of our own sidecar (set only when this shell spawned
    /// one), kept for `fall_back_to_spawn`.
    spawn_spec: Arc<Mutex<Option<CommandSpec>>>,
    /// True after `switch_to_runtime` moved us off our own sidecar: only then
    /// may a lost launchd runtime bring our own sidecar back.
    switched_from_spawn: Arc<AtomicBool>,
    /// Consecutive failed re-discoveries of the attached runtime.
    attach_loss: Arc<Mutex<AttachLoss>>,
    /// Set by the exit handler: no fallback may spawn a sidecar after it.
    quitting: Arc<AtomicBool>,
}

/// One supervisor generation: its stop flag and its child slot.
#[derive(Clone, Default)]
struct Supervisor {
    stop: Arc<AtomicBool>,
    child: Arc<Mutex<Option<Child>>>,
}

/// Failed re-discoveries before a switched shell gives up on the launchd
/// runtime and spawns its own sidecar again. BOTH thresholds must be met, so a
/// runtime restart (new port within seconds) never triggers it, and a runtime
/// that answers one probe and dies cannot make the shell flap faster than
/// once per `ATTACH_LOST_AFTER` (+ `REATTACH_CHECK_INTERVAL` to switch back).
const ATTACH_LOST_FAILURES: u32 = 3;
const ATTACH_LOST_AFTER: Duration = Duration::from_secs(30);

/// Streak of failed re-discoveries of the attached runtime. Any successful
/// poll or re-discovery resets it.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
struct AttachLoss {
    failures: u32,
    since: Option<std::time::Instant>,
}

impl AttachLoss {
    fn record_ok(&mut self) {
        *self = AttachLoss::default();
    }

    /// Record one failure at `now`; true once the runtime counts as lost.
    fn record_failure(&mut self, now: std::time::Instant) -> bool {
        self.failures = self.failures.saturating_add(1);
        let since = *self.since.get_or_insert(now);
        self.failures >= ATTACH_LOST_FAILURES && now.duration_since(since) >= ATTACH_LOST_AFTER
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
struct NotifyCursor {
    generation: Option<String>,
    seq: u64,
}

#[derive(Debug, Clone)]
struct PendingNotification {
    id: String,
    generation: String,
    seq: u64,
    title: String,
    body: String,
    sound: serde_json::Value,
    /// PNG of the agent's project mark written by the runtime (notify_icons).
    icon: Option<String>,
    created_at_ms: Option<i64>,
}

/// Minimum interval between on-disk re-discovery attempts.
const REDISCOVER_MIN_INTERVAL: Duration = Duration::from_secs(5);

/// How often a shell running on its own spawned sidecar re-reads runtime.json
/// for a healthy launchd runtime to hand over to (one small file read; a
/// `/health` probe only when the file names a runtime other than ours).
const REATTACH_CHECK_INTERVAL: Duration = Duration::from_secs(12);

/// Default timeout for the Rust-side sidecar proxy calls.
const SIDECAR_TIMEOUT: Duration = Duration::from_secs(3);

/// The runtime holds a long poll for 25 s; leave transport headroom around it.
const NOTIFY_POLL_TIMEOUT: Duration = Duration::from_secs(30);
const NOTIFY_RETRY_DELAY: Duration = Duration::from_millis(500);
/// A source without a notification feed (demo/mock) answers `/notifications`
/// with 404. That will not change until the shell is pointed at a different
/// runtime, so the pump sleeps this long — or until discovery changes.
const NOTIFY_UNSUPPORTED_BACKOFF: Duration = Duration::from_secs(30);

/// `/setup/connect` runs, inside the sidecar, the whole remote transaction: a probe
/// (≈4 s) THEN build + render-prepare + keychain/config snapshots + write + swap. The
/// proxy must comfortably outlast the full worst case (not just the probe) so it never
/// times out while the sidecar is mid-persist (a torn result). 15 s leaves wide margin
/// over the 4 s probe + the sub-second post-probe work; far above the 3 s SIDECAR_TIMEOUT.
const SETUP_CONNECT_TIMEOUT: Duration = Duration::from_secs(15);

/// The sidecar's mutating routes authenticate with this header (matches web.py
/// and the deck `/press`). GET routes use a `?token=` query param instead.
const HDR_TOKEN: &str = "X-Herdeck-Token";

/// What the WebView is told about the sidecar. The access **token is deliberately
/// omitted**: the frontend never talks to the sidecar directly. It invokes the
/// token-free `check_health` / `deck_state` / `deck_tile` / `deck_panel` /
/// `deck_press` commands below, which inject the token Rust-side, so it never
/// lives in JS. `DiscoveryView` is just the readiness signal + `source`/url info.
#[derive(Debug, Clone, serde::Serialize)]
struct DiscoveryView {
    url: String,
    host: String,
    port: u16,
    source: String,
}

impl From<&Discovery> for DiscoveryView {
    fn from(d: &Discovery) -> Self {
        DiscoveryView {
            url: d.url.clone(),
            host: d.host.clone(),
            port: d.port,
            source: d.source.clone(),
        }
    }
}

#[derive(Debug, Clone, serde::Serialize)]
struct UpdateMetadata {
    version: String,
    current_version: String,
}

/// Check only the signed HTTPS updater channel. A missing release or offline
/// network is an error to the caller, which the automatic UI check suppresses.
#[tauri::command]
async fn update_check(app: tauri::AppHandle) -> Result<Option<UpdateMetadata>, String> {
    if !build_channel::updates_enabled() {
        return Ok(None);
    }
    let update = app
        .updater()
        .map_err(|e| e.to_string())?
        .check()
        .await
        .map_err(|e| e.to_string())?;
    Ok(update.map(|update| UpdateMetadata {
        version: update.version,
        current_version: app.package_info().version.to_string(),
    }))
}

/// Re-check the signed channel immediately before installation, then let the
/// updater verify, replace, and restart the complete desktop bundle.
#[tauri::command]
async fn update_install(app: tauri::AppHandle) -> Result<bool, String> {
    if !build_channel::updates_enabled() {
        return Ok(false);
    }
    let update = app
        .updater()
        .map_err(|e| e.to_string())?
        .check()
        .await
        .map_err(|e| e.to_string())?;
    let Some(update) = update else {
        return Ok(false);
    };
    update
        .download_and_install(|_, _| {}, || {})
        .await
        .map_err(|e| e.to_string())?;
    // A `herdeck-service install runtime --from-app` unit runs the runtime
    // bundled in this .app; restart it now so runtime and app stay in step.
    let _ = tauri::async_runtime::spawn_blocking(
        runtime_service::restart_bundled_runtime_after_update,
    )
    .await;
    app.request_restart();
    Ok(true)
}

/// Frontend pulls the latest sidecar discovery (url + source — no token). Returns
/// `None` until the supervised sidecar has reported in; the WebView retries.
#[tauri::command]
fn get_discovery(state: tauri::State<'_, AppState>) -> Option<DiscoveryView> {
    state
        .discovery
        .lock()
        .unwrap()
        .as_ref()
        .map(DiscoveryView::from)
}

/// The current discovery, or an error until the supervised sidecar has reported
/// in. Shared by every proxy command so the token-pull lives in one place.
fn current_discovery(state: &tauri::State<'_, AppState>) -> Result<Discovery, String> {
    state
        .discovery
        .lock()
        .unwrap()
        .clone()
        .ok_or_else(|| "sidecar not ready".to_string())
}

/// The one log line recording which runtime this shell is on and why. Lands in
/// the app log (`app_log`), so "why did two runtimes run / why did a banner go
/// through osascript" is answerable afterwards. Never includes the token.
fn plan_log_line(plan: &str, reason: &str, url: Option<&str>) -> String {
    match url {
        Some(url) => format!("herdeck: runtime plan={plan} reason={reason} url={url}"),
        None => format!("herdeck: runtime plan={plan} reason={reason}"),
    }
}

/// Should a shell running on its own spawned sidecar switch to `candidate`
/// (the runtime named by runtime.json)? Only when it is a DIFFERENT runtime
/// than the current one and its `/health` answers.
fn reattach_target<F>(
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
fn switch_to_runtime(app: &tauri::AppHandle, d: Discovery, reason: &str) -> bool {
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
fn try_reattach(app: &tauri::AppHandle, reason: &str) -> Option<Discovery> {
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
fn start_reattach_watch(app: tauri::AppHandle) {
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
fn note_runtime_ok(state: &AppState) {
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
fn fall_back_to_spawn(app: &tauri::AppHandle) {
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
fn rediscover_runtime(app: &tauri::AppHandle) -> Option<Discovery> {
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

/// Run a blocking sidecar HTTP call off the invoking thread. The proxy commands
/// are `async fn`s (so Tauri dispatches them on its async runtime instead of the
/// main thread) and push their blocking TCP I/O onto the runtime's dedicated
/// blocking pool — a slow or wedged sidecar can no longer freeze window drag,
/// the tray, or the other webview for seconds per call.
async fn run_blocking<T: Send + 'static>(
    f: impl FnOnce() -> Result<T, String> + Send + 'static,
) -> Result<T, String> {
    tauri::async_runtime::spawn_blocking(f)
        .await
        .map_err(|e| format!("sidecar proxy task failed: {e}"))?
}

/// Probe an already-running headless runtime's token-authed `GET /health`
/// (Rust-side, so the token never enters JS). `true` iff it responds — the
/// signal that a `runtime.json` we found is live (not stale) and we should
/// ATTACH to it rather than spawn our own sidecar.
fn probe_runtime_health(d: &Discovery) -> bool {
    http::http_get(
        &d.host,
        d.port,
        &format!("/health?token={}", d.token),
        SIDECAR_TIMEOUT,
    )
    .is_ok()
}

/// Probe the sidecar's token-authed `GET /health` and return its JSON. Done
/// Rust-side (not via WebView `fetch`) so it isn't blocked by CORS, and so the
/// access token never has to live in JS. `Err` if the sidecar isn't ready yet
/// or is unreachable. The shell adds its own `app_version`, so the window can
/// warn when it is attached to a runtime of a different release.
#[tauri::command]
async fn check_health(state: tauri::State<'_, AppState>) -> Result<serde_json::Value, String> {
    let d = current_discovery(&state)?;
    run_blocking(move || {
        let body = http::http_get(
            &d.host,
            d.port,
            &format!("/health?token={}", d.token),
            SIDECAR_TIMEOUT,
        )?;
        let health = serde_json::from_str::<serde_json::Value>(&body)
            .map_err(|e| format!("invalid /health JSON from sidecar: {e}"))?;
        Ok(with_app_version(health, env!("CARGO_PKG_VERSION")))
    })
    .await
}

/// Stamp the shell's version onto a `/health` object (non-objects pass through).
fn with_app_version(mut health: serde_json::Value, version: &str) -> serde_json::Value {
    if let Some(map) = health.as_object_mut() {
        map.insert("app_version".into(), serde_json::Value::from(version));
    }
    health
}

/// Proxy `GET /state` (token injected Rust-side) → its JSON. This is the deck's
/// poll endpoint; the WebView never sees the token. The poll carries the
/// banner-claim headers (`X-Herdeck-Shell`/`-Gen`, only with a granted
    /// permission) — a claim is just liveness; the actual posting of feed entries
/// lives in `start_notify_pump` (single poster by design).
///
/// Long poll (C2): with `after` (JS `after`, the last `version` seen) the
/// runtime holds the request until the version moves or `waitMs` (JS key; clamped
/// to 25 s) passes. The HTTP read timeout then outlasts the wait by 6 s.
///
/// The body is handed to the WebView as the raw JSON text (`ipc::Response`), not
/// parsed into a `Value` and re-serialised. One typed pass still validates it —
/// never forward unchecked text as JSON — and reads the blocked-agent count for
/// the tray tooltip on the way.
#[tauri::command]
async fn deck_state(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    after: Option<u64>,
    wait_ms: Option<u64>,
) -> Result<tauri::ipc::Response, String> {
    let d = current_discovery(&state)?;
    // The dedicated long-poll pump owns the claim. UI state polling must not
    // compete with it or keep a failed native-notification claim alive.
    let claim = false;
    let timeout = state_request_timeout(after, wait_ms);
    let fetch = move |d: Discovery| {
        run_blocking(move || {
            http::fetch_state_poll(
                &d.host,
                d.port,
                &d.token,
                timeout,
                claim,
                Some(&shell_gen()),
                after,
                wait_ms,
            )
        })
    };
    let body = match fetch(d).await {
        Ok(body) => {
            note_runtime_ok(&state);
            body
        }
        Err(first_err) => {
            // Stale external sidecar (launchd restart → fresh port): re-read
            // runtime.json once and retry against the live runtime.
            let Some(d) = rediscover_runtime(&app) else {
                return Err(first_err);
            };
            fetch(d).await?
        }
    };
    let peek = peek_state(&body)?;
    update_tray_blocked(&app, peek.blocked());
    Ok(tauri::ipc::Response::new(body))
}

/// HTTP read timeout for a `/state` request: the plain proxy timeout, or — for
/// a long poll — the (clamped) wait plus 6 s of transport headroom (C2).
fn state_request_timeout(after: Option<u64>, wait_ms: Option<u64>) -> Duration {
    match after {
        Some(_) => {
            let wait = wait_ms.unwrap_or(0).min(http::STATE_MAX_WAIT_MS);
            SIDECAR_TIMEOUT.max(Duration::from_millis(wait) + Duration::from_secs(6))
        }
        None => SIDECAR_TIMEOUT,
    }
}

/// The only `/state` fields the shell itself reads. Deserialising into this
/// validates the whole document without building a `serde_json::Value`.
#[derive(serde::Deserialize)]
struct StatePeek {
    #[serde(default)]
    summary: Option<SummaryPeek>,
}

#[derive(serde::Deserialize)]
struct SummaryPeek {
    #[serde(default)]
    blocked: Option<u64>,
}

impl StatePeek {
    fn blocked(&self) -> Option<u64> {
        self.summary.as_ref().and_then(|s| s.blocked)
    }
}

fn peek_state(body: &str) -> Result<StatePeek, String> {
    serde_json::from_str(body).map_err(|e| format!("invalid /state JSON from sidecar: {e}"))
}

/// Per-process shell identity for the banner claim. A fresh shell process
/// carries a new generation, so the runtime can reject a stale shell's
/// fallback request without resetting its acknowledged feed.
fn shell_gen() -> String {
    use std::sync::OnceLock;
    static GEN: OnceLock<String> = OnceLock::new();
    GEN.get_or_init(|| {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        format!("{}-{:x}", std::process::id(), nanos)
    })
    .clone()
}

fn notification_batch(
    state_json: &serde_json::Value,
    cursor: &NotifyCursor,
) -> Option<(String, u64, Vec<PendingNotification>)> {
    let generation = state_json.get("generation")?.as_str()?.to_string();
    let acked_seq = state_json
        .get("acked_seq")
        .and_then(|v| v.as_u64())
        .unwrap_or(0);
    let floor = if cursor.generation.as_deref() == Some(generation.as_str()) {
        cursor.seq.max(acked_seq)
    } else {
        acked_seq
    };
    let mut items = Vec::new();
    for item in state_json.get("items").and_then(|v| v.as_array()).into_iter().flatten() {
        let seq = item.get("seq").and_then(|v| v.as_u64()).unwrap_or(0);
        if seq <= floor {
            continue;
        }
        items.push(PendingNotification {
            id: item
                .get("id")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string(),
            generation: generation.clone(),
            seq,
            title: item
                .get("title")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string(),
            body: item
                .get("body")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string(),
            sound: item.get("sound").cloned().unwrap_or(serde_json::Value::Bool(false)),
            icon: item.get("icon").and_then(|v| v.as_str()).map(str::to_string),
            created_at_ms: item.get("created_at_ms").and_then(|v| v.as_i64()),
        });
    }
    items.sort_by_key(|item| item.seq);
    Some((generation, acked_seq, items))
}

/// The sound played for `sound = true` (and for a test banner with no sound
/// picked), matching the runtime's osascript fallback.
const DEFAULT_NOTIFICATION_SOUND: &str = "Glass";

/// File extensions a named sound can have. Only used to list and check names:
/// the banner itself carries the bare name (`NSUserNotification.soundName`, and
/// the runtime's osascript `sound name`), which macOS resolves by `NSSound(named:)`.
const SOUND_EXTENSIONS: [&str; 6] = ["aiff", "aif", "caf", "wav", "m4a", "mp3"];

/// Where named sounds live, in `NSSound(named:)`'s own search order: the
/// user's, then the machine's, then the system's. Empty off macOS — there the
/// settings UI falls back to a free-text sound field.
fn sound_dirs() -> Vec<PathBuf> {
    if !cfg!(target_os = "macos") {
        return Vec::new();
    }
    let mut dirs = Vec::new();
    if let Ok(home) = env::var("HOME") {
        if !home.is_empty() {
            dirs.push(PathBuf::from(home).join("Library/Sounds"));
        }
    }
    dirs.push(PathBuf::from("/Library/Sounds"));
    dirs.push(PathBuf::from("/System/Library/Sounds"));
    dirs
}

/// A sound name safe to put on a banner / hand to the runtime: letters, digits, space,
/// `_` and `-` — no path separators, no dots, nothing to escape.
fn valid_sound_name(name: &str) -> bool {
    !name.is_empty()
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, ' ' | '_' | '-'))
}

fn sound_stem(path: &Path) -> Option<String> {
    let ext = path.extension()?.to_str()?.to_ascii_lowercase();
    if !SOUND_EXTENSIONS.contains(&ext.as_str()) {
        return None;
    }
    let stem = path.file_stem()?.to_str()?;
    valid_sound_name(stem).then(|| stem.to_string())
}

/// Every playable sound name found in `dirs`, sorted and de-duplicated. Only
/// names that pass `valid_sound_name` are offered, so the settings picker can
/// never list a sound that playback would then refuse.
fn list_sound_names(dirs: &[PathBuf]) -> Vec<String> {
    let mut names: Vec<String> = dirs
        .iter()
        .filter_map(|dir| std::fs::read_dir(dir).ok())
        .flatten()
        .filter_map(|entry| entry.ok())
        .filter(|entry| entry.path().is_file())
        .filter_map(|entry| sound_stem(&entry.path()))
        .collect();
    names.sort_by_key(|n| n.to_lowercase());
    names.dedup();
    names
}

/// The file a sound name plays, searching `dirs` in order.
fn resolve_sound_path(name: &str, dirs: &[PathBuf]) -> Option<PathBuf> {
    if !valid_sound_name(name) {
        return None;
    }
    dirs.iter().find_map(|dir| {
        SOUND_EXTENSIONS
            .iter()
            .map(|ext| dir.join(format!("{name}.{ext}")))
            .find(|path| path.is_file())
    })
}

/// The sound a feed item (or test request) asks for: a name, `true` for the
/// default, anything else for silence.
fn requested_sound_name(sound: &serde_json::Value) -> Option<&str> {
    match sound {
        serde_json::Value::String(name) if !name.is_empty() => Some(name.as_str()),
        serde_json::Value::Bool(true) => Some(DEFAULT_NOTIFICATION_SOUND),
        _ => None,
    }
}

/// The sound to attach to a banner: `Ok(None)` = silent (`false`, `""`,
/// `sound = false`), `Ok(Some(name))` = a name found in `dirs`, `Err` = a bad or
/// missing name. The sound rides on the notification itself (never a separate
/// player), so Focus / Do Not Disturb silences the sound together with the
/// banner instead of leaving a sound with no banner.
fn banner_sound_name(
    sound: &serde_json::Value,
    dirs: &[PathBuf],
) -> Result<Option<String>, String> {
    let Some(name) = requested_sound_name(sound) else {
        return Ok(None);
    };
    if !valid_sound_name(name) {
        return Err(format!("invalid notification sound name: {name:?}"));
    }
    if resolve_sound_path(name, dirs).is_none() {
        return Err(format!("notification sound not found: {name}"));
    }
    Ok(Some(name.to_string()))
}

/// The banner image path from a feed item, if it is one the runtime's
/// notify_icons wrote: an absolute path to `[a-z0-9-]+.png` directly inside a
/// `notification-icons` directory. Anything else is ignored (no image), so a
/// feed item can never make the shell read an arbitrary file.
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
fn banner_image_path(raw: &str) -> Option<&str> {
    let path = Path::new(raw);
    let name = path.file_name()?.to_str()?;
    let stem = name.strip_suffix(".png")?;
    let parent_ok = path
        .parent()
        .and_then(|p| p.file_name())
        .is_some_and(|dir| dir == "notification-icons");
    let stem_ok = !stem.is_empty()
        && stem
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-');
    (path.is_absolute() && parent_ok && stem_ok).then_some(raw)
}

/// Bring the deck forward: un-hide the app (macOS), show and focus the deck
/// window. What a click on one of our banners, and a Dock/Finder reopen, do.
fn reveal_deck(app: &tauri::AppHandle) {
    let handle = app.clone();
    let _ = app.run_on_main_thread(move || {
        #[cfg(target_os = "macos")]
        let _ = handle.show();
        show_role_window(&handle, DECK_WINDOW);
    });
}

/// Localized title/body of the banner the settings "Test notification" button
/// shows. Native text, so it lives beside `tray_labels` rather than in a
/// WebView catalog.
fn test_notification_texts(lang: &str) -> (&'static str, &'static str) {
    match lang {
        "cs" => (
            "Herdeck – zkušební oznámení",
            "Takhle vypadá upozornění, když agent potřebuje vaši pozornost.",
        ),
        _ => (
            "Herdeck test notification",
            "This is how an alert looks when an agent needs your attention.",
        ),
    }
}

/// Named sounds the notification settings can offer (C4): sorted unique stems
/// from the macOS sound folders. Empty elsewhere — the UI then shows a free-text
/// field instead of a picker.
#[tauri::command]
fn notification_sounds() -> Vec<String> {
    list_sound_names(&sound_dirs())
}

/// Show a localized test banner through the same native path real alerts take,
/// carrying `sound` (a name; `None` = the default sound, `""` = silent). `Err`
/// carries a message the settings UI shows as is. The native post blocks until
/// Notification Center confirms delivery (up to ~2 s) and that confirmation
/// arrives on the main thread, so it runs on the blocking pool — never on the
/// main thread or an async worker.
#[tauri::command]
async fn test_notification(
    app: tauri::AppHandle,
    tray: tauri::State<'_, TrayHandles>,
    sound: Option<String>,
) -> Result<(), String> {
    let lang = tray
        .0
        .lock()
        .unwrap()
        .as_ref()
        .map(TrayMenuItems::current_lang)
        .unwrap_or_else(|| "en".to_string());
    let (title, body) = test_notification_texts(&lang);
    let sound = match sound {
        Some(name) => serde_json::Value::String(name),
        None => serde_json::Value::Bool(true),
    };
    // A bad or missing sound still shows the (silent) banner, then reports
    // why it was silent. Off macOS banners carry no sound at all.
    let sound_problem = if cfg!(target_os = "macos") {
        banner_sound_name(&sound, &sound_dirs()).err()
    } else {
        None
    };
    let item = PendingNotification {
        id: "test".to_string(),
        generation: String::new(),
        seq: 0,
        title: title.to_string(),
        body: body.to_string(),
        sound,
        icon: None,
        created_at_ms: None,
    };
    run_blocking(move || {
        post_native_notification(&app, &item)?;
        sound_problem.map_or(Ok(()), Err)
    })
    .await
}

/// Whether native notifications are permitted (C4). Always `None` ("unknown"):
/// banners go through the legacy `NSUserNotificationCenter` on macOS, which has
/// no permission query, and the notification plugin reports a hard-coded
/// "granted" on every desktop OS — passing that on would claim a certainty the
/// shell does not have.
#[tauri::command]
fn notification_permission() -> Option<bool> {
    None
}

/// Keep the time-sensitive long-poll pump out of App Nap while this process is
/// responsible for native banners. The allowing-idle-system-sleep option keeps
/// the Mac itself free to sleep; it only prevents macOS from throttling this
/// background app and letting the runtime's osascript fallback win the race.
#[cfg(target_os = "macos")]
fn prevent_notification_pump_app_nap() {
    use objc2_foundation::{ns_string, NSActivityOptions, NSProcessInfo};

    let activity = NSProcessInfo::processInfo().beginActivityWithOptions_reason(
        NSActivityOptions::UserInitiatedAllowingIdleSystemSleep,
        ns_string!("Deliver Herdeck agent notifications"),
    );
    // Banner duty lasts for the process lifetime. Leaking the opaque activity
    // token intentionally mirrors ending it only during process teardown.
    std::mem::forget(activity);
}

#[cfg(not(target_os = "macos"))]
fn prevent_notification_pump_app_nap() {}

/// What the pump does with the result of one `/notifications` round trip.
#[derive(Debug, PartialEq, Eq)]
enum NotifyPoll {
    /// A 2xx feed snapshot to deliver from.
    Deliver(String),
    /// 404: this runtime's source has no feed (demo/mock). Not an outage —
    /// re-discovery would find the very same runtime — so back off instead of
    /// hammering it twice a second.
    Unsupported,
    /// Transport failure or any other status: retry, possibly re-discovering.
    Failed,
}

fn classify_notify_poll(result: Result<(u16, String), String>) -> NotifyPoll {
    match result {
        Ok((code, body)) if (200..300).contains(&code) => NotifyPoll::Deliver(body),
        Ok((404, _)) => NotifyPoll::Unsupported,
        _ => NotifyPoll::Failed,
    }
}

/// The runtime the pump should poll right now, or how long to idle first.
/// Both "no permission yet" and "sidecar not discovered yet" must SLEEP: a bare
/// `continue` there spun a core at 100% through every sidecar boot, and forever
/// while a crashing sidecar never reported in.
fn notify_target(permission: bool, discovery: Option<Discovery>) -> Result<Discovery, Duration> {
    match (permission, discovery) {
        (true, Some(d)) => Ok(d),
        _ => Err(NOTIFY_RETRY_DELAY),
    }
}

/// Identity of a discovered runtime, for "has the shell been repointed?".
fn discovery_key(d: &Discovery) -> (String, u16, String) {
    (d.host.clone(), d.port, d.token.clone())
}

/// Sleep up to `max`, in `NOTIFY_RETRY_DELAY` steps, returning early as soon as
/// `changed()` reports true. Returns whether it woke early.
fn backoff_until(max: Duration, changed: impl Fn() -> bool) -> bool {
    let deadline = std::time::Instant::now() + max;
    loop {
        if changed() {
            return true;
        }
        let now = std::time::Instant::now();
        if now >= deadline {
            return false;
        }
        std::thread::sleep(NOTIFY_RETRY_DELAY.min(deadline - now));
    }
}

/// Generation-aware long-poll notification pump. The blocking request itself
/// keeps banner duty claimed and wakes immediately when the runtime queues an
/// event. Only a successfully shown banner is acknowledged.
fn start_notify_pump(app: tauri::AppHandle) {
    let permission = app.state::<AppState>().notify_permission.clone();
    std::thread::spawn(move || loop {
        let state = app.state::<AppState>();
        let d = match notify_target(
            permission.load(Ordering::Relaxed),
            state.discovery.lock().unwrap().clone(),
        ) {
            Ok(d) => d,
            Err(delay) => {
                std::thread::sleep(delay);
                continue;
            }
        };
        let poll = |d: &Discovery| {
            let cursor = state.notify_cursor.lock().unwrap().clone();
            classify_notify_poll(http::fetch_notifications_status(
                &d.host,
                d.port,
                &d.token,
                NOTIFY_POLL_TIMEOUT,
                cursor.generation.as_deref(),
                cursor.seq,
                &shell_gen(),
            ))
        };
        let unsupported_backoff = |d: &Discovery| {
            let key = discovery_key(d);
            backoff_until(NOTIFY_UNSUPPORTED_BACKOFF, || {
                state.discovery.lock().unwrap().as_ref().map(discovery_key) != Some(key.clone())
            });
        };
        let (body, active_discovery) = match poll(&d) {
            NotifyPoll::Deliver(body) => {
                note_runtime_ok(&state);
                (body, d)
            }
            NotifyPoll::Unsupported => {
                note_runtime_ok(&state);
                unsupported_backoff(&d);
                continue;
            }
            NotifyPoll::Failed => {
                let Some(d) = rediscover_runtime(&app) else {
                    std::thread::sleep(NOTIFY_RETRY_DELAY);
                    continue;
                };
                match poll(&d) {
                    NotifyPoll::Deliver(body) => (body, d),
                    NotifyPoll::Unsupported => {
                        unsupported_backoff(&d);
                        continue;
                    }
                    NotifyPoll::Failed => {
                        std::thread::sleep(NOTIFY_RETRY_DELAY);
                        continue;
                    }
                }
            }
        };
        let Ok(parsed) = serde_json::from_str::<serde_json::Value>(&body) else {
            std::thread::sleep(NOTIFY_RETRY_DELAY);
            continue;
        };
        let Some((generation, acked_seq, items)) = notification_batch(
            &parsed,
            &state.notify_cursor.lock().unwrap().clone(),
        ) else {
            std::thread::sleep(NOTIFY_RETRY_DELAY);
            continue;
        };
        {
            let mut cursor = state.notify_cursor.lock().unwrap();
            if cursor.generation.as_deref() != Some(generation.as_str()) {
                cursor.generation = Some(generation.clone());
                cursor.seq = acked_seq;
            } else {
                cursor.seq = cursor.seq.max(acked_seq);
            }
        }
        for item in items {
            if let Err(err) = post_native_notification(&app, &item) {
                eprintln!("herdeck: native notification failed id={}: {err}", item.id);
                let code = http::fallback_notification(
                    &active_discovery.host,
                    active_discovery.port,
                    &active_discovery.token,
                    SIDECAR_TIMEOUT,
                    &item.generation,
                    item.seq,
                    &shell_gen(),
                    &err.to_string(),
                );
                if code == Ok(204) {
                    let mut cursor = state.notify_cursor.lock().unwrap();
                    cursor.generation = Some(item.generation.clone());
                    cursor.seq = cursor.seq.max(item.seq);
                    eprintln!("herdeck: notification fallback delivered id={}", item.id);
                } else {
                    eprintln!(
                        "herdeck: notification fallback failed id={} result={code:?}",
                        item.id
                    );
                }
                std::thread::sleep(NOTIFY_RETRY_DELAY);
                break;
            }
            // Advance the process-local cursor immediately after visible
            // delivery. A transient ACK failure must never show the banner a
            // second time in this shell; the next long poll carries this cursor
            // and repairs the server ACK before waiting.
            {
                let mut cursor = state.notify_cursor.lock().unwrap();
                cursor.generation = Some(item.generation.clone());
                cursor.seq = cursor.seq.max(item.seq);
            }
            let code = http::ack_notification(
                &active_discovery.host,
                active_discovery.port,
                &active_discovery.token,
                SIDECAR_TIMEOUT,
                &item.generation,
                item.seq,
            );
            if code != Ok(204) {
                eprintln!("herdeck: notification ack failed id={} result={code:?}", item.id);
                std::thread::sleep(NOTIFY_RETRY_DELAY);
                break;
            }
            let latency = item.created_at_ms.map(|created| {
                let now = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_millis() as i64)
                    .unwrap_or(created);
                (now - created).max(0)
            });
            eprintln!(
                "herdeck: notification delivered id={} latency_ms={}",
                item.id,
                latency.map(|v| v.to_string()).unwrap_or_else(|| "unknown".into())
            );
        }
    });
}

/// Attribute our banners to this app's bundle — the same choice the
/// notification plugin makes (Terminal in `tauri dev`, where the binary has no
/// registered bundle). First caller wins; the plugin shares this global.
#[cfg(target_os = "macos")]
fn ensure_notification_application(app: &tauri::AppHandle) {
    use std::sync::Once;
    static SET: Once = Once::new();
    let identifier = if tauri::is_dev() {
        "com.apple.Terminal".to_string()
    } else {
        app.config().identifier.clone()
    };
    SET.call_once(|| {
        let _ = mac_notification_sys::set_application(&identifier);
    });
}

/// Post one banner, fire-and-forget, with its sound attached. On macOS it goes
/// straight through `mac-notification-sys`, which blocks this (non-main) thread
/// only until Notification Center confirms delivery (at most ~2 s). Clicks are
/// observed by `banner_clicks`, not by waiting on the banner: a parked
/// wait-for-click thread per banner also parked a 0.5 s main-run-loop timer
/// that called the synchronous `deliveredNotifications` XPC until the banner
/// left Notification Center — i.e. forever for a banner the user never
/// cleared. There is no per-agent grouping — the legacy `NSUserNotification`
/// API has no thread id.
#[cfg(target_os = "macos")]
fn post_native_notification(
    app: &tauri::AppHandle,
    item: &PendingNotification,
) -> Result<(), String> {
    use mac_notification_sys::Notification;

    ensure_notification_application(app);
    let sound = banner_sound_name(&item.sound, &sound_dirs()).unwrap_or_else(|err| {
        eprintln!("herdeck: {err}; posting id={} silently", item.id);
        None
    });
    let mut banner = Notification::new();
    banner
        .title(&item.title)
        .message(&item.body)
        .maybe_sound(sound.as_deref())
        .asynchronous(true);
    // The project mark twice: `app_icon` swaps the left-hand app icon through
    // a private key that newer macOS may ignore; `content_image` (public API)
    // shows it on the right either way.
    if let Some(image) = item.icon.as_deref().and_then(banner_image_path) {
        banner.app_icon(image).content_image(image);
    }
    banner
        .send()
        .map_err(|err| format!("native notification failed: {err}"))?;
    // Guard: re-wrap if anything replaced the center delegate since startup.
    banner_clicks::ensure_installed(app);
    Ok(())
}

/// Whether a banner activation (the raw `NSUserNotificationActivationType`)
/// should bring the deck forward: a click on the banner body or on one of its
/// action buttons. `None` (0) and an inline reply (3) do not.
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
fn activation_reveals_deck(activation_type: isize) -> bool {
    // ContentsClicked = 1, ActionButtonClicked = 2, AdditionalActionClicked = 4.
    matches!(activation_type, 1 | 2 | 4)
}

/// Whether to present a banner while Herdeck is frontmost: the wrapped
/// delegate's answer when it has one, else yes (AppKit's own default is no).
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
fn should_present_banner(inner_answer: Option<bool>) -> bool {
    inner_answer.unwrap_or(true)
}

/// Banner clicks → reveal the deck, with no per-banner thread and no polling.
///
/// `NSUserNotificationCenter` has exactly one delegate, owned by
/// `mac-notification-sys` (it needs `didDeliverNotification:` to end each
/// send). We put a forwarding proxy in front of it: every delegate message is
/// passed on unchanged, and `didActivateNotification:` also reveals the deck.
/// That is one object for the whole process, event driven, on the main thread
/// where AppKit delivers these callbacks anyway.
///
/// It also answers `shouldPresentNotification:` (the crate does not), so a
/// banner — and the sound it carries — still shows while Herdeck is frontmost.
///
/// `install_at_startup` makes the crate create its delegate (normally done
/// lazily inside its first send) and wraps it before any banner is posted, so
/// the first banner already goes through the proxy. `ensure_installed` after
/// each post is a cheap guard in case anything replaced the delegate since.
#[cfg(target_os = "macos")]
#[allow(deprecated)] // NSUserNotification*: the legacy API mac-notification-sys posts through
mod banner_clicks {
    use std::cell::RefCell;
    use std::sync::OnceLock;

    use objc2::rc::Retained;
    use objc2::runtime::{NSObject, NSObjectProtocol, ProtocolObject};
    use objc2::{
        define_class, msg_send, sel, ClassType, DefinedClass, MainThreadMarker, MainThreadOnly,
    };
    use objc2_foundation::{
        NSUserNotification, NSUserNotificationCenter, NSUserNotificationCenterDelegate,
    };

    type Delegate = ProtocolObject<dyn NSUserNotificationCenterDelegate>;

    pub struct Ivars {
        /// The delegate we stand in front of (the crate's), if any.
        inner: Option<Retained<Delegate>>,
    }

    define_class!(
        #[unsafe(super(NSObject))]
        #[thread_kind = MainThreadOnly]
        #[name = "HerdeckBannerClickDelegate"]
        #[ivars = Ivars]
        pub struct ClickDelegate;

        unsafe impl NSObjectProtocol for ClickDelegate {}

        unsafe impl NSUserNotificationCenterDelegate for ClickDelegate {
            #[unsafe(method(userNotificationCenter:didDeliverNotification:))]
            fn did_deliver(&self, center: &NSUserNotificationCenter, notification: &NSUserNotification) {
                if let Some(inner) = self.forward_to(sel!(userNotificationCenter:didDeliverNotification:)) {
                    let _: () = unsafe {
                        msg_send![inner, userNotificationCenter: center, didDeliverNotification: notification]
                    };
                }
            }

            // NSUserNotificationCenter's default is to NOT present a banner
            // while the posting app is frontmost, and the crate's delegate does
            // not implement this. Now that the sound rides on the banner, an
            // unpresented banner would be a fully silent alert (including the
            // settings Test button, which is pressed with Herdeck frontmost).
            #[unsafe(method(userNotificationCenter:shouldPresentNotification:))]
            fn should_present(&self, center: &NSUserNotificationCenter, notification: &NSUserNotification) -> bool {
                let inner = self
                    .forward_to(sel!(userNotificationCenter:shouldPresentNotification:))
                    .map(|inner| -> bool {
                        unsafe {
                            msg_send![inner, userNotificationCenter: center, shouldPresentNotification: notification]
                        }
                    });
                super::should_present_banner(inner)
            }

            #[unsafe(method(userNotificationCenter:didActivateNotification:))]
            fn did_activate(&self, center: &NSUserNotificationCenter, notification: &NSUserNotification) {
                if super::activation_reveals_deck(notification.activationType().0) {
                    if let Some(app) = APP.get() {
                        super::reveal_deck(app);
                    }
                }
                if let Some(inner) = self.forward_to(sel!(userNotificationCenter:didActivateNotification:)) {
                    let _: () = unsafe {
                        msg_send![inner, userNotificationCenter: center, didActivateNotification: notification]
                    };
                }
            }
        }

        impl ClickDelegate {
            // Private NSUserNotificationCenter callback the crate implements
            // (close button); forwarded so it keeps working.
            #[unsafe(method(userNotificationCenter:didDismissAlert:))]
            fn did_dismiss_alert(&self, center: &NSUserNotificationCenter, notification: &NSUserNotification) {
                if let Some(inner) = self.forward_to(sel!(userNotificationCenter:didDismissAlert:)) {
                    let _: () = unsafe {
                        msg_send![inner, userNotificationCenter: center, didDismissAlert: notification]
                    };
                }
            }
        }
    );

    impl ClickDelegate {
        fn forward_to(&self, selector: objc2::runtime::Sel) -> Option<&Delegate> {
            self.ivars()
                .inner
                .as_deref()
                .filter(|inner| inner.respondsToSelector(selector))
        }
    }

    static APP: OnceLock<tauri::AppHandle> = OnceLock::new();

    thread_local! {
        /// The installed proxy. The center holds its delegate unretained, so
        /// this keeps it alive; a replaced proxy is dropped once the center no
        /// longer points at it.
        static PROXY: RefCell<Option<Retained<ClickDelegate>>> = const { RefCell::new(None) };
    }

    extern "C" {
        /// mac-notification-sys's own ObjC entry point (notify.m, in the
        /// `libnotify.a` the crate links): creates its delegate singleton and
        /// sets it on the center, once (`dispatch_once`). The crate only calls
        /// it inside its first send, which would put a banner through the
        /// crate's delegate before our proxy could wrap it.
        fn setupDelegate();
    }

    /// Install the proxy before anything is posted, so the very first banner
    /// already gets `shouldPresentNotification:` (presented while Herdeck is
    /// frontmost) and its click is seen. Order matters: attribute the bundle
    /// first (an unbundled `tauri dev` binary has no notification center
    /// otherwise), then let the crate create its delegate, then wrap it; the
    /// crate's later `setupDelegate` calls are then no-ops. Main thread.
    pub fn install_at_startup(app: &tauri::AppHandle) {
        super::ensure_notification_application(app);
        // SAFETY: a plain C function with no arguments; idempotent.
        unsafe { setupDelegate() };
        ensure_installed(app);
    }

    /// Put the proxy in front of the center's current delegate unless it is
    /// already there. Main thread only.
    fn install(mtm: MainThreadMarker) {
        // Nil when the process has no bundle identity; nothing to wrap then.
        let center: Option<Retained<NSUserNotificationCenter>> = unsafe {
            msg_send![NSUserNotificationCenter::class(), defaultUserNotificationCenter]
        };
        let Some(center) = center else {
            return;
        };
        // SAFETY: the current delegate is either ours (kept alive by PROXY) or
        // the crate's process-lifetime singleton.
        let current = unsafe { center.delegate() };
        if current
            .as_deref()
            .is_some_and(|d| d.isKindOfClass(ClickDelegate::class()))
        {
            return;
        }
        let proxy = ClickDelegate::alloc(mtm).set_ivars(Ivars { inner: current });
        let proxy: Retained<ClickDelegate> = unsafe { msg_send![super(proxy), init] };
        // SAFETY: PROXY keeps the delegate alive for as long as it is set.
        unsafe { center.setDelegate(Some(ProtocolObject::from_ref(&*proxy))) };
        PROXY.with(|slot| *slot.borrow_mut() = Some(proxy));
    }

    /// Install (or re-install) the proxy. Cheap and idempotent: one property
    /// read on the main thread per call.
    pub fn ensure_installed(app: &tauri::AppHandle) {
        let _ = APP.set(app.clone());
        if let Some(mtm) = MainThreadMarker::new() {
            install(mtm);
            return;
        }
        let _ = app.run_on_main_thread(|| {
            if let Some(mtm) = MainThreadMarker::new() {
                install(mtm);
            }
        });
    }
}

#[cfg(not(target_os = "macos"))]
fn post_native_notification(
    app: &tauri::AppHandle,
    item: &PendingNotification,
) -> Result<(), String> {
    use tauri_plugin_notification::NotificationExt;
    app.notification()
        .builder()
        .title(&item.title)
        .body(&item.body)
        .show()
        .map_err(|err| err.to_string())
}

/// Proxy `GET /tile/{index}` → a `data:image/png;base64,…` URL (or `None` if the
/// tile is absent), so the WebView `<img>` renders it without touching the token.
#[tauri::command]
async fn deck_tile(
    state: tauri::State<'_, AppState>,
    index: u32,
) -> Result<Option<String>, String> {
    let d = current_discovery(&state)?;
    run_blocking(move || {
        http::fetch_image(
            &d.host,
            d.port,
            &format!("/tile/{index}"),
            &d.token,
            SIDECAR_TIMEOUT,
        )
    })
    .await
}

/// Custom URI scheme that serves tile/panel PNGs to the WebView straight from
/// the discovered runtime, token injected here in Rust — no base64 `data:` URLs
/// over IPC. The frontend builds image URLs in exactly this form:
///
/// - macOS / Linux: `herdeck://localhost/tile/<i>?v=<ver>` and
///   `herdeck://localhost/panel?v=<ver>`
/// - Windows (WebView2 maps custom schemes onto http): `http://herdeck.localhost/tile/<i>?v=<ver>`
///   and `http://herdeck.localhost/panel?v=<ver>`
///
/// `v` is the tile/panel version from `/state`; it only makes the URL change
/// when the image does and is not forwarded. A missing tile/panel is a 404
/// (the `<img>` fires `error`), no discovery yet a 503, an unreachable runtime
/// a 502. `deck_tile`/`deck_panel` stay as the fallback transport.
const IMAGE_SCHEME: &str = "herdeck";

/// Map a request path on the image scheme to the runtime endpoint it proxies —
/// only `/panel` and `/tile/<index>` (decimal) are served, nothing else.
fn image_proxy_path(uri_path: &str) -> Option<String> {
    if uri_path == "/panel" {
        return Some("/panel".to_string());
    }
    let index = uri_path.strip_prefix("/tile/")?;
    if index.is_empty() || index.len() > 4 || !index.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    index.parse::<u32>().ok().map(|i| format!("/tile/{i}"))
}

fn image_response(status: u16, png: Vec<u8>) -> tauri::http::Response<Vec<u8>> {
    let mut builder = tauri::http::Response::builder().status(status);
    if status == 200 {
        // Versions restart from zero when the runtime restarts, so the same
        // `?v=` can name a different image later: revalidate rather than trust
        // a cached copy. An unchanged `src` is never re-requested by the <img>
        // anyway, which is where the saving is.
        builder = builder
            .header("Content-Type", "image/png")
            .header("Cache-Control", "no-cache");
    } else {
        builder = builder.header("Cache-Control", "no-store");
    }
    builder.body(png).unwrap_or_else(|_| {
        let mut resp = tauri::http::Response::new(Vec::new());
        *resp.status_mut() = tauri::http::StatusCode::INTERNAL_SERVER_ERROR;
        resp
    })
}

/// Serve one image-scheme request (blocking — runs on the blocking pool).
fn serve_image_request(app: &tauri::AppHandle, uri_path: &str) -> tauri::http::Response<Vec<u8>> {
    let Some(path) = image_proxy_path(uri_path) else {
        return image_response(404, Vec::new());
    };
    let Some(d) = app
        .try_state::<AppState>()
        .and_then(|s| s.discovery.lock().unwrap().clone())
    else {
        return image_response(503, Vec::new());
    };
    match http::fetch_png(&d.host, d.port, &path, &d.token, SIDECAR_TIMEOUT) {
        Ok(Some(png)) => image_response(200, png),
        Ok(None) => image_response(404, Vec::new()),
        Err(err) => {
            eprintln!("herdeck: image proxy {path}: {err}");
            image_response(502, Vec::new())
        }
    }
}

/// Proxy `GET /panel` → a `data:` PNG URL (or `None` if there is no panel yet).
#[tauri::command]
async fn deck_panel(state: tauri::State<'_, AppState>) -> Result<Option<String>, String> {
    let d = current_discovery(&state)?;
    run_blocking(move || http::fetch_image(&d.host, d.port, "/panel", &d.token, SIDECAR_TIMEOUT))
        .await
}

/// Proxy `POST /press/{index}` (token in the `X-Herdeck-Token` header) → the
/// sidecar's HTTP status code (204 ok, 403 bad token, 400 bad index).
#[tauri::command]
async fn deck_press(state: tauri::State<'_, AppState>, index: u32) -> Result<u16, String> {
    let d = current_discovery(&state)?;
    run_blocking(move || http::send_press(&d.host, d.port, index, &d.token, SIDECAR_TIMEOUT)).await
}

/// Proxy `GET /config` (token as query param) → the redacted config JSON
/// `{base, profiles, local, secrets}`. `Err` if the sidecar has no config
/// service (404) or is unreachable.
#[tauri::command]
async fn config_read(state: tauri::State<'_, AppState>) -> Result<serde_json::Value, String> {
    let d = current_discovery(&state)?;
    run_blocking(move || {
        let body = http::http_get(
            &d.host,
            d.port,
            &format!("/config?token={}", d.token),
            SIDECAR_TIMEOUT,
        )?;
        serde_json::from_str(&body).map_err(|e| format!("invalid /config JSON from sidecar: {e}"))
    })
    .await
}

/// Proxy `POST /config/validate` (header token) with the proposed config body →
/// `{errors: [...]}`. The body is the JS `{base, profiles, local}` object.
#[tauri::command]
async fn config_validate(
    state: tauri::State<'_, AppState>,
    body: serde_json::Value,
) -> Result<serde_json::Value, String> {
    let d = current_discovery(&state)?;
    run_blocking(move || config_post_json(&d, "/config/validate", &body)).await
}

/// Proxy `POST /config` (header token) — atomic write + reload on the sidecar
/// when `errors` is empty. Returns `{errors: [...]}`.
#[tauri::command]
async fn config_write(
    state: tauri::State<'_, AppState>,
    body: serde_json::Value,
) -> Result<serde_json::Value, String> {
    let d = current_discovery(&state)?;
    run_blocking(move || config_post_json(&d, "/config", &body)).await
}

/// Proxy `POST /profiles/active` (header token) → `{changed: bool}`. A 400
/// (unknown/invalid profile name) surfaces as `Err` so the UI can show it.
#[tauri::command]
async fn config_set_active(
    state: tauri::State<'_, AppState>,
    name: String,
) -> Result<serde_json::Value, String> {
    let d = current_discovery(&state)?;
    run_blocking(move || {
        config_post_json(&d, "/profiles/active", &serde_json::json!({ "name": name }))
    })
    .await
}

/// Proxy `POST /secret` (header token) — store `value` for `token_env` in the
/// OS keychain. Returns the HTTP status (204 ok, 400 missing fields). The value
/// is one-way: it is never read back.
#[tauri::command]
async fn config_secret_set(
    state: tauri::State<'_, AppState>,
    token_env: String,
    value: String,
) -> Result<u16, String> {
    let d = current_discovery(&state)?;
    run_blocking(move || {
        let body = serde_json::json!({ "token_env": token_env, "value": value }).to_string();
        let (code, _resp) = http::http_post_json(
            &d.host,
            d.port,
            "/secret",
            (HDR_TOKEN, &d.token),
            &body,
            SIDECAR_TIMEOUT,
        )?;
        Ok(code)
    })
    .await
}

/// Proxy `DELETE /secret/{token_env}` (header token) → status (204 ok).
#[tauri::command]
async fn config_secret_clear(
    state: tauri::State<'_, AppState>,
    token_env: String,
) -> Result<u16, String> {
    let d = current_discovery(&state)?;
    run_blocking(move || {
        http::http_delete(
            &d.host,
            d.port,
            &format!("/secret/{}", http::percent_encode_segment(&token_env)),
            (HDR_TOKEN, &d.token),
            SIDECAR_TIMEOUT,
        )
    })
    .await
}

/// Proxy `GET /setup` (token as query param) → the first-run status JSON.
#[tauri::command]
async fn setup_status(state: tauri::State<'_, AppState>) -> Result<serde_json::Value, String> {
    let d = current_discovery(&state)?;
    run_blocking(move || {
        let body = http::fetch_setup(&d.host, d.port, &d.token, SIDECAR_TIMEOUT)?;
        serde_json::from_str(&body).map_err(|e| format!("invalid /setup JSON from sidecar: {e}"))
    })
    .await
}

/// Proxy `POST /setup/connect` (header token) → the connect result `{ok, …}`. Uses a
/// dedicated timeout longer than the sidecar's remote probe. The typed token VALUE is
/// in the forwarded body; it is never read back. Runs off the main thread — the old
/// sync version blocked the UI for up to 15 s on the very first user interaction.
#[tauri::command]
async fn setup_connect(
    state: tauri::State<'_, AppState>,
    body: serde_json::Value,
) -> Result<serde_json::Value, String> {
    let d = current_discovery(&state)?;
    run_blocking(move || {
        let (code, resp) = http::post_setup_connect(
            &d.host,
            d.port,
            &d.token,
            &body.to_string(),
            SETUP_CONNECT_TIMEOUT,
        )?;
        if code == 200 {
            serde_json::from_str(&resp).map_err(|e| format!("invalid /setup/connect JSON: {e}"))
        } else {
            Err(format!("sidecar returned HTTP {code} for /setup/connect"))
        }
    })
    .await
}

/// Shared POST-JSON-and-parse for the config routes that return a JSON object on
/// 200. A non-200 (e.g. 400 for a malformed body / bad profile name) is an `Err`
/// the command surfaces to JS. Blocking — call inside `run_blocking`.
fn config_post_json(
    d: &Discovery,
    path: &str,
    body: &serde_json::Value,
) -> Result<serde_json::Value, String> {
    let (code, resp) = http::http_post_json(
        &d.host,
        d.port,
        path,
        (HDR_TOKEN, &d.token),
        &body.to_string(),
        SIDECAR_TIMEOUT,
    )?;
    if code == 200 {
        serde_json::from_str(&resp).map_err(|e| format!("invalid JSON from {path}: {e}"))
    } else {
        Err(format!("sidecar returned HTTP {code} for {path}"))
    }
}

/// How the sidecar is obtained: either an externally-managed one (dev override
/// via env, no spawn) or a child process we spawn and supervise.
enum SidecarPlan {
    External(Discovery, bool),
    Spawn(CommandSpec),
}

/// `<repo>/desktop/src-tauri` -> `<repo>`. Used to locate the dev `.venv`.
/// (Dev-mode only; the frozen/bundled sidecar is a later phase.)
fn repo_root_from_manifest() -> PathBuf {
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
fn default_config_path() -> PathBuf {
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
fn parse_host_port(url: &str) -> (String, u16) {
    let after_scheme = url.split_once("://").map(|(_, rest)| rest).unwrap_or(url);
    let authority = after_scheme.split('/').next().unwrap_or(after_scheme);
    match authority.rsplit_once(':') {
        Some((h, p)) => (h.to_string(), p.parse::<u16>().unwrap_or(0)),
        None => (authority.to_string(), 0),
    }
}

/// The automatic plan plus the reason logged for it (`plan_log_line`).
fn resolve_automatic_plan<F>(
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
fn resolve_plan(resource_dir: Option<PathBuf>) -> (SidecarPlan, &'static str) {
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

/// Keep this process's and the sidecar's stderr in a log file when launched
/// outside a terminal (see `app_log`); the marker line separates launches.
fn start_app_log() {
    let Ok(home) = env::var("HOME") else {
        return;
    };
    let dir = app_log::log_dir(Path::new(&home), env::var("XDG_STATE_HOME").ok().as_deref());
    let path = dir.join(app_log::log_file_name(build_channel::is_dev()));
    match app_log::capture_stderr(&path) {
        Ok(Some(_)) => eprintln!(
            "herdeck: ---- {} {} (pid {}) started ----",
            build_channel::current(),
            env!("CARGO_PKG_VERSION"),
            std::process::id()
        ),
        Ok(None) => {}
        Err(err) => eprintln!("herdeck: log file {} unavailable: {err}", path.display()),
    }
}

#[cfg(test)]
mod plan_tests {
    use super::*;

    #[test]
    fn health_carries_the_shell_version_for_mismatch_warnings() {
        let health = serde_json::json!({"ok": true, "version": "1.2.3"});
        let stamped = with_app_version(health, "1.2.4");
        assert_eq!(stamped["app_version"], "1.2.4");
        assert_eq!(stamped["version"], "1.2.3");
        assert_eq!(with_app_version(serde_json::json!(null), "1"), serde_json::json!(null));
    }

    #[test]
    fn notification_generation_change_delivers_low_sequence_item() {
        let cursor = NotifyCursor {
            generation: Some("old".into()),
            seq: 10,
        };
        let state = serde_json::json!({
            "generation": "new",
            "seq": 1,
            "acked_seq": 0,
            "items": [{
                "id": "new:1", "seq": 1, "title": "done", "body": "p1",
                "sound": "Hero", "created_at_ms": 1
            }]
        });
        let (generation, _, items) = notification_batch(&state, &cursor).unwrap();
        assert_eq!(generation, "new");
        assert_eq!(items.len(), 1);
        assert_eq!(items[0].seq, 1);
    }

    #[test]
    fn notification_item_carries_its_banner_icon() {
        let state = serde_json::json!({
            "generation": "g",
            "seq": 2,
            "acked_seq": 0,
            "items": [
                {"id": "g:1", "seq": 1, "title": "done", "body": "p1",
                 "icon": "/c/notification-icons/v1-p-ab12.png"},
                {"id": "g:2", "seq": 2, "title": "done", "body": "p2", "icon": null}
            ]
        });
        let (_, _, items) = notification_batch(&state, &NotifyCursor::default()).unwrap();
        assert_eq!(
            items[0].icon.as_deref(),
            Some("/c/notification-icons/v1-p-ab12.png")
        );
        assert_eq!(items[1].icon, None);
    }

    #[test]
    fn banner_image_path_accepts_only_runtime_icon_files() {
        let ok = "/Users/a/.cache/herdeck/notification-icons/v1-m-0f3a.png";
        assert_eq!(banner_image_path(ok), Some(ok));
        for bad in [
            "notification-icons/v1-p-ab.png",                 // relative
            "/Users/a/.ssh/id_ed25519",                       // not a png
            "/Users/a/Pictures/v1-p-ab.png",                  // wrong directory
            "/c/notification-icons/../../etc/v1.png",         // parent is not the dir
            "/c/notification-icons/V1 P.png",                 // outside [a-z0-9-]
            "/c/notification-icons/.png",                     // empty stem
            "",
        ] {
            assert_eq!(banner_image_path(bad), None, "{bad}");
        }
    }

    #[test]
    fn notification_cursor_filters_an_already_delivered_item() {
        let cursor = NotifyCursor {
            generation: Some("same".into()),
            seq: 1,
        };
        let state = serde_json::json!({
            "generation": "same",
            "seq": 1,
            "acked_seq": 0,
            "items": [{"id": "same:1", "seq": 1, "title": "done", "body": "p1"}]
        });
        let (_, _, items) = notification_batch(&state, &cursor).unwrap();
        assert!(items.is_empty());
    }

    fn discovery_on(port: u16) -> Discovery {
        Discovery {
            url: format!("http://127.0.0.1:{port}"),
            host: "127.0.0.1".into(),
            port,
            token: "t".into(),
            source: "mock".into(),
        }
    }

    // A bare `continue` on either of these spun the pump thread at 100% CPU
    // for as long as the sidecar had not reported in.
    #[test]
    fn the_pump_idles_instead_of_spinning_without_permission_or_discovery() {
        assert_eq!(notify_target(true, None), Err(NOTIFY_RETRY_DELAY));
        assert_eq!(notify_target(false, Some(discovery_on(1))), Err(NOTIFY_RETRY_DELAY));
        assert_eq!(notify_target(false, None), Err(NOTIFY_RETRY_DELAY));
        assert!(NOTIFY_RETRY_DELAY > Duration::ZERO);
        assert_eq!(notify_target(true, Some(discovery_on(1))), Ok(discovery_on(1)));
    }

    #[test]
    fn a_feedless_source_is_unsupported_not_a_failure() {
        assert_eq!(
            classify_notify_poll(Ok((200, "{}".into()))),
            NotifyPoll::Deliver("{}".into())
        );
        assert_eq!(classify_notify_poll(Ok((404, String::new()))), NotifyPoll::Unsupported);
        assert_eq!(classify_notify_poll(Ok((409, String::new()))), NotifyPoll::Failed);
        assert_eq!(classify_notify_poll(Err("down".into())), NotifyPoll::Failed);
        assert!(NOTIFY_UNSUPPORTED_BACKOFF >= Duration::from_secs(30));
    }

    #[test]
    fn the_unsupported_backoff_wakes_when_discovery_changes() {
        let start = std::time::Instant::now();
        assert!(backoff_until(Duration::from_secs(30), || true));
        assert!(start.elapsed() < Duration::from_secs(1));
        // And it does wait out the full period when nothing changes.
        let start = std::time::Instant::now();
        assert!(!backoff_until(Duration::from_millis(30), || false));
        assert!(start.elapsed() >= Duration::from_millis(30));
        assert_ne!(discovery_key(&discovery_on(1)), discovery_key(&discovery_on(2)));
    }

    fn sound_scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("herdeck-sounds-{name}"));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn sound_names_are_sorted_unique_playable_stems() {
        let user = sound_scratch("list-user");
        let system = sound_scratch("list-system");
        for f in ["Glass.aiff", "Zap.wav", "notes.txt", "bad.name.aiff", "Ping.caf"] {
            std::fs::write(user.join(f), b"").unwrap();
        }
        for f in ["Glass.aiff", "Basso.aiff"] {
            std::fs::write(system.join(f), b"").unwrap();
        }
        std::fs::create_dir_all(user.join("Folder.aiff")).unwrap();
        let names = list_sound_names(&[user, system, PathBuf::from("/nonexistent-herdeck")]);
        assert_eq!(names, vec!["Basso", "Glass", "Ping", "Zap"]);
    }

    // Every listed sound must be one playback can find: a user sound from
    // ~/Library/Sounds used to be listed-but-unplayable (only the system folder
    // and .aiff were searched).
    #[test]
    fn sound_resolution_searches_the_user_folder_first_and_any_extension() {
        let user = sound_scratch("resolve-user");
        let system = sound_scratch("resolve-system");
        std::fs::write(user.join("Glass.m4a"), b"").unwrap();
        std::fs::write(system.join("Glass.aiff"), b"").unwrap();
        std::fs::write(system.join("Basso.aiff"), b"").unwrap();
        let dirs = [user.clone(), system.clone()];
        assert_eq!(resolve_sound_path("Glass", &dirs), Some(user.join("Glass.m4a")));
        assert_eq!(resolve_sound_path("Basso", &dirs), Some(system.join("Basso.aiff")));
        assert_eq!(resolve_sound_path("Missing", &dirs), None);
        assert_eq!(resolve_sound_path("../Basso", &dirs), None);
    }

    // The banner carries its own sound (no separate afplay): silence stays
    // silence, and only a name the OS can find is attached.
    #[test]
    fn banner_sound_is_attached_only_for_a_findable_name() {
        use serde_json::json;
        let dir = sound_scratch("banner");
        std::fs::write(dir.join("Hero.aiff"), b"").unwrap();
        std::fs::write(dir.join("Glass.aiff"), b"").unwrap();
        let dirs = [dir];
        assert_eq!(banner_sound_name(&json!("Hero"), &dirs), Ok(Some("Hero".into())));
        assert_eq!(
            banner_sound_name(&json!(true), &dirs),
            Ok(Some(DEFAULT_NOTIFICATION_SOUND.into()))
        );
        assert_eq!(banner_sound_name(&json!(""), &dirs), Ok(None));
        assert_eq!(banner_sound_name(&json!(false), &dirs), Ok(None));
        assert!(banner_sound_name(&json!("Missing"), &dirs).is_err());
        assert!(banner_sound_name(&json!("../Hero"), &dirs).is_err());
    }

    #[test]
    fn banners_present_while_frontmost_unless_the_inner_delegate_says_no() {
        assert!(should_present_banner(None));
        assert!(should_present_banner(Some(true)));
        assert!(!should_present_banner(Some(false)));
    }

    #[test]
    fn only_a_banner_or_action_click_reveals_the_deck() {
        assert!(!activation_reveals_deck(0)); // none
        assert!(activation_reveals_deck(1)); // contents clicked
        assert!(activation_reveals_deck(2)); // action button
        assert!(!activation_reveals_deck(3)); // replied
        assert!(activation_reveals_deck(4)); // additional action
    }

    #[test]
    fn requested_sound_maps_true_to_the_default_and_false_to_silence() {
        use serde_json::json;
        assert_eq!(requested_sound_name(&json!(true)), Some(DEFAULT_NOTIFICATION_SOUND));
        assert_eq!(requested_sound_name(&json!("Hero")), Some("Hero"));
        assert_eq!(requested_sound_name(&json!(false)), None);
        assert_eq!(requested_sound_name(&json!("")), None);
        assert_eq!(requested_sound_name(&json!(null)), None);
    }

    #[test]
    fn test_notification_text_exists_in_both_languages() {
        let (en_t, en_b) = test_notification_texts("en");
        let (cs_t, cs_b) = test_notification_texts("cs");
        assert!(!en_t.is_empty() && !en_b.is_empty());
        assert_ne!(en_t, cs_t);
        assert_ne!(en_b, cs_b);
        // Unknown languages fall back to English.
        assert_eq!(test_notification_texts("de"), (en_t, en_b));
    }

    #[test]
    fn a_long_poll_state_request_outlasts_its_wait() {
        assert_eq!(state_request_timeout(None, None), SIDECAR_TIMEOUT);
        assert_eq!(state_request_timeout(None, Some(20_000)), SIDECAR_TIMEOUT);
        // C2: read timeout must exceed wait_ms + 5 s.
        assert!(state_request_timeout(Some(3), Some(20_000)) > Duration::from_millis(25_000));
        // The runtime clamps to 25 s; so does the timeout, instead of hanging on.
        assert_eq!(
            state_request_timeout(Some(3), Some(600_000)),
            state_request_timeout(Some(3), Some(25_000))
        );
        assert!(state_request_timeout(Some(3), Some(0)) >= SIDECAR_TIMEOUT);
    }

    #[test]
    fn state_peek_validates_and_reads_the_blocked_count() {
        let peek = peek_state(r#"{"version":3,"summary":{"blocked":2,"working":1},"tiles":{}}"#)
            .unwrap();
        assert_eq!(peek.blocked(), Some(2));
        assert_eq!(peek_state(r#"{"version":3}"#).unwrap().blocked(), None);
        assert!(peek_state("{\"version\":").is_err());
        assert!(peek_state("not json").is_err());
    }

    #[test]
    fn tray_tooltip_names_blocked_agents_in_both_languages() {
        assert_eq!(tray_tooltip("en", "Herdeck", None), "Herdeck");
        assert_eq!(tray_tooltip("en", "Herdeck", Some(0)), "Herdeck");
        assert_eq!(tray_tooltip("en", "Herdeck", Some(3)), "Herdeck — 3 blocked");
        assert_eq!(tray_tooltip("cs", "Herdeck", Some(3)), "Herdeck — zablokováno: 3");
        assert_ne!(tray_blocked_label("en", 1), tray_blocked_label("cs", 1));
    }

    #[test]
    fn tray_left_click_opens_the_menu_on_macos() {
        assert_eq!(tray_menu_on_left_click(), cfg!(target_os = "macos"));
    }

    #[test]
    fn the_image_scheme_only_proxies_tiles_and_the_panel() {
        assert_eq!(image_proxy_path("/panel").as_deref(), Some("/panel"));
        assert_eq!(image_proxy_path("/tile/0").as_deref(), Some("/tile/0"));
        assert_eq!(image_proxy_path("/tile/14").as_deref(), Some("/tile/14"));
        assert_eq!(image_proxy_path("/tile/007").as_deref(), Some("/tile/7"));
        for bad in ["/", "/state", "/config", "/tile/", "/tile/-1", "/tile/1/x", "/tile/99999", "/panel/x", "/tile/1%2F"] {
            assert_eq!(image_proxy_path(bad), None, "{bad} must not be proxied");
        }
    }

    #[test]
    fn image_responses_carry_type_and_cache_policy() {
        let ok = image_response(200, vec![1, 2]);
        assert_eq!(ok.status(), 200);
        assert_eq!(ok.headers()["Content-Type"], "image/png");
        assert_eq!(ok.headers()["Cache-Control"], "no-cache");
        assert_eq!(ok.body(), &vec![1, 2]);
        let missing = image_response(404, Vec::new());
        assert_eq!(missing.status(), 404);
        assert_eq!(missing.headers()["Cache-Control"], "no-store");
    }

    #[test]
    fn main_window_capability_allows_compact_window_control() {
        let capability: serde_json::Value = serde_json::from_str(include_str!(
            "../capabilities/default.json"
        ))
        .expect("default capability must be valid JSON");
        let permissions = capability["permissions"]
            .as_array()
            .expect("default capability must declare permissions");

        assert!(permissions.iter().any(|permission| {
            permission.as_str() == Some("core:window:allow-start-dragging")
        }));
        assert!(permissions.iter().any(|permission| {
            permission.as_str() == Some("core:window:allow-set-position")
        }));
    }

    // Placement geometry. The deck is pinned to the top-right of the monitor's
    // USABLE area — the screen rect would put it under the macOS menu bar.
    #[test]
    fn floating_deck_sits_inside_the_work_area_not_the_screen() {
        // A 1920x1080 display whose top 37 points belong to the menu bar.
        let (x, y) = floating_origin((0.0, 37.0), (1920.0, 1043.0), (328.0, 300.0), 16.0);
        assert_eq!((x, y), (1576.0, 53.0));
    }

    // The whole point of following the pointer: a monitor left of the primary one
    // has a NEGATIVE origin, and placement must be relative to it.
    #[test]
    fn floating_deck_places_relative_to_its_own_monitor_origin() {
        let (x, y) = floating_origin((-1920.0, 240.0), (1920.0, 1080.0), (328.0, 300.0), 16.0);
        assert_eq!((x, y), (-344.0, 256.0));
    }

    // A deck zoomed past a small external display must still land ON it: an
    // off-screen borderless window has no titlebar to drag it back with.
    #[test]
    fn floating_deck_never_starts_off_the_screen_it_is_placed_on() {
        let (x, y) = floating_origin((100.0, 50.0), (300.0, 200.0), (328.0, 300.0), 16.0);
        assert_eq!((x, y), (100.0, 50.0));
    }

    // A Retina built-in beside a 1x external is the setup that breaks if the
    // cursor reading is handed to the lookup untouched: tao scales the logical
    // point by the PRIMARY monitor's factor, but the lookup wants logical.
    #[test]
    fn pointer_lookup_undoes_the_primary_monitors_scaling() {
        // Pointer at logical (1000, 400) on a 2x primary reads back as (2000, 800).
        assert_eq!(cursor_in_lookup_space((2000.0, 800.0), 2.0), (1000.0, 400.0));
    }

    #[test]
    fn pointer_lookup_leaves_an_unscaled_reading_alone() {
        assert_eq!(cursor_in_lookup_space((3000.0, 120.0), 1.0), (3000.0, 120.0));
    }

    // A monitor tao could not inspect reports 0; dividing by it would hand the
    // lookup an infinity, and `usable_scale` guards every other divisor too.
    #[test]
    fn a_nonsense_scale_factor_never_produces_an_infinity() {
        let (x, y) = cursor_in_lookup_space((640.0, 480.0), 0.0);
        assert_eq!((x, y), (640.0, 480.0));
        assert_eq!(usable_scale(-2.0), 1.0);
        assert_eq!(usable_scale(2.0), 2.0);
    }

    // Wayland hands back a hard (0, 0) instead of an error, so a believed
    // pointer reading would pin the deck to whatever monitor owns the origin —
    // worse than the current_monitor() fallback it would be overriding. Detection
    // is asserted on every host so the shipped Linux behaviour is not only
    // covered by the one CI runner that compiles this branch.
    #[test]
    fn a_wayland_session_is_recognised_by_either_signal() {
        assert!(is_wayland_session(Some("wayland"), None, None));
        assert!(is_wayland_session(Some("Wayland"), None, None));
        assert!(is_wayland_session(None, Some("wayland-0"), None));
        assert!(is_wayland_session(Some("x11"), Some("wayland-0"), None));
    }

    #[test]
    fn an_x11_session_is_not_mistaken_for_wayland() {
        assert!(!is_wayland_session(Some("x11"), None, None));
        assert!(!is_wayland_session(None, None, None));
        // Exported-but-empty is how a cleared variable survives in a session env.
        assert!(!is_wayland_session(Some("x11"), Some(""), None));
    }

    // GDK_BACKEND=x11 in a Wayland session is the standard WebKitGTK workaround:
    // WAYLAND_DISPLAY stays exported, but GDK — and so tao — really is on X11 and
    // reports a usable pointer. Believing the socket there would discard it.
    #[test]
    fn a_forced_backend_outranks_the_session_variables() {
        // The workaround this exists for: x11 forced in a Wayland session.
        assert!(!is_wayland_session(
            Some("wayland"),
            Some("wayland-0"),
            Some("x11")
        ));
        assert!(is_wayland_session(Some("wayland"), None, Some("wayland")));
        assert!(!is_wayland_session(None, None, Some(" x11 ")));
    }

    // A list that forbids x11 leaves nothing to fall through to, so it decides
    // alone — wl_display_connect(NULL) finds wayland-0 with WAYLAND_DISPLAY
    // unset, and believing the session variables there would trust a (0, 0).
    // The rule is which backends are named, not how many entries there are.
    #[test]
    fn a_list_that_forbids_x11_settles_it() {
        assert!(is_wayland_session(None, None, Some("wayland")));
        assert!(is_wayland_session(Some("x11"), None, Some("wayland")));
        assert!(is_wayland_session(Some("x11"), None, Some("wayland,broadway")));
    }

    // A list naming BOTH permits backends without naming the one that connected,
    // so there it filters the session signals instead of replacing them. Reading
    // entry zero as the answer got both orders wrong, in opposite directions.
    #[test]
    fn a_backend_list_naming_both_filters_rather_than_decides() {
        // Belt-and-braces value in an X11 session: wayland is permitted but the
        // session is not offering it, so the real pointer must survive.
        assert!(!is_wayland_session(Some("x11"), None, Some("wayland,x11")));
        // Wayland session with no Xwayland: x11 is preferred but unreachable, so
        // GDK falls through to wayland and the (0, 0) reading must not be believed.
        assert!(is_wayland_session(
            Some("wayland"),
            Some("wayland-0"),
            Some("x11,wayland")
        ));
    }

    #[test]
    fn an_unusable_backend_value_leaves_the_session_signals_standing() {
        // Exported-but-empty, whitespace, and a name we do not recognise are all
        // "no filter" — not a definitive "this is not Wayland".
        assert!(is_wayland_session(Some("wayland"), None, Some("")));
        assert!(is_wayland_session(None, Some("wayland-0"), Some("  ")));
        assert!(is_wayland_session(Some("wayland"), None, Some("gdk")));
        // And the other direction: an unusable value must not manufacture one.
        assert!(!is_wayland_session(Some("x11"), None, Some("gdk")));
        assert!(!is_wayland_session(None, None, Some("")));
    }

    #[test]
    #[cfg(all(unix, not(target_os = "macos")))]
    fn the_gtk_backend_drops_the_pointer_reading_under_wayland() {
        assert!(!pointer_is_locatable(Some("wayland"), None, None));
        assert!(pointer_is_locatable(Some("x11"), None, None));
    }

    // The variables mean nothing off the GTK backend, and a stray export must
    // not cost a macOS user the pointer preference this path exists for.
    #[test]
    #[cfg(not(all(unix, not(target_os = "macos"))))]
    fn wayland_variables_are_ignored_off_the_gtk_backend() {
        assert!(pointer_is_locatable(
            Some("wayland"),
            Some("wayland-0"),
            None
        ));
    }

    // The space decision is what broke once already, and it is the one part that
    // cannot be reached from a test on this host — so it takes the platform as an
    // argument and both shapes are asserted everywhere.
    #[test]
    fn logical_placement_divides_by_each_factor_and_keeps_the_margin() {
        let p = placement_units(false, 2.0, 2.0);
        assert_eq!((p.screen_div, p.window_div, p.margin), (2.0, 2.0, 16.0));
        // A window still on a 1x display, moving to a 2x one.
        let mixed = placement_units(false, 2.0, 1.0);
        assert_eq!((mixed.screen_div, mixed.window_div), (2.0, 1.0));
        // A monitor tao could not inspect must not become a divide by zero.
        let broken = placement_units(false, 0.0, -1.0);
        assert_eq!((broken.screen_div, broken.window_div), (1.0, 1.0));
    }

    #[test]
    fn physical_placement_leaves_the_screen_rect_and_scales_the_margin() {
        // Windows measures coordinates in one global physical space, so the
        // screen rect is untouched — but a fixed 16 would be 8 points at 200%.
        let p = placement_units(true, 2.0, 2.0);
        assert_eq!((p.screen_div, p.window_div, p.margin), (1.0, 1.0, 32.0));
        assert_eq!(placement_units(true, 0.0, 0.0).margin, 16.0);
    }

    // Sizes are not DPI-invariant anywhere: a window measured on a 1x display
    // occupies twice the pixels once the OS rescales it onto a 2x one, and
    // anchoring to the right edge with the old width misses by half a deck.
    #[test]
    fn a_window_is_measured_in_the_pixels_of_the_monitor_it_moves_to() {
        assert_eq!(placement_units(true, 2.0, 1.0).window_div, 0.5);
        assert_eq!(placement_units(true, 1.0, 2.0).window_div, 2.0);
        assert_eq!(placement_units(true, 2.0, 2.0).window_div, 1.0);
        assert_eq!(placement_units(true, 0.0, 0.0).window_div, 1.0);
    }

    #[test]
    fn placement_hands_set_position_the_space_it_was_measured_in() {
        assert!(matches!(
            placement_position(false, 100.5, -20.5),
            tauri::Position::Logical(p) if p.x == 100.5 && p.y == -20.5
        ));
        assert!(matches!(
            placement_position(true, 100.6, -20.4),
            tauri::Position::Physical(p) if p.x == 101 && p.y == -20
        ));
    }

    // The labels read backwards — `main` is the DECK — so the mapping from a
    // label to the flag it owns is worth pinning down rather than eyeballing.
    #[test]
    fn visibility_is_recorded_against_the_role_the_label_names() {
        let mut s = window_state::WindowState::default();
        assert_eq!((s.app_visible, s.deck_visible), (true, false));
        set_role_visible(&mut s, DECK_WINDOW, true);
        assert_eq!((s.app_visible, s.deck_visible), (true, true));
        set_role_visible(&mut s, APP_WINDOW, false);
        assert_eq!((s.app_visible, s.deck_visible), (false, true));
        // A label that owns neither flag must change nothing, rather than be
        // quietly filed under "the app".
        set_role_visible(&mut s, "somewhere-else", true);
        assert_eq!((s.app_visible, s.deck_visible), (false, true));
    }

    // The role is read by the frontend before first paint; a typo here is a
    // window that silently renders the other surface.
    #[test]
    fn the_role_script_sets_the_attribute_the_frontend_reads() {
        assert_eq!(
            window_role_script("deck"),
            "document.documentElement.dataset.windowRole='deck'"
        );
    }

    // The single-coordinate half of the same space decision, and unreachable
    // from a test on this host for the same reason — so it too takes the
    // platform as an argument and both shapes are asserted everywhere.
    #[test]
    fn a_coordinate_is_divided_off_windows_and_left_alone_on_it() {
        assert_eq!(placement_divisor(false, 2.0), 2.0);
        assert_eq!(placement_divisor(true, 2.0), 1.0);
        // A monitor tao could not inspect must not become a divide by zero.
        assert_eq!(placement_divisor(false, 0.0), 1.0);
        assert_eq!(placement_divisor(false, -1.0), 1.0);
    }

    // A deck the user dragged somewhere deliberate comes back there — but only
    // if "there" still exists. The numbers are a real two-display desk: a Retina
    // built-in and an external one up and to the left of it.
    #[test]
    fn a_remembered_position_off_every_monitor_is_rejected() {
        let monitors = [((0, 0), (2514u32, 1410u32)), ((-1343, -1050), (1680, 1050))];
        assert!(position_is_on_any(&monitors, (2170, 46)));
        assert!(position_is_on_any(&monitors, (-1000, -900)));
        // The unplugged display: remembered, but nothing is there any more.
        assert!(!position_is_on_any(&monitors, (4000, 300)));
        // Off in Y alone is off as well — the same two displays restacked
        // vertically leave every X in range and no Y anywhere near it.
        assert!(!position_is_on_any(&monitors, (2170, 5000)));
        // Exactly on the far edge is off: a window placed there is invisible.
        assert!(!position_is_on_any(&monitors, (2514, 0)));
    }

    // Monitor choice. `pick_monitor` is generic, so a &str stands in for a Monitor.
    #[test]
    fn monitor_choice_prefers_the_pointers_screen() {
        let picked = pick_monitor(
            Some((-800.0, 400.0)),
            |_, _| Some("cursor"),
            || panic!("consulted the window's monitor despite a located pointer"),
            || panic!("fell back to primary despite a located pointer"),
        );
        assert_eq!(picked, Some("cursor"));
    }

    #[test]
    fn monitor_choice_falls_back_when_the_pointer_is_on_no_display() {
        let picked = pick_monitor(
            Some((99_999.0, 99_999.0)),
            |_, _| None,
            || Some("current"),
            || panic!("skipped the window's own monitor"),
        );
        assert_eq!(picked, Some("current"));
    }

    #[test]
    fn monitor_choice_skips_the_point_lookup_without_a_pointer() {
        let picked = pick_monitor(
            None,
            |_, _| panic!("looked up a monitor for a pointer that was never located"),
            || Some("current"),
            || panic!("skipped the window's own monitor"),
        );
        assert_eq!(picked, Some("current"));
    }

    #[test]
    fn monitor_choice_ends_at_primary() {
        assert_eq!(
            pick_monitor(None, |_, _| None, || None, || Some("primary")),
            Some("primary"),
        );
        assert_eq!(
            pick_monitor::<&str>(None, |_, _| None, || None, || None),
            None,
        );
    }

    fn stable_runtime() -> Discovery {
        Discovery {
            url: "http://127.0.0.1:8800".to_string(),
            host: "127.0.0.1".to_string(),
            port: 8800,
            token: "stable-token".to_string(),
            source: "live".to_string(),
        }
    }

    #[test]
    fn a_missed_or_unhealthy_runtime_spawns_with_the_reason_logged() {
        let (plan, reason) =
            resolve_automatic_plan("stable", None, Path::new("/repo"), None, |_| true);
        assert!(matches!(plan, SidecarPlan::Spawn(_)));
        assert_eq!(reason, "no_runtime_json");
        let (plan, reason) = resolve_automatic_plan(
            "stable",
            None,
            Path::new("/repo"),
            Some(stable_runtime()),
            |_| false,
        );
        assert!(matches!(plan, SidecarPlan::Spawn(_)));
        assert_eq!(reason, "runtime_unhealthy");
    }

    #[test]
    fn plan_log_line_names_plan_reason_and_url_but_never_the_token() {
        let d = stable_runtime();
        let line = plan_log_line("attach", "launchd_runtime_appeared", Some(&d.url));
        assert_eq!(
            line,
            "herdeck: runtime plan=attach reason=launchd_runtime_appeared url=http://127.0.0.1:8800"
        );
        assert!(!line.contains(&d.token));
        assert_eq!(
            plan_log_line("spawn", "runtime_unhealthy", None),
            "herdeck: runtime plan=spawn reason=runtime_unhealthy"
        );
    }

    #[test]
    fn an_attached_runtime_counts_as_lost_only_after_three_failures_and_30s() {
        let t0 = std::time::Instant::now();
        let mut loss = AttachLoss::default();
        // Three quick failures (a runtime restarting) are not a loss.
        assert!(!loss.record_failure(t0));
        assert!(!loss.record_failure(t0 + Duration::from_secs(5)));
        assert!(!loss.record_failure(t0 + Duration::from_secs(10)));
        // Nor is a long gap with too few failures.
        let mut sparse = AttachLoss::default();
        assert!(!sparse.record_failure(t0));
        assert!(!sparse.record_failure(t0 + Duration::from_secs(40)));
        // Both thresholds met -> lost.
        assert!(!loss.record_failure(t0 + Duration::from_secs(29)));
        assert!(loss.record_failure(t0 + ATTACH_LOST_AFTER));
        // Any success restarts the clock: no flapping faster than the window.
        loss.record_ok();
        let t1 = t0 + Duration::from_secs(60);
        for i in 0..10 {
            assert!(!loss.record_failure(t1 + Duration::from_secs(i)));
        }
        assert!(loss.record_failure(t1 + ATTACH_LOST_AFTER));
    }

    #[test]
    fn a_self_spawned_shell_reattaches_only_to_a_different_healthy_runtime() {
        let own = discovery_on(51000);
        let launchd = stable_runtime();
        // Healthy launchd runtime appeared -> switch.
        assert_eq!(
            reattach_target(true, Some(&own), Some(launchd.clone()), |_| true),
            Some(launchd.clone())
        );
        // Not (yet) healthy, or no runtime.json -> stay on our sidecar.
        assert!(reattach_target(true, Some(&own), Some(launchd.clone()), |_| false).is_none());
        assert!(reattach_target(true, Some(&own), None, |_| true).is_none());
        // Already on it -> nothing to do (and no probe).
        assert!(reattach_target(true, Some(&launchd), Some(launchd.clone()), |_| panic!(
            "probed the runtime we are already on"
        ))
        .is_none());
        // Env override / attached / dev channel -> never repointed.
        assert!(reattach_target(false, Some(&own), Some(launchd), |_| true).is_none());
    }

    #[test]
    fn dev_plan_spawns_instead_of_attaching_a_healthy_stable_runtime() {
        let plan = resolve_automatic_plan(
            build_channel::DEV_CHANNEL,
            None,
            Path::new("/repo"),
            Some(stable_runtime()),
            |_| true,
        );

        assert_eq!(plan.1, "attach_disabled_for_channel");
        match plan.0 {
            SidecarPlan::Spawn(spec) => {
                assert!(spec.program.ends_with("/.venv/bin/python"));
            }
            SidecarPlan::External(..) => panic!("dev build attached the stable runtime"),
        }
    }

    #[test]
    fn stable_plan_attaches_with_the_runtime_json_flag_set() {
        let plan = resolve_automatic_plan(
            "stable",
            None,
            Path::new("/repo"),
            Some(stable_runtime()),
            |_| true,
        );

        assert_eq!(plan.1, "runtime_json_healthy");
        match plan.0 {
            // The bool is the whole re-discovery safety gate: only this path
            // may ever be repointed by a re-read runtime.json (review LOW — a
            // polarity regression here would reintroduce the hijack).
            SidecarPlan::External(_, true) => {}
            _ => panic!("stable build must attach from runtime.json with flag=true"),
        }
    }

    // Task 4: the tray's window-mode picker is gone, replaced by a single
    // deck-visibility toggle and an always-on-top checkbox. One label per menu
    // item now, not per mode.
    #[test]
    fn every_tray_label_exists_in_both_languages() {
        let en = tray_labels("en");
        let cs = tray_labels("cs");
        assert_eq!(en.len(), cs.len());
        assert!(en.iter().all(|l| !l.is_empty()));
        assert!(cs.iter().all(|l| !l.is_empty()));
        // The mode radio items are gone; nothing may name them any more.
        assert!(!cs.iter().any(|l| l.contains("Režim okna")));
        // show_app + toggle_deck(show) + toggle_deck(hide) + deck_aot +
        // autostart + reconnect + check_update + quit — `TrayMenuItems::
        // retitle` indexes every one of these, so the array length must
        // match exactly.
        assert_eq!(en.len(), 8);
        // A label accidentally left English in the cs array (or vice versa)
        // is exactly the defect this test exists to catch.
        for (i, (e, c)) in en.iter().zip(cs.iter()).enumerate() {
            assert_ne!(e, c, "tray_labels[{i}] is identical in en and cs");
        }
    }

    // The new "Check for updates" tray item (slot 6, just above "Quit" — see
    // `build_tray`/`TrayMenuItems::retitle`), pinned by exact text rather than
    // just "non-empty and differs from cs", which the generic parity test
    // above already covers: a future edit that renames it in one language
    // only, or that shuffles it to the wrong slot, must fail HERE.
    #[test]
    fn check_update_tray_label_exists_in_both_languages() {
        assert_eq!(tray_labels("en")[6], "Check for updates");
        assert_eq!(tray_labels("cs")[6], "Zkontrolovat aktualizace");
    }

    // The `toggle_deck` tray item's text depends on BOTH the language and the
    // deck's current visibility. Pulled out of the (untestable-without-a-tray)
    // retitle logic into a pure function so that interaction has a test at all.
    #[test]
    fn toggle_deck_label_reflects_visibility_in_both_languages() {
        assert_eq!(toggle_deck_label("en", false), "Show deck");
        assert_eq!(toggle_deck_label("en", true), "Hide deck");
        assert_eq!(toggle_deck_label("cs", false), "Zobrazit deck");
        assert_eq!(toggle_deck_label("cs", true), "Skrýt deck");
    }

    // The label follows the DECK's visibility and nothing else. Showing the app
    // window (from the tray, or from re-onboarding) must leave "Show deck"
    // alone; syncing on every window would make the item name the wrong gesture
    // just as surely as never syncing it does.
    #[test]
    fn only_the_deck_window_refreshes_the_deck_tray_label() {
        assert_eq!(deck_label_refresh(DECK_WINDOW, true), Some(true));
        assert_eq!(deck_label_refresh(DECK_WINDOW, false), Some(false));
        assert_eq!(deck_label_refresh(APP_WINDOW, true), None);
        assert_eq!(deck_label_refresh("some-future-window", false), None);
    }

    // A config that cannot be READ is not a config that says false: applying
    // false there would unfloat the deck, uncheck the tray box and rewrite the
    // cached flag while config.toml still said true.
    #[test]
    fn an_unreadable_config_leaves_the_live_always_on_top_alone() {
        let dir = std::env::temp_dir().join("herdeck-aot-target");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();

        assert_eq!(deck_always_on_top_target(&dir.join("absent.toml")), None);
        // A directory is readable-but-not-a-file: still no value to apply.
        assert_eq!(deck_always_on_top_target(&dir), None);

        let empty = dir.join("empty.toml");
        std::fs::write(&empty, "").unwrap();
        assert_eq!(deck_always_on_top_target(&empty), Some(false));

        let on = dir.join("on.toml");
        std::fs::write(&on, "[desktop]\ndeck_always_on_top = true\n").unwrap();
        assert_eq!(deck_always_on_top_target(&on), Some(true));

        // And it resolves, not just parses: the legacy migration fallback
        // applies to a live reload exactly as it does at startup.
        let legacy = dir.join("legacy.toml");
        std::fs::write(&legacy, "[desktop]\nwindow_mode = \"always_on_top\"\n").unwrap();
        assert_eq!(deck_always_on_top_target(&legacy), Some(true));
    }

    // The deck's right-click context menu shares three of its six items with
    // the tray: this pins down that they are, byte-for-byte, the SAME string —
    // not just "close enough" translations typed twice.
    #[test]
    fn deck_context_menu_reuses_the_trays_hide_aot_and_open_labels() {
        for lang in ["en", "cs"] {
            let tray = tray_labels(lang);
            let ctx = deck_context_menu_texts(lang);
            assert_eq!(ctx[0], tray[2], "hide label diverged from the tray's ({lang})");
            assert_eq!(ctx[4], tray[3], "always-on-top label diverged from the tray's ({lang})");
            assert_eq!(ctx[5], tray[0], "open-app label diverged from the tray's ({lang})");
        }
    }

    // Mirrors `every_tray_label_exists_in_both_languages`: every context-menu
    // text must exist in both languages and actually differ between them (a
    // label left English in the cs array, or vice versa, is the bug this
    // guards against).
    #[test]
    fn every_deck_context_menu_text_exists_in_both_languages() {
        let en = deck_context_menu_texts("en");
        let cs = deck_context_menu_texts("cs");
        assert!(en.iter().all(|l| !l.is_empty()));
        assert!(cs.iter().all(|l| !l.is_empty()));
        for (i, (e, c)) in en.iter().zip(cs.iter()).enumerate() {
            assert_ne!(e, c, "deck_context_menu_texts[{i}] is identical in en and cs");
        }
    }

    // Same shape of guarantee for the three zoom-only labels, on their own —
    // `deck_context_menu_texts` already covers them too, but this pins the
    // smaller building block down directly.
    #[test]
    fn every_zoom_label_exists_in_both_languages() {
        let en = deck_context_zoom_labels("en");
        let cs = deck_context_zoom_labels("cs");
        assert!(en.iter().all(|l| !l.is_empty()));
        assert!(cs.iter().all(|l| !l.is_empty()));
        // Element-wise, not just "the arrays as a whole differ somewhere" —
        // the latter would miss a single label left untranslated as long as
        // the other two still differed.
        for (i, (e, c)) in en.iter().zip(cs.iter()).enumerate() {
            assert_ne!(e, c, "deck_context_zoom_labels[{i}] is identical in en and cs");
        }
    }

    // The id→command mapping the `on_menu_event` match delegates to. Any id
    // that is not one of the three zoom items must resolve to `None` rather
    // than accidentally firing a zoom on an unrelated click.
    #[test]
    fn zoom_menu_ids_map_to_the_floating_scale_commands_they_name() {
        assert_eq!(zoom_command_for_menu_id("zoom_in"), Some("in"));
        assert_eq!(zoom_command_for_menu_id("zoom_out"), Some("out"));
        assert_eq!(zoom_command_for_menu_id("zoom_reset"), Some("reset"));
        assert_eq!(zoom_command_for_menu_id("show_app"), None);
        assert_eq!(zoom_command_for_menu_id("hide_deck"), None);
        assert_eq!(zoom_command_for_menu_id("deck_aot"), None);
    }
}

/// Gap, in logical points, between the floating deck and the edges of its screen.
const FLOATING_MARGIN: f64 = 16.0;

/// Top-right corner of a monitor's USABLE area, one margin in. Both axes are
/// clamped to the area's origin so a deck larger than the screen (zoomed up, or
/// a small external display) still starts on-screen instead of off past the
/// edge, where it could not be dragged back. Unit-agnostic: correct for whatever
/// consistent space the caller measures the area and the window in.
fn floating_origin(
    area_pos: (f64, f64),
    area_size: (f64, f64),
    win_size: (f64, f64),
    margin: f64,
) -> (f64, f64) {
    let (ax, ay) = area_pos;
    let x = (ax + area_size.0 - win_size.0 - margin).max(ax);
    let y = (ay + margin).min(ay + area_size.1 - win_size.1).max(ay);
    (x, y)
}

/// A scale factor as reported by a monitor or window, guarded for use as a
/// divisor — a zero or negative factor (a monitor tao could not inspect) would
/// otherwise turn a coordinate into an infinity and throw the window off-screen.
fn usable_scale(factor: f64) -> f64 {
    if factor > 0.0 {
        factor
    } else {
        1.0
    }
}

/// Put tao's cursor reading into the space its point lookup hit-tests.
///
/// tao reports the cursor as "physical" on every platform, but on two of the
/// three it gets there by scaling a LOGICAL reading by a single global factor,
/// while the matching lookup hit-tests logical rects:
///
/// - macOS scales `NSEvent.mouseLocation` by the PRIMARY monitor's factor;
///   `monitor_from_point` tests raw `CGDisplayBounds`, which are logical points.
/// - Linux scales the GDK pointer by the default window group's factor;
///   `monitor_at_point` takes GDK logical coordinates. Invisible at scale 1,
///   which is why X11 looks fine until someone sets `GDK_SCALE=2`.
///
/// Left uncorrected, a pointer on a 2x display resolves to a monitor to its
/// right — or, further out, to none at all, so the whole preference silently
/// never fires. Windows is the exception: `GetCursorPos` and `MonitorFromPoint`
/// are both raw physical, so it passes a scale of 1.
fn cursor_in_lookup_space(cursor: (f64, f64), scale: f64) -> (f64, f64) {
    let scale = usable_scale(scale);
    (cursor.0 / scale, cursor.1 / scale)
}

/// Does this environment describe a Wayland session? Either signal alone is
/// enough: the session type is what the seat advertises, and the socket is what
/// GTK connects to when the session type lies or is unset.
///
/// `GDK_BACKEND` outranks both. Forcing x11 is the standard WebKitGTK
/// workaround and it leaves `WAYLAND_DISPLAY` exported in a session where tao
/// then reports a perfectly real pointer — throwing that away would lose the
/// preference on exactly the desks most likely to run it.
///
/// The variable is a comma-separated preference ORDER, and which entry actually
/// connected is not knowable from the environment. What IS knowable is which
/// backends the list permits, so the rule keys off that, not off how many
/// entries there are:
///
/// - names both: a filter, not an answer — Wayland is possible only if the list
///   permits it and real only if the session says so. `wayland,x11` in an X11
///   session is X11; `x11,wayland` in a Wayland session with no Xwayland stays
///   Wayland, erring in the one case that cannot be settled here toward
///   distrusting the pointer rather than believing a `(0, 0)`.
/// - names wayland and no x11: nothing to fall through to, so it decides alone.
///   That matters for `GDK_BACKEND=wayland` with `WAYLAND_DISPLAY` unexported (a
///   service-launched or sanitised environment), where `wl_display_connect(NULL)`
///   still finds `wayland-0` while the session variables alone read it as X11.
/// - names x11 and no wayland: cannot be Wayland.
/// - names neither: no filter at all, and ignored.
fn is_wayland_session(
    session_type: Option<&str>,
    wayland_display: Option<&str>,
    gdk_backend: Option<&str>,
) -> bool {
    struct Named {
        wayland: bool,
        x11: bool,
    }
    let named = gdk_backend.map(|list| {
        list.split(',')
            .map(str::trim)
            .fold(Named { wayland: false, x11: false }, |acc, entry| Named {
                wayland: acc.wayland || entry.eq_ignore_ascii_case("wayland"),
                x11: acc.x11 || entry.eq_ignore_ascii_case("x11"),
            })
    });
    let session_is_wayland = session_type.map_or(false, |s| s.eq_ignore_ascii_case("wayland"))
        || wayland_display.map_or(false, |d| !d.is_empty());
    match named {
        Some(Named { wayland: true, x11: false }) => true,
        Some(Named { wayland: false, x11: true }) => false,
        Some(Named { wayland: true, x11: true }) => session_is_wayland,
        // Unset, blank, or naming nothing we recognise.
        _ => session_is_wayland,
    }
}

/// Which space a placement is computed in, and what the margin means there.
///
/// Windows keeps monitor rects and window positions in one global PHYSICAL
/// space, so the screen rect is not divided — but the margin is then consumed in
/// physical pixels, where a fixed 16 would shrink to 8 points on a 200% display,
/// so it scales with the screen instead. macOS and Linux compute in logical
/// points, where the margin already means what it says.
///
/// `window_div` puts `outer_size()` into whichever space that path measures in,
/// and the two are not the same job. The logical path divides by the window's
/// own factor to reach points, where a size is DPI-invariant and nothing further
/// is needed. The physical path has no such space, so it divides by the ratio of
/// the two factors to re-express the size in the TARGET monitor's pixels —
/// `outer_size()` is the size at the window's CURRENT monitor and the OS
/// rescales the window on arrival, so anchoring to the right edge with the old
/// width misses by half a deck moving 1x to 2x. An identity whenever the two
/// factors agree, which is every uniformly-scaled desk.
struct Placement {
    screen_div: f64,
    window_div: f64,
    margin: f64,
}

fn placement_units(is_windows: bool, monitor_scale: f64, window_scale: f64) -> Placement {
    if is_windows {
        Placement {
            screen_div: 1.0,
            window_div: usable_scale(window_scale) / usable_scale(monitor_scale),
            margin: FLOATING_MARGIN * usable_scale(monitor_scale),
        }
    } else {
        Placement {
            screen_div: usable_scale(monitor_scale),
            window_div: usable_scale(window_scale),
            margin: FLOATING_MARGIN,
        }
    }
}

/// The origin in the space `set_position` expects. tao converts a PHYSICAL
/// argument with the WINDOW's factor, which is wrong for a target derived from
/// the MONITOR's rect — so macOS and Linux hand it logical points and let the
/// conversion be an identity, while Windows hands back the physical space it
/// measured in to begin with.
fn placement_position(is_windows: bool, x: f64, y: f64) -> tauri::Position {
    if is_windows {
        PhysicalPosition {
            x: x.round() as i32,
            y: y.round() as i32,
        }
        .into()
    } else {
        LogicalPosition { x, y }.into()
    }
}

/// Can tao report where the pointer actually is? Under Wayland it cannot — the
/// compositor keeps the global cursor to itself, and tao returns a hard `(0, 0)`
/// rather than an error. Taken at face value that reads as "the pointer is on
/// whichever monitor owns the origin", which does not merely make the preference
/// inert: it OVERRIDES the better `current_monitor()` fallback with a wrong
/// answer. Only the GTK backend can be in a Wayland session — that is every unix
/// but macOS, since tao builds the same backend on the BSDs. Elsewhere the
/// variables mean nothing and a stray export must not cost anyone the pointer
/// preference this whole path exists for.
fn pointer_is_locatable(
    session_type: Option<&str>,
    wayland_display: Option<&str>,
    gdk_backend: Option<&str>,
) -> bool {
    !cfg!(all(unix, not(target_os = "macos")))
        || !is_wayland_session(session_type, wayland_display, gdk_backend)
}

/// Which monitor the deck belongs on, preferring the one under the pointer.
/// Placement used to hard-code the PRIMARY monitor, so on a multi-display desk
/// the deck opened on a screen the user was not looking at and read as "the
/// window never appeared". The pointer is the cheapest proxy for attention; the
/// window's own monitor and the primary one cover a pointer that cannot be
/// located — a failed query, or a cursor parked outside every display.
///
/// Pure over its lookups: the ORDER is the fix, and this way it is testable
/// without a display attached.
fn pick_monitor<M>(
    cursor: Option<(f64, f64)>,
    at_point: impl Fn(f64, f64) -> Option<M>,
    current: impl Fn() -> Option<M>,
    primary: impl Fn() -> Option<M>,
) -> Option<M> {
    cursor
        .and_then(|(x, y)| at_point(x, y))
        .or_else(current)
        .or_else(primary)
}

fn active_monitor(window: &tauri::WebviewWindow) -> Option<tauri::Monitor> {
    // See cursor_in_lookup_space. The primary monitor's factor is exactly what
    // macOS scales by, and the closest reachable stand-in for the GDK default
    // group's factor on Linux — the two coincide on any uniformly-scaled desk.
    let scale = if cfg!(windows) {
        1.0
    } else {
        window
            .primary_monitor()
            .ok()
            .flatten()
            .map_or(1.0, |m| m.scale_factor())
    };
    let cursor = if pointer_is_locatable(
        env::var("XDG_SESSION_TYPE").ok().as_deref(),
        env::var("WAYLAND_DISPLAY").ok().as_deref(),
        env::var("GDK_BACKEND").ok().as_deref(),
    ) {
        window
            .cursor_position()
            .ok()
            .map(|p| cursor_in_lookup_space((p.x, p.y), scale))
    } else {
        None
    };
    pick_monitor(
        cursor,
        |x, y| window.monitor_from_point(x, y).ok().flatten(),
        || window.current_monitor().ok().flatten(),
        || window.primary_monitor().ok().flatten(),
    )
}

/// Is a remembered deck origin still on a connected screen? Checked before
/// restoring it, so a deck last seen on an unplugged monitor comes back where
/// the user is rather than nowhere. Unit-agnostic like `floating_origin`: the
/// caller measures the areas and the position in one space (see
/// `placement_space_position`) and this only compares them.
fn position_is_on_any(areas: &[((i32, i32), (u32, u32))], pos: (i32, i32)) -> bool {
    areas.iter().any(|((ax, ay), (w, h))| {
        pos.0 >= *ax && pos.0 < ax + *w as i32 && pos.1 >= *ay && pos.1 < ay + *h as i32
    })
}

/// What divides a tao "physical" reading to reach the space placements are
/// measured in — the same choice `Placement` makes, for a single coordinate.
///
/// Off Windows that space is logical points (see `place_floating`), and tao got
/// to "physical" by scaling a logical value UP, so the same factor divides it
/// back down. `scale` is therefore whichever factor produced the reading: a
/// monitor rect is scaled by the MONITOR's, a window origin by the WINDOW's, and
/// on a mixed-DPI desk those are not the same number. Windows keeps both in one
/// global physical space and needs no conversion at all.
fn placement_divisor(is_windows: bool, scale: f64) -> f64 {
    if is_windows {
        1.0
    } else {
        usable_scale(scale)
    }
}

/// Every connected monitor's work area, in the space placements are measured in.
/// Dividing per monitor is the point: on a desk mixing a 2x built-in with a 1x
/// external the two PHYSICAL work rects overlap, so a containment test run in
/// that space answers for the wrong screen.
fn monitor_work_areas(window: &tauri::WebviewWindow) -> Vec<((i32, i32), (u32, u32))> {
    window
        .available_monitors()
        .into_iter()
        .flatten()
        .map(|monitor| {
            let div = placement_divisor(cfg!(windows), monitor.scale_factor());
            let area = monitor.work_area();
            (
                (
                    (area.position.x as f64 / div).round() as i32,
                    (area.position.y as f64 / div).round() as i32,
                ),
                (
                    (area.size.width as f64 / div).round() as u32,
                    (area.size.height as f64 / div).round() as u32,
                ),
            )
        })
        .collect()
}

/// A window origin as tao reports it (`Moved`, `outer_position()`), put into the
/// space placements are measured in. This is the space the remembered deck
/// position is STORED in, so that restoring it is exactly `placement_position`'s
/// job — the same conversion `place_floating` already trusts, run backwards.
fn placement_space_position(
    window: &tauri::WebviewWindow,
    pos: PhysicalPosition<i32>,
) -> (i32, i32) {
    let div = placement_divisor(cfg!(windows), window.scale_factor().unwrap_or(1.0));
    (
        (pos.x as f64 / div).round() as i32,
        (pos.y as f64 / div).round() as i32,
    )
}

/// Position the floating window near the top-right of the monitor the user is on.
/// `deck_always_on_top` is applied separately; this only places the window.
/// Placement uses the WORK area, not the full screen, so the deck never opens
/// under the macOS menu bar or behind the dock.
///
/// On macOS and Linux this is computed in LOGICAL points, the one space the
/// inputs agree on. `work_area()` and `outer_size()` are both "physical" there,
/// but each is scaled by a DIFFERENT factor — the monitor's and the window's —
/// and those part company the moment the deck moves between a Retina screen and
/// an external one. tao then converts a physical `set_position` argument with
/// the WINDOW's factor, so a physical target derived from the MONITOR's rect
/// lands wrong as well. A logical position removes both mismatches.
///
/// Windows is the opposite case and takes the untouched physical path:
/// `rcWork` and window positions already live in one global physical space, so
/// there is nothing to convert and dividing would invent a space of its own.
fn place_floating(window: &tauri::WebviewWindow) {
    if let (Some(monitor), Ok(win_size)) = (active_monitor(window), window.outer_size()) {
        let monitor_scale = monitor.scale_factor();
        let units = placement_units(
            cfg!(windows),
            monitor_scale,
            window.scale_factor().unwrap_or(monitor_scale),
        );
        let (screen, win) = (units.screen_div, units.window_div);
        let area = monitor.work_area();
        let (x, y) = floating_origin(
            (area.position.x as f64 / screen, area.position.y as f64 / screen),
            (area.size.width as f64 / screen, area.size.height as f64 / screen),
            (win_size.width as f64 / win, win_size.height as f64 / win),
            units.margin,
        );
        let _ = window.set_position(placement_position(cfg!(windows), x, y));
    }
}

/// Stamp a window's role on `<html>` before its first paint. The frontend picks
/// its surface from that attribute, so injecting it any later would show the
/// deck a frame of the opaque settings styling first (FOUC).
fn window_role_script(role: &str) -> String {
    format!("document.documentElement.dataset.windowRole='{role}'")
}

/// Put the deck back where the user last dragged it, or near the top-right of
/// the monitor the pointer is on. A remembered origin is honoured only while it
/// still lands on a connected screen: a borderless window dropped onto an
/// unplugged display has no titlebar to drag it back with.
fn place_deck(window: &tauri::WebviewWindow, remembered: Option<(i32, i32)>) {
    if let Some((x, y)) = remembered {
        if position_is_on_any(&monitor_work_areas(window), (x, y)) {
            let _ = window.set_position(placement_position(cfg!(windows), x as f64, y as f64));
            return;
        }
    }
    place_floating(window);
}

/// Which visibility flag a window label owns. Split out because the labels are
/// historical and read backwards: `main` is the deck, `config` is the app.
///
/// A label that is neither records NOTHING. An `else` arm that assumed "the app"
/// would turn any future third window — or a renamed constant — into a silently
/// wrong remembered layout, which is exactly the class of bug this whole file is
/// getting rid of.
fn set_role_visible(state: &mut WindowState, label: &str, visible: bool) {
    match label {
        DECK_WINDOW => state.deck_visible = visible,
        APP_WINDOW => state.app_visible = visible,
        _ => {}
    }
}

/// The ONE place `window-state.json` is written from, so its two callers cannot
/// drift apart. Snapshots under the lock and writes outside it. Best-effort:
/// losing the file costs the next launch its remembered layout and nothing else.
fn store_window_state(state: &AppState) {
    let snapshot = *state.window_state.lock().unwrap();
    window_state::store(&window_state::state_dir(), &snapshot);
}

/// Mutate the live window state and mirror it to disk.
fn update_window_state(app: &tauri::AppHandle, f: impl FnOnce(&mut WindowState)) {
    let Some(state) = app.try_state::<AppState>() else {
        return;
    };
    f(&mut state.window_state.lock().unwrap());
    store_window_state(&state);
}

/// Write the live state out as it stands — the exit path, and the flush that
/// pairs with `remember_deck_position`.
fn persist_window_state(app: &tauri::AppHandle) {
    if let Some(state) = app.try_state::<AppState>() {
        store_window_state(&state);
    }
}

/// Record the deck's new origin WITHOUT touching the disk: one drag emits
/// hundreds of `Moved` events. Hiding or closing the deck writes, and so does
/// exit, which between them cover every way a position can be the last thing
/// that changed.
fn remember_deck_position(app: &tauri::AppHandle, position: (i32, i32)) {
    if let Some(state) = app.try_state::<AppState>() {
        state.window_state.lock().unwrap().deck_position = Some(position);
    }
}

/// Whether a visibility change should retitle the tray's `toggle_deck` item, and
/// to what: `Some(visible)` for the deck, `None` for any other window. The app
/// window opening must not turn "Show deck" into "Hide deck".
///
/// Split out of `show_role_window`/`hide_role_window` because it is the only part
/// of the sync a unit test can reach — the rest needs a live tray menu.
fn deck_label_refresh(label: &str, visible: bool) -> Option<bool> {
    (label == DECK_WINDOW).then_some(visible)
}

/// Retitle the tray's `toggle_deck` item for the deck's new visibility, so the
/// item always names what it will do next.
///
/// Takes both locks it needs one at a time and holds neither across the other:
/// its callers have already released `AppState.window_state` by the time they
/// get here (`update_window_state` drops its guard when it returns), and nothing
/// reached from `TrayHandles` takes a window-state lock.
fn sync_deck_tray_label(app: &tauri::AppHandle, deck_visible: bool) {
    if let Some(handles) = app.try_state::<TrayHandles>() {
        if let Some(items) = handles.0.lock().unwrap().as_ref() {
            items.sync_toggle_deck_label(deck_visible);
        }
    }
}

/// Show a role window and record that it is open. Every entry point — tray,
/// hotkey, frontend command — goes through here, so the remembered layout can
/// never drift from what is actually on screen, and neither can the tray's own
/// show/hide-deck label.
fn show_role_window(app: &tauri::AppHandle, label: &str) {
    if let Some(w) = app.get_webview_window(label) {
        let _ = w.show();
        let _ = w.set_focus();
    }
    update_window_state(app, |s| set_role_visible(s, label, true));
    if let Some(deck_visible) = deck_label_refresh(label, true) {
        sync_deck_tray_label(app, deck_visible);
        let _ = app.emit_to(APP_WINDOW, DECK_VISIBILITY_EVENT, deck_visible);
    }
}

/// Hide a role window and record that it is closed (see `show_role_window`).
fn hide_role_window(app: &tauri::AppHandle, label: &str) {
    if let Some(w) = app.get_webview_window(label) {
        let _ = w.hide();
    }
    update_window_state(app, |s| set_role_visible(s, label, false));
    if let Some(deck_visible) = deck_label_refresh(label, false) {
        sync_deck_tray_label(app, deck_visible);
        let _ = app.emit_to(APP_WINDOW, DECK_VISIBILITY_EVENT, deck_visible);
    }
}

/// Open the deck overlay. The tray and the app window's pop-out control both
/// land here; the frontend reaches it through the command of the same name.
#[tauri::command]
fn show_deck(app: tauri::AppHandle) {
    show_role_window(&app, DECK_WINDOW);
}

/// Close the deck overlay back to the tray.
#[tauri::command]
fn hide_deck(app: tauri::AppHandle) {
    hide_role_window(&app, DECK_WINDOW);
}

/// Open the settings window — the app surface, and where onboarding lives.
#[tauri::command]
fn show_app(app: tauri::AppHandle) {
    show_role_window(&app, APP_WINDOW);
}

/// The deck's actual on-screen visibility, read straight from the window
/// rather than `WindowState` — every caller needs "is it visible right now",
/// not "was it last recorded so".
fn deck_is_visible(app: &tauri::AppHandle) -> bool {
    app.get_webview_window(DECK_WINDOW)
        .and_then(|w| w.is_visible().ok())
        .unwrap_or(false)
}

/// Whether the deck overlay is visible right now. The app window's toggle
/// button calls this once on mount — the deck may already be open (tray,
/// hotkey, a previous session) by the time the app window appears, and after
/// that its label follows `DECK_VISIBILITY_EVENT` instead of polling this.
#[tauri::command]
fn deck_visible(app: tauri::AppHandle) -> bool {
    deck_is_visible(&app)
}

/// Show/hide the deck overlay — shared by the tray's `toggle_deck` item and
/// the deck-toggle hotkey, so both flip the SAME window the SAME way. The tray
/// item's own text follows from `show_role_window`/`hide_role_window`, which
/// every deck-visibility path goes through.
fn toggle_deck_window(app: &tauri::AppHandle) {
    if deck_is_visible(app) {
        hide_role_window(app, DECK_WINDOW);
    } else {
        show_role_window(app, DECK_WINDOW);
    }
}

/// Persist `[desktop].deck_always_on_top = target` to base config via the
/// sidecar. Read-modify-write over the existing `/config` routes (token
/// injected Rust-side, like the editor) — the same shape the pre-roles
/// `persist_window_mode` used for `window_mode`. It deliberately sends no
/// `revision`: `tests/test_config_service.py::
/// test_write_without_revision_stays_compatible` documents that the sidecar
/// must keep accepting a revision-free body like this one.
///
/// Returns `Ok(())` ONLY on a confirmed write: the `/config` contract returns
/// validation failures as HTTP 200 with a non-empty `errors`, writing NOTHING,
/// so success requires HTTP 200 AND `errors == []`. The POST blocks on the
/// sidecar's `_setup_lock`, so it uses the longer `SETUP_CONNECT_TIMEOUT`; a
/// timeout there is a genuine wedge, not a slow-but-fine write.
fn persist_deck_always_on_top(state: &AppState, target: bool) -> Result<(), String> {
    let d = state
        .discovery
        .lock()
        .unwrap()
        .clone()
        .ok_or_else(|| "sidecar not ready".to_string())?;
    let body = http::http_get(
        &d.host,
        d.port,
        &format!("/config?token={}", d.token),
        SIDECAR_TIMEOUT,
    )?;
    let mut cfg: serde_json::Value =
        serde_json::from_str(&body).map_err(|e| format!("invalid /config JSON: {e}"))?;
    {
        let base = cfg
            .get_mut("base")
            .and_then(|b| b.as_object_mut())
            .ok_or_else(|| "config response missing base table".to_string())?;
        let desktop = base
            .entry("desktop")
            .or_insert_with(|| serde_json::json!({}));
        let desktop_obj = desktop
            .as_object_mut()
            .ok_or_else(|| "config desktop is not a table".to_string())?;
        desktop_obj.insert(
            "deck_always_on_top".to_string(),
            serde_json::Value::Bool(target),
        );
    }
    // POST only {base, profiles, local} — the redacted `secrets` field from the GET
    // is display-only and never written back (secret values are one-way).
    let payload = serde_json::json!({
        "base": cfg.get("base").cloned().unwrap_or_else(|| serde_json::json!({})),
        "profiles": cfg.get("profiles").cloned().unwrap_or_else(|| serde_json::json!({})),
        "local": cfg.get("local").cloned().unwrap_or_else(|| serde_json::json!({})),
    });
    let (code, resp) = http::http_post_json(
        &d.host,
        d.port,
        "/config",
        (HDR_TOKEN, &d.token),
        &payload.to_string(),
        SETUP_CONNECT_TIMEOUT,
    )?;
    if code != 200 {
        return Err(format!("POST /config returned HTTP {code}"));
    }
    let parsed: serde_json::Value =
        serde_json::from_str(&resp).map_err(|e| format!("invalid /config response JSON: {e}"))?;
    match parsed.get("errors").and_then(|e| e.as_array()) {
        Some(arr) if arr.is_empty() => Ok(()),
        Some(_) => Err("config rejected (validation errors)".to_string()),
        None => Err("config response missing 'errors' field".to_string()),
    }
}

/// (Re)register the global shortcuts from the sidecar's `/config`: the deck
/// toggle and the opt-in "next blocked agent" hotkey. A failure leaves the deck
/// usable without a hotkey; it is returned as a message so `reload_hotkey` can
/// surface it in the settings UI (C4). When the configured toggle accelerator
/// cannot be registered the default is tried instead, and the error still says
/// the configured one failed. The two registrations are independent: one
/// failing never keeps the other from being registered.
fn register_toggle_hotkey(app: &tauri::AppHandle, d: &Discovery) -> Result<(), String> {
    use tauri_plugin_global_shortcut::GlobalShortcutExt;

    let gs = app.global_shortcut();
    let _ = gs.unregister_all();

    let body = http::http_get(
        &d.host,
        d.port,
        &format!("/config?token={}", d.token),
        SIDECAR_TIMEOUT,
    )
    .map_err(|e| format!("hotkey: /config fetch failed: {e}"))?;
    let cfg: serde_json::Value =
        serde_json::from_str(&body).map_err(|e| format!("hotkey: invalid /config JSON: {e}"))?;

    let errors: Vec<String> = [
        register_toggle_accelerator(app, &cfg),
        register_next_blocked_accelerator(app, &cfg),
    ]
    .into_iter()
    .filter_map(Result::err)
    .collect();
    if errors.is_empty() {
        Ok(())
    } else {
        Err(format!("hotkey: {}", errors.join("; ")))
    }
}

fn register_toggle_accelerator(app: &tauri::AppHandle, cfg: &serde_json::Value) -> Result<(), String> {
    use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};

    let gs = app.global_shortcut();
    let accel = match hotkey::toggle_deck_accelerator(cfg) {
        Some(a) => a,
        None => return Ok(()), // explicitly disabled
    };

    let app_for_cb = app.clone();
    let handler = move |_app: &tauri::AppHandle, _sc: &tauri_plugin_global_shortcut::Shortcut, event: tauri_plugin_global_shortcut::ShortcutEvent| {
        if event.state == ShortcutState::Pressed {
            toggle_deck_window(&app_for_cb);
        }
    };
    let Err(e) = gs.on_shortcut(accel.as_str(), handler) else {
        return Ok(());
    };
    let mut msg = format!("could not register the deck hotkey '{accel}': {e}");
    if accel != hotkey::DEFAULT_TOGGLE_DECK {
        let app_for_fb = app.clone();
        let fallback = gs.on_shortcut(
            hotkey::DEFAULT_TOGGLE_DECK,
            move |_app: &tauri::AppHandle, _sc: &tauri_plugin_global_shortcut::Shortcut, event: tauri_plugin_global_shortcut::ShortcutEvent| {
                if event.state == ShortcutState::Pressed {
                    toggle_deck_window(&app_for_fb);
                }
            },
        );
        if fallback.is_ok() {
            msg.push_str(&format!(" (using the default '{}' instead)", hotkey::DEFAULT_TOGGLE_DECK));
        }
    }
    Err(msg)
}

/// Register the opt-in `[hotkeys].next_blocked` accelerator (no default).
fn register_next_blocked_accelerator(app: &tauri::AppHandle, cfg: &serde_json::Value) -> Result<(), String> {
    use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};

    let Some(accel) = hotkey::next_blocked_accelerator(cfg) else {
        return Ok(()); // not configured
    };
    let app_for_cb = app.clone();
    app.global_shortcut()
        .on_shortcut(
            accel.as_str(),
            move |_app: &tauri::AppHandle, _sc: &tauri_plugin_global_shortcut::Shortcut, event: tauri_plugin_global_shortcut::ShortcutEvent| {
                if event.state == ShortcutState::Pressed {
                    jump_to_next_blocked(&app_for_cb);
                }
            },
        )
        .map_err(|e| format!("could not register the next-blocked hotkey '{accel}': {e}"))
}

/// The "next blocked agent" hotkey: show the deck and ask the runtime to open
/// the drill of the agent that has been blocked longest (`POST /triage`, the
/// same action as a press on the NEEDS YOU panel). The HTTP call runs off the
/// shortcut thread; a failure is only logged — the deck is shown either way.
fn jump_to_next_blocked(app: &tauri::AppHandle) {
    show_role_window(app, DECK_WINDOW);
    let discovery = {
        let state = app.state::<AppState>();
        current_discovery(&state)
    };
    let Ok(d) = discovery else {
        eprintln!("next-blocked hotkey: runtime not ready");
        return;
    };
    tauri::async_runtime::spawn_blocking(move || {
        match http::send_triage(&d.host, d.port, &d.token, SIDECAR_TIMEOUT) {
            Ok(204) | Ok(404) => {} // 404 = demo source without drills
            Ok(code) => eprintln!("next-blocked hotkey: POST /triage returned HTTP {code}"),
            Err(e) => eprintln!("next-blocked hotkey: POST /triage failed: {e}"),
        }
    });
}

/// Startup/discovery-time registration: nobody is waiting for the result, so a
/// failure is only logged (the settings UI learns it from `reload_hotkey`).
fn register_toggle_hotkey_logged(app: &tauri::AppHandle, d: &Discovery) {
    if let Err(e) = register_toggle_hotkey(app, d) {
        eprintln!("{e}");
    }
}

/// Re-read `/config` and re-register the deck-toggle hotkey (the editor calls
/// this after a successful config write so a changed accelerator takes effect).
/// `Err` carries the registration failure for the settings UI to show.
#[tauri::command]
async fn reload_hotkey(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
) -> Result<(), String> {
    let d = current_discovery(&state)?;
    run_blocking(move || register_toggle_hotkey(&app, &d)).await
}

/// The always-on-top value a live re-read should apply, or `None` when the
/// config could not be READ at all — which is not the same answer as a config
/// that says `false`. Reading `""` on failure would resolve to `false` and hand
/// that to the window, `AppState` and the tray checkbox while `config.toml`
/// still said `true`, so the two outcomes stay apart here.
fn deck_always_on_top_target(config_path: &Path) -> Option<bool> {
    let config_text = std::fs::read_to_string(config_path).ok()?;
    Some(deck_prefs::resolve_deck_always_on_top(&config_text))
}

/// Re-read `[desktop].deck_always_on_top` from config.toml and apply it live:
/// the deck window's actual always-on-top state, the cached `AppState` value,
/// and the tray checkbox all move together — the same three the tray's own
/// `deck_aot` menu handler keeps in sync. The editor calls this after a
/// successful config write, exactly like `reload_hotkey` for the accelerator.
///
/// Reads straight from disk instead of taking the new value as an argument,
/// so it can never disagree with what Apply just persisted: Apply writes
/// through the sidecar's `/config` route, not through this process's own file
/// handle, so this process has no other way to learn the confirmed value. A
/// read that fails outright changes NOTHING (see `deck_always_on_top_target`).
#[tauri::command]
fn reload_deck_always_on_top(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    tray: tauri::State<'_, TrayHandles>,
) {
    let Some(target) = deck_always_on_top_target(&default_config_path()) else {
        eprintln!("deck always-on-top: config unreadable, leaving the live value alone");
        return;
    };
    if let Some(w) = app.get_webview_window(DECK_WINDOW) {
        let _ = w.set_always_on_top(target);
    }
    *state.deck_always_on_top.lock().unwrap() = target;
    if let Some(items) = tray.0.lock().unwrap().as_ref() {
        let _ = items.deck_aot.set_checked(target);
    }
}

/// English/Czech texts for every tray item, keyed by the item order in
/// `TrayMenuItems::retitle`. The tray is native — the WebView retitles it via
/// the `tray_set_language` command when the deck's `[view].language` changes.
///
/// `toggle_deck` occupies TWO slots (show/hide) because its text also depends
/// on the deck's current visibility, not just the language — see
/// `toggle_deck_label`, which picks between them.
fn tray_labels(lang: &str) -> [&'static str; 8] {
    match lang {
        "cs" => [
            "Otevřít Herdeck",
            "Zobrazit deck",
            "Skrýt deck",
            "Deck vždy navrchu",
            "Spouštět po přihlášení",
            "Změnit připojení…",
            "Zkontrolovat aktualizace",
            "Ukončit",
        ],
        _ => [
            "Open Herdeck",
            "Show deck",
            "Hide deck",
            "Deck always on top",
            "Start at login",
            "Change connection…",
            "Check for updates",
            "Quit",
        ],
    }
}

/// The tray icon's id, for `tray_by_id` lookups after `build_tray`.
const TRAY_ID: &str = "herdeck-tray";

/// The blocked-agent part of the tray tooltip. A count-first "label: n" form in
/// Czech sidesteps plural agreement.
fn tray_blocked_label(lang: &str, blocked: u64) -> String {
    match lang {
        "cs" => format!("zablokováno: {blocked}"),
        _ => format!("{blocked} blocked"),
    }
}

/// The tray tooltip: the app's display name, plus how many agents are blocked
/// when any are (the count the deck's own `/state` poll just reported).
fn tray_tooltip(lang: &str, name: &str, blocked: Option<u64>) -> String {
    match blocked {
        Some(n) if n > 0 => format!("{name} — {}", tray_blocked_label(lang, n)),
        _ => name.to_string(),
    }
}

/// Which text the `toggle_deck` tray item (and its hotkey-driven refresh)
/// shows: it depends on both the language and whether the deck is currently
/// visible. Pulled out of `TrayMenuItems::retitle` as a pure function — the
/// interaction is the part worth pinning down with a test, and building a
/// real tray in a unit test isn't possible.
fn toggle_deck_label(lang: &str, deck_visible: bool) -> &'static str {
    let l = tray_labels(lang);
    if deck_visible {
        l[2] // "Hide deck" / "Skrýt deck"
    } else {
        l[1] // "Show deck" / "Zobrazit deck"
    }
}

/// English/Czech texts unique to the deck's right-click context menu — the
/// three zoom items, which the tray has no equivalent of. "Hide deck",
/// "Deck always on top" and "Open Herdeck" are NOT retyped here: see
/// `deck_context_menu_texts`, which pulls them from `tray_labels` instead so
/// the two menus can never describe the same action two different ways.
fn deck_context_zoom_labels(lang: &str) -> [&'static str; 3] {
    match lang {
        "cs" => ["Zvětšit", "Zmenšit", "Původní velikost"],
        _ => ["Zoom in", "Zoom out", "Actual size"],
    }
}

/// The six texts on the deck's right-click context menu, in on-screen order:
/// hide, the three zoom commands, the always-on-top checkbox, then open-app.
/// Split out of `build_deck_context_menu` (which needs a live `AppHandle` to
/// build real menu items and so cannot run in a unit test) purely so the
/// EN/CS mapping — and the fact that three of the six reuse `tray_labels`
/// byte-for-byte — has a test that does not need a display.
fn deck_context_menu_texts(lang: &str) -> [&'static str; 6] {
    let tray = tray_labels(lang);
    let zoom = deck_context_zoom_labels(lang);
    [
        tray[2], // "Hide deck" / "Skrýt deck" — identical to the tray's
        zoom[0], zoom[1], zoom[2],
        tray[3], // "Deck always on top" / "Deck vždy navrchu" — identical to the tray's
        tray[0], // "Open Herdeck" / "Otevřít Herdeck" — identical to the tray's
    ]
}

/// Which floating-scale command a context-menu zoom item's id names, or
/// `None` for any other id. Pulled out of the `on_menu_event` match so the
/// id→command mapping has a test that does not need a live tray or window.
fn zoom_command_for_menu_id(id: &str) -> Option<&'static str> {
    match id {
        MENU_ID_ZOOM_IN => Some("in"),
        MENU_ID_ZOOM_OUT => Some("out"),
        MENU_ID_ZOOM_RESET => Some("reset"),
        _ => None,
    }
}

/// Handles to every retitlable tray item, managed as Tauri state so the
/// `tray_set_language` command can reach them after setup.
#[derive(Default)]
struct TrayHandles(Mutex<Option<TrayMenuItems>>);

struct TrayMenuItems {
    show_app: MenuItem<tauri::Wry>,
    toggle_deck: MenuItem<tauri::Wry>,
    deck_aot: CheckMenuItem<tauri::Wry>,
    autostart: CheckMenuItem<tauri::Wry>,
    reconnect: MenuItem<tauri::Wry>,
    check_update: MenuItem<tauri::Wry>,
    quit: MenuItem<tauri::Wry>,
    /// The language `retitle` was last called with. Needed so a
    /// visibility-only refresh of `toggle_deck` (`sync_toggle_deck_label`,
    /// run from every path that shows or hides the deck) can pick the right
    /// string without its caller having to track the language too.
    lang: Mutex<String>,
    /// The blocked-agent count the tooltip last showed, so the tooltip is only
    /// touched when it actually changes (`/state` is polled many times a second).
    blocked: Mutex<Option<u64>>,
}

impl TrayMenuItems {
    fn retitle(&self, lang: &str, deck_visible: bool) {
        let l = tray_labels(lang);
        let _ = self.show_app.set_text(l[0]);
        let _ = self.toggle_deck.set_text(toggle_deck_label(lang, deck_visible));
        let _ = self.deck_aot.set_text(l[3]);
        let _ = self.autostart.set_text(l[4]);
        let _ = self.reconnect.set_text(l[5]);
        let _ = self.check_update.set_text(l[6]);
        let _ = self.quit.set_text(l[7]);
        *self.lang.lock().unwrap() = lang.to_string();
    }

    /// Refresh only `toggle_deck`'s text for a visibility change, in whichever
    /// language `retitle` was last called with. The counterpart to `retitle`,
    /// which instead needs the visibility handed in because IT runs on a
    /// language change.
    fn sync_toggle_deck_label(&self, deck_visible: bool) {
        let lang = self.lang.lock().unwrap().clone();
        let _ = self.toggle_deck.set_text(toggle_deck_label(&lang, deck_visible));
    }

    /// The language `retitle` was last called with. The single source of
    /// truth for "what language is the UI in right now" — the deck's own
    /// `[view].language` feeds it via `tray_set_language` (see `App.svelte`'s
    /// `locale` effect), so anything else that needs the current language
    /// (the context menu below) reads it from here instead of tracking it a
    /// second time.
    fn current_lang(&self) -> String {
        self.lang.lock().unwrap().clone()
    }
}

/// Retitle the native tray menu for `lang` ("en"/"cs"). Called by the WebView
/// whenever the deck-reported language changes; unknown values fall back to en.
#[tauri::command]
fn tray_set_language(app: tauri::AppHandle, lang: String, handles: tauri::State<'_, TrayHandles>) {
    let deck_visible = deck_is_visible(&app);
    if let Some(items) = handles.0.lock().unwrap().as_ref() {
        items.retitle(&lang, deck_visible);
        let blocked = *items.blocked.lock().unwrap();
        set_tray_tooltip(&app, &lang, blocked);
    }
}

fn set_tray_tooltip(app: &tauri::AppHandle, lang: &str, blocked: Option<u64>) {
    if let Some(tray) = app.tray_by_id(TRAY_ID) {
        let text = tray_tooltip(lang, &build_channel::display_name(), blocked);
        let _ = tray.set_tooltip(Some(text));
    }
}

/// Record the blocked-agent count from a `/state` response and retitle the
/// tray tooltip — only when the count changed.
fn update_tray_blocked(app: &tauri::AppHandle, blocked: Option<u64>) {
    let Some(handles) = app.try_state::<TrayHandles>() else {
        return;
    };
    let lang = {
        let guard = handles.0.lock().unwrap();
        let Some(items) = guard.as_ref() else {
            return;
        };
        let mut last = items.blocked.lock().unwrap();
        if *last == blocked {
            return;
        }
        *last = blocked;
        items.current_lang()
    };
    set_tray_tooltip(app, &lang, blocked);
}

/// Build the deck's right-click context menu fresh for every popup, so its
/// "Deck always on top" checkbox always shows the CURRENT flag rather than a
/// snapshot from whenever the tray was last built.
///
/// "Hide deck", "Deck always on top" and "Open Herdeck" carry the SAME ids as
/// their tray counterparts ("deck_aot" and "show_app" ARE the tray's ids;
/// "hide_deck" is new but goes through the same `on_menu_event` closure as
/// `toggle_deck`'s hide path). `TrayIconBuilder::on_menu_event` is registered
/// ONCE for the whole app and — per its own doc comment — fires "for any menu
/// event, whether it is coming from this window, another window, or from the
/// tray icon menu". Reusing an id therefore reuses the tray's existing
/// handler outright; it does not add a second copy of that logic.
fn build_deck_context_menu(
    app: &tauri::AppHandle,
    lang: &str,
    always_on_top: bool,
) -> tauri::Result<Menu<tauri::Wry>> {
    let texts = deck_context_menu_texts(lang);
    let hide = MenuItem::with_id(app, MENU_ID_HIDE_DECK, texts[0], true, None::<&str>)?;
    let zoom_in = MenuItem::with_id(app, MENU_ID_ZOOM_IN, texts[1], true, Some("CmdOrCtrl+="))?;
    let zoom_out = MenuItem::with_id(app, MENU_ID_ZOOM_OUT, texts[2], true, Some("CmdOrCtrl+-"))?;
    let zoom_reset =
        MenuItem::with_id(app, MENU_ID_ZOOM_RESET, texts[3], true, Some("CmdOrCtrl+0"))?;
    let deck_aot = CheckMenuItem::with_id(
        app,
        MENU_ID_DECK_AOT,
        texts[4],
        true,
        always_on_top,
        None::<&str>,
    )?;
    let open_app = MenuItem::with_id(app, MENU_ID_SHOW_APP, texts[5], true, None::<&str>)?;
    let sep1 = PredefinedMenuItem::separator(app)?;
    let sep2 = PredefinedMenuItem::separator(app)?;
    Menu::with_items(
        app,
        &[
            &hide,
            &sep1,
            &zoom_in,
            &zoom_out,
            &zoom_reset,
            &sep2,
            &deck_aot,
            &open_app,
        ],
    )
}

/// Pop the deck's right-click context menu on the deck window, at the
/// cursor. Called by the deck's own `contextmenu` listener (never the app
/// window's — see `App.svelte`). Plain `fn`, not `async`: every other
/// command here that touches a window or the tray (`show_deck`, `tray_set_
/// language`, `reload_deck_always_on_top`, …) is sync for the same reason —
/// the native menu/window APIs it calls are main-thread-only, and Tauri's
/// `Menu::popup` already hops there itself.
#[tauri::command]
fn show_deck_context_menu(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    tray: tauri::State<'_, TrayHandles>,
) -> Result<(), String> {
    let window = app
        .get_webview_window(DECK_WINDOW)
        .ok_or_else(|| "deck window missing".to_string())?;
    let lang = tray
        .0
        .lock()
        .unwrap()
        .as_ref()
        .map(TrayMenuItems::current_lang)
        .unwrap_or_else(|| "en".to_string());
    let always_on_top = *state.deck_always_on_top.lock().unwrap();
    let menu = build_deck_context_menu(&app, &lang, always_on_top).map_err(|e| e.to_string())?;
    let webview: &tauri::Webview<tauri::Wry> = window.as_ref();
    menu.popup(webview.window()).map_err(|e| e.to_string())
}

/// Whether a left click on the tray icon opens its menu. Left click with the
/// menu disabled and no click handler did NOTHING on macOS, where the menu bar
/// has no right-click habit to fall back on.
fn tray_menu_on_left_click() -> bool {
    cfg!(target_os = "macos")
}

/// Build the tray icon. `deck_always_on_top` and `deck_visible` are the
/// values `run()` already resolved at startup (config text + window state) —
/// handed in rather than re-read here, so the tray's initial checkbox and
/// `toggle_deck` label always agree with what actually got applied to the
/// windows.
fn build_tray(app: &tauri::App, deck_always_on_top: bool, deck_visible: bool) -> tauri::Result<()> {
    use tauri_plugin_autostart::ManagerExt;
    // Built with the English (default-language) labels; tray_set_language
    // retitles everything the moment the WebView learns the configured language.
    let l = tray_labels("en");
    let show_app = MenuItem::with_id(app, MENU_ID_SHOW_APP, l[0], true, None::<&str>)?;
    let toggle_deck = MenuItem::with_id(
        app,
        MENU_ID_TOGGLE_DECK,
        toggle_deck_label("en", deck_visible),
        true,
        None::<&str>,
    )?;
    let deck_aot = CheckMenuItem::with_id(
        app,
        MENU_ID_DECK_AOT,
        l[3],
        true,
        deck_always_on_top,
        None::<&str>,
    )?;
    let autostart = CheckMenuItem::with_id(
        app,
        MENU_ID_AUTOSTART,
        l[4],
        true,
        app.autolaunch().is_enabled().unwrap_or(false),
        None::<&str>,
    )?;
    let reconnect = MenuItem::with_id(app, MENU_ID_RECONNECT, l[5], true, None::<&str>)?;
    let check_update = MenuItem::with_id(app, MENU_ID_CHECK_UPDATE, l[6], true, None::<&str>)?;
    let quit = MenuItem::with_id(app, MENU_ID_QUIT, l[7], true, None::<&str>)?;
    let menu = Menu::with_items(
        app,
        &[
            &show_app,
            &toggle_deck,
            &deck_aot,
            &autostart,
            &reconnect,
            &check_update,
            &quit,
        ],
    )?;
    let autostart_cb = autostart.clone();
    let deck_aot_cb = deck_aot.clone();
    if let Some(handles) = app.try_state::<TrayHandles>() {
        *handles.0.lock().unwrap() = Some(TrayMenuItems {
            show_app: show_app.clone(),
            toggle_deck: toggle_deck.clone(),
            deck_aot: deck_aot.clone(),
            autostart: autostart.clone(),
            reconnect: reconnect.clone(),
            check_update: check_update.clone(),
            quit: quit.clone(),
            lang: Mutex::new("en".to_string()),
            blocked: Mutex::new(None),
        });
    }

    let mut builder = TrayIconBuilder::with_id(TRAY_ID)
        .tooltip(build_channel::display_name())
        .menu(&menu)
        // macOS convention: a menu-bar extra opens its menu on a left click
        // too. Elsewhere the right click opens the menu and a left click
        // toggles the deck (see `on_tray_icon_event`).
        .show_menu_on_left_click(tray_menu_on_left_click())
        .on_tray_icon_event(|tray, event| {
            use tauri::tray::{MouseButton, MouseButtonState, TrayIconEvent};
            if tray_menu_on_left_click() {
                return;
            }
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                toggle_deck_window(tray.app_handle());
            }
        })
        .on_menu_event(move |app, event| match event.id.as_ref() {
            MENU_ID_SHOW_APP => {
                // Also the deck context menu's "Open Herdeck" item
                // (`build_deck_context_menu` gives it this SAME id, on
                // purpose, so it runs through this one arm instead of a copy).
                //
                // Mirrors what the pre-roles `normal` mode did: opening the app
                // dismisses a re-onboarding card the user never went through with.
                let _ = app.emit_to(APP_WINDOW, "open-settings", ());
                show_role_window(app, APP_WINDOW);
            }
            MENU_ID_TOGGLE_DECK => toggle_deck_window(app),
            MENU_ID_DECK_AOT => {
                // Also the deck context menu's "Deck always on top" checkbox,
                // for the same reason as "show_app" above.
                let Some(state) = app.try_state::<AppState>() else {
                    return;
                };
                let current = *state.deck_always_on_top.lock().unwrap();
                let target = !current;
                match persist_deck_always_on_top(&state, target) {
                    Ok(()) => {
                        if let Some(w) = app.get_webview_window(DECK_WINDOW) {
                            let _ = w.set_always_on_top(target);
                        }
                        *state.deck_always_on_top.lock().unwrap() = target;
                        let _ = deck_aot_cb.set_checked(target);
                    }
                    Err(e) => {
                        eprintln!("deck always-on-top: persist failed, not applying: {e}");
                        // Nothing changed on disk or on the window — force the
                        // checkbox back to that same unchanged value, in case
                        // the native widget already flipped itself on click.
                        let _ = deck_aot_cb.set_checked(current);
                    }
                }
            }
            MENU_ID_RECONNECT => {
                // Onboarding lives on the app surface, so the re-onboard event
                // and the window that has to be looking at it are the same one.
                let _ = app.emit_to(APP_WINDOW, "reonboard", ());
                show_role_window(app, APP_WINDOW);
            }
            MENU_ID_CHECK_UPDATE => {
                // The actual check runs in the WebView (`updateClient.ts`, via
                // `update_check`/`update_install`) — this just asks App.svelte
                // to run it and brings the app window forward so the result
                // (available / up to date / failed) is never rendered into a
                // hidden window. Unlike the silent mount-time check, a
                // user-requested one must report every outcome, including
                // failure — see App.svelte's "check-for-updates" listener.
                let _ = app.emit_to(APP_WINDOW, "check-for-updates", ());
                show_role_window(app, APP_WINDOW);
            }
            MENU_ID_AUTOSTART => {
                let mgr = app.autolaunch();
                let now = mgr.is_enabled().unwrap_or(false);
                let res = if now { mgr.disable() } else { mgr.enable() };
                if let Err(e) = res {
                    eprintln!("autostart toggle failed: {e}");
                }
                let _ = autostart_cb.set_checked(mgr.is_enabled().unwrap_or(false));
            }
            MENU_ID_QUIT => app.exit(0),
            // The deck's own right-click context menu (`build_deck_context_menu`).
            // MENU_ID_HIDE_DECK is new; MENU_ID_DECK_AOT and MENU_ID_SHOW_APP
            // above already cover the context menu's checkbox and open-app
            // items because those items are built with the SAME ids.
            MENU_ID_HIDE_DECK => hide_role_window(app, DECK_WINDOW),
            // Catch-all: anything left is either one of the three zoom ids
            // (routed through the SAME mapper `zoom_command_for_menu_id`
            // tests pin down) or truly unknown, in which case it is a no-op —
            // one arm instead of listing the zoom ids again here too.
            id => {
                if let Some(cmd) = zoom_command_for_menu_id(id) {
                    let _ = app.emit_to(DECK_WINDOW, FLOATING_ZOOM_EVENT, cmd);
                }
            }
        });

    // Reuse the embedded app icon for the tray (skip gracefully if absent).
    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }
    builder.build(app)?;
    Ok(())
}

/// Start the sidecar supervisor (or record the external discovery). `config_path`
/// is exported as `HERDECK_CONFIG` so the spawned sidecar reads the SAME config
/// file Rust resolved the deck preferences from (mooting the sidecar's CWD-relative
/// branch — important for the frozen `.app`, where CWD is nondeterministic).
fn start_sidecar(app: &tauri::App, discovery: Arc<Mutex<Option<Discovery>>>, config_path: &Path) {
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
fn start_supervisor(handle: tauri::AppHandle, spec: CommandSpec, sup: Supervisor) {
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

/// Tauri entry point.
pub fn run() {
    start_app_log();
    let discovery: Arc<Mutex<Option<Discovery>>> = Arc::new(Mutex::new(None));

    // Resolve config.toml with the sidecar's existence-check order and read it
    // ONCE. Both answers below can fall back to the legacy window_mode the fixed
    // roles replaced, each only where its own newer source is absent —
    // `deck_always_on_top` for the flag, `window-state.json` for the visibility.
    // That is the design doc's migration table.
    //
    // The two halves stop consulting it at different times. Visibility: after
    // ONE launch, because exit always writes `window-state.json`. The flag: only
    // once the user deliberately sets it, from the tray or the editor — nothing
    // writes it automatically, so until then the legacy key decides every
    // launch. `configClient.ts`'s `deckAlwaysOnTop` mirrors that fallback so the
    // editor checkbox agrees with the deck in the meantime.
    let config_path = default_config_path();
    let config_text = std::fs::read_to_string(&config_path).unwrap_or_default();
    let deck_always_on_top = deck_prefs::resolve_deck_always_on_top(&config_text);
    let startup = window_state::startup_state(
        window_state::load(&window_state::state_dir()),
        deck_prefs::parse_legacy_window_mode(&config_text).as_deref(),
    );

    // Clones for the setup closure and the supervisor.
    let setup_discovery = discovery.clone();
    let setup_config_path = config_path.clone();

    let state = AppState {
        discovery,
        window_state: Arc::new(Mutex::new(startup)),
        deck_always_on_top: Arc::new(Mutex::new(deck_always_on_top)),
        notify_cursor: Arc::new(Mutex::new(NotifyCursor::default())),
        notify_permission: Arc::new(AtomicBool::new(false)),
        rediscover_last: Arc::new(Mutex::new(None)),
        attached_from_runtime_json: Arc::new(AtomicBool::new(false)),
        reattach_eligible: Arc::new(AtomicBool::new(false)),
        supervisor: Arc::new(Mutex::new(Supervisor::default())),
        spawn_spec: Arc::new(Mutex::new(None)),
        switched_from_spawn: Arc::new(AtomicBool::new(false)),
        attach_loss: Arc::new(Mutex::new(AttachLoss::default())),
        quitting: Arc::new(AtomicBool::new(false)),
    };
    let notify_permission = state.notify_permission.clone();

    tauri::Builder::default()
        .manage(state)
        .manage(TrayHandles::default())
        .register_asynchronous_uri_scheme_protocol(IMAGE_SCHEME, |ctx, request, responder| {
            let app = ctx.app_handle().clone();
            let path = request.uri().path().to_string();
            // Never block the WebView's scheme thread on loopback I/O.
            tauri::async_runtime::spawn_blocking(move || {
                responder.respond(serve_image_request(&app, &path));
            });
        })
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            None,
        ))
        .invoke_handler(tauri::generate_handler![
            get_discovery,
            update_check,
            update_install,
            check_health,
            deck_state,
            deck_tile,
            deck_panel,
            deck_press,
            config_read,
            config_validate,
            config_write,
            config_set_active,
            config_secret_set,
            config_secret_clear,
            setup_status,
            setup_connect,
            reload_hotkey,
            reload_deck_always_on_top,
            show_deck,
            hide_deck,
            show_app,
            deck_visible,
            tray_set_language,
            show_deck_context_menu,
            notification_sounds,
            test_notification,
            notification_permission
        ])
        .setup(move |app| {
            // Mark banner duty as claimed up-front (the desktop plugin's
            // permission model is synchronous and defaults to granted on
            // macOS); the runtime stops its osascript fallback as soon as the
            // first /state poll with the claim header arrives.
            {
                use tauri_plugin_notification::{NotificationExt, PermissionState};
                let granted = app
                    .notification()
                    .request_permission()
                    .map(|state| state == PermissionState::Granted)
                    .unwrap_or(false);
                notify_permission.store(granted, Ordering::Relaxed);
                if granted {
                    prevent_notification_pump_app_nap();
                }
            }
            // The notification pump runs for the whole app lifetime, detached
            // from WebView visibility (deck windows may hide into the tray).
            // Banner clicks reveal the deck and banners show while Herdeck is
            // frontmost (see `banner_clicks`) — from the very first post on.
            #[cfg(target_os = "macos")]
            banner_clicks::install_at_startup(app.handle());
            start_notify_pump(app.handle().clone());
            // NEITHER window is declared in tauri.conf.json: both are built here
            // so both get an initialization script, which stamps the window's
            // role on `<html>` before its first paint. The frontend routes its
            // surface off that attribute, so injecting it any later would show a
            // flash of the wrong styling (FOUC).
            //
            // The two shapes are fixed and never swap. `main` is the borderless
            // deck overlay, `config` the decorated settings window — exactly the
            // properties each already had, minus the mode that shuffled them.
            let app_handle = app.handle().clone();
            let display_name = build_channel::display_name();
            // The borderless card carries no CSS drop shadow (it fills the window
            // exactly, so one would only pool in the corner notches). macOS
            // derives a transparent window's shadow from the drawn content's
            // alpha, i.e. from the rounded card itself — so let it.
            let deck_window =
                WebviewWindowBuilder::new(&app_handle, DECK_WINDOW, WebviewUrl::default())
                    .title(&display_name)
                    .shadow(true)
                    .initialization_script(window_role_script("deck"))
                    .decorations(false)
                    .transparent(true)
                    .resizable(false)
                    .inner_size(328.0, 300.0)
                    .skip_taskbar(true)
                    .visible(false)
                    .build()?;

            // Dev builds carry the channel + revision in the title so two
            // installs are never confused for one another.
            let app_title = if build_channel::is_dev() {
                format!("{display_name} - Settings")
            } else {
                "Herdeck Settings".to_string()
            };
            let app_window =
                WebviewWindowBuilder::new(&app_handle, APP_WINDOW, WebviewUrl::default())
                    .title(&app_title)
                    .shadow(true)
                    .initialization_script(window_role_script("app"))
                    .decorations(true)
                    .transparent(false)
                    .resizable(true)
                    .inner_size(1180.0, 780.0)
                    .min_inner_size(680.0, 540.0)
                    .skip_taskbar(false)
                    .visible(false)
                    .build()?;

            // Not a creation-time property, unlike transparent/decorations: this
            // is the same call the tray makes, and it never needs a restart.
            let _ = deck_window.set_always_on_top(deck_always_on_top);
            place_deck(&deck_window, startup.deck_position);

            // Both are built hidden and opened per the remembered layout, so a
            // window that should stay closed never flashes on screen first.
            // `startup_state` guarantees at least one of these is true.
            if startup.deck_visible {
                let _ = deck_window.show();
            }
            if startup.app_visible {
                let _ = app_window.show();
                let _ = app_window.set_focus();
            }

            // Closing either window hides it: the tray brings it back, and the
            // app + sidecar keep running. Without this Tauri would DESTROY the
            // window and every later "show" would fail. CloseRequested is
            // window-close only — it does NOT fire for app.exit/app.restart, so
            // this never blocks quit or an updater restart.
            {
                let handle = app_handle.clone();
                let deck = deck_window.clone();
                deck_window.on_window_event(move |event| match event {
                    tauri::WindowEvent::CloseRequested { api, .. } => {
                        api.prevent_close();
                        hide_role_window(&handle, DECK_WINDOW);
                    }
                    // Memory only: a single drag emits hundreds of these. The
                    // write happens when the deck is hidden, and on exit.
                    //
                    // A position reported while the deck is hidden is not a user
                    // drag — some window managers emit one on hide — and must
                    // not overwrite the place the user actually left it.
                    tauri::WindowEvent::Moved(position) => {
                        if deck.is_visible().unwrap_or(false) {
                            remember_deck_position(
                                &handle,
                                placement_space_position(&deck, *position),
                            );
                        }
                    }
                    _ => {}
                });
            }
            {
                let handle = app_handle.clone();
                app_window.on_window_event(move |event| {
                    if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                        api.prevent_close();
                        hide_role_window(&handle, APP_WINDOW);
                    }
                });
            }

            build_tray(app, deck_always_on_top, startup.deck_visible)?;
            start_sidecar(app, setup_discovery, &setup_config_path);
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build herdeck desktop app")
        .run(move |app_handle, event| {
            // Dock icon click / relaunch from Finder while already running:
            // with both windows hidden it would otherwise do nothing at all.
            #[cfg(target_os = "macos")]
            if let tauri::RunEvent::Reopen { .. } = event {
                reveal_deck(app_handle);
                return;
            }
            if let tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit = event {
                // The last chance to save a deck position that was dragged and
                // never hidden — `Moved` deliberately does not touch the disk.
                persist_window_state(app_handle);
                // Tear the supervised sidecar down so it never outlives the shell:
                // closing its stdin runs its clean shutdown (D200 released,
                // files removed); SIGKILL only after SIDECAR_STOP_GRACE. A crash
                // skips this handler, but the kernel closes the same pipe then.
                let state = app_handle.state::<AppState>();
                let sup = {
                    // Same lock order as fall_back_to_spawn (discovery, then
                    // supervisor): a fallback racing the quit either sees
                    // `quitting` or its fresh supervisor is the one stopped here.
                    let _discovery = state.discovery.lock().unwrap();
                    state.quitting.store(true, Ordering::SeqCst);
                    let sup = state.supervisor.lock().unwrap().clone();
                    sup.stop.store(true, Ordering::SeqCst);
                    sup
                };
                // Take the child out first so the slot's lock is not held
                // through the stop grace.
                let taken = sup.child.lock().unwrap().take();
                if let Some(mut c) = taken {
                    sidecar::stop_child(&mut c, sidecar::SIDECAR_STOP_GRACE);
                }
            }
        });
}
