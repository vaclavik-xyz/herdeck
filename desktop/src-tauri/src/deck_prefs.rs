//! Deck window preference config logic (`[desktop]` in `config.toml`).
//!
//! Framework-free (no Tauri types) so it is unit-testable without a GUI. Neither
//! key here is a creation-time window property: the two windows have fixed
//! chrome, and `deck_always_on_top` is applied live via `set_always_on_top`, so
//! nothing in this module can require a restart. The legacy `window_mode` it
//! replaced is read once per launch as a migration fallback and never written.

use std::path::{Path, PathBuf};

/// Resolve the `config.toml` path with the SAME existence-check order as the
/// sidecar's `bootstrap._discover_config_path`, so Rust and the sidecar read the
/// same file: `HERDECK_CONFIG` (if set & non-empty, absolutized) → existing
/// `$HOME/.config/herdeck/config.toml` → existing `<repo_root>/config.toml` (dev)
/// → default `$HOME/.config/herdeck/config.toml` (first-run/write fallback; may
/// not exist). Hardcoded `$HOME/.config/...` (NOT `XDG_CONFIG_HOME`) to match the
/// sidecar's `expanduser`.
pub fn resolve_config_path(env_override: Option<&str>, home: &Path, repo_root: &Path) -> PathBuf {
    if let Some(p) = env_override {
        if !p.is_empty() {
            return make_absolute(p);
        }
    }
    let home_cfg = home.join(".config").join("herdeck").join("config.toml");
    if home_cfg.exists() {
        return home_cfg;
    }
    let repo_cfg = repo_root.join("config.toml");
    if repo_cfg.exists() {
        return repo_cfg;
    }
    home_cfg
}

/// Mirror the sidecar's `os.path.abspath`: leave absolute paths alone; resolve
/// relative ones against the current dir.
fn make_absolute(p: &str) -> PathBuf {
    let pb = PathBuf::from(p);
    if pb.is_absolute() {
        pb
    } else {
        std::env::current_dir().map(|d| d.join(&pb)).unwrap_or(pb)
    }
}

/// `[desktop].deck_always_on_top` exactly as the key reads it: `None` when the
/// key is absent, the wrong type, or the file does not parse — never panics.
///
/// Deliberately does NOT default to false. Absent and explicitly-false lead to
/// opposite answers once the migration fallback runs, so the two must stay
/// apart; `resolve_deck_always_on_top` is what applies a default.
pub fn parse_deck_always_on_top(toml_str: &str) -> Option<bool> {
    toml_str
        .parse::<toml::Value>()
        .ok()
        .and_then(|v| v.get("desktop")?.get("deck_always_on_top")?.as_bool())
}

/// Whether the deck floats above other windows — the value the app acts on.
///
/// A key holding a BOOLEAN wins in either direction: a user who wrote
/// `deck_always_on_top = false` must not have a `window_mode` left behind in the
/// same file turn it back on, and upgrading deliberately does not rewrite that
/// file. Anything else — an absent key, or one holding some other type, which
/// `parse_deck_always_on_top` cannot tell apart and which the editor cannot
/// produce — defers to the legacy mode. That is row 3 of the design doc's
/// migration table: the one launch after an upgrade where a user who had a
/// floating deck would otherwise silently lose it.
///
/// `configClient.ts`'s `deckAlwaysOnTop` resolves the same value for the editor
/// checkbox and must agree on both halves, wrong-typed key included.
///
/// Applied live via `set_always_on_top`; unlike the `window_mode` it replaces,
/// it never needs a restart, because it is not a creation-time window property.
pub fn resolve_deck_always_on_top(toml_str: &str) -> bool {
    parse_deck_always_on_top(toml_str)
        .unwrap_or_else(|| parse_legacy_window_mode(toml_str).as_deref() == Some("always_on_top"))
}

