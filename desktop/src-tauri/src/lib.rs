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

mod agent_card;
pub mod app_log;
pub mod banners;
pub mod build_channel;
pub mod deck_prefs;
pub mod hotkey;
pub mod http;
mod notify_pump;
pub mod runtime_service;
pub mod sidecar;
mod sync_util;
mod window_roles;
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
    Emitter, Manager, WebviewUrl, WebviewWindowBuilder,
};
use tauri_plugin_updater::UpdaterExt;

use notify_pump::{discovery_key, NotifyCursor, PendingNotification};
use sidecar::{supervise, CommandSpec, Discovery, SupervisorConfig};
use sync_util::LockExt;
use window_state::WindowState;
use window_roles::{
    deck_is_visible, hide_role_window, persist_deck_always_on_top, persist_window_state,
    place_deck, placement_space_position, remember_deck_position, reveal_deck,
    show_role_window, toggle_deck_window, window_role_script, APP_WINDOW, DECK_WINDOW,
};

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

/// Minimum interval between on-disk re-discovery attempts.
const REDISCOVER_MIN_INTERVAL: Duration = Duration::from_secs(5);

/// How often a shell running on its own spawned sidecar re-reads runtime.json
/// for a healthy launchd runtime to hand over to (one small file read; a
/// `/health` probe only when the file names a runtime other than ours).
const REATTACH_CHECK_INTERVAL: Duration = Duration::from_secs(12);

