//! herdeck desktop shell (phase 1, slice 3).
//!
//! A floating, always-on-top window that hosts the DeckView WebView, plus a tray
//! icon (show/hide/quit). On startup it spawns and supervises the Python sidecar
//! (`python -m herdeck.deckapp`), reads its first stdout line (the discovery JSON
//! `{url, host, port, token, source}`), and hands the url+token to the WebView so
//! the frontend can reach the sidecar over loopback. The sidecar is restarted on
//! crash and killed on quit.

pub mod http;
pub mod sidecar;

use std::env;
use std::path::{Path, PathBuf};
use std::process::Child;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{Emitter, Manager, PhysicalPosition};

use sidecar::{resolve_dev_sidecar, supervise, CommandSpec, Discovery, SupervisorConfig};

/// Managed state read by the `get_discovery` command and by the supervisor
/// callback. The live child handle and stop flag are held as separate `Arc`s
/// owned by the supervisor + exit-handler closures (not routed through here).
struct AppState {
    discovery: Arc<Mutex<Option<Discovery>>>,
}

/// Default timeout for the Rust-side sidecar proxy calls.
const SIDECAR_TIMEOUT: Duration = Duration::from_secs(3);

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

/// Probe the sidecar's token-authed `GET /health` and return its JSON. Done
/// Rust-side (not via WebView `fetch`) so it isn't blocked by CORS, and so the
/// access token never has to live in JS. `Err` if the sidecar isn't ready yet
/// or is unreachable.
#[tauri::command]
fn check_health(state: tauri::State<'_, AppState>) -> Result<serde_json::Value, String> {
    let d = current_discovery(&state)?;
    let body = http::http_get(
        &d.host,
        d.port,
        &format!("/health?token={}", d.token),
        SIDECAR_TIMEOUT,
    )?;
    serde_json::from_str::<serde_json::Value>(&body)
        .map_err(|e| format!("invalid /health JSON from sidecar: {e}"))
}

/// Proxy `GET /state` (token injected Rust-side) → its JSON. This is the deck's
/// poll endpoint; the WebView never sees the token.
#[tauri::command]
fn deck_state(state: tauri::State<'_, AppState>) -> Result<serde_json::Value, String> {
    let d = current_discovery(&state)?;
    let body = http::fetch_state(&d.host, d.port, &d.token, SIDECAR_TIMEOUT)?;
    serde_json::from_str::<serde_json::Value>(&body)
        .map_err(|e| format!("invalid /state JSON from sidecar: {e}"))
}

/// Proxy `GET /tile/{index}` → a `data:image/png;base64,…` URL (or `None` if the
/// tile is absent), so the WebView `<img>` renders it without touching the token.
#[tauri::command]
fn deck_tile(state: tauri::State<'_, AppState>, index: u32) -> Result<Option<String>, String> {
    let d = current_discovery(&state)?;
    http::fetch_image(
        &d.host,
        d.port,
        &format!("/tile/{index}"),
        &d.token,
        SIDECAR_TIMEOUT,
    )
}

/// Proxy `GET /panel` → a `data:` PNG URL (or `None` if there is no panel yet).
#[tauri::command]
fn deck_panel(state: tauri::State<'_, AppState>) -> Result<Option<String>, String> {
    let d = current_discovery(&state)?;
    http::fetch_image(&d.host, d.port, "/panel", &d.token, SIDECAR_TIMEOUT)
}

/// Proxy `POST /press/{index}` (token in the `X-Herdeck-Token` header) → the
/// sidecar's HTTP status code (204 ok, 403 bad token, 400 bad index).
#[tauri::command]
fn deck_press(state: tauri::State<'_, AppState>, index: u32) -> Result<u16, String> {
    let d = current_discovery(&state)?;
    http::send_press(&d.host, d.port, index, &d.token, SIDECAR_TIMEOUT)
}

/// Proxy `GET /config` (token as query param) → the redacted config JSON
/// `{base, profiles, local, secrets}`. `Err` if the sidecar has no config
/// service (404) or is unreachable.
#[tauri::command]
fn config_read(state: tauri::State<'_, AppState>) -> Result<serde_json::Value, String> {
    let d = current_discovery(&state)?;
    let body = http::http_get(
        &d.host,
        d.port,
        &format!("/config?token={}", d.token),
        SIDECAR_TIMEOUT,
    )?;
    serde_json::from_str(&body).map_err(|e| format!("invalid /config JSON from sidecar: {e}"))
}

/// Proxy `POST /config/validate` (header token) with the proposed config body →
/// `{errors: [...]}`. The body is the JS `{base, profiles, local}` object.
#[tauri::command]
fn config_validate(
    state: tauri::State<'_, AppState>,
    body: serde_json::Value,
) -> Result<serde_json::Value, String> {
    config_post_json(&state, "/config/validate", &body)
}

/// Proxy `POST /config` (header token) — atomic write + reload on the sidecar
/// when `errors` is empty. Returns `{errors: [...]}`.
#[tauri::command]
fn config_write(
    state: tauri::State<'_, AppState>,
    body: serde_json::Value,
) -> Result<serde_json::Value, String> {
    config_post_json(&state, "/config", &body)
}

/// Proxy `POST /profiles/active` (header token) → `{changed: bool}`. A 400
/// (unknown/invalid profile name) surfaces as `Err` so the UI can show it.
#[tauri::command]
fn config_set_active(
    state: tauri::State<'_, AppState>,
    name: String,
) -> Result<serde_json::Value, String> {
    config_post_json(&state, "/profiles/active", &serde_json::json!({ "name": name }))
}