/// The pre-roles `[desktop].window_mode`, read ONCE per launch as the migration
/// fallback for both halves of the table — the flag (see
/// `resolve_deck_always_on_top`) and the visibility (see
/// `window_state::startup_state`). Deliberately returns the raw string rather
/// than an enum: nothing in the app models these as modes any more, and the
/// mapping lives in one place.
pub fn parse_legacy_window_mode(toml_str: &str) -> Option<String> {
    toml_str
        .parse::<toml::Value>()
        .ok()
        .and_then(|v| Some(v.get("desktop")?.get("window_mode")?.as_str()?.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        let p = std::env::temp_dir().join(format!("herdeck-wm-{name}"));
        let _ = std::fs::remove_dir_all(&p);
        std::fs::create_dir_all(&p).unwrap();
        p
    }

    #[test]
    fn resolve_prefers_absolute_env_override() {
        let home = scratch("env-home");
        let repo = scratch("env-repo");
        let got = resolve_config_path(Some("/abs/cfg.toml"), &home, &repo);
        assert_eq!(got, PathBuf::from("/abs/cfg.toml"));
    }

    #[test]
    fn resolve_ignores_empty_env_override() {
        let home = scratch("empty-home");
        let repo = scratch("empty-repo");
        let got = resolve_config_path(Some(""), &home, &repo);
        assert_eq!(got, home.join(".config").join("herdeck").join("config.toml"));
    }

    #[test]
    fn resolve_home_wins_over_repo_when_both_exist() {
        let home = scratch("home-wins-home");
        let repo = scratch("home-wins-repo");
        let home_cfg = home.join(".config").join("herdeck").join("config.toml");
        std::fs::create_dir_all(home_cfg.parent().unwrap()).unwrap();
        std::fs::write(&home_cfg, "").unwrap();
        std::fs::write(repo.join("config.toml"), "").unwrap();
        assert_eq!(resolve_config_path(None, &home, &repo), home_cfg);
    }

    #[test]
    fn resolve_falls_back_to_repo_when_home_absent() {
        let home = scratch("repo-home"); // no config under it
        let repo = scratch("repo-repo");
        let repo_cfg = repo.join("config.toml");
        std::fs::write(&repo_cfg, "").unwrap();
        assert_eq!(resolve_config_path(None, &home, &repo), repo_cfg);
    }

    #[test]
    fn resolve_default_need_not_exist() {
        let home = scratch("none-home");
        let repo = scratch("none-repo");
        let got = resolve_config_path(None, &home, &repo);
        assert_eq!(got, home.join(".config").join("herdeck").join("config.toml"));
        assert!(!got.exists());
    }

    // Absent is NOT false: the two lead to opposite answers once the legacy
    // fallback is applied, so the raw parser must keep them apart.
    #[test]
    fn an_absent_or_unusable_flag_reads_as_absent_not_as_false() {
        assert_eq!(parse_deck_always_on_top(""), None);
        assert_eq!(parse_deck_always_on_top("[desktop]\n"), None);
        assert_eq!(parse_deck_always_on_top("this is not toml ["), None);
        assert_eq!(
            parse_deck_always_on_top("[desktop]\ndeck_always_on_top = \"yes\"\n"),
            None
        );
    }

    #[test]
    fn deck_always_on_top_reads_the_explicit_flag() {
        assert_eq!(
            parse_deck_always_on_top("[desktop]\ndeck_always_on_top = true\n"),
            Some(true)
        );
        assert_eq!(
            parse_deck_always_on_top("[desktop]\ndeck_always_on_top = false\n"),
            Some(false)
        );
    }

    // The legacy key is a MIGRATION source, read only when the new one is absent —
    // so it must survive being read, not be interpreted as the new flag.
    #[test]
    fn the_legacy_mode_is_readable_but_separate() {
        let toml = "[desktop]\nwindow_mode = \"always_on_top\"\n";
        assert_eq!(parse_legacy_window_mode(toml).as_deref(), Some("always_on_top"));
        // `parse_` reads ONE key and sees no such key here. Not to be confused
        // with `resolve_`, which composes both sources and answers `true` for
        // this very input — the contrast is the point of the two names.
        assert_eq!(parse_deck_always_on_top(toml), None);
        assert!(resolve_deck_always_on_top(toml));
        assert_eq!(parse_legacy_window_mode("[desktop]\n"), None);
    }

    // The design doc's migration table, one test per row, because its own risk
    // note says every row needs one. Rows 1, 2 and 4 differ only in visibility,
    // which `window_state::startup_state` owns; the flag column is here.

    // Row 3: the only row that turns the flag on, and the only reason the
    // fallback exists. Without it an upgrading user's floating deck comes back
    // NOT floating, which is the one thing the migration promises not to do.
    #[test]
    fn a_legacy_always_on_top_deck_keeps_floating_after_the_upgrade() {
        assert!(resolve_deck_always_on_top(
            "[desktop]\nwindow_mode = \"always_on_top\"\n"
        ));
    }

    // Rows 1 and 2: the legacy modes that were never on top stay off.
    #[test]
    fn the_other_legacy_modes_do_not_turn_the_flag_on() {
        for mode in ["normal", "floating"] {
            let toml = format!("[desktop]\nwindow_mode = \"{mode}\"\n");
            assert!(!resolve_deck_always_on_top(&toml), "mode {mode}");
        }
    }

    // Row 4: absent or unparseable is the documented default, and a first run
    // has neither key.
    #[test]
    fn no_key_and_no_legacy_mode_leaves_the_flag_off() {
        assert!(!resolve_deck_always_on_top(""));
        assert!(!resolve_deck_always_on_top("[desktop]\n"));
        assert!(!resolve_deck_always_on_top("this is not toml ["));
        assert!(!resolve_deck_always_on_top("[desktop]\nwindow_mode = \"sideways\"\n"));
    }

    // The new key decides whenever it is present, in EITHER direction. The
    // false case is the one that matters: a user who deliberately turned the
    // deck off must not have a stale window_mode turn it back on, and the
    // legacy key is left in config.toml precisely because upgrading does not
    // rewrite a user's file.
    #[test]
    fn the_explicit_key_outranks_the_legacy_mode_both_ways() {
        assert!(!resolve_deck_always_on_top(
            "[desktop]\nwindow_mode = \"always_on_top\"\ndeck_always_on_top = false\n"
        ));
        assert!(resolve_deck_always_on_top(
            "[desktop]\nwindow_mode = \"normal\"\ndeck_always_on_top = true\n"
        ));
        // And with no legacy key in the file at all.
        assert!(resolve_deck_always_on_top(
            "[desktop]\ndeck_always_on_top = true\n"
        ));
    }

    // A key that is present but not a boolean is treated as ABSENT, so it defers
    // to the legacy mode instead of reading as false. Only hand-editing produces
    // it, and `configClient.ts`'s `deckAlwaysOnTop` answers the same way for the
    // editor checkbox — the two resolvers must not disagree anywhere.
    #[test]
    fn a_wrong_typed_key_defers_to_the_legacy_mode() {
        assert!(resolve_deck_always_on_top(
            "[desktop]\nwindow_mode = \"always_on_top\"\ndeck_always_on_top = \"yes\"\n"
        ));
        assert!(!resolve_deck_always_on_top(
            "[desktop]\nwindow_mode = \"normal\"\ndeck_always_on_top = \"yes\"\n"
        ));
    }

    #[test]
    fn parse_legacy_window_mode_degrades_on_bad_input() {
        assert_eq!(parse_legacy_window_mode("[desktop]\nwindow_mode = 3\n"), None); // wrong type
        assert_eq!(parse_legacy_window_mode("this is not toml ["), None); // unparseable
    }
}
