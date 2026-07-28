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

pub mod build_channel;
pub mod deck_prefs;
pub mod hotkey;
pub mod http;
pub mod sidecar;
pub mod window_state;

use std::env;
use std::path::{Path, PathBuf};
use std::process::Child;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tauri::menu::{CheckMenuItem, Menu, MenuItem};
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
}

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
/// or is unreachable.
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
        serde_json::from_str::<serde_json::Value>(&body)
            .map_err(|e| format!("invalid /health JSON from sidecar: {e}"))
    })
    .await
}

/// Proxy `GET /state` (token injected Rust-side) → its JSON. This is the deck's
/// poll endpoint; the WebView never sees the token.
#[tauri::command]
async fn deck_state(state: tauri::State<'_, AppState>) -> Result<serde_json::Value, String> {
    let d = current_discovery(&state)?;
    run_blocking(move || {
        let body = http::fetch_state(&d.host, d.port, &d.token, SIDECAR_TIMEOUT)?;
        serde_json::from_str::<serde_json::Value>(&body)
            .map_err(|e| format!("invalid /state JSON from sidecar: {e}"))
    })
    .await
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
    External(Discovery),
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

fn resolve_automatic_plan<F>(
    channel: &str,
    resource_dir: Option<&Path>,
    repo_root: &Path,
    runtime_discovery: Option<Discovery>,
    healthy: F,
) -> SidecarPlan
where
    F: Fn(&Discovery) -> bool,
{
    if build_channel::shared_runtime_attach_enabled_for(channel) {
        if let Some(discovery) = sidecar::decide_runtime_attach(runtime_discovery, healthy) {
            return SidecarPlan::External(discovery);
        }
    }
    SidecarPlan::Spawn(sidecar::choose_spawn(resource_dir, repo_root))
}

/// Decide how to obtain the sidecar. If `HERDECK_DECKAPP_URL` +
/// `HERDECK_DECKAPP_TOKEN` are set, trust that externally-started sidecar (handy
/// for manual `tauri dev` smoke without a `.venv`); otherwise spawn the dev venv.
fn resolve_plan(resource_dir: Option<PathBuf>) -> SidecarPlan {
    if let (Ok(url), Ok(token)) = (
        env::var("HERDECK_DECKAPP_URL"),
        env::var("HERDECK_DECKAPP_TOKEN"),
    ) {
        if !url.is_empty() && !token.is_empty() {
            let (host, port) = parse_host_port(&url);
            let source =
                env::var("HERDECK_DECKAPP_SOURCE").unwrap_or_else(|_| "external".to_string());
            return SidecarPlan::External(Discovery {
                url,
                host,
                port,
                token,
                source,
            });
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

#[cfg(test)]
mod plan_tests {
    use super::*;

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
    fn dev_plan_spawns_instead_of_attaching_a_healthy_stable_runtime() {
        let plan = resolve_automatic_plan(
            build_channel::DEV_CHANNEL,
            None,
            Path::new("/repo"),
            Some(stable_runtime()),
            |_| true,
        );

        match plan {
            SidecarPlan::Spawn(spec) => {
                assert!(spec.program.ends_with("/.venv/bin/python"));
            }
            SidecarPlan::External(_) => panic!("dev build attached the stable runtime"),
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
        // autostart + reconnect + quit — `TrayMenuItems::retitle` indexes
        // every one of these, so the array length must match exactly.
        assert_eq!(en.len(), 7);
        // A label accidentally left English in the cs array (or vice versa)
        // is exactly the defect this test exists to catch.
        for (i, (e, c)) in en.iter().zip(cs.iter()).enumerate() {
            assert_ne!(e, c, "tray_labels[{i}] is identical in en and cs");
        }
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
/// rather than `WindowState` — both callers need "is it visible right now",
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

/// (Re)register the deck-toggle global shortcut from the sidecar's `/config`.
/// Best-effort: any failure is logged and leaves the deck usable without a hotkey.
fn register_toggle_hotkey(app: &tauri::AppHandle, d: &Discovery) {
    use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};

    let gs = app.global_shortcut();
    let _ = gs.unregister_all();

    let body = match http::http_get(
        &d.host,
        d.port,
        &format!("/config?token={}", d.token),
        SIDECAR_TIMEOUT,
    ) {
        Ok(b) => b,
        Err(e) => {
            eprintln!("hotkey: /config fetch failed: {e}");
            return;
        }
    };
    let cfg: serde_json::Value = match serde_json::from_str(&body) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("hotkey: invalid /config JSON: {e}");
            return;
        }
    };
    let accel = match hotkey::toggle_deck_accelerator(&cfg) {
        Some(a) => a,
        None => return, // explicitly disabled
    };

    let app_for_cb = app.clone();
    let handler = move |_app: &tauri::AppHandle, _sc: &tauri_plugin_global_shortcut::Shortcut, event: tauri_plugin_global_shortcut::ShortcutEvent| {
        if event.state == ShortcutState::Pressed {
            toggle_deck_window(&app_for_cb);
        }
    };
    if let Err(e) = gs.on_shortcut(accel.as_str(), handler) {
        eprintln!("hotkey: register '{accel}' failed: {e}");
        if accel != hotkey::DEFAULT_TOGGLE_DECK {
            let app_for_fb = app.clone();
            let _ = gs.on_shortcut(
                hotkey::DEFAULT_TOGGLE_DECK,
                move |_app: &tauri::AppHandle, _sc: &tauri_plugin_global_shortcut::Shortcut, event: tauri_plugin_global_shortcut::ShortcutEvent| {
                    if event.state == ShortcutState::Pressed {
                        toggle_deck_window(&app_for_fb);
                    }
                },
            );
        }
    }
}

/// Re-read `/config` and re-register the deck-toggle hotkey (the editor calls
/// this after a successful config write so a changed accelerator takes effect).
#[tauri::command]
async fn reload_hotkey(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
) -> Result<(), String> {
    let d = current_discovery(&state)?;
    run_blocking(move || {
        register_toggle_hotkey(&app, &d);
        Ok(())
    })
    .await
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
fn tray_labels(lang: &str) -> [&'static str; 7] {
    match lang {
        "cs" => [
            "Otevřít Herdeck",
            "Zobrazit deck",
            "Skrýt deck",
            "Deck vždy navrchu",
            "Spouštět po přihlášení",
            "Změnit připojení…",
            "Ukončit",
        ],
        _ => [
            "Open Herdeck",
            "Show deck",
            "Hide deck",
            "Deck always on top",
            "Start at login",
            "Change connection…",
            "Quit",
        ],
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
    quit: MenuItem<tauri::Wry>,
    /// The language `retitle` was last called with. Needed so a
    /// visibility-only refresh of `toggle_deck` (`sync_toggle_deck_label`,
    /// run from every path that shows or hides the deck) can pick the right
    /// string without its caller having to track the language too.
    lang: Mutex<String>,
}

impl TrayMenuItems {
    fn retitle(&self, lang: &str, deck_visible: bool) {
        let l = tray_labels(lang);
        let _ = self.show_app.set_text(l[0]);
        let _ = self.toggle_deck.set_text(toggle_deck_label(lang, deck_visible));
        let _ = self.deck_aot.set_text(l[3]);
        let _ = self.autostart.set_text(l[4]);
        let _ = self.reconnect.set_text(l[5]);
        let _ = self.quit.set_text(l[6]);
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
}

/// Retitle the native tray menu for `lang` ("en"/"cs"). Called by the WebView
/// whenever the deck-reported language changes; unknown values fall back to en.
#[tauri::command]
fn tray_set_language(app: tauri::AppHandle, lang: String, handles: tauri::State<'_, TrayHandles>) {
    let deck_visible = app
        .get_webview_window(DECK_WINDOW)
        .and_then(|w| w.is_visible().ok())
        .unwrap_or(false);
    if let Some(items) = handles.0.lock().unwrap().as_ref() {
        items.retitle(&lang, deck_visible);
    }
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
    let show_app = MenuItem::with_id(app, "show_app", l[0], true, None::<&str>)?;
    let toggle_deck = MenuItem::with_id(
        app,
        "toggle_deck",
        toggle_deck_label("en", deck_visible),
        true,
        None::<&str>,
    )?;
    let deck_aot = CheckMenuItem::with_id(
        app,
        "deck_aot",
        l[3],
        true,
        deck_always_on_top,
        None::<&str>,
    )?;
    let autostart = CheckMenuItem::with_id(
        app,
        "autostart",
        l[4],
        true,
        app.autolaunch().is_enabled().unwrap_or(false),
        None::<&str>,
    )?;
    let reconnect = MenuItem::with_id(app, "reconnect", l[5], true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", l[6], true, None::<&str>)?;
    let menu = Menu::with_items(
        app,
        &[&show_app, &toggle_deck, &deck_aot, &autostart, &reconnect, &quit],
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
            quit: quit.clone(),
            lang: Mutex::new("en".to_string()),
        });
    }

    let mut builder = TrayIconBuilder::with_id("herdeck-tray")
        .tooltip(build_channel::display_name())
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(move |app, event| match event.id.as_ref() {
            "show_app" => {
                // Mirrors what the pre-roles `normal` mode did: opening the app
                // dismisses a re-onboarding card the user never went through with.
                let _ = app.emit_to(APP_WINDOW, "open-settings", ());
                show_role_window(app, APP_WINDOW);
            }
            "toggle_deck" => toggle_deck_window(app),
            "deck_aot" => {
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
            "reconnect" => {
                // Onboarding lives on the app surface, so the re-onboard event
                // and the window that has to be looking at it are the same one.
                let _ = app.emit_to(APP_WINDOW, "reonboard", ());
                show_role_window(app, APP_WINDOW);
            }
            "autostart" => {
                let mgr = app.autolaunch();
                let now = mgr.is_enabled().unwrap_or(false);
                let res = if now { mgr.disable() } else { mgr.enable() };
                if let Err(e) = res {
                    eprintln!("autostart toggle failed: {e}");
                }
                let _ = autostart_cb.set_checked(mgr.is_enabled().unwrap_or(false));
            }
            "quit" => app.exit(0),
            _ => {}
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
fn start_sidecar(
    app: &tauri::App,
    discovery: Arc<Mutex<Option<Discovery>>>,
    child: Arc<Mutex<Option<Child>>>,
    stop: Arc<AtomicBool>,
    config_path: &Path,
) {
    let primary_resource_dir = app.path().resource_dir().ok();
    let executable = env::current_exe().ok();
    let resource_dir = sidecar::resolve_resource_dir(
        primary_resource_dir.as_deref(),
        executable.as_deref(),
    );
    match resolve_plan(resource_dir) {
        SidecarPlan::External(d) => {
            let view = DiscoveryView::from(&d);
            register_toggle_hotkey(app.handle(), &d);
            *discovery.lock().unwrap() = Some(d);
            let _ = app.handle().emit("discovery", view); // token-free
        }
        SidecarPlan::Spawn(mut spec) => {
            eprintln!("herdeck sidecar: spawning {}", spec.program);
            spec.envs.push((
                "HERDECK_CONFIG".to_string(),
                config_path.to_string_lossy().into_owned(),
            ));
            if let Some(service) = build_channel::keyring_service_override() {
                spec.envs
                    .push(("HERDECK_KEYRING_SERVICE".to_string(), service.to_string()));
            }
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                supervise(SupervisorConfig::new(spec), child, stop, move |d| {
                    let view = DiscoveryView::from(&d);
                    register_toggle_hotkey(&handle, &d);
                    if let Some(state) = handle.try_state::<AppState>() {
                        *state.discovery.lock().unwrap() = Some(d);
                    }
                    let _ = handle.emit("discovery", view); // token-free
                });
            });
        }
    }
}

/// Tauri entry point.
pub fn run() {
    let discovery: Arc<Mutex<Option<Discovery>>> = Arc::new(Mutex::new(None));
    let child: Arc<Mutex<Option<Child>>> = Arc::new(Mutex::new(None));
    let stop = Arc::new(AtomicBool::new(false));

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
    let setup_child = child.clone();
    let setup_stop = stop.clone();
    let setup_config_path = config_path.clone();
    // Clones for the exit handler.
    let exit_child = child.clone();
    let exit_stop = stop.clone();

    let state = AppState {
        discovery,
        window_state: Arc::new(Mutex::new(startup)),
        deck_always_on_top: Arc::new(Mutex::new(deck_always_on_top)),
    };

    tauri::Builder::default()
        .manage(state)
        .manage(TrayHandles::default())
        .plugin(tauri_plugin_updater::Builder::new().build())
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
            tray_set_language
        ])
        .setup(move |app| {
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
            start_sidecar(
                app,
                setup_discovery,
                setup_child,
                setup_stop,
                &setup_config_path,
            );
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build herdeck desktop app")
        .run(move |app_handle, event| {
            if let tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit = event {
                // The last chance to save a deck position that was dragged and
                // never hidden — `Moved` deliberately does not touch the disk.
                persist_window_state(app_handle);
                // Tear the supervised sidecar down so it never outlives the shell.
                exit_stop.store(true, Ordering::SeqCst);
                if let Some(mut c) = exit_child.lock().unwrap().take() {
                    let _ = c.kill();
                    let _ = c.wait();
                }
            }
        });
}
