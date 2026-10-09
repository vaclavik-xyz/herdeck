//! The mini dock's window geometry. The floating window grows beside the
//! strip while a card is open and shrinks back when it closes, without the
//! strip itself moving on screen. Pure helpers (unit-tested) live here; the
//! `dock_layout` / `reload_floating` commands below them do the window calls.
//!
//! All rects are in the PLACEMENT space of `window_roles` (logical points on
//! macOS/Linux, global physical pixels on Windows); `css_scale` converts the
//! frontend's CSS pixels into it.

use std::sync::atomic::Ordering;
use std::time::Instant;

use tauri::{Emitter, LogicalSize, Manager, PhysicalSize};

use crate::deck_prefs;
use crate::runtime_plan::default_config_path;
use crate::sync_util::LockExt;
use crate::window_roles::{
    placement_position, placement_space_position, placement_units, remember_deck_position, usable_scale,
    DECK_WINDOW,
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

/// A strip dropped this close to a work-area edge (CSS px) snaps flush to it.
pub(crate) const DOCK_SNAP_DISTANCE: f64 = 24.0;

/// A strip this close to an edge (placement units) counts as snapped there:
/// positions are whole units, a zoomed strip's width need not be.
const FLUSH_TOLERANCE: f64 = 1.0;

/// The left or right work-area edge a strip is snapped to.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Edge {
    Left,
    Right,
}

impl Edge {
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Edge::Left => "left",
            Edge::Right => "right",
        }
    }
}

/// The strip's x after a drop: flush to the left or right work-area edge when
/// it lies within `distance` of it (or past it), else `None`. The nearer
/// edge wins.
pub(crate) fn snap_x(strip: Rect, area: Rect, distance: f64) -> Option<f64> {
    let left_gap = strip.x - area.x;
    let right = area.x + area.w;
    let right_gap = right - (strip.x + strip.w);
    if left_gap <= distance && left_gap <= right_gap {
        Some(area.x)
    } else if right_gap <= distance {
        Some(right - strip.w)
    } else {
        None
    }
}

/// The edge a strip sits flush against, if any (autohide only works there).
pub(crate) fn snapped_edge(strip: Rect, area: Rect) -> Option<Edge> {
    if (strip.x - area.x).abs() <= FLUSH_TOLERANCE {
        Some(Edge::Left)
    } else if (area.x + area.w - (strip.x + strip.w)).abs() <= FLUSH_TOLERANCE {
        Some(Edge::Right)
    } else {
        None
    }
}

/// Where the strip is drawn: at its remembered origin, or lifted just enough
/// that it does not run off the work area's bottom (a group expanded near
/// the bottom), but never above the work area's top. Only drawn there: the
/// remembered origin stays, so the strip drops back once the group folds.
pub(crate) fn drawn_strip(strip: Rect, area: Rect) -> Rect {
    let bottom = area.y + area.h;
    let y = strip.y.min((bottom - strip.h).max(area.y)).min(strip.y);
    Rect { y, ..strip }
}

/// The hidden dock: a window of `width` at the snapped edge, as tall as the
/// strip. The strip keeps its on-screen place (drawn outside the window).
pub(crate) fn sliver_frame(strip: Rect, edge: Edge, width: f64) -> DockFrame {
    let x = match edge {
        Edge::Left => strip.x,
        Edge::Right => strip.x + strip.w - width,
    };
    DockFrame {
        window: Rect { x, y: strip.y, w: width, h: strip.h },
        strip_dx: strip.x - x,
        strip_dy: 0.0,
    }
}

/// What `dock_layout` does with the window, from the REMEMBERED strip rect.
#[derive(Debug, Clone, Copy, PartialEq)]
pub(crate) struct FramePlan {
    pub(crate) frame: DockFrame,
    pub(crate) side: Side,
    pub(crate) edge: Option<Edge>,
    /// Remembered strip origin − window origin (`DockGeom::offset`).
    pub(crate) offset: (f64, f64),
    /// How far the strip is drawn above its remembered origin.
    pub(crate) lift: f64,
}

