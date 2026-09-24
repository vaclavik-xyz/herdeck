//! Global shortcuts: the deck toggle and the opt-in "next blocked agent" and
//! "restart deck" hotkeys, (re)registered from the runtime's `/config` (the accelerator
//! parsing itself lives in `hotkey`).

use tauri::Manager;

use crate::proxy::{current_discovery, run_blocking};
use crate::sidecar::Discovery;
use crate::window_roles::{show_role_window, toggle_deck_window, DECK_WINDOW};
use crate::{hotkey, http, AppState, SIDECAR_TIMEOUT};

/// (Re)register the global shortcuts from the sidecar's `/config`: the deck
/// toggle and the opt-in "next blocked agent" hotkey. A failure leaves the deck
/// usable without a hotkey; it is returned as a message so `reload_hotkey` can
/// surface it in the settings UI (C4). When the configured toggle accelerator
/// cannot be registered the default is tried instead, and the error still says
/// the configured one failed. The two registrations are independent: one
/// failing never keeps the other from being registered.
pub(crate) fn register_toggle_hotkey(app: &tauri::AppHandle, d: &Discovery) -> Result<(), String> {
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
        register_restart_deck_accelerator(app, &cfg),
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

pub(crate) fn register_toggle_accelerator(app: &tauri::AppHandle, cfg: &serde_json::Value) -> Result<(), String> {
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
pub(crate) fn register_next_blocked_accelerator(app: &tauri::AppHandle, cfg: &serde_json::Value) -> Result<(), String> {
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

/// Register the opt-in `[hotkeys].restart_deck` accelerator (no default): the
/// same action as the tray's "Restart deck".
pub(crate) fn register_restart_deck_accelerator(app: &tauri::AppHandle, cfg: &serde_json::Value) -> Result<(), String> {
    use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};

    let Some(accel) = hotkey::restart_deck_accelerator(cfg) else {
        return Ok(()); // not configured
    };
    let app_for_cb = app.clone();
    app.global_shortcut()
        .on_shortcut(
            accel.as_str(),
            move |_app: &tauri::AppHandle, _sc: &tauri_plugin_global_shortcut::Shortcut, event: tauri_plugin_global_shortcut::ShortcutEvent| {
                if event.state == ShortcutState::Pressed {
                    crate::maintenance::restart_deck_from_shell(&app_for_cb);
                }
            },
        )
        .map_err(|e| format!("could not register the restart-deck hotkey '{accel}': {e}"))
}

/// The "next blocked agent" hotkey: show the deck and ask the runtime to open
/// the drill of the agent that has been blocked longest (`POST /triage`, the
/// same action as a press on the NEEDS YOU panel). The HTTP call runs off the
/// shortcut thread; a failure is only logged — the deck is shown either way.
pub(crate) fn jump_to_next_blocked(app: &tauri::AppHandle) {
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
pub(crate) fn register_toggle_hotkey_logged(app: &tauri::AppHandle, d: &Discovery) {
    if let Err(e) = register_toggle_hotkey(app, d) {
        eprintln!("{e}");
    }
}

/// Re-read `/config` and re-register the deck-toggle hotkey (the editor calls
/// this after a successful config write so a changed accelerator takes effect).
/// `Err` carries the registration failure for the settings UI to show.
#[tauri::command]
pub(crate) async fn reload_hotkey(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
) -> Result<(), String> {
    let d = current_discovery(&state)?;
    run_blocking(move || register_toggle_hotkey(&app, &d)).await
}