/// Proxy `POST /secret` (header token) — store `value` for `token_env` in the
/// OS keychain. Returns the HTTP status (204 ok, 400 missing fields). The value
/// is one-way: it is never read back.
#[tauri::command]
fn config_secret_set(
    state: tauri::State<'_, AppState>,
    token_env: String,
    value: String,
) -> Result<u16, String> {
    let d = current_discovery(&state)?;
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
}

/// Proxy `DELETE /secret/{token_env}` (header token) → status (204 ok).
#[tauri::command]
fn config_secret_clear(
    state: tauri::State<'_, AppState>,
    token_env: String,
) -> Result<u16, String> {
    let d = current_discovery(&state)?;
    http::http_delete(
        &d.host,
        d.port,
        &format!("/secret/{}", http::percent_encode_segment(&token_env)),
        (HDR_TOKEN, &d.token),
        SIDECAR_TIMEOUT,
    )
}

/// Shared POST-JSON-and-parse for the config routes that return a JSON object on
/// 200. A non-200 (e.g. 400 for a malformed body / bad profile name) is an `Err`
/// the command surfaces to JS.
fn config_post_json(
    state: &tauri::State<'_, AppState>,
    path: &str,
    body: &serde_json::Value,
) -> Result<serde_json::Value, String> {
    let d = current_discovery(state)?;
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

/// Show + focus the (hidden-at-startup) config editor window.
#[tauri::command]
fn open_config(app: tauri::AppHandle) -> Result<(), String> {
    let w = app
        .get_webview_window("config")
        .ok_or_else(|| "config window not found".to_string())?;
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

/// Decide how to obtain the sidecar. If `HERDECK_DECKAPP_URL` +
/// `HERDECK_DECKAPP_TOKEN` are set, trust that externally-started sidecar (handy
/// for manual `tauri dev` smoke without a `.venv`); otherwise spawn the dev venv.
fn resolve_plan() -> SidecarPlan {
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
    SidecarPlan::Spawn(resolve_dev_sidecar(&repo_root_from_manifest()))
}

/// Position the floating window near the top-right corner and pin it on top.
fn place_floating(window: &tauri::WebviewWindow) {
    let _ = window.set_always_on_top(true);
    if let (Ok(Some(monitor)), Ok(win_size)) = (window.current_monitor(), window.outer_size()) {
        let screen = monitor.size();
        // The monitor's origin in the virtual desktop; without it a "top-right"
        // calc lands on the wrong screen (or off-screen) on multi-monitor setups.
        let origin = monitor.position();
        let margin = 16i32;
        let x = (origin.x + screen.width as i32 - win_size.width as i32 - margin).max(origin.x);
        let y = origin.y + margin;
        let _ = window.set_position(PhysicalPosition { x, y });
    }
}

/// Build the tray icon with a show/hide/quit menu.
fn build_tray(app: &tauri::App) -> tauri::Result<()> {
    let settings = MenuItem::with_id(app, "settings", "Settings…", true, None::<&str>)?;
    let show = MenuItem::with_id(app, "show", "Show", true, None::<&str>)?;
    let hide = MenuItem::with_id(app, "hide", "Hide", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&settings, &show, &hide, &quit])?;

    let mut builder = TrayIconBuilder::with_id("herdeck-tray")
        .tooltip("herdeck")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "settings" => {
                if let Some(w) = app.get_webview_window("config") {
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

/// Start the sidecar supervisor (or record the external discovery).
fn start_sidecar(
    app: &tauri::App,
    discovery: Arc<Mutex<Option<Discovery>>>,
    child: Arc<Mutex<Option<Child>>>,
    stop: Arc<AtomicBool>,
) {
    match resolve_plan() {
        SidecarPlan::External(d) => {
            let view = DiscoveryView::from(&d);
            *discovery.lock().unwrap() = Some(d);
            let _ = app.handle().emit("discovery", view); // token-free
        }
        SidecarPlan::Spawn(spec) => {
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                supervise(SupervisorConfig::new(spec), child, stop, move |d| {
                    let view = DiscoveryView::from(&d);
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

    // Clones for the setup closure and the supervisor.
    let setup_discovery = discovery.clone();
    let setup_child = child.clone();
    let setup_stop = stop.clone();
    // Clones for the exit handler.
    let exit_child = child.clone();
    let exit_stop = stop.clone();

    let state = AppState { discovery };

    tauri::Builder::default()
        .manage(state)
        .invoke_handler(tauri::generate_handler![
            get_discovery,
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
            open_config
        ])
        .setup(move |app| {
            if let Some(window) = app.get_webview_window("main") {
                place_floating(&window);
            }
            build_tray(app)?;
            // The config window is hidden at startup and reopened on demand; if it
            // were allowed to close, Tauri would DESTROY it and open_config would
            // then fail with "config window not found". Intercept close -> hide, so
            // it persists for the app's lifetime (the floating deck + sidecar run on).
            if let Some(cfg_win) = app.get_webview_window("config") {
                let w = cfg_win.clone();
                cfg_win.on_window_event(move |event| {
                    if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                        api.prevent_close();
                        let _ = w.hide();
                    }
                });
            }
            start_sidecar(app, setup_discovery, setup_child, setup_stop);
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