/// The window frame for the strip alone, the strip with a card (`card`, with
/// `gap` between them), or the autohide sliver (`sliver` > 0, only when the
/// strip is snapped and no card is open).
pub(crate) fn plan_frame(strip: Rect, area: Rect, card: Option<(f64, f64)>, sliver: f64, gap: f64) -> FramePlan {
    let drawn = drawn_strip(strip, area);
    let side = expand_side(drawn, area);
    let edge = snapped_edge(drawn, area);
    let frame = match (card, edge) {
        (Some(card), _) if card.0 > 0.0 => expanded_frame(drawn, card, gap, side, area),
        (_, Some(edge)) if sliver > 0.0 => sliver_frame(drawn, edge, sliver),
        _ => expanded_frame(drawn, (0.0, 0.0), 0.0, side, area),
    };
    let lift = strip.y - drawn.y;
    FramePlan {
        frame,
        side,
        edge,
        offset: (frame.strip_dx, frame.strip_dy + lift),
        lift,
    }
}

/// The dock window's geometry state (in `AppState`), in placement units.
#[derive(Debug, Clone, Copy, PartialEq, Default)]
pub(crate) struct DockGeom {
    /// Remembered strip origin − window origin: (0, 0) except while a card,
    /// the sliver or a lift moves the window off the strip's own origin.
    /// `Moved` adds it back so the remembered position is the strip's own.
    pub(crate) offset: (f64, f64),
    /// The strip is drawn this far above its remembered origin.
    pub(crate) lift: f64,
    /// The strip's last size (w, h); (0, 0) until the dock first laid out.
    pub(crate) strip: (f64, f64),
    /// The remembered strip origin as last settled: a settle that finds it
    /// unchanged was our own move (a card, the sliver), not a drop.
    pub(crate) settled: Option<(i32, i32)>,
}

/// A user drop, settled: move the window by `dx` (the snap) and remember the
/// strip at `remembered`.
#[derive(Debug, Clone, Copy, PartialEq)]
pub(crate) struct Settled {
    pub(crate) dx: f64,
    pub(crate) remembered: (i32, i32),
}

/// After the window stopped moving: when the strip's remembered origin
/// changed (the user dropped it), keep it where it was dropped (a lift ends
/// there) and snap it to a work-area edge within `distance`.
pub(crate) fn settle(geom: &mut DockGeom, window: (i32, i32), area: Rect, distance: f64) -> Option<Settled> {
    let (w, h) = geom.strip;
    if w <= 0.0 {
        return None;
    }
    let remembered = strip_origin(window, geom.offset);
    if geom.settled == Some(remembered) {
        return None;
    }
    // The strip the user saw while dragging is the drawn one.
    geom.offset.1 -= geom.lift;
    geom.lift = 0.0;
    let dropped = strip_origin(window, geom.offset);
    let drawn = Rect { x: dropped.0 as f64, y: dropped.1 as f64, w, h };
    let dx = snap_x(drawn, area, distance).map_or(0.0, |x| x - drawn.x);
    let remembered = strip_origin(window, (geom.offset.0 + dx, geom.offset.1));
    geom.settled = Some(remembered);
    Some(Settled { dx, remembered })
}

/// The window counts as dropped once it has not moved for this long…
pub(crate) const SETTLE_QUIET: std::time::Duration = std::time::Duration::from_millis(160);
const SETTLE_POLL: std::time::Duration = std::time::Duration::from_millis(50);

/// …and the mouse button is up: a pause mid-drag never snaps.
pub(crate) fn settle_due(since_move: std::time::Duration, button_down: bool) -> bool {
    since_move >= SETTLE_QUIET && !button_down
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
    /// The work-area edge the strip is snapped to ("left" / "right"), or
    /// null: autohide only works at an edge.
    pub(crate) edge: Option<&'static str>,
}

