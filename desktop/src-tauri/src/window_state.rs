//! Which windows were open last time, and where the deck sat.
//!
//! This is UI state, not user configuration: it changes on every show/hide, so
//! it lives beside `runtime.json` in the cache dir rather than in `config.toml`,
//! which the user edits by hand and the sidecar rewrites transactionally.

use std::fs;
use std::path::{Path, PathBuf};

const FILE: &str = "window-state.json";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WindowState {
    pub app_visible: bool,
    pub deck_visible: bool,
    pub deck_position: Option<(i32, i32)>,
}

impl Default for WindowState {
    fn default() -> Self {
        Self { app_visible: true, deck_visible: false, deck_position: None }
    }
}

/// What to show at startup. `stored` wins; the legacy `window_mode` is the
/// fallback for the one launch after an upgrade (see the design doc's migration
/// table). Never returns an all-hidden state: a launch with nothing but a tray
/// icon reads as "the app failed to start".
pub fn startup_state(stored: Option<WindowState>, legacy_mode: Option<&str>) -> WindowState {
    let mut state = stored.unwrap_or_else(|| match legacy_mode {
        Some("floating") | Some("always_on_top") => {
            WindowState { app_visible: false, deck_visible: true, deck_position: None }
        }
        _ => WindowState::default(),
    });
    if !state.app_visible && !state.deck_visible {
        state.app_visible = true;
    }
    state
}

/// Directory the window-state file lives in: the same cache dir `runtime.json`
/// uses (see `sidecar::runtime_file_path`), so the two sit side by side and both
/// honor `HERDECK_RUNTIME_DIR`.
pub fn state_dir() -> PathBuf {
    crate::sidecar::runtime_file_path()
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."))
}

/// Best-effort read. A missing, unreadable or corrupt file is "no state" — the
/// caller then falls back to the legacy mode, which is the correct behaviour for
/// a first run as well.
pub fn load(dir: &Path) -> Option<WindowState> {
    let raw = fs::read_to_string(dir.join(FILE)).ok()?;
    let v: serde_json::Value = serde_json::from_str(&raw).ok()?;
    Some(WindowState {
        app_visible: v.get("app_visible")?.as_bool()?,
        deck_visible: v.get("deck_visible")?.as_bool()?,
        // `try_from` rejects out-of-range coordinates instead of wrapping them
        // into a plausible-looking (but wrong) position.
        deck_position: v.get("deck_position").and_then(|p| {
            Some((
                i32::try_from(p.get(0)?.as_i64()?).ok()?,
                i32::try_from(p.get(1)?.as_i64()?).ok()?,
            ))
        }),
    })
}

