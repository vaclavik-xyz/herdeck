//! Window-mode config logic for the floating deck window (`main`).
//!
//! Framework-free (no Tauri types) so it is unit-testable without a GUI. The
//! mode is read from `config.toml` at startup — BEFORE the window is built —
//! because `transparent`/`decorations` are creation-time properties in Tauri 2.

use std::path::{Path, PathBuf};

/// The three deck window modes. `Normal` is the default everywhere.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WindowMode {
    Normal,
    Floating,
    AlwaysOnTop,
}

impl WindowMode {
    /// The canonical config string (matches the frontend `WINDOW_MODES`).
    pub fn as_str(self) -> &'static str {
        match self {
            WindowMode::Normal => "normal",
            WindowMode::Floating => "floating",
            WindowMode::AlwaysOnTop => "always_on_top",
        }
    }

    /// Borderless modes are transparent + undecorated (rounded CSS card + drag
    /// handle). Normal is the native OS-decorated window.
    pub fn is_borderless(self) -> bool {
        matches!(self, WindowMode::Floating | WindowMode::AlwaysOnTop)
    }
}

/// Parse `desktop.window_mode` from a config.toml string. Defaults to `Normal`
/// for a missing key, a wrong type, an unknown value, or an unparseable file —
/// never panics.
pub fn parse_window_mode(toml_str: &str) -> WindowMode {
    let value: toml::Value = match toml_str.parse() {
        Ok(v) => v,
        Err(_) => return WindowMode::Normal,
    };
    let mode = value
        .get("desktop")
        .and_then(|d| d.get("window_mode"))
        .and_then(|m| m.as_str());
    match mode {
        Some("floating") => WindowMode::Floating,
        Some("always_on_top") => WindowMode::AlwaysOnTop,
        _ => WindowMode::Normal,
    }
}

/// Whether switching `from`→`to` needs an app restart. Only a switch BETWEEN the
/// two borderless modes (floating↔always_on_top) can be applied live (toggle
/// `always_on_top`); any change involving `normal` flips `transparent`, which is
/// a creation-time prop → restart.
pub fn switch_needs_restart(from: WindowMode, to: WindowMode) -> bool {
    if from == to {
        return false;
    }
    !(from.is_borderless() && to.is_borderless())
}

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

/// Read the window mode from `path`. A missing/unreadable file → `Normal` (first
/// run). Delegates value parsing to `parse_window_mode`.
pub fn read_window_mode(path: &Path) -> WindowMode {
    match std::fs::read_to_string(path) {
        Ok(s) => parse_window_mode(&s),
        Err(_) => WindowMode::Normal,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_known_modes() {
        assert_eq!(
            parse_window_mode("[desktop]\nwindow_mode = \"floating\"\n"),
            WindowMode::Floating
        );
        assert_eq!(
            parse_window_mode("[desktop]\nwindow_mode = \"always_on_top\"\n"),
            WindowMode::AlwaysOnTop
        );
        assert_eq!(
            parse_window_mode("[desktop]\nwindow_mode = \"normal\"\n"),
            WindowMode::Normal
        );
    }

    #[test]
    fn defaults_to_normal_on_anything_unexpected() {
        assert_eq!(parse_window_mode(""), WindowMode::Normal); // empty
        assert_eq!(parse_window_mode("[other]\nx = 1\n"), WindowMode::Normal); // no [desktop]
        assert_eq!(
            parse_window_mode("[desktop]\nwindow_mode = \"bogus\"\n"),
            WindowMode::Normal
        ); // unknown value
        assert_eq!(
            parse_window_mode("[desktop]\nwindow_mode = 5\n"),
            WindowMode::Normal
        ); // wrong type
        assert_eq!(parse_window_mode("this is { not valid toml"), WindowMode::Normal); // unparseable
    }

    #[test]
    fn as_str_round_trips_through_parse() {
        for m in [WindowMode::Normal, WindowMode::Floating, WindowMode::AlwaysOnTop] {
            let toml = format!("[desktop]\nwindow_mode = \"{}\"\n", m.as_str());
            assert_eq!(parse_window_mode(&toml), m);
        }
    }

    #[test]
    fn restart_needed_only_when_normal_involved() {
        assert!(!switch_needs_restart(WindowMode::Floating, WindowMode::AlwaysOnTop));
        assert!(!switch_needs_restart(WindowMode::AlwaysOnTop, WindowMode::Floating));
        assert!(switch_needs_restart(WindowMode::Normal, WindowMode::Floating));
        assert!(switch_needs_restart(WindowMode::Floating, WindowMode::Normal));
        assert!(switch_needs_restart(WindowMode::Normal, WindowMode::AlwaysOnTop));
    }

    #[test]
    fn same_mode_never_restarts() {
        assert!(!switch_needs_restart(WindowMode::Normal, WindowMode::Normal));
        assert!(!switch_needs_restart(WindowMode::Floating, WindowMode::Floating));
        assert!(!switch_needs_restart(WindowMode::AlwaysOnTop, WindowMode::AlwaysOnTop));
    }

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
    fn read_missing_file_is_normal() {
        let p = scratch("read-missing").join("nope.toml");
        assert_eq!(read_window_mode(&p), WindowMode::Normal);
    }

    #[test]
    fn read_reads_existing_file() {
        let p = scratch("read-file").join("config.toml");
        std::fs::write(&p, "[desktop]\nwindow_mode = \"floating\"\n").unwrap();
        assert_eq!(read_window_mode(&p), WindowMode::Floating);
    }
}
