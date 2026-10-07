//! The mini dock's window geometry. The floating window grows beside the
//! strip while a card is open and shrinks back when it closes, without the
//! strip itself moving on screen. Pure helpers (unit-tested) live here; the
//! `dock_layout` / `reload_floating` commands below them do the window calls.
//!
//! All rects are in the PLACEMENT space of `window_roles` (logical points on
//! macOS/Linux, global physical pixels on Windows); `css_scale` converts the
//! frontend's CSS pixels into it.

use crate::window_roles::usable_scale;

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
}