/// Default timeout for the Rust-side sidecar proxy calls.
const SIDECAR_TIMEOUT: Duration = Duration::from_secs(3);

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
/// carries a message the settings UI shows as is. The native post may block on
/// Notification Center (and, off macOS, on the notification plugin), so it runs
/// on the blocking pool — never on the main thread or an async worker.
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
    // A fresh identifier per press: a banner with the same identifier as one
    // still in Notification Center replaces it silently (no banner, no sound).
    static TEST_SEQ: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);
    let item = PendingNotification {
        id: "test".to_string(),
        generation: format!("test-{}", shell_gen()),
        seq: TEST_SEQ.fetch_add(1, Ordering::Relaxed),
        title: title.to_string(),
        body: body.to_string(),
        sound,
        icon: None,
        created_at_ms: None,
        meta: banners::BannerMeta::default(),
        kind: None,
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

/// The shell side of the notify pump: `AppState` (permission, discovery,
/// re-discovery), the loopback HTTP calls, and the native poster.
struct AppNotify(tauri::AppHandle);

impl notify_pump::NotifyHost for AppNotify {
    fn permission(&self) -> bool {
        self.0.state::<AppState>().notify_permission.load(Ordering::Relaxed)
    }

    fn discovery(&self) -> Option<Discovery> {
        self.0.state::<AppState>().discovery.lock_or_recover().clone()
    }

    fn rediscover(&self) -> Option<Discovery> {
        rediscover_runtime(&self.0)
    }

    fn note_runtime_ok(&self) {
        note_runtime_ok(&self.0.state::<AppState>());
    }
}

impl notify_pump::NotifyTransport for AppNotify {
    fn poll(&self, d: &Discovery, cursor: &NotifyCursor) -> Result<(u16, String), String> {
        http::fetch_notifications_status(
            &d.host,
            d.port,
            &d.token,
            notify_pump::NOTIFY_POLL_TIMEOUT,
            cursor.generation.as_deref(),
            cursor.seq,
            &shell_gen(),
        )
    }

    fn ack(&self, d: &Discovery, generation: &str, seq: u64) -> Result<u16, String> {
        http::ack_notification(&d.host, d.port, &d.token, SIDECAR_TIMEOUT, generation, seq)
    }

    fn fallback(
        &self,
        d: &Discovery,
        generation: &str,
        seq: u64,
        error: &str,
    ) -> Result<u16, String> {
        http::fallback_notification(
            &d.host,
            d.port,
            &d.token,
            SIDECAR_TIMEOUT,
            generation,
            seq,
            &shell_gen(),
            error,
        )
    }
}

impl notify_pump::BannerPoster for AppNotify {
    fn post(&self, item: &PendingNotification) -> Result<(), String> {
        post_native_notification(&self.0, item)
    }

    fn withdraw(&self, agent: &banners::AgentRef) {
        withdraw_banners(agent);
    }
}

/// Generation-aware long-poll notification pump (see `notify_pump`). The
/// blocking request itself keeps banner duty claimed and wakes immediately
/// when the runtime queues an event. Only a successfully shown banner is
/// acknowledged.
fn start_notify_pump(app: tauri::AppHandle) {
    let cursor = app.state::<AppState>().notify_cursor.clone();
    std::thread::spawn(move || {
        notify_pump::NotifyPump {
            host: AppNotify(app.clone()),
            transport: AppNotify(app.clone()),
            poster: AppNotify(app),
            clock: notify_pump::SystemClock,
            cursor,
        }
        .run()
    });
}

/// Remove this agent's banners from Notification Center (only the ones this
/// shell delivered and still remembers — the book is bounded).
fn withdraw_banners(agent: &banners::AgentRef) {
    let identifiers = BANNER_BOOK
        .lock()
        .map(|mut book| book.take_agent(agent))
        .unwrap_or_default();
    #[cfg(target_os = "macos")]
    banner_post::remove_delivered(&identifiers);
    #[cfg(not(target_os = "macos"))]
    let _ = identifiers; // the plugin's banners cannot be withdrawn
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

/// Banners this shell delivered, by Notification Center identifier (bounded).
/// Read on the main thread when a banner is activated.
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
static BANNER_BOOK: Mutex<banners::BannerBook> = Mutex::new(banners::BannerBook::new());

/// The answer context of a delivered banner, if this shell posted it.
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
fn banner_context(identifier: &str) -> Option<banners::BannerContext> {
    BANNER_BOOK.lock().ok()?.get(identifier).cloned()
}

/// Post one banner, fire-and-forget, with its sound attached. On macOS it is
/// built directly as an `NSUserNotification` (objc2) so it carries OUR
/// identifier — the key the click handler and `BANNER_BOOK` use to know which
/// agent (and block episode) a banner belongs to — and, for an actionable
/// blocked alert, Approve/Deny buttons or an inline reply field.
/// `mac-notification-sys` still provides the bundle attribution
/// (`ensure_notification_application`) and the center delegate we proxy
/// (`banner_clicks`); its own `send` could do neither (a random UUID per
/// banner, and a blocking wait for any banner with buttons). Clicks are
/// observed by `banner_clicks`, never by a parked per-banner thread.
#[cfg(target_os = "macos")]
fn post_native_notification(
    app: &tauri::AppHandle,
    item: &PendingNotification,
) -> Result<(), String> {
    ensure_notification_application(app);
    // Before the post, so even the very first banner's click is seen.
    banner_clicks::ensure_installed(app);
    let sound = banner_sound_name(&item.sound, &sound_dirs()).unwrap_or_else(|err| {
        eprintln!("herdeck: {err}; posting id={} silently", item.id);
        None
    });
    let identifier = banners::banner_identifier(&item.generation, item.seq);
    if let Some(agent) = item.meta.agent.clone() {
        // Recorded first: a click racing the delivery must find its context.
        if let Ok(mut book) = BANNER_BOOK.lock() {
            book.record(
                identifier.clone(),
                banners::BannerContext {
                    agent,
                    episode: item.meta.episode.clone(),
                    sig: item.meta.sig.clone(),
                    actions: item.meta.actions.clone(),
                },
            );
        }
    }
    let image = item.icon.as_deref().and_then(banner_image_path);
    banner_post::deliver(&identifier, item, sound.as_deref(), image).inspect_err(|_| {
        if let Ok(mut book) = BANNER_BOOK.lock() {
            book.forget(&identifier);
        }
    })
}

/// Building and delivering one `NSUserNotification` (the legacy API
/// `mac-notification-sys` uses too — no permission prompt, works unsigned).
#[cfg(target_os = "macos")]
#[allow(deprecated)]
mod banner_post {
    use objc2::rc::{Allocated, Retained};
    use objc2::runtime::{AnyClass, AnyObject};
    use objc2::{msg_send, ClassType};
    use objc2_foundation::{
        ns_string, NSArray, NSNumber, NSString, NSUserNotification, NSUserNotificationAction,
        NSUserNotificationCenter,
    };

    use super::PendingNotification;

    fn image(path: &str) -> Option<Retained<AnyObject>> {
        let class = AnyClass::get(c"NSImage")?;
        // SAFETY: -[NSImage initWithContentsOfFile:] takes an NSString and
        // returns nil for an unreadable file.
        unsafe {
            let alloc: Allocated<AnyObject> = msg_send![class, alloc];
            msg_send![alloc, initWithContentsOfFile: &*NSString::from_str(path)]
        }
    }

    fn default_center() -> Option<Retained<NSUserNotificationCenter>> {
        // Nil when the process has no bundle identity (an unbundled dev binary
        // without the Terminal attribution).
        unsafe { msg_send![NSUserNotificationCenter::class(), defaultUserNotificationCenter] }
    }

    pub fn deliver(
        identifier: &str,
        item: &PendingNotification,
        sound: Option<&str>,
        image_path: Option<&str>,
    ) -> Result<(), String> {
        let center = default_center().ok_or("no notification center for this process")?;
        let banner = NSUserNotification::new();
        banner.setIdentifier(Some(&NSString::from_str(identifier)));
        banner.setTitle(Some(&NSString::from_str(&item.title)));
        banner.setInformativeText(Some(&NSString::from_str(&item.body)));
        if let Some(sound) = sound {
            banner.setSoundName(Some(&NSString::from_str(sound)));
        }
        let meta = &item.meta;
        if let Some((first, rest)) = meta.actions.split_first() {
            // Approve on the action button, Deny in its drop-down
            // (additionalActions, macOS 10.10+). `_showsButtons` (the private
            // key mac-notification-sys sets for its own drop-down) keeps the
            // buttons visible on a banner-style alert.
            banner.setHasActionButton(true);
            banner.setActionButtonTitle(&NSString::from_str(&first.label));
            let extra: Vec<Retained<NSUserNotificationAction>> = rest
                .iter()
                .map(|action| {
                    NSUserNotificationAction::actionWithIdentifier_title(
                        Some(&NSString::from_str(&action.id)),
                        Some(&NSString::from_str(&action.label)),
                    )
                })
                .collect();
            if !extra.is_empty() {
                banner.setAdditionalActions(Some(&NSArray::from_retained_slice(&extra)));
            }
            // SAFETY: KVC on a key NSUserNotification has on every macOS that
            // still ships the legacy API (mac-notification-sys relies on it).
            unsafe {
                let _: () = msg_send![&*banner, setValue: &*NSNumber::new_bool(true), forKey: ns_string!("_showsButtons")];
            }
        } else if let Some(placeholder) = meta.reply.as_deref() {
            banner.setHasReplyButton(true);
            banner.setResponsePlaceholder(Some(&NSString::from_str(placeholder)));
        } else {
            banner.setHasActionButton(false);
        }
        if let Some(image) = image_path.and_then(image) {
            // The project mark twice: `_identityImage` swaps the left-hand app
            // icon through a private key that newer macOS may ignore;
            // `contentImage` (public API) shows it on the right either way.
            // SAFETY: the same keys/selectors mac-notification-sys uses.
            unsafe {
                let _: () = msg_send![&*banner, setValue: &*image, forKey: ns_string!("_identityImage")];
                let _: () = msg_send![&*banner, setValue: &*NSNumber::new_bool(false), forKey: ns_string!("_identityImageHasBorder")];
                let _: () = msg_send![&*banner, setContentImage: &*image];
            }
        }
        center.deliverNotification(&banner);
        Ok(())
    }

    /// Remove every delivered banner whose identifier is in `identifiers`.
    /// `deliveredNotifications` is a synchronous XPC round trip: called only
    /// for a withdraw item, on the pump thread, never on a timer.
    pub fn remove_delivered(identifiers: &[String]) {
        if identifiers.is_empty() {
            return;
        }
        let Some(center) = default_center() else {
            return;
        };
        for banner in center.deliveredNotifications().iter() {
            let ours = banner
                .identifier()
                .is_some_and(|id| identifiers.iter().any(|want| *want == id.to_string()));
            if ours {
                center.removeDeliveredNotification(&banner);
            }
        }
    }
}

/// Carry out what a banner activation asked for (main thread; the HTTP calls
/// run on their own thread). A successful answer does NOT bring the deck
/// forward — answering from the banner is the point; a stale or failed one
/// opens that agent's drill so the user sees the prompt as it is now.
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
fn handle_banner_intent(app: &tauri::AppHandle, intent: banners::BannerIntent) {
    use banners::BannerIntent;
    match intent {
        BannerIntent::Ignore => {}
        BannerIntent::Reveal => reveal_deck(app),
        BannerIntent::Drill(agent) => {
            reveal_deck(app);
            let app = app.clone();
            std::thread::spawn(move || open_agent_drill(&app, &agent));
        }
        BannerIntent::Answer { ref agent, .. } | BannerIntent::Reply { ref agent, .. } => {
            let agent = agent.clone();
            let Some(body) = banners::answer_body(&intent) else {
                return;
            };
            let app = app.clone();
            std::thread::spawn(move || {
                let discovery = app.state::<AppState>().discovery.lock().unwrap().clone();
                let result = match discovery {
                    Some(d) => http::post_agent_action(
                        &d.host,
                        d.port,
                        &d.token,
                        SIDECAR_TIMEOUT,
                        http::AGENT_ANSWER_PATH,
                        &body,
                    ),
                    None => Err("runtime not discovered".to_string()),
                };
                eprintln!(
                    "herdeck: banner answer agent={}:{} result={result:?}",
                    agent.server_id, agent.pane_id
                );
                if banners::answer_needs_drill(&result) {
                    reveal_deck(&app);
                    open_agent_drill(&app, &agent);
                }
            });
        }
    }
}

/// `POST /agents/drill` for one agent (blocking; call off the main thread).
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
fn open_agent_drill(app: &tauri::AppHandle, agent: &banners::AgentRef) {
    let Some(d) = app.state::<AppState>().discovery.lock().unwrap().clone() else {
        return;
    };
    let code = http::post_agent_action(
        &d.host,
        d.port,
        &d.token,
        SIDECAR_TIMEOUT,
        http::AGENT_DRILL_PATH,
        &banners::drill_body(agent),
    );
    if code != Ok(204) {
        eprintln!(
            "herdeck: banner drill agent={}:{} result={code:?}",
            agent.server_id, agent.pane_id
        );
    }
}

/// Whether to present a banner while Herdeck is frontmost: the wrapped
/// delegate's answer when it has one, else yes (AppKit's own default is no).
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
fn should_present_banner(inner_answer: Option<bool>) -> bool {
    inner_answer.unwrap_or(true)
}

/// Banner clicks → reveal the deck, with no per-banner thread and no polling.
///
/// `NSUserNotificationCenter` has exactly one delegate. `mac-notification-sys`
/// creates it (we still call its `setupDelegate`, and its handler removes an
/// activated banner from Notification Center); our banners no longer go
/// through the crate's `send`, so nothing waits on its `didDeliverNotification:`.
/// We put a forwarding proxy in front of it: every delegate message is passed
/// on unchanged, and `didActivateNotification:` also carries out the banner's
/// intent (reveal / drill / answer / reply, see `banners::banner_intent`).
/// That is one object for the whole process, event driven, on the main thread
/// where AppKit delivers these callbacks anyway.
///
/// It also answers `shouldPresentNotification:` (the crate does not), so a
/// banner — and the sound it carries — still shows while Herdeck is frontmost.
///
/// `install_at_startup` makes the crate create its delegate (normally done
/// lazily inside its first send) and wraps it before any banner is posted, so
/// the first banner already goes through the proxy. `ensure_installed` before
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
                let identifier = notification.identifier().map(|id| id.to_string());
                let context = identifier.as_deref().and_then(super::banner_context);
                let additional = notification
                    .additionalActivationAction()
                    .and_then(|action| action.identifier())
                    .map(|id| id.to_string());
                let reply = notification.response().map(|text| text.string().to_string());
                let intent = super::banners::banner_intent(
                    notification.activationType().0,
                    context.as_ref(),
                    additional.as_deref(),
                    reply.as_deref(),
                );
                if let Some(app) = APP.get() {
                    super::handle_banner_intent(app, intent);
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
mod plan_tests;

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
            agent_card::agent_call,
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
            window_roles::reload_deck_always_on_top,
            window_roles::show_deck,
            window_roles::hide_deck,
            window_roles::show_app,
            window_roles::deck_visible,
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