pub(crate) fn placement_reply(frame: DockFrame, side: Side, css: f64, edge: Option<Edge>) -> DockPlacement {
    DockPlacement {
        side: side.as_str(),
        strip_x: frame.strip_dx / css,
        strip_y: frame.strip_dy / css,
        edge: edge.map(Edge::as_str),
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

/// The floating window's monitor work area in placement units, and the
/// CSS px → placement factor.
fn dock_space(window: &tauri::WebviewWindow) -> Result<(Rect, f64), String> {
    let monitor = window
        .current_monitor()
        .ok()
        .flatten()
        .or_else(|| window.primary_monitor().ok().flatten())
        .ok_or_else(|| "dock: no monitor".to_string())?;
    let is_windows = cfg!(windows);
    let monitor_scale = monitor.scale_factor();
    let window_scale = window.scale_factor().unwrap_or(monitor_scale);
    let units = placement_units(is_windows, monitor_scale, window_scale);
    let area_raw = monitor.work_area();
    let div = usable_scale(units.screen_div);
    let area = Rect {
        x: area_raw.position.x as f64 / div,
        y: area_raw.position.y as f64 / div,
        w: area_raw.size.width as f64 / div,
        h: area_raw.size.height as f64 / div,
    };
    Ok((area, css_scale(is_windows, window_scale)))
}

/// Size the floating window for the mini dock: the bare strip (`card_width`
/// 0), the strip plus a card beside it, or — `sliver_width` > 0, autohide —
/// a thin sliver at the edge the strip is snapped to. The strip's on-screen
/// position is kept (see `expanded_frame`); it is only drawn lifted while it
/// would run off the bottom (`drawn_strip`). The card opens toward the
/// roomier half of the monitor the window is on. Sizes are CSS px from the
/// frontend (already multiplied by the dock's zoom).
#[tauri::command]
#[allow(clippy::too_many_arguments)]
pub(crate) fn dock_layout(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    strip_width: f64,
    strip_height: f64,
    card_width: f64,
    card_height: f64,
    sliver_width: Option<f64>,
) -> Result<DockPlacement, String> {
    let window = app
        .get_webview_window(DECK_WINDOW)
        .ok_or_else(|| "dock_layout: floating window missing".to_string())?;
    let is_windows = cfg!(windows);
    let (area, css) = dock_space(&window).map_err(|e| format!("dock_layout: {e}"))?;
    let origin = placement_space_position(
        &window,
        window.outer_position().map_err(|e| format!("dock_layout: {e}"))?,
    );
    let plan;
    {
        // Updated BEFORE the window moves, and released before any window
        // call: set_position may deliver `Moved` synchronously, and that
        // handler reads this offset.
        let mut geom = state.dock_geom.lock_or_recover();
        let (sx, sy) = strip_origin(origin, geom.offset);
        let strip = Rect {
            x: sx as f64,
            y: sy as f64,
            w: strip_width * css,
            h: strip_height * css,
        };
        let card = (card_width > 0.0).then_some((card_width * css, card_height * css));
        let sliver = sliver_width.unwrap_or(0.0).max(0.0) * css;
        plan = plan_frame(strip, area, card, sliver, DOCK_CARD_GAP * css);
        geom.offset = plan.offset;
        geom.lift = plan.lift;
        geom.strip = (strip.w, strip.h);
        // `settled` is left alone: a layout landing between a drop and its
        // settle must not swallow the snap (the remembered origin it keeps is
        // the dropped one, which settle has not seen yet).
    }
    let frame = plan.frame;
    let _ = window.set_size(placement_size(is_windows, frame.window.w, frame.window.h));
    let _ = window.set_position(placement_position(is_windows, frame.window.x, frame.window.y));
    Ok(placement_reply(frame, plan.side, css, plan.edge))
}

/// Rust -> dock window: a drag ended (and the strip may have snapped to an
/// edge); the dock lays itself out again to learn the new edge.
pub(crate) const DOCK_MOVED_EVENT: &str = "dock-moved";

/// The left mouse button is held (a drag still in progress). macOS asks
/// CoreGraphics (thread-safe, already linked by the windowing stack);
/// elsewhere the quiet spell alone ends a drag.
fn mouse_button_down() -> bool {
    #[cfg(target_os = "macos")]
    {
        #[link(name = "CoreGraphics", kind = "framework")]
        extern "C" {
            fn CGEventSourceButtonState(state_id: i32, button: u32) -> bool;
        }
        // kCGEventSourceStateCombinedSessionState, kCGMouseButtonLeft.
        unsafe { CGEventSourceButtonState(0, 0) }
    }
    #[cfg(not(target_os = "macos"))]
    {
        false
    }
}

/// The dock window moved (`Moved`, many times per drag): once it has been
/// still for `SETTLE_QUIET` with the button up, settle the drop on the main
/// thread. One watcher thread per drag, not one per event.
pub(crate) fn note_dock_moved(app: &tauri::AppHandle) {
    let Some(state) = app.try_state::<AppState>() else {
        return;
    };
    *state.dock_moved_at.lock_or_recover() = Instant::now();
    if state.dock_settling.swap(true, Ordering::SeqCst) {
        return;
    }
    let app = app.clone();
    std::thread::spawn(move || {
        let state = app.state::<AppState>();
        loop {
            std::thread::sleep(SETTLE_POLL);
            let since = state.dock_moved_at.lock_or_recover().elapsed();
            if !settle_due(since, mouse_button_down()) {
                continue;
            }
            state.dock_settling.store(false, Ordering::SeqCst);
            // A move that slipped in after the check saw the flag still set
            // and left: keep watching for it unless another watcher took over.
            if state.dock_moved_at.lock_or_recover().elapsed() < SETTLE_QUIET {
                if state.dock_settling.swap(true, Ordering::SeqCst) {
                    return;
                }
                continue;
            }
            let handle = app.clone();
            let _ = app.run_on_main_thread(move || settle_dock(&handle));
            return;
        }
    });
}

/// Settle a drop: snap the strip to a near work-area edge, remember where it
/// ended up and tell the dock (`DOCK_MOVED_EVENT`). Our own moves (a card,
/// the sliver) leave the remembered origin alone and do nothing here.
fn settle_dock(app: &tauri::AppHandle) {
    let state = app.state::<AppState>();
    if *state.floating.lock_or_recover() != deck_prefs::Floating::Dock {
        return;
    }
    let Some(window) = app.get_webview_window(DECK_WINDOW) else {
        return;
    };
    if !window.is_visible().unwrap_or(false) {
        return;
    }
    let Ok((area, css)) = dock_space(&window) else {
        return;
    };
    let Ok(pos) = window.outer_position() else {
        return;
    };
    let origin = placement_space_position(&window, pos);
    let settled = settle(&mut state.dock_geom.lock_or_recover(), origin, area, DOCK_SNAP_DISTANCE * css);
    let Some(settled) = settled else {
        return;
    };
    if settled.dx != 0.0 {
        let _ = window.set_position(placement_position(
            cfg!(windows),
            origin.0 as f64 + settled.dx,
            origin.1 as f64,
        ));
    }
    remember_deck_position(app, settled.remembered);
    let _ = app.emit_to(DECK_WINDOW, DOCK_MOVED_EVENT, ());
}

/// Put the window back on the strip's own origin and forget the offset (a
/// switch away from the dock must not leave an expanded window behind).
fn collapse_offset(window: &tauri::WebviewWindow, state: &AppState) {
    let geom = std::mem::take(&mut *state.dock_geom.lock_or_recover());
    if geom.offset == (0.0, 0.0) {
        return;
    }
    if let Ok(pos) = window.outer_position() {
        let (x, y) = strip_origin(placement_space_position(window, pos), geom.offset);
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
            placement_reply(f, Side::Left, 1.0, None),
            DockPlacement { side: "left", strip_x: 308.0, strip_y: 0.0, edge: None }
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
            placement_reply(g, Side::Left, 1.5, None),
            DockPlacement { side: "left", strip_x: 308.0, strip_y: 0.0, edge: None }
        );
        let json = serde_json::to_value(placement_reply(f, Side::Right, 1.0, None)).unwrap();
        assert_eq!(json, serde_json::json!({"side": "right", "stripX": 308.0, "stripY": 0.0, "edge": null}));
    }

    #[test]
    fn a_drop_near_an_edge_snaps_flush_to_it() {
        let strip = |x: f64| Rect { x, y: 200.0, w: 56.0, h: 300.0 };
        // Left: within 24 px (and beyond the edge) snaps to the area's x.
        assert_eq!(snap_x(strip(24.0), AREA, 24.0), Some(0.0));
        assert_eq!(snap_x(strip(3.0), AREA, 24.0), Some(0.0));
        assert_eq!(snap_x(strip(-20.0), AREA, 24.0), Some(0.0));
        assert_eq!(snap_x(strip(24.5), AREA, 24.0), None);
        // Right: the strip's right side within 24 px of the area's right.
        assert_eq!(snap_x(strip(1440.0 - 56.0 - 24.0), AREA, 24.0), Some(1384.0));
        assert_eq!(snap_x(strip(1400.0), AREA, 24.0), Some(1384.0));
        assert_eq!(snap_x(strip(1300.0), AREA, 24.0), None);
        // A second monitor: its own edges, not the desk's.
        let second = Rect { x: 1440.0, y: 0.0, w: 1920.0, h: 1080.0 };
        assert_eq!(snap_x(strip(1450.0), second, 24.0), Some(1440.0));
        assert_eq!(snap_x(strip(3290.0), second, 24.0), Some(3304.0));
        // Zoomed strip (width 56 x 1.25 = 70): flush means its own width.
        let zoomed = Rect { x: 1360.0, y: 200.0, w: 70.0, h: 300.0 };
        assert_eq!(snap_x(zoomed, AREA, 24.0), Some(1370.0));
    }

    #[test]
    fn the_snapped_edge_is_read_from_the_geometry() {
        let strip = |x: f64| Rect { x, y: 200.0, w: 56.0, h: 300.0 };
        assert_eq!(snapped_edge(strip(0.0), AREA), Some(Edge::Left));
        assert_eq!(snapped_edge(strip(0.6), AREA), Some(Edge::Left)); // rounding
        assert_eq!(snapped_edge(strip(1384.0), AREA), Some(Edge::Right));
        assert_eq!(snapped_edge(strip(1383.4), AREA), Some(Edge::Right));
        assert_eq!(snapped_edge(strip(4.0), AREA), None);
        assert_eq!(snapped_edge(strip(700.0), AREA), None);
        assert_eq!(Edge::Left.as_str(), "left");
        assert_eq!(Edge::Right.as_str(), "right");
    }

    #[test]
    fn the_sliver_sits_at_the_edge_and_the_strip_stays_put() {
        let left = Rect { x: 0.0, y: 200.0, w: 56.0, h: 300.0 };
        let f = sliver_frame(left, Edge::Left, 6.0);
        assert_eq!(f.window, Rect { x: 0.0, y: 200.0, w: 6.0, h: 300.0 });
        assert_eq!((f.strip_dx, f.strip_dy), (0.0, 0.0));
        let right = Rect { x: 1384.0, y: 200.0, w: 56.0, h: 300.0 };
        let g = sliver_frame(right, Edge::Right, 6.0);
        assert_eq!(g.window, Rect { x: 1434.0, y: 200.0, w: 6.0, h: 300.0 });
        // The strip is drawn where it really is: off the window's left side.
        assert_eq!(g.window.x + g.strip_dx, right.x);
        assert_eq!((g.strip_dx, g.strip_dy), (-50.0, 0.0));
    }

    #[test]
    fn a_strip_that_runs_off_the_bottom_is_drawn_lifted() {
        // A group expanded near the bottom: 200 + 760 > 900.
        let tall = Rect { x: 40.0, y: 200.0, w: 56.0, h: 760.0 };
        assert_eq!(drawn_strip(tall, AREA), Rect { y: 140.0, ..tall });
        // Fits: untouched.
        let short = Rect { x: 40.0, y: 200.0, w: 56.0, h: 300.0 };
        assert_eq!(drawn_strip(short, AREA), short);
        // Taller than the area: pinned to its top, never above it.
        let huge = Rect { x: 40.0, y: 200.0, w: 56.0, h: 1200.0 };
        assert_eq!(drawn_strip(huge, AREA).y, 25.0);
        // Above the top already (never pushed down: only the bottom is guarded).
        let high = Rect { x: 40.0, y: 10.0, w: 56.0, h: 1200.0 };
        assert_eq!(drawn_strip(high, AREA), high);
    }

    #[test]
    fn plan_lifts_a_groups_window_without_moving_the_remembered_strip() {
        let tall = Rect { x: 40.0, y: 200.0, w: 56.0, h: 760.0 };
        let p = plan_frame(tall, AREA, None, 0.0, 0.0);
        assert_eq!(p.frame.window, Rect { x: 40.0, y: 140.0, w: 56.0, h: 760.0 });
        assert_eq!((p.frame.strip_dx, p.frame.strip_dy), (0.0, 0.0));
        // Remembered = window + offset = the unlifted strip.
        assert_eq!(p.offset, (0.0, 60.0));
        assert_eq!(p.lift, 60.0);
        assert_eq!(p.edge, None);
    }

    #[test]
    fn plan_hides_into_a_sliver_only_when_snapped_and_no_card_is_open() {
        let snapped = Rect { x: 1384.0, y: 200.0, w: 56.0, h: 300.0 };
        let p = plan_frame(snapped, AREA, None, 6.0, 0.0);
        assert_eq!(p.edge, Some(Edge::Right));
        assert_eq!(p.frame.window.w, 6.0);
        assert_eq!(p.offset, (-50.0, 0.0));
        // A card wins over the sliver.
        let q = plan_frame(snapped, AREA, Some((300.0, 380.0)), 6.0, 8.0);
        assert_eq!(q.frame.window.w, 56.0 + 8.0 + 300.0);
        assert_eq!(q.side, Side::Left);
        // Not snapped: no sliver.
        let loose = Rect { x: 700.0, y: 200.0, w: 56.0, h: 300.0 };
        let r = plan_frame(loose, AREA, None, 6.0, 0.0);
        assert_eq!(r.edge, None);
        assert_eq!(r.frame.window, loose);
    }

    #[test]
    fn settle_snaps_a_dropped_strip_once() {
        let mut g = DockGeom { strip: (56.0, 300.0), ..DockGeom::default() };
        // Dropped 10 px from the right edge.
        let s = settle(&mut g, (1374, 200), AREA, 24.0).expect("a user drop");
        assert_eq!(s, Settled { dx: 10.0, remembered: (1384, 200) });
        // The window then moves by dx: nothing left to do.
        assert_eq!(settle(&mut g, (1384, 200), AREA, 24.0), None);
        // Dropped in the middle: remembered, not snapped.
        let t = settle(&mut g, (700, 300), AREA, 24.0).expect("a user drop");
        assert_eq!(t, Settled { dx: 0.0, remembered: (700, 300) });
        assert_eq!(settle(&mut g, (700, 300), AREA, 24.0), None);
    }

    #[test]
    fn a_layout_between_a_drop_and_its_settle_does_not_swallow_the_snap() {
        let mut g = DockGeom { strip: (56.0, 300.0), settled: Some((700, 300)), ..DockGeom::default() };
        // Dropped 10 px from the right edge; a card opens before the watcher
        // fires: the window moves left, the offset follows (as dock_layout does).
        let strip = Rect { x: 1374.0, y: 200.0, w: 56.0, h: 300.0 };
        let p = plan_frame(strip, AREA, Some((300.0, 380.0)), 0.0, 8.0);
        g.offset = p.offset;
        g.lift = p.lift;
        let window = (p.frame.window.x as i32, p.frame.window.y as i32);
        let s = settle(&mut g, window, AREA, 24.0).expect("still a drop");
        assert_eq!(s, Settled { dx: 10.0, remembered: (1384, 200) });
    }

    #[test]
    fn settle_ignores_an_unmeasured_strip() {
        let mut g = DockGeom::default();
        assert_eq!(settle(&mut g, (3, 200), AREA, 24.0), None);
    }

    #[test]
    fn settle_after_a_drag_while_lifted_keeps_where_the_strip_was_dropped() {
        // A group was open near the bottom: drawn 60 above the remembered y.
        let mut g = DockGeom {
            offset: (0.0, 60.0),
            lift: 60.0,
            strip: (56.0, 760.0),
            settled: Some((40, 200)),
        };
        // The user dragged the window up by 100: it is now at y 40.
        let s = settle(&mut g, (500, 40), AREA, 24.0).expect("a user drop");
        // The strip is where it was dropped (the window's origin), not 60 below.
        assert_eq!(s.remembered, (500, 40));
        assert_eq!((g.offset, g.lift), ((0.0, 0.0), 0.0));
    }

    #[test]
    fn settle_is_due_after_a_quiet_spell_with_the_button_up() {
        let quiet = SETTLE_QUIET;
        assert!(!settle_due(quiet / 2, false));
        assert!(!settle_due(quiet, true)); // still holding: mid-drag pause
        assert!(settle_due(quiet, false));
    }

    #[test]
    fn the_reply_names_the_snapped_edge() {
        let snapped = Rect { x: 0.0, y: 200.0, w: 56.0, h: 300.0 };
        let p = plan_frame(snapped, AREA, None, 0.0, 0.0);
        let json = serde_json::to_value(placement_reply(p.frame, p.side, 1.0, p.edge)).unwrap();
        assert_eq!(json, serde_json::json!({"side": "right", "stripX": 0.0, "stripY": 0.0, "edge": "left"}));
        let loose = plan_frame(Rect { x: 700.0, ..snapped }, AREA, None, 0.0, 0.0);
        let json = serde_json::to_value(placement_reply(loose.frame, loose.side, 1.0, None)).unwrap();
        assert_eq!(json["edge"], serde_json::Value::Null);
    }
}
