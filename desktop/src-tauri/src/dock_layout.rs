//! The mini dock's window geometry. The floating window grows beside the
//! strip while a card is open and shrinks back when it closes, without the
//! strip itself moving on screen. Pure helpers (unit-tested) live here; the
//! `dock_layout` / `reload_floating` commands below them do the window calls.
//!
//! All rects are in the PLACEMENT space of `window_roles` (logical points on
//! macOS/Linux, global physical pixels on Windows); `css_scale` converts the
//! frontend's CSS pixels into it.

use tauri::{Emitter, LogicalSize, Manager, PhysicalSize};

use crate::deck_prefs;
use crate::runtime_plan::default_config_path;
use crate::sync_util::LockExt;
use crate::window_roles::{
    placement_position, placement_space_position, placement_units, usable_scale, DECK_WINDOW,
};
use crate::AppState;

/// Gap between the strip and an open card, in CSS px (`CARD_GAP` in
/// desktop/src/lib/dockModel.ts).
pub(crate) const DOCK_CARD_GAP: f64 = 8.0;

#[derive(Debug, Clone, Copy, PartialEq)]
pub(crate) struct Rect {
    pub(crate) x: f64,
    pub(crate) y: f64,
    pub(crate) w: f64,
    pub(crate) h: f64,
}

/// Which side of the strip a card opens on.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Side {
    Left,
    Right,
}

impl Side {
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Side::Left => "left",
            Side::Right => "right",
        }
    }
}

/// Open toward the side with more room on the strip's monitor: left when the
/// strip's centre is in the right half of the work area, else right.
pub(crate) fn expand_side(strip: Rect, area: Rect) -> Side {
    if strip.x + strip.w / 2.0 > area.x + area.w / 2.0 {
        Side::Left
    } else {
        Side::Right
    }
}

/// The window rect for a strip with a card of `card` (w, h) beside it, and
/// where the strip sits inside that window. A `card` width of 0 collapses
/// the window to the strip itself.
#[derive(Debug, Clone, Copy, PartialEq)]
pub(crate) struct DockFrame {
    pub(crate) window: Rect,
    pub(crate) strip_dx: f64,
    pub(crate) strip_dy: f64,
}

/// The strip never moves on screen (`window + strip offset == strip`): a left
/// card moves the window's origin left instead, and a window that would
/// leave the work area's bottom is lifted (the strip then sits lower inside
/// it), but never above the work area's top.
pub(crate) fn expanded_frame(strip: Rect, card: (f64, f64), gap: f64, side: Side, area: Rect) -> DockFrame {
    let (card_w, card_h) = card;
    if card_w <= 0.0 {
        return DockFrame { window: strip, strip_dx: 0.0, strip_dy: 0.0 };
    }
    let w = strip.w + gap + card_w;
    let h = strip.h.max(card_h);
    let x = match side {
        Side::Left => strip.x - gap - card_w,
        Side::Right => strip.x,
    };
    let bottom = area.y + area.h;
    let y = strip.y.min((bottom - h).max(area.y));
    DockFrame {
        window: Rect { x, y, w, h },
        strip_dx: strip.x - x,
        strip_dy: strip.y - y,
    }
}

/// The strip's origin, given the window's origin and the strip's offset in
/// it. The remembered deck position is always the strip's: a `Moved` event
/// while a card is open must not store the expanded window's origin.
pub(crate) fn strip_origin(window_pos: (i32, i32), offset: (f64, f64)) -> (i32, i32) {
    (
        window_pos.0 + offset.0.round() as i32,
        window_pos.1 + offset.1.round() as i32,
    )
}

/// CSS px → placement units: identity where placement is logical points,
/// the window's scale factor on Windows (physical placement).
pub(crate) fn css_scale(is_windows: bool, window_scale: f64) -> f64 {
    if is_windows {
        usable_scale(window_scale)
    } else {
        1.0
    }
}

/// What `dock_layout` tells the frontend: which side the card opens on and
/// where the strip sits inside the (possibly grown) window, in CSS px.
#[derive(Debug, Clone, Copy, PartialEq, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct DockPlacement {
    pub(crate) side: &'static str,
    pub(crate) strip_x: f64,
    pub(crate) strip_y: f64,
}

pub(crate) fn placement_reply(frame: DockFrame, side: Side, css: f64) -> DockPlacement {
    DockPlacement {
        side: side.as_str(),
        strip_x: frame.strip_dx / css,
        strip_y: frame.strip_dy / css,
    }
}

/// A size in the space `set_size` should get it in (see `placement_position`).
pub(crate) fn placement_size(is_windows: bool, w: f64, h: f64) -> tauri::Size {
    if is_windows {
        PhysicalSize { width: w.round() as u32, height: h.round() as u32 }.into()
    } else {
        LogicalSize { width: w, height: h }.into()
    }
}

