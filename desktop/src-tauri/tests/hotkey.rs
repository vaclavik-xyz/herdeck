//! Unit tests for the deck-toggle accelerator extraction (pure, no Tauri).
use herdeck_desktop_lib::hotkey::{toggle_deck_accelerator, DEFAULT_TOGGLE_DECK};
use serde_json::json;

#[test]
fn missing_key_falls_back_to_default() {
    assert_eq!(
        toggle_deck_accelerator(&json!({ "base": {} })),
        Some(DEFAULT_TOGGLE_DECK.to_string())
    );
}

#[test]
fn missing_base_falls_back_to_default() {
    assert_eq!(
        toggle_deck_accelerator(&json!({})),
        Some(DEFAULT_TOGGLE_DECK.to_string())
    );
}

#[test]
fn explicit_empty_string_disables() {
    assert_eq!(
        toggle_deck_accelerator(&json!({ "base": { "hotkeys": { "toggle_deck": "" } } })),
        None
    );
}

#[test]
fn explicit_value_is_used_verbatim() {
    assert_eq!(
        toggle_deck_accelerator(&json!({ "base": { "hotkeys": { "toggle_deck": "Alt+Space" } } })),
        Some("Alt+Space".to_string())
    );
}
