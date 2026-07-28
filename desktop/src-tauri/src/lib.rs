//! herdeck desktop shell (phase 1, slice 3).
//!
//! A floating, always-on-top window that hosts the DeckView WebView, plus a tray
//! icon (show/hide/quit). On startup it spawns and supervises the Python sidecar
//! (`python -m herdeck.deckapp`), reads its first stdout line (the discovery JSON
//! `{url, host, port, token, source}`), and hands the url+token to the WebView so
//! the frontend can reach the sidecar over loopback. The sidecar is restarted on
//! crash and killed on quit.

pub mod build_channel;
pub mod hotkey;
pub mod http;
pub mod sidecar;
pub mod window_mode;

use std::env;
use std::path::{Path, PathBuf};
use std::process::Child;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tauri::menu::{CheckMenuItem, Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{Emitter, LogicalPosition, Manager, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_updater::UpdaterExt;

use window_mode::WindowMode;

use sidecar::{supervise, CommandSpec, Discovery, SupervisorConfig};

/// Managed state read by the `get_discovery` command and by the supervisor
/// callback. The live child handle and stop flag are held as separate `Arc`s
/// owned by the supervisor + exit-handler closures (not routed through here).
struct AppState {
    discovery: Arc<Mutex<Option<Discovery>>>,
    /// The live window mode. Set at startup from config; updated in-process on a
    /// live floating↔always_on_top switch (a restart-mode switch replaces the
    /// whole process, which re-reads config).
    window_mode: Arc<Mutex<WindowMode>>,
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

/// The window mode the deck was built with (updated live on a borderless switch).
/// The frontend ALSO reads `<html data-window-mode>` (set pre-paint by Rust); this
/// command is the programmatic path for logic that needs it after mount.
#[tauri::command]
fn get_window_mode(state: tauri::State<'_, AppState>) -> String {
    state.window_mode.lock().unwrap().as_str().to_string()
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

/// Show + focus the desktop editor. Normal mode already renders it in `main`;
/// compact overlay modes use the dedicated full-size config window.
#[tauri::command]
fn open_config(app: tauri::AppHandle, state: tauri::State<'_, AppState>) -> Result<(), String> {
    let mode = *state.window_mode.lock().unwrap();
    if mode == WindowMode::Normal {
        let _ = app.emit_to("main", "open-settings", ());
    }
    let w = app
        .get_webview_window(window_mode::settings_window_label(mode))
        .ok_or_else(|| "desktop settings window not found".to_string())?;
    w.show().map_err(|e| e.to_string())?;
    w.set_focus().map_err(|e| e.to_string())?;
    Ok(())
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
/// macOS is the odd one out. `cursor_position()` there takes the logical
/// `NSEvent.mouseLocation` and multiplies it by the PRIMARY monitor's scale
/// factor, while `monitor_from_point` hit-tests raw `CGDisplayBounds`, which are
/// logical. Left uncorrected on a Retina primary, a pointer on the built-in
/// display resolves to a monitor to its right, or — further out — to no monitor
/// at all, so the whole pointer preference silently never fires. Windows
/// (physical against `MonitorFromPoint`) and X11 (GDK points against
/// `monitor_at_point`) already agree with themselves, hence a scale of 1.
fn cursor_in_lookup_space(cursor: (f64, f64), scale: f64) -> (f64, f64) {
    let scale = usable_scale(scale);
    (cursor.0 / scale, cursor.1 / scale)
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
    // See cursor_in_lookup_space: only macOS reads the cursor in a different
    // space from the one it hit-tests.
    let scale = if cfg!(target_os = "macos") {
        window
            .primary_monitor()
            .ok()
            .flatten()
            .map_or(1.0, |m| m.scale_factor())
    } else {
        1.0
    };
    pick_monitor(
        window
            .cursor_position()
            .ok()
            .map(|p| cursor_in_lookup_space((p.x, p.y), scale)),
        |x, y| window.monitor_from_point(x, y).ok().flatten(),
        || window.current_monitor().ok().flatten(),
        || window.primary_monitor().ok().flatten(),
    )
}

/// Position the floating window near the top-right of the monitor the user is on.
/// The builder owns `always_on_top` (per mode); this only places the window.
/// Placement uses the WORK area, not the full screen, so the deck never opens
/// under the macOS menu bar or behind the dock.
///
/// Everything is computed in LOGICAL points, the one space all three inputs
/// agree on. `work_area()` and `outer_size()` are both "physical", but each is
/// scaled by a DIFFERENT factor — the monitor's and the window's — and those
/// part company the moment the deck moves between a Retina screen and an
/// external one. tao then converts a physical `set_position` argument with the
/// WINDOW's factor, so a physical target derived from the MONITOR's rect lands
/// wrong as well. Handing it a logical position removes both mismatches.
fn place_floating(window: &tauri::WebviewWindow) {
    if let (Some(monitor), Ok(win_size)) = (active_monitor(window), window.outer_size()) {
        let screen = usable_scale(monitor.scale_factor());
        let win = usable_scale(window.scale_factor().unwrap_or(screen));
        let area = monitor.work_area();
        let (x, y) = floating_origin(
            (area.position.x as f64 / screen, area.position.y as f64 / screen),
            (area.size.width as f64 / screen, area.size.height as f64 / screen),
            (win_size.width as f64 / win, win_size.height as f64 / win),
            FLOATING_MARGIN,
        );
        let _ = window.set_position(LogicalPosition { x, y });
    }
}

/// Show/hide the floating `main` window — the deck-toggle hotkey action.
fn toggle_main_window(app: &tauri::AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        if w.is_visible().unwrap_or(false) {
            let _ = w.hide();
        } else {
            let _ = w.show();
            let _ = w.set_focus();
        }
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
            toggle_main_window(&app_for_cb);
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
                        toggle_main_window(&app_for_fb);
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

/// The three window-mode tray checkboxes. There is no native radio group, so we
/// hold all three handles and drive the checkmarks ourselves (like `autostart_cb`).
#[derive(Clone)]
struct WmItems {
    normal: CheckMenuItem<tauri::Wry>,
    floating: CheckMenuItem<tauri::Wry>,
    aot: CheckMenuItem<tauri::Wry>,
}

/// English/Czech texts for every tray item, keyed by the item order in
/// `TrayHandles::retitle`. The tray is native — the WebView retitles it via the
/// `tray_set_language` command when the deck's `[view].language` changes.
fn tray_labels(lang: &str) -> [&'static str; 10] {
    match lang {
        "cs" => [
            "Nastavení…",
            "Zobrazit",
            "Skrýt",
            "Normální",
            "Plovoucí",
            "Vždy navrchu",
            "Režim okna",
            "Spouštět po přihlášení",
            "Změnit připojení…",
            "Ukončit",
        ],
        _ => [
            "Settings…",
            "Show",
            "Hide",
            "Normal",
            "Floating",
            "Always on top",
            "Window mode",
            "Start at login",
            "Change connection…",
            "Quit",
        ],
    }
}

/// Handles to every retitlable tray item, managed as Tauri state so the
/// `tray_set_language` command can reach them after setup.
#[derive(Default)]
struct TrayHandles(Mutex<Option<TrayMenuItems>>);

struct TrayMenuItems {
    settings: MenuItem<tauri::Wry>,
    show: MenuItem<tauri::Wry>,
    hide: MenuItem<tauri::Wry>,
    wm: WmItems,
    wm_submenu: tauri::menu::Submenu<tauri::Wry>,
    autostart: CheckMenuItem<tauri::Wry>,
    reconnect: MenuItem<tauri::Wry>,
    quit: MenuItem<tauri::Wry>,
}

impl TrayMenuItems {
    fn retitle(&self, lang: &str) {
        let l = tray_labels(lang);
        let _ = self.settings.set_text(l[0]);
        let _ = self.show.set_text(l[1]);
        let _ = self.hide.set_text(l[2]);
        let _ = self.wm.normal.set_text(l[3]);
        let _ = self.wm.floating.set_text(l[4]);
        let _ = self.wm.aot.set_text(l[5]);
        let _ = self.wm_submenu.set_text(l[6]);
        let _ = self.autostart.set_text(l[7]);
        let _ = self.reconnect.set_text(l[8]);
        let _ = self.quit.set_text(l[9]);
    }
}

/// Retitle the native tray menu for `lang` ("en"/"cs"). Called by the WebView
/// whenever the deck-reported language changes; unknown values fall back to en.
#[tauri::command]
fn tray_set_language(lang: String, handles: tauri::State<'_, TrayHandles>) {
    if let Some(items) = handles.0.lock().unwrap().as_ref() {
        items.retitle(&lang);
    }
}

/// Check exactly the item for `mode`, uncheck the other two.
fn set_wm_checks(items: &WmItems, mode: WindowMode) {
    let _ = items.normal.set_checked(mode == WindowMode::Normal);
    let _ = items.floating.set_checked(mode == WindowMode::Floating);
    let _ = items.aot.set_checked(mode == WindowMode::AlwaysOnTop);
}

/// Persist `window_mode = target` to base config via the sidecar. Read-modify-write
/// over the existing `/config` routes (token injected Rust-side, like the editor).
/// Returns `Ok(())` ONLY on a confirmed write: the `/config` contract returns
/// validation failures as HTTP 200 with a non-empty `errors`, writing NOTHING, so
/// success requires HTTP 200 AND `errors == []`. The POST blocks on `_setup_lock`,
/// so it uses the longer `SETUP_CONNECT_TIMEOUT`; a timeout there is a genuine wedge.
fn persist_window_mode(state: &AppState, target: WindowMode) -> Result<(), String> {
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
            "window_mode".to_string(),
            serde_json::Value::String(target.as_str().to_string()),
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

/// Tray handler for a window-mode choice: persist FIRST, apply only on success.
/// floating↔always_on_top applies live (toggle always_on_top); any change to/from
/// normal restarts (transparent is creation-time). On persist failure: revert the
/// checkmarks, log, do nothing else.
fn select_window_mode(app: &tauri::AppHandle, target: WindowMode, items: &WmItems) {
    let state = match app.try_state::<AppState>() {
        Some(s) => s,
        None => return,
    };
    let current = *state.window_mode.lock().unwrap();
    if target == current {
        set_wm_checks(items, current); // re-assert, no-op
        return;
    }
    if let Err(e) = persist_window_mode(&state, target) {
        eprintln!("window mode: persist failed, not applying: {e}");
        set_wm_checks(items, current); // revert to the real persisted mode
        return;
    }
    if window_mode::switch_needs_restart(current, target) {
        // NOT app.restart(): a tray menu event runs on the MAIN THREAD, where
        // Tauri's restart() skips RunEvent::ExitRequested/Exit and would ORPHAN
        // the sidecar child (its kill lives in that handler). request_restart()
        // routes through the event loop so the exit handler runs before restart.
        app.request_restart();
        return;
    }
    // Reached only for a live borderless↔borderless switch.
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.set_always_on_top(target == WindowMode::AlwaysOnTop);
    }
    *state.window_mode.lock().unwrap() = target;
    set_wm_checks(items, target);
}

/// Build the tray icon with a show/hide/quit menu.
fn build_tray(app: &tauri::App, current_mode: WindowMode) -> tauri::Result<()> {
    use tauri_plugin_autostart::ManagerExt;
    // Built with the English (default-language) labels; tray_set_language
    // retitles everything the moment the WebView learns the configured language.
    let l = tray_labels("en");
    let settings = MenuItem::with_id(app, "settings", l[0], true, None::<&str>)?;
    let show = MenuItem::with_id(app, "show", l[1], true, None::<&str>)?;
    let hide = MenuItem::with_id(app, "hide", l[2], true, None::<&str>)?;
    let wm_normal = CheckMenuItem::with_id(
        app,
        "wm_normal",
        l[3],
        true,
        current_mode == WindowMode::Normal,
        None::<&str>,
    )?;
    let wm_floating = CheckMenuItem::with_id(
        app,
        "wm_floating",
        l[4],
        true,
        current_mode == WindowMode::Floating,
        None::<&str>,
    )?;
    let wm_aot = CheckMenuItem::with_id(
        app,
        "wm_aot",
        l[5],
        true,
        current_mode == WindowMode::AlwaysOnTop,
        None::<&str>,
    )?;
    let wm_submenu = tauri::menu::Submenu::with_items(
        app,
        l[6],
        true,
        &[&wm_normal, &wm_floating, &wm_aot],
    )?;
    let wm_items = WmItems {
        normal: wm_normal,
        floating: wm_floating,
        aot: wm_aot,
    };
    let autostart = CheckMenuItem::with_id(
        app,
        "autostart",
        l[7],
        true,
        app.autolaunch().is_enabled().unwrap_or(false),
        None::<&str>,
    )?;
    let reconnect = MenuItem::with_id(app, "reconnect", l[8], true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", l[9], true, None::<&str>)?;
    let menu = Menu::with_items(
        app,
        &[&settings, &show, &hide, &wm_submenu, &reconnect, &autostart, &quit],
    )?;
    let autostart_cb = autostart.clone();
    let wm_items_cb = wm_items.clone();
    if let Some(handles) = app.try_state::<TrayHandles>() {
        *handles.0.lock().unwrap() = Some(TrayMenuItems {
            settings: settings.clone(),
            show: show.clone(),
            hide: hide.clone(),
            wm: wm_items.clone(),
            wm_submenu: wm_submenu.clone(),
            autostart: autostart.clone(),
            reconnect: reconnect.clone(),
            quit: quit.clone(),
        });
    }

    let mut builder = TrayIconBuilder::with_id("herdeck-tray")
        .tooltip(build_channel::display_name())
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(move |app, event| {
            let wm_items = &wm_items_cb;
            match event.id.as_ref() {
            "settings" => {
                if current_mode == WindowMode::Normal {
                    let _ = app.emit_to("main", "open-settings", ());
                }
                if let Some(w) =
                    app.get_webview_window(window_mode::settings_window_label(current_mode))
                {
                    let _ = w.show();
                    let _ = w.set_focus();
                }
            }
            "show" => {
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.show();
                    let _ = w.set_focus();
                }
            }
            "hide" => {
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.hide();
                }
            }
            "reconnect" => {
                let _ = app.emit_to("main", "reonboard", ());
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.show();
                    let _ = w.set_focus();
                }
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
            "wm_normal" => select_window_mode(app, WindowMode::Normal, wm_items),
            "wm_floating" => select_window_mode(app, WindowMode::Floating, wm_items),
            "wm_aot" => select_window_mode(app, WindowMode::AlwaysOnTop, wm_items),
            "quit" => app.exit(0),
            _ => {}
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
/// file Rust resolved the window mode from (mooting the sidecar's CWD-relative
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

    // Resolve config.toml with the sidecar's existence-check order, then read the
    // window mode BEFORE the window is built (transparent/decorations are
    // creation-time props in Tauri 2).
    let home = PathBuf::from(env::var("HOME").unwrap_or_default());
    let repo_root = repo_root_from_manifest();
    let explicit_config = env::var("HERDECK_CONFIG").ok();
    let config_override = build_channel::config_override(explicit_config.as_deref(), &home);
    let config_path = window_mode::resolve_config_path(
        config_override.as_ref().and_then(|path| path.to_str()),
        &home,
        &repo_root,
    );
    let mode = window_mode::read_window_mode(&config_path);

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
        window_mode: Arc::new(Mutex::new(mode)),
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
            open_config,
            reload_hotkey,
            get_window_mode,
            tray_set_language
        ])
        .setup(move |app| {
            // `main` is no longer in tauri.conf.json — build it here so its
            // transparent/decorations match the mode. The init script sets
            // `<html data-window-mode>` BEFORE first paint so the borderless CSS
            // applies with no flash of opaque-normal styling (FOUC).
            let app_handle = app.handle().clone();
            let init = format!(
                "document.documentElement.dataset.windowMode='{}'",
                mode.as_str()
            );
            let display_name = build_channel::display_name();
            // The borderless card carries no CSS drop shadow (it fills the window
            // exactly, so one would only pool in the corner notches). macOS
            // derives a transparent window's shadow from the drawn content's
            // alpha, i.e. from the rounded card itself — so let it.
            let builder = WebviewWindowBuilder::new(&app_handle, "main", WebviewUrl::default())
                .title(&display_name)
                .shadow(true)
                .initialization_script(init);
            let builder = match mode {
                WindowMode::Normal => builder
                    .decorations(true)
                    .transparent(false)
                    .always_on_top(false)
                    .resizable(true)
                    .inner_size(1180.0, 780.0)
                    .min_inner_size(680.0, 540.0)
                    .skip_taskbar(false),
                WindowMode::Floating => builder
                    .decorations(false)
                    .transparent(true)
                    .always_on_top(false)
                    .resizable(false)
                    .inner_size(328.0, 300.0)
                    .skip_taskbar(true),
                WindowMode::AlwaysOnTop => builder
                    .decorations(false)
                    .transparent(true)
                    .always_on_top(true)
                    .resizable(false)
                    .inner_size(328.0, 300.0)
                    .skip_taskbar(true),
            };
            let main_window = builder.build()?;

            // Borderless modes get the top-right placement; normal opens where the
            // OS puts it and is user-movable via the titlebar.
            if mode.is_borderless() {
                place_floating(&main_window);
            }

            // Normal mode has a close button; intercept close -> hide (like the
            // `config` window) so the tray "Show" brings it back and the app +
            // sidecar keep running. CloseRequested is window-close only — it does
            // NOT fire for app.exit/app.restart, so this never blocks quit/restart.
            {
                let w = main_window.clone();
                main_window.on_window_event(move |event| {
                    if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                        api.prevent_close();
                        let _ = w.hide();
                    }
                });
            }

            build_tray(app, mode)?;

            // The config window is hidden at startup and reopened on demand; if it
            // were allowed to close, Tauri would DESTROY it and open_config would
            // then fail with "config window not found". Intercept close -> hide.
            if let Some(cfg_win) = app.get_webview_window("config") {
                if build_channel::is_dev() {
                    let _ = cfg_win.set_title(&format!("{display_name} - Settings"));
                }
                let w = cfg_win.clone();
                cfg_win.on_window_event(move |event| {
                    if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                        api.prevent_close();
                        let _ = w.hide();
                    }
                });
            }
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
        .run(move |_app_handle, event| {
            if let tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit = event {
                // Tear the supervised sidecar down so it never outlives the shell.
                exit_stop.store(true, Ordering::SeqCst);
                if let Some(mut c) = exit_child.lock().unwrap().take() {
                    let _ = c.kill();
                    let _ = c.wait();
                }
            }
        });
}
