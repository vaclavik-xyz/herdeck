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
        deck_position: v.get("deck_position").and_then(|p| {
            Some((p.get(0)?.as_i64()? as i32, p.get(1)?.as_i64()? as i32))
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
        let dir = std::env::temp_dir().join(format!("herdeck-ws-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let s = WindowState { app_visible: false, deck_visible: true, deck_position: Some((-344, 256)) };
        store(&dir, &s);
        assert_eq!(load(&dir), Some(s));
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn unreadable_or_corrupt_state_is_no_state() {
        let dir = std::env::temp_dir().join(format!("herdeck-ws-bad-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("window-state.json"), "{ not json").unwrap();
        assert_eq!(load(&dir), None);
        std::fs::remove_dir_all(&dir).ok();
    }
}