/// Size the floating window for the mini dock: the bare strip (`card_width`
/// 0) or the strip plus a card beside it. The strip's on-screen position is
/// kept (see `expanded_frame`); the card opens toward the roomier half of
/// the monitor the window is on. Sizes are CSS px from the frontend.
#[tauri::command]
pub(crate) fn dock_layout(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    strip_width: f64,
    strip_height: f64,
    card_width: f64,
    card_height: f64,
) -> Result<DockPlacement, String> {
    let window = app
        .get_webview_window(DECK_WINDOW)
        .ok_or_else(|| "dock_layout: floating window missing".to_string())?;
    let monitor = window
        .current_monitor()
        .ok()
        .flatten()
        .or_else(|| window.primary_monitor().ok().flatten())
        .ok_or_else(|| "dock_layout: no monitor".to_string())?;
    let is_windows = cfg!(windows);
    let monitor_scale = monitor.scale_factor();
    let window_scale = window.scale_factor().unwrap_or(monitor_scale);
    let units = placement_units(is_windows, monitor_scale, window_scale);
    let css = css_scale(is_windows, window_scale);
    let origin = placement_space_position(
        &window,
        window.outer_position().map_err(|e| format!("dock_layout: {e}"))?,
    );
    let area_raw = monitor.work_area();
    let div = usable_scale(units.screen_div);
    let area = Rect {
        x: area_raw.position.x as f64 / div,
        y: area_raw.position.y as f64 / div,
        w: area_raw.size.width as f64 / div,
        h: area_raw.size.height as f64 / div,
    };
    let frame;
    let side;
    {
        // Updated BEFORE the window moves, and released before any window
        // call: set_position may deliver `Moved` synchronously, and that
        // handler reads this offset.
        let mut offset = state.dock_offset.lock_or_recover();
        let (sx, sy) = strip_origin(origin, *offset);
        let strip = Rect {
            x: sx as f64,
            y: sy as f64,
            w: strip_width * css,
            h: strip_height * css,
        };
        side = expand_side(strip, area);
        let gap = if card_width > 0.0 { DOCK_CARD_GAP * css } else { 0.0 };
        frame = expanded_frame(strip, (card_width * css, card_height * css), gap, side, area);
        *offset = (frame.strip_dx, frame.strip_dy);
    }
    let _ = window.set_size(placement_size(is_windows, frame.window.w, frame.window.h));
    let _ = window.set_position(placement_position(is_windows, frame.window.x, frame.window.y));
    Ok(placement_reply(frame, side, css))
}

/// Put the window back on the strip's own origin and forget the offset (a
/// switch away from the dock must not leave an expanded window behind).
fn collapse_offset(window: &tauri::WebviewWindow, state: &AppState) {
    let offset = std::mem::replace(&mut *state.dock_offset.lock_or_recover(), (0.0, 0.0));
    if offset == (0.0, 0.0) {
        return;
    }
    if let Ok(pos) = window.outer_position() {
        let (x, y) = strip_origin(placement_space_position(window, pos), offset);
        let _ = window.set_position(placement_position(cfg!(windows), x as f64, y as f64));
    }
}

