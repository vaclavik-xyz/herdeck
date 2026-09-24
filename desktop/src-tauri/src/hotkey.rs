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

/// The accelerator for the "jump to the next blocked agent" hotkey, or `None`
/// when it is not configured. Unlike the deck toggle this one is OPT-IN: an
/// absent `base.hotkeys.next_blocked` key, a non-string, or an empty string
/// all leave it unregistered (no default chord is taken from other apps).
pub fn next_blocked_accelerator(config: &Value) -> Option<String> {
    match config.pointer("/base/hotkeys/next_blocked") {
        Some(Value::String(s)) if !s.trim().is_empty() => Some(s.trim().to_string()),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn next_blocked_is_opt_in() {
        assert_eq!(next_blocked_accelerator(&json!({})), None);
        assert_eq!(next_blocked_accelerator(&json!({ "base": { "hotkeys": {} } })), None);
        assert_eq!(
            next_blocked_accelerator(&json!({ "base": { "hotkeys": { "next_blocked": "  " } } })),
            None
        );
        assert_eq!(
            next_blocked_accelerator(&json!({ "base": { "hotkeys": { "next_blocked": 3 } } })),
            None
        );
    }

    #[test]
    fn next_blocked_returns_the_configured_chord() {
        assert_eq!(
            next_blocked_accelerator(
                &json!({ "base": { "hotkeys": { "next_blocked": "CmdOrCtrl+Shift+B" } } })
            ),
            Some("CmdOrCtrl+Shift+B".to_string())
        );
    }
}