/// Best-effort write. Losing window visibility to a failed write is not worth an
/// error path: the next launch falls back to the default, which is visible.
pub fn store(dir: &Path, s: &WindowState) {
    let _ = fs::create_dir_all(dir);
    let mut body = serde_json::json!({
        "app_visible": s.app_visible,
        "deck_visible": s.deck_visible,
    });
    if let Some((x, y)) = s.deck_position {
        body["deck_position"] = serde_json::json!([x, y]);
    }
    let _ = fs::write(dir.join(FILE), body.to_string());
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        // Dependency-free temp dir keyed by the (unique) test name — matches the
        // sidecar.rs / window_mode.rs idiom, purged first so leftovers from a
        // previous run can't leak into an assertion.
        let p = std::env::temp_dir().join(format!("herdeck-ws-{name}"));
        let _ = std::fs::remove_dir_all(&p);
        std::fs::create_dir_all(&p).unwrap();
        p
    }

    // The migration table in the design doc, one row per test.
    #[test]
    fn legacy_normal_opens_the_app_and_hides_the_deck() {
        let s = startup_state(None, Some("normal"));
        assert_eq!((s.app_visible, s.deck_visible), (true, false));
    }

    #[test]
    fn legacy_floating_modes_open_the_deck_only() {
        for mode in ["floating", "always_on_top"] {
            let s = startup_state(None, Some(mode));
            assert_eq!((s.app_visible, s.deck_visible), (false, true), "mode {mode}");
        }
    }

    #[test]
    fn no_legacy_key_opens_the_app() {
        let s = startup_state(None, None);
        assert_eq!((s.app_visible, s.deck_visible), (true, false));
        let junk = startup_state(None, Some("sideways"));
        assert_eq!((junk.app_visible, junk.deck_visible), (true, false));
    }

    // Stored state wins: the legacy key is a fallback, consulted once.
    #[test]
    fn stored_state_outranks_the_legacy_mode() {
        let stored = WindowState { app_visible: true, deck_visible: true, deck_position: Some((10, 20)) };
        let s = startup_state(Some(stored), Some("normal"));
        assert_eq!((s.app_visible, s.deck_visible), (true, true));
        assert_eq!(s.deck_position, Some((10, 20)));
    }

    // A launch that paints nothing but a tray icon is the invisible-window
    // defect again (see a5f5e42). The app window is the floor.
    #[test]
    fn a_state_with_everything_hidden_still_shows_the_app() {
        let stored = WindowState { app_visible: false, deck_visible: false, deck_position: None };
        let s = startup_state(Some(stored), None);
        assert!(s.app_visible);
        assert!(!s.deck_visible);
    }

    #[test]
    fn state_survives_a_round_trip_through_disk() {
        let dir = scratch("round-trip");
        let s = WindowState { app_visible: false, deck_visible: true, deck_position: Some((-344, 256)) };
        store(&dir, &s);
        assert_eq!(load(&dir), Some(s));
    }

    // Every state written before the deck is first dragged looks like this —
    // the key-absent path must round-trip, not just the key-present one.
    #[test]
    fn no_deck_position_round_trips_as_none() {
        let dir = scratch("no-position");
        let s = WindowState { app_visible: true, deck_visible: false, deck_position: None };
        store(&dir, &s);
        assert_eq!(load(&dir), Some(s));
    }

    #[test]
    fn unreadable_or_corrupt_state_is_no_state() {
        let corrupt = scratch("corrupt");
        std::fs::write(corrupt.join("window-state.json"), "{ not json").unwrap();
        assert_eq!(load(&corrupt), None);

        // A directory where a file is expected makes `read_to_string` fail —
        // the "unreadable" half of the name.
        let unreadable = scratch("unreadable");
        std::fs::create_dir_all(unreadable.join("window-state.json")).unwrap();
        assert_eq!(load(&unreadable), None);
    }

    // The doc comment on `load` promises "missing, unreadable or corrupt" is
    // all "no state" — this is the missing-file leg, the one every launch
    // right after an upgrade actually takes.
    #[test]
    fn missing_state_is_no_state() {
        let dir = scratch("missing");
        assert_eq!(load(&dir), None);
    }

    #[test]
    fn malformed_deck_position_degrades_to_none_but_keeps_the_bools() {
        let dir = scratch("malformed-position");
        std::fs::write(
            dir.join("window-state.json"),
            r#"{"app_visible":true,"deck_visible":false,"deck_position":[1]}"#,
        )
        .unwrap();
        let s = load(&dir).unwrap();
        assert_eq!((s.app_visible, s.deck_visible), (true, false));
        assert_eq!(s.deck_position, None);
    }

    // A coordinate outside i32's range must be rejected, not silently wrapped
    // into a plausible-looking position on the wrong side of the screen. Covers
    // both components and both directions of overflow — each is a separate
    // `try_from` call site that a refactor could revert to `as i32` alone.
    #[test]
    fn out_of_range_deck_position_is_rejected_not_wrapped() {
        for (i, shape) in [
            r#"[4294967296,0]"#,
            r#"[0,4294967296]"#,
            r#"[-4294967296,0]"#,
        ]
        .into_iter()
        .enumerate()
        {
            let dir = scratch(&format!("oob-position-{i}"));
            std::fs::write(
                dir.join("window-state.json"),
                format!(r#"{{"app_visible":true,"deck_visible":false,"deck_position":{shape}}}"#),
            )
            .unwrap();
            let s = load(&dir).unwrap();
            assert_eq!(s.deck_position, None, "shape {shape}");
        }
    }
}
