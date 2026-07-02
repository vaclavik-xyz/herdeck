//! herdeck desktop shell (phase 1, slice 3).
//!
//! A floating, always-on-top window that hosts the DeckView WebView, plus a tray
//! icon (show/hide/quit). On startup it spawns and supervises the Python sidecar
//! (`python -m herdeck.deckapp`), reads its first stdout line (the discovery JSON
//! `{url, host, port, token, source}`), and hands the url+token to the WebView so
//! the frontend can reach the sidecar over loopback. The sidecar is restarted on
//! crash and killed on quit.

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
use tauri::{Emitter, Manager, PhysicalPosition, WebviewUrl, WebviewWindowBuilder};

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
    if let Some(d) = sidecar::decide_runtime_attach(
        sidecar::read_runtime_discovery(&sidecar::runtime_file_path()),
        probe_runtime_health,
    ) {
        return SidecarPlan::External(d);
    }

    SidecarPlan::Spawn(sidecar::choose_spawn(
        resource_dir.as_deref(),
        &repo_root_from_manifest(),
    ))
}

/// Position the floating window near the top-right of the PRIMARY monitor. The
/// builder owns `always_on_top` (per mode); this only places the window.
fn place_floating(window: &tauri::WebviewWindow) {
    if let (Ok(Some(monitor)), Ok(win_size)) = (window.primary_monitor(), window.outer_size()) {
        let screen = monitor.size();
        let origin = monitor.position();
        let margin = 16i32;
        let x = (origin.x + screen.width as i32 - win_size.width as i32 - margin).max(origin.x);
        let y = origin.y + margin;
        let _ = window.set_position(PhysicalPosition { x, y });
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
    let settings = MenuItem::with_id(app, "settings", "Nastavení…", true, None::<&str>)?;
    let show = MenuItem::with_id(app, "show", "Zobrazit", true, None::<&str>)?;
    let hide = MenuItem::with_id(app, "hide", "Schovat", true, None::<&str>)?;
    let wm_normal = CheckMenuItem::with_id(
        app,
        "wm_normal",
        "Normální",
        true,
        current_mode == WindowMode::Normal,
        None::<&str>,
    )?;
    let wm_floating = CheckMenuItem::with_id(
        app,
        "wm_floating",
        "Plovoucí",
        true,
        current_mode == WindowMode::Floating,
        None::<&str>,
    )?;
    let wm_aot = CheckMenuItem::with_id(
        app,
        "wm_aot",
        "Vždy navrchu",
        true,
        current_mode == WindowMode::AlwaysOnTop,
        None::<&str>,
    )?;
    let wm_submenu = tauri::menu::Submenu::with_items(
        app,
        "Režim okna",
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
        "Spouštět po přihlášení",
        true,
        app.autolaunch().is_enabled().unwrap_or(false),
        None::<&str>,
    )?;
    let reconnect = MenuItem::with_id(app, "reconnect", "Změnit připojení…", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Ukončit", true, None::<&str>)?;
    let menu = Menu::with_items(
        app,
        &[&settings, &show, &hide, &wm_submenu, &reconnect, &autostart, &quit],
    )?;
    let autostart_cb = autostart.clone();
    let wm_items_cb = wm_items.clone();

    let mut builder = TrayIconBuilder::with_id("herdeck-tray")
        .tooltip("herdeck")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(move |app, event| {
            let wm_items = &wm_items_cb;
            match event.id.as_ref() {
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
    let resource_dir = app.path().resource_dir().ok();
    match resolve_plan(resource_dir) {
        SidecarPlan::External(d) => {
            let view = DiscoveryView::from(&d);
            register_toggle_hotkey(app.handle(), &d);
            *discovery.lock().unwrap() = Some(d);
            let _ = app.handle().emit("discovery", view); // token-free
        }
        SidecarPlan::Spawn(mut spec) => {
            spec.envs.push((
                "HERDECK_CONFIG".to_string(),
                config_path.to_string_lossy().into_owned(),
            ));
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
    let env_override = env::var("HERDECK_CONFIG").ok();
    let config_path =
        window_mode::resolve_config_path(env_override.as_deref(), &home, &repo_root);
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
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            None,
        ))
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
            setup_status,
            setup_connect,
            open_config,
            reload_hotkey,
            get_window_mode
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
            let builder = WebviewWindowBuilder::new(&app_handle, "main", WebviewUrl::default())
                .title("herdeck")
                .shadow(true)
                .initialization_script(init);
            let builder = match mode {
                WindowMode::Normal => builder
                    .decorations(true)
                    .transparent(false)
                    .always_on_top(false)
                    .resizable(true)
                    .inner_size(380.0, 340.0)
                    .skip_taskbar(false),
                WindowMode::Floating => builder
                    .decorations(false)
                    .transparent(true)
                    .always_on_top(false)
                    .resizable(false)
                    .inner_size(360.0, 320.0)
                    .skip_taskbar(true),
                WindowMode::AlwaysOnTop => builder
                    .decorations(false)
                    .transparent(true)
                    .always_on_top(true)
                    .resizable(false)
                    .inner_size(360.0, 320.0)
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
                let w = cfg_win.clone();
                cfg_win.on_window_event(move |event| {
                    if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                        api.prevent_close();
                        let _ = w.hide();
                    }
                });
            }
            start_sidecar(app, setup_discovery, setup_child, setup_stop, &setup_config_path);
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
