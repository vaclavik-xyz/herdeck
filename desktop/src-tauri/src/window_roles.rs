//! The two fixed-role windows: the borderless deck overlay (`main`) and the
//! decorated settings window (`config`). Deck placement across monitors and
//! DPI spaces, show/hide bookkeeping (mirrored to `window-state.json` and the
//! tray label), and the deck's always-on-top flag.

use std::env;
use std::path::Path;

use tauri::{Emitter, LogicalPosition, Manager, PhysicalPosition};

use crate::runtime_plan::default_config_path;
use crate::sync_util::LockExt;
use crate::tray::TrayHandles;
use crate::window_state::{self, WindowState};
use crate::{deck_prefs, http, AppState, HDR_TOKEN, SETUP_CONNECT_TIMEOUT, SIDECAR_TIMEOUT};

/// The two fixed window roles. The labels are historical — `main` is the
/// borderless deck overlay, `config` the decorated settings window — and are
/// kept deliberately: `capabilities/default.json` scopes permissions to exactly
/// these two strings, and renaming would buy nothing a user can see.
pub(crate) const DECK_WINDOW: &str = "main";
pub(crate) const APP_WINDOW: &str = "config";

/// Told to the app window whenever the DECK's visibility changes, so its own
/// toggle button stays honest even when the tray, the hotkey, or the deck's
/// own close (⌘W) changed it from somewhere else. Emitted beside the tray
/// label sync in `show_role_window`/`hide_role_window` — same trigger, same
/// `deck_label_refresh` gate.
pub(crate) const DECK_VISIBILITY_EVENT: &str = "deck-visibility-changed";

/// Bring the deck forward: un-hide the app (macOS), show and focus the deck
/// window. What a click on one of our banners, and a Dock/Finder reopen, do.
pub(crate) fn reveal_deck(app: &tauri::AppHandle) {
    let handle = app.clone();
    let _ = app.run_on_main_thread(move || {
        #[cfg(target_os = "macos")]
        let _ = handle.show();
        show_role_window(&handle, DECK_WINDOW);
    });
}

/// Gap, in logical points, between the floating deck and the edges of its screen.
pub(crate) const FLOATING_MARGIN: f64 = 16.0;

