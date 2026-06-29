//! Deck-toggle global-hotkey accelerator extraction (pure; no Tauri deps so it
//! is unit-testable). Mirrors the spec semantics: an ABSENT `base.hotkeys.
//! toggle_deck` key falls back to the default accelerator; an explicit empty
//! (or whitespace-only) string DISABLES the hotkey (returns None).

use serde_json::Value;

/// Cross-platform default: `CmdOrCtrl` maps to Cmd on macOS, Ctrl elsewhere.
pub const DEFAULT_TOGGLE_DECK: &str = "CmdOrCtrl+Shift+D";

/// The accelerator to register for the deck toggle, or `None` to leave the
/// hotkey unregistered (the user cleared the field).
pub fn toggle_deck_accelerator(config: &Value) -> Option<String> {
    match config.pointer("/base/hotkeys/toggle_deck") {
        Some(Value::String(s)) if s.trim().is_empty() => None,
        Some(Value::String(s)) => Some(s.clone()),
        _ => Some(DEFAULT_TOGGLE_DECK.to_string()),
    }
}
