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

/// `[desktop].deck_always_on_top`. Defaults to false for a missing key, a wrong
/// type, or an unparseable file — never panics. Applied live via
/// `set_always_on_top`; unlike the `window_mode` it replaces, it never needs a
/// restart, because it is not a creation-time window property.
pub fn parse_deck_always_on_top(toml_str: &str) -> bool {
    toml_str
        .parse::<toml::Value>()
        .ok()
        .and_then(|v| v.get("desktop")?.get("deck_always_on_top")?.as_bool())
        .unwrap_or(false)
}

/// The pre-roles `[desktop].window_mode`, read ONCE per launch as the migration
/// fallback (see window_state::startup_state). Deliberately returns the raw
/// string rather than an enum: nothing in the app models these as modes any
/// more, and the mapping lives in one place.
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

    #[test]
    fn deck_always_on_top_defaults_to_false() {
        assert!(!parse_deck_always_on_top(""));
        assert!(!parse_deck_always_on_top("[desktop]\n"));
        assert!(!parse_deck_always_on_top("this is not toml ["));
        assert!(!parse_deck_always_on_top("[desktop]\ndeck_always_on_top = \"yes\"\n"));
    }

    #[test]
    fn deck_always_on_top_reads_the_explicit_flag() {
        assert!(parse_deck_always_on_top("[desktop]\ndeck_always_on_top = true\n"));
        assert!(!parse_deck_always_on_top("[desktop]\ndeck_always_on_top = false\n"));
    }

    // The legacy key is a MIGRATION source, read only when the new one is absent —
    // so it must survive being read, not be interpreted as the new flag.
    #[test]
    fn the_legacy_mode_is_readable_but_separate() {
        let toml = "[desktop]\nwindow_mode = \"always_on_top\"\n";
        assert_eq!(parse_legacy_window_mode(toml).as_deref(), Some("always_on_top"));
        assert!(!parse_deck_always_on_top(toml));
        assert_eq!(parse_legacy_window_mode("[desktop]\n"), None);
    }

    #[test]
    fn parse_legacy_window_mode_degrades_on_bad_input() {
        assert_eq!(parse_legacy_window_mode("[desktop]\nwindow_mode = 3\n"), None); // wrong type
        assert_eq!(parse_legacy_window_mode("this is not toml ["), None); // unparseable
    }
}