/// Top-right corner of a monitor's USABLE area, one margin in. Both axes are
/// clamped to the area's origin so a deck larger than the screen (zoomed up, or
/// a small external display) still starts on-screen instead of off past the
/// edge, where it could not be dragged back. Unit-agnostic: correct for whatever
/// consistent space the caller measures the area and the window in.
pub(crate) fn floating_origin(
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
pub(crate) fn usable_scale(factor: f64) -> f64 {
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
pub(crate) fn cursor_in_lookup_space(cursor: (f64, f64), scale: f64) -> (f64, f64) {
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
pub(crate) fn is_wayland_session(
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
pub(crate) struct Placement {
    pub(crate) screen_div: f64,
    pub(crate) window_div: f64,
    pub(crate) margin: f64,
}

pub(crate) fn placement_units(is_windows: bool, monitor_scale: f64, window_scale: f64) -> Placement {
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
pub(crate) fn placement_position(is_windows: bool, x: f64, y: f64) -> tauri::Position {
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
pub(crate) fn pointer_is_locatable(
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
pub(crate) fn pick_monitor<M>(
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

pub(crate) fn active_monitor(window: &tauri::WebviewWindow) -> Option<tauri::Monitor> {
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
pub(crate) fn position_is_on_any(areas: &[((i32, i32), (u32, u32))], pos: (i32, i32)) -> bool {
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
pub(crate) fn placement_divisor(is_windows: bool, scale: f64) -> f64 {
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
pub(crate) fn monitor_work_areas(window: &tauri::WebviewWindow) -> Vec<((i32, i32), (u32, u32))> {
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
pub(crate) fn placement_space_position(
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
pub(crate) fn place_floating(window: &tauri::WebviewWindow) {
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
pub(crate) fn window_role_script(role: &str) -> String {
    format!("document.documentElement.dataset.windowRole='{role}'")
}

/// Put the deck back where the user last dragged it, or near the top-right of
/// the monitor the pointer is on. A remembered origin is honoured only while it
/// still lands on a connected screen: a borderless window dropped onto an
/// unplugged display has no titlebar to drag it back with.
pub(crate) fn place_deck(window: &tauri::WebviewWindow, remembered: Option<(i32, i32)>) {
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
pub(crate) fn set_role_visible(state: &mut WindowState, label: &str, visible: bool) {
    match label {
        DECK_WINDOW => state.deck_visible = visible,
        APP_WINDOW => state.app_visible = visible,
        _ => {}
    }
}

/// The ONE place `window-state.json` is written from, so its two callers cannot
/// drift apart. Snapshots under the lock and writes outside it. Best-effort:
/// losing the file costs the next launch its remembered layout and nothing else.
pub(crate) fn store_window_state(state: &AppState) {
    let snapshot = *state.window_state.lock_or_recover();
    window_state::store(&window_state::state_dir(), &snapshot);
}

/// Mutate the live window state and mirror it to disk.
pub(crate) fn update_window_state(app: &tauri::AppHandle, f: impl FnOnce(&mut WindowState)) {
    let Some(state) = app.try_state::<AppState>() else {
        return;
    };
    f(&mut state.window_state.lock_or_recover());
    store_window_state(&state);
}

/// Write the live state out as it stands — the exit path, and the flush that
/// pairs with `remember_deck_position`.
pub(crate) fn persist_window_state(app: &tauri::AppHandle) {
    if let Some(state) = app.try_state::<AppState>() {
        store_window_state(&state);
    }
}

/// Record the deck's new origin WITHOUT touching the disk: one drag emits
/// hundreds of `Moved` events. Hiding or closing the deck writes, and so does
/// exit, which between them cover every way a position can be the last thing
/// that changed.
pub(crate) fn remember_deck_position(app: &tauri::AppHandle, position: (i32, i32)) {
    if let Some(state) = app.try_state::<AppState>() {
        state.window_state.lock_or_recover().deck_position = Some(position);
    }
}

/// Whether a visibility change should retitle the tray's `toggle_deck` item, and
/// to what: `Some(visible)` for the deck, `None` for any other window. The app
/// window opening must not turn "Show deck" into "Hide deck".
///
/// Split out of `show_role_window`/`hide_role_window` because it is the only part
/// of the sync a unit test can reach — the rest needs a live tray menu.
pub(crate) fn deck_label_refresh(label: &str, visible: bool) -> Option<bool> {
    (label == DECK_WINDOW).then_some(visible)
}

/// Retitle the tray's `toggle_deck` item for the deck's new visibility, so the
/// item always names what it will do next.
///
/// Takes both locks it needs one at a time and holds neither across the other:
/// its callers have already released `AppState.window_state` by the time they
/// get here (`update_window_state` drops its guard when it returns), and nothing
/// reached from `TrayHandles` takes a window-state lock.
pub(crate) fn sync_deck_tray_label(app: &tauri::AppHandle, deck_visible: bool) {
    if let Some(handles) = app.try_state::<TrayHandles>() {
        if let Some(items) = handles.0.lock_or_recover().as_ref() {
            items.sync_toggle_deck_label(deck_visible);
        }
    }
}

/// Show a role window and record that it is open. Every entry point — tray,
/// hotkey, frontend command — goes through here, so the remembered layout can
/// never drift from what is actually on screen, and neither can the tray's own
/// show/hide-deck label.
pub(crate) fn show_role_window(app: &tauri::AppHandle, label: &str) {
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
pub(crate) fn hide_role_window(app: &tauri::AppHandle, label: &str) {
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
pub(crate) fn show_deck(app: tauri::AppHandle) {
    show_role_window(&app, DECK_WINDOW);
}

/// Close the deck overlay back to the tray.
#[tauri::command]
pub(crate) fn hide_deck(app: tauri::AppHandle) {
    hide_role_window(&app, DECK_WINDOW);
}

/// Open the settings window — the app surface, and where onboarding lives.
#[tauri::command]
pub(crate) fn show_app(app: tauri::AppHandle) {
    show_role_window(&app, APP_WINDOW);
}

/// The deck's actual on-screen visibility, read straight from the window
/// rather than `WindowState` — every caller needs "is it visible right now",
/// not "was it last recorded so".
pub(crate) fn deck_is_visible(app: &tauri::AppHandle) -> bool {
    app.get_webview_window(DECK_WINDOW)
        .and_then(|w| w.is_visible().ok())
        .unwrap_or(false)
}

/// Whether the deck overlay is visible right now. The app window's toggle
/// button calls this once on mount — the deck may already be open (tray,
/// hotkey, a previous session) by the time the app window appears, and after
/// that its label follows `DECK_VISIBILITY_EVENT` instead of polling this.
#[tauri::command]
pub(crate) fn deck_visible(app: tauri::AppHandle) -> bool {
    deck_is_visible(&app)
}

/// Show/hide the deck overlay — shared by the tray's `toggle_deck` item and
/// the deck-toggle hotkey, so both flip the SAME window the SAME way. The tray
/// item's own text follows from `show_role_window`/`hide_role_window`, which
/// every deck-visibility path goes through.
pub(crate) fn toggle_deck_window(app: &tauri::AppHandle) {
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
pub(crate) fn persist_deck_always_on_top(state: &AppState, target: bool) -> Result<(), String> {
    let d = state
        .discovery
        .lock_or_recover()
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

/// The always-on-top value a live re-read should apply, or `None` when the
/// config could not be READ at all — which is not the same answer as a config
/// that says `false`. Reading `""` on failure would resolve to `false` and hand
/// that to the window, `AppState` and the tray checkbox while `config.toml`
/// still said `true`, so the two outcomes stay apart here.
pub(crate) fn deck_always_on_top_target(config_path: &Path) -> Option<bool> {
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
pub(crate) fn reload_deck_always_on_top(
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
    *state.deck_always_on_top.lock_or_recover() = target;
    if let Some(items) = tray.0.lock_or_recover().as_ref() {
        let _ = items.deck_aot.set_checked(target);
    }
}