/// Re-read `[desktop].floating` and, when it changed, reload the floating
/// window's page with the new content (the settings editor calls this after
/// Apply, like `reload_deck_always_on_top`); when it is still the dock, tell
/// the dock to re-read its own `[desktop]` keys (`DOCK_PREFS_EVENT`). The init script is fixed at
/// window creation, so the choice rides through the reload in sessionStorage
/// (`deck_prefs::floating_reload_script`). An unreadable config changes nothing.
#[tauri::command]
pub(crate) fn reload_floating(app: tauri::AppHandle, state: tauri::State<'_, AppState>) {
    let Ok(text) = std::fs::read_to_string(default_config_path()) else {
        eprintln!("floating: config unreadable, leaving the window alone");
        return;
    };
    let target = deck_prefs::parse_floating(&text);
    let previous = std::mem::replace(&mut *state.floating.lock_or_recover(), target);
    match deck_prefs::floating_apply(previous, target) {
        deck_prefs::FloatingApply::Reload(target) => {
            if let Some(window) = app.get_webview_window(DECK_WINDOW) {
                collapse_offset(&window, &state);
                let _ = window.eval(&deck_prefs::floating_reload_script(target));
            }
        }
        deck_prefs::FloatingApply::RefreshDock => {
            let _ = app.emit_to(DECK_WINDOW, deck_prefs::DOCK_PREFS_EVENT, ());
        }
        deck_prefs::FloatingApply::Nothing => {}
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const AREA: Rect = Rect { x: 0.0, y: 25.0, w: 1440.0, h: 875.0 };

    #[test]
    fn expand_side_opens_toward_the_roomier_half() {
        let strip = |x: f64| Rect { x, y: 100.0, w: 56.0, h: 300.0 };
        assert_eq!(expand_side(strip(1360.0), AREA), Side::Left);
        assert_eq!(expand_side(strip(40.0), AREA), Side::Right);
        // A second monitor to the right: its own halves count, not the desk's.
        let second = Rect { x: 1440.0, y: 0.0, w: 1920.0, h: 1080.0 };
        assert_eq!(expand_side(strip(1500.0), second), Side::Right);
        assert_eq!(expand_side(strip(3300.0), second), Side::Left);
        assert_eq!(Side::Left.as_str(), "left");
        assert_eq!(Side::Right.as_str(), "right");
    }

    #[test]
    fn a_collapsed_frame_is_exactly_the_strip() {
        let strip = Rect { x: 1360.0, y: 120.0, w: 56.0, h: 300.0 };
        let f = expanded_frame(strip, (0.0, 0.0), 0.0, Side::Left, AREA);
        assert_eq!(f, DockFrame { window: strip, strip_dx: 0.0, strip_dy: 0.0 });
    }

    #[test]
    fn expanded_frame_keeps_the_strip_fixed_on_screen() {
        for strip in [
            Rect { x: 1360.0, y: 120.0, w: 56.0, h: 300.0 },
            Rect { x: 40.0, y: 600.0, w: 56.0, h: 200.0 },
            Rect { x: 700.0, y: 25.0, w: 56.0, h: 860.0 },
        ] {
            for side in [Side::Left, Side::Right] {
                let f = expanded_frame(strip, (300.0, 380.0), 8.0, side, AREA);
                assert_eq!(f.window.x + f.strip_dx, strip.x, "{side:?} {strip:?}");
                assert_eq!(f.window.y + f.strip_dy, strip.y, "{side:?} {strip:?}");
                assert_eq!(f.window.w, 56.0 + 8.0 + 300.0);
                assert_eq!(f.window.h, strip.h.max(380.0));
            }
        }
    }

    #[test]
    fn a_left_expansion_moves_the_window_not_the_strip() {
        let strip = Rect { x: 1360.0, y: 120.0, w: 56.0, h: 300.0 };
        let f = expanded_frame(strip, (300.0, 380.0), 8.0, Side::Left, AREA);
        assert_eq!(f.window, Rect { x: 1052.0, y: 120.0, w: 364.0, h: 380.0 });
        assert_eq!((f.strip_dx, f.strip_dy), (308.0, 0.0));
    }

    #[test]
    fn expanded_frame_lifts_a_window_that_would_leave_the_bottom() {
        let strip = Rect { x: 40.0, y: 600.0, w: 56.0, h: 200.0 };
        let f = expanded_frame(strip, (300.0, 380.0), 8.0, Side::Right, AREA);
        assert_eq!(f.window, Rect { x: 40.0, y: 520.0, w: 364.0, h: 380.0 });
        assert_eq!((f.strip_dx, f.strip_dy), (0.0, 80.0));
        // Taller than the work area: pinned to its top, never above it.
        let short = Rect { x: 0.0, y: 25.0, w: 1440.0, h: 300.0 };
        let g = expanded_frame(strip, (300.0, 380.0), 8.0, Side::Right, short);
        assert_eq!(g.window.y, 25.0);
        assert_eq!(g.window.y + g.strip_dy, strip.y);
    }

    #[test]
    fn strip_origin_undoes_the_expansion_offset() {
        assert_eq!(strip_origin((1052, 120), (308.0, 0.0)), (1360, 120));
        assert_eq!(strip_origin((40, 520), (0.0, 80.0)), (40, 600));
        assert_eq!(strip_origin((-1500, 10), (308.4, 0.0)), (-1192, 10));
        assert_eq!(strip_origin((5, 5), (0.0, 0.0)), (5, 5));
    }

    #[test]
    fn css_pixels_are_placement_units_off_windows() {
        assert_eq!(css_scale(false, 2.0), 1.0);
        assert_eq!(css_scale(true, 1.5), 1.5);
        assert_eq!(css_scale(true, 0.0), 1.0);
    }

    #[test]
    fn the_reply_names_the_side_and_the_strip_offset_in_css_pixels() {
        let strip = Rect { x: 1360.0, y: 120.0, w: 56.0, h: 300.0 };
        let f = expanded_frame(strip, (300.0, 380.0), 8.0, Side::Left, AREA);
        assert_eq!(
            placement_reply(f, Side::Left, 1.0),
            DockPlacement { side: "left", strip_x: 308.0, strip_y: 0.0 }
        );
        // Windows at 150%: placement is physical, CSS is not.
        let g = expanded_frame(
            Rect { x: 2040.0, y: 180.0, w: 84.0, h: 450.0 },
            (450.0, 570.0),
            12.0,
            Side::Left,
            Rect { x: 0.0, y: 0.0, w: 2160.0, h: 1300.0 },
        );
        assert_eq!(
            placement_reply(g, Side::Left, 1.5),
            DockPlacement { side: "left", strip_x: 308.0, strip_y: 0.0 }
        );
        let json = serde_json::to_value(placement_reply(f, Side::Right, 1.0)).unwrap();
        assert_eq!(json, serde_json::json!({"side": "right", "stripX": 308.0, "stripY": 0.0}));
    }
}
