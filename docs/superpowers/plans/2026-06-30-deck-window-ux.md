# Deck Window UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the floating deck window (`main`) offer three user-selectable window modes (normal / floating / always-on-top) with rounded corners, content-fit sizing (no empty area), and a drag handle.

**Architecture:** A `[desktop].window_mode` config key (Python passthrough, like 3d's `[hotkeys]`) drives the window. Rust reads it from `config.toml` BEFORE building `main` (transparent/decorations are creation-time props in Tauri 2), builds the window dynamically with per-mode flags, and offers a tray "Window mode" submenu that persists the choice then applies it (live `set_always_on_top` for floating↔always_on_top; `app.restart()` for any change involving normal, because `transparent` cannot be toggled at runtime). Svelte reads the mode from `<html data-window-mode>` (injected pre-paint by Rust) and adds rounded `.shell` + drag handle + a `ResizeObserver` content-fit loop.

**Tech Stack:** Tauri 2.11.3 (Rust), Svelte 5 (runes), Python deckapp sidecar, `toml` crate (Rust), Vitest + pytest + cargo test.

## Global Constraints

- **Default window mode = `normal`.** Missing key / unknown value / unparseable config → `normal`, in BOTH Rust (`parse_window_mode`) and frontend (`windowMode`). Backend round-trips any string (no backend validation).
- **`macos-private-api` is mandatory.** Add `macos-private-api` to the tauri `features` in `Cargo.toml` AND `"macOSPrivateApi": true` under `app` in `tauri.conf.json`. Without BOTH, the borderless modes do not compile (even `.transparent(false)` is `#[cfg]`-gated behind this feature on macOS) — verified against tauri 2.11.3 `src/window/mod.rs:911-915`.
- **Config path agreement.** Rust resolves `config.toml` with the SAME existence-check order as the sidecar's `_discover_config_path` (`HERDECK_CONFIG` env → `$HOME/.config/herdeck/config.toml` IF EXISTS → `<repo_root>/config.toml` IF EXISTS → default `$HOME/.config/herdeck/config.toml`), hardcoded `$HOME/.config/...` (NOT `XDG_CONFIG_HOME`), and **exports the resolved absolute path as `HERDECK_CONFIG` into the sidecar spawn env** so both read the same file.
- **`window_mode` lives in base `config.toml` `[desktop]`, never in `local.toml`.**
- **POST /config persist succeeds ONLY when HTTP status is 200 AND `errors == []`.** `/config` returns validation failures as HTTP 200 with `{errors:[...]}` and writes nothing. Use a timeout ≥ 15 s (`SETUP_CONNECT_TIMEOUT`) for the persist POST — `/config` blocks on `_setup_lock`. Apply (restart / set_always_on_top) ONLY after a confirmed-successful persist.
- **Restart from the tray MUST use `app.request_restart()`, NOT `app.restart()`** (verified against Tauri 2.11.3 `app.rs`): a tray menu handler runs on the MAIN THREAD, where `restart()` calls `cleanup_before_exit()` + `process::restart()` directly and SKIPS `RunEvent::ExitRequested`/`Exit` (its doc comment: "we cannot guarantee the delivery of those events, so we skip them"). The sidecar kill lives in that handler, so `restart()` would ORPHAN the sidecar. `request_restart()` routes through `request_exit(RESTART_EXIT_CODE)` so the event loop fires the exit events (the kill handler runs) before restarting. `request_restart()` returns `()`, so it MUST be followed by `return` (else control falls through to the live-apply branch). The main-window close-intercept is a WindowEvent and never swallows the RunEvent exit/restart.
- **Token never in JS.** The tray persist uses the Rust `http::http_get` / `http::http_post_json` helpers with the token injected Rust-side (existing pattern). **Secret values are one-way:** the persist drops the redacted `secrets` field from the GET response and POSTs only `{base, profiles, local}` (the established editor write contract) — never logs or echoes secret values.
- **Remove `min-height: 100vh` from ALL THREE files:** `App.svelte` (`main`), `DeckView.svelte` (`.deck`), `Onboarding.svelte` (`.onboarding`). Otherwise content-fit cannot shrink the window.
- **Capability:** add `core:window:allow-set-size` to `capabilities/default.json` permissions (the JS `setSize` is ACL-gated; `core:default` is read-only).
- **Commits:** Conventional Commits, English. No `Co-Authored-By`. Never squash.

**Testing note (for reviewers):** Per the spec's Testing table, the unit-testable logic is concentrated in **Task 2** (Rust `parse_window_mode` / `switch_needs_restart` / `resolve_config_path` / `read_window_mode`) and **Task 5** (Svelte `windowMode` helpers + `fitDecision`). **Tasks 3, 4, and 6 are build/compile/smoke-gated** (a dynamic window, tray wiring, CSS, and a `ResizeObserver` are not unit-testable without a GUI) — their gate is `cargo build` + `cargo test` (the latter proves `generate_context!` accepts `tauri.conf.json` and that Task 2's tests still pass) for Rust, and `npm run build` + `npm test` for Svelte. Do NOT treat the absence of new unit tests in Tasks 3/4/6 as a defect; judge them by the stated build gate.

---

### Task 1: Config passthrough — `[desktop].window_mode` (Python)

Mirror exactly how 3d added `[hotkeys]`: one tuple entry makes `read()` include `base.desktop` and `write()` round-trip it, with zero core impact (`desktop` is not in the `Config` dataclass nor `_OVERLAY_SECTIONS`, and `validate_settings` has no section whitelist).

**Files:**
- Modify: `src/herdeck/deckapp/config_service.py:20-31` (`ConfigService.BASE_SECTIONS`)
- Test: `tests/test_config_service.py` (append two tests next to the hotkeys ones at lines 313-333)

**Interfaces:**
- Produces: `ConfigService.read()` returns `data["base"]["desktop"]` when `config.toml` has `[desktop]`; `ConfigService.write(data)` persists `data["base"]["desktop"]` back to `[desktop]` in `config.toml`.

- [ ] **Step 1: Write the failing read+write round-trip tests**

Append to `tests/test_config_service.py` (the `_svc`, `_FakeKeyring`, `_tomllib`, `secret_store` helpers already exist in this file):

```python
def test_read_roundtrips_desktop_section(tmp_path, monkeypatch):
    monkeypatch.delenv("TOK", raising=False)
    monkeypatch.setattr(secret_store, "_keyring", _FakeKeyring)
    text = (
        '[[servers]]\nid="local"\nurl="ws://x"\ntoken_env="TOK"\n'
        '[desktop]\nwindow_mode = "floating"\n'
    )
    svc = _svc(tmp_path, text=text)
    assert svc.read()["base"]["desktop"] == {"window_mode": "floating"}


def test_write_roundtrips_desktop_section(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "real")
    monkeypatch.setattr(secret_store, "_keyring", _FakeKeyring)
    svc = _svc(tmp_path)
    data = svc.read()
    data["base"]["desktop"] = {"window_mode": "always_on_top"}
    assert svc.write(data) == []  # no structural errors
    assert _tomllib.loads((tmp_path / "config.toml").read_text())["desktop"] == {
        "window_mode": "always_on_top"
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Users/admin/projects/herdeck && .venv/bin/python -m pytest tests/test_config_service.py::test_read_roundtrips_desktop_section tests/test_config_service.py::test_write_roundtrips_desktop_section -v`
Expected: FAIL — `test_read_...` gets `KeyError: 'desktop'` (the section is filtered out of `base` because `"desktop"` is not in `BASE_SECTIONS`).

- [ ] **Step 3: Add `"desktop"` to `BASE_SECTIONS`**

In `src/herdeck/deckapp/config_service.py`, change the tuple (currently ends with `"hotkeys",`):

```python
    BASE_SECTIONS = (
        "servers",
        "deck",
        "answer_profiles",
        "macros",
        "start_profiles",
        "notifications",
        "theme",
        "view",
        "safety",
        "hotkeys",
        "desktop",
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /Users/admin/projects/herdeck && .venv/bin/python -m pytest tests/test_config_service.py -v`
Expected: PASS (all config-service tests, including the two new ones).

- [ ] **Step 5: Run the full Python suite + lint to confirm no regression**

Run: `cd /Users/admin/projects/herdeck && .venv/bin/python -m pytest -q && ruff check src tests`
Expected: all tests pass; ruff clean. (An inert `[desktop]` must not break `validate_settings` — confirm no settings/validate test regresses.)

- [ ] **Step 6: Commit**

```bash
git add src/herdeck/deckapp/config_service.py tests/test_config_service.py
git commit -m "feat(config): round-trip [desktop].window_mode via BASE_SECTIONS"
```

---

### Task 2: Rust window-mode module — pure logic (`window_mode.rs`)

All FS/env-free decision logic, fully cargo-testable. The window builder, tray, and sidecar wiring (Tasks 3-4) consume these.

**Files:**
- Create: `desktop/src-tauri/src/window_mode.rs`
- Modify: `desktop/src-tauri/Cargo.toml` (add `toml` dep)
- Modify: `desktop/src-tauri/src/lib.rs:10-12` (add `pub mod window_mode;`)

**Interfaces:**
- Produces:
  - `enum WindowMode { Normal, Floating, AlwaysOnTop }` (derives `Debug, Clone, Copy, PartialEq, Eq`)
  - `WindowMode::as_str(self) -> &'static str` → `"normal"` | `"floating"` | `"always_on_top"`
  - `WindowMode::is_borderless(self) -> bool` (true for Floating + AlwaysOnTop)
  - `parse_window_mode(toml_str: &str) -> WindowMode` (default Normal; never panics)
  - `switch_needs_restart(from: WindowMode, to: WindowMode) -> bool`
  - `resolve_config_path(env_override: Option<&str>, home: &Path, repo_root: &Path) -> PathBuf`
  - `read_window_mode(path: &Path) -> WindowMode`

- [ ] **Step 1: Add the `toml` dependency**

In `desktop/src-tauri/Cargo.toml`, under `[dependencies]` (next to `serde`), add:

```toml
toml = "0.8"
```

- [ ] **Step 2: Write the module with its failing tests**

Create `desktop/src-tauri/src/window_mode.rs`:

```rust
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
```

- [ ] **Step 3: Register the module**

In `desktop/src-tauri/src/lib.rs`, the module declarations are:

```rust
pub mod hotkey;
pub mod http;
pub mod sidecar;
```

Add a fourth line:

```rust
pub mod hotkey;
pub mod http;
pub mod sidecar;
pub mod window_mode;
```

- [ ] **Step 4: Run the tests to verify they pass (and fetch `toml`)**

Run: `cd /Users/admin/projects/herdeck/desktop/src-tauri && cargo test window_mode -- --nocapture`
Expected: the `window_mode::tests::*` tests compile and PASS (cargo fetches `toml` on first build).

- [ ] **Step 5: Run the full Rust suite**

Run: `(cd /Users/admin/projects/herdeck/desktop/src-tauri && cargo test)`
Expected: all tests pass (existing `sidecar` tests + the new `window_mode` tests). Exit code 0.

- [ ] **Step 6: Commit**

```bash
git add desktop/src-tauri/src/window_mode.rs desktop/src-tauri/src/lib.rs desktop/src-tauri/Cargo.toml desktop/src-tauri/Cargo.lock
git commit -m "feat(desktop): window_mode parse/resolve/restart logic + toml dep"
```

---

### Task 3: Rust — dynamic `main` window + deps + config wiring

Build `main` dynamically with per-mode flags (it leaves `tauri.conf.json`), enable `macos-private-api`, export `HERDECK_CONFIG` to the sidecar, and add the `get_window_mode` command. **Gate: `cargo build` + `cargo test`** (no new unit tests — a dynamic window cannot be unit-tested; the compile proves `generate_context!` accepts the new `tauri.conf.json`, and `cargo test` proves Task 2's logic still passes). The tray still has NO window-mode submenu yet — that is Task 4.

**Files:**
- Modify: `desktop/src-tauri/tauri.conf.json` (remove `main` window; add `macOSPrivateApi`)
- Modify: `desktop/src-tauri/Cargo.toml` (add `macos-private-api` tauri feature)
- Modify: `desktop/src-tauri/capabilities/default.json` (add `core:window:allow-set-size`)
- Modify: `desktop/src-tauri/src/lib.rs` (AppState, imports, `place_floating`, `start_sidecar`, `get_window_mode`, `generate_handler`, `run()` head, `setup()`)

**Interfaces:**
- Consumes (Task 2): `window_mode::{WindowMode, resolve_config_path, read_window_mode}`.
- Produces:
  - `AppState { discovery, window_mode: Arc<Mutex<WindowMode>> }`
  - command `get_window_mode(state) -> String`
  - `start_sidecar(app, discovery, child, stop, config_path: &Path)` (exports `HERDECK_CONFIG`)
  - `place_floating(window)` no longer sets `always_on_top` and uses `primary_monitor()`
  - in `setup()`, `main` is built dynamically; the startup mode is in scope as `mode` (a `WindowMode`) for Task 4's `build_tray` call.

- [ ] **Step 1: Remove `main` from `tauri.conf.json` and enable `macOSPrivateApi`**

In `desktop/src-tauri/tauri.conf.json`, replace the whole `"app"` block (the `windows` array currently holds `main` then `config`) so only `config` remains and `macOSPrivateApi` is set:

```json
  "app": {
    "withGlobalTauri": false,
    "macOSPrivateApi": true,
    "windows": [
      {
        "label": "config",
        "title": "herdeck — Config",
        "width": 900,
        "height": 680,
        "minWidth": 640,
        "minHeight": 480,
        "resizable": true,
        "decorations": true,
        "alwaysOnTop": false,
        "transparent": false,
        "visible": false,
        "shadow": true,
        "skipTaskbar": false
      }
    ],
    "security": {
      "csp": null
    }
  },
```

(Leave `build`, `bundle`, `$schema`, `productName`, `version`, `identifier` untouched.)

- [ ] **Step 2: Add the `macos-private-api` tauri feature**

In `desktop/src-tauri/Cargo.toml`, change:

```toml
tauri = { version = "2", features = ["tray-icon"] }
```

to:

```toml
tauri = { version = "2", features = ["tray-icon", "macos-private-api"] }
```

- [ ] **Step 3: Add the `set-size` capability**

In `desktop/src-tauri/capabilities/default.json`, change `"permissions"`:

```json
  "permissions": ["core:default", "core:window:allow-set-size"]
```

- [ ] **Step 4: Update imports + `AppState` + `get_window_mode` in `lib.rs`**

Add `WebviewUrl`/`WebviewWindowBuilder` to the `tauri::` import and the window-mode use. The current import (lib.rs:23) is:

```rust
use tauri::{Emitter, Manager, PhysicalPosition};
```

Change to:

```rust
use tauri::{Emitter, Manager, PhysicalPosition, WebviewUrl, WebviewWindowBuilder};

use window_mode::WindowMode;
```

Change `AppState` (lib.rs:30-32) to carry the current mode:

```rust
struct AppState {
    discovery: Arc<Mutex<Option<Discovery>>>,
    /// The live window mode. Set at startup from config; updated in-process on a
    /// live floating↔always_on_top switch (a restart-mode switch replaces the
    /// whole process, which re-reads config).
    window_mode: Arc<Mutex<WindowMode>>,
}
```

Add the `get_window_mode` command immediately after the `get_discovery` command (after lib.rs:82):

```rust
/// The window mode the deck was built with (updated live on a borderless switch).
/// The frontend ALSO reads `<html data-window-mode>` (set pre-paint by Rust); this
/// command is the programmatic path for logic that needs it after mount.
#[tauri::command]
fn get_window_mode(state: tauri::State<'_, AppState>) -> String {
    state.window_mode.lock().unwrap().as_str().to_string()
}
```

- [ ] **Step 5: Update `place_floating` (no `always_on_top`, primary monitor)**

Replace `place_floating` (lib.rs:352-365). The builder now owns `always_on_top`; `place_floating` only positions, and uses `primary_monitor()` so "top-right" lands on the primary display (macbench has 3):

```rust
/// Position the floating window near the top-right of the PRIMARY monitor. The
/// builder owns `always_on_top` (per mode); this only places the window.
fn place_floating(window: &tauri::WebviewWindow) {
    if let (Ok(Some(monitor)), Ok(win_size)) = (window.primary_monitor(), window.outer_size()) {
        let screen = monitor.size();
        let origin = monitor.position();
        let margin = 16i32;
        let x = (origin.x + screen.width as i32 - win_size.width as i32 - margin).max(origin.x);
        let y = origin.y + margin;
        let _ = window.set_position(PhysicalPosition { x, y });
    }
}
```

- [ ] **Step 6: Export `HERDECK_CONFIG` into the sidecar spawn**

Change `start_sidecar` (lib.rs:511-540) to take the resolved config path and push it onto the spawn env (so the sidecar reads the same file Rust read the mode from):

```rust
/// Start the sidecar supervisor (or record the external discovery). `config_path`
/// is exported as `HERDECK_CONFIG` so the spawned sidecar reads the SAME config
/// file Rust resolved the window mode from (mooting the sidecar's CWD-relative
/// branch — important for the frozen `.app`, where CWD is nondeterministic).
fn start_sidecar(
    app: &tauri::App,
    discovery: Arc<Mutex<Option<Discovery>>>,
    child: Arc<Mutex<Option<Child>>>,
    stop: Arc<AtomicBool>,
    config_path: &Path,
) {
    let resource_dir = app.path().resource_dir().ok();
    match resolve_plan(resource_dir) {
        SidecarPlan::External(d) => {
            let view = DiscoveryView::from(&d);
            register_toggle_hotkey(app.handle(), &d);
            *discovery.lock().unwrap() = Some(d);
            let _ = app.handle().emit("discovery", view); // token-free
        }
        SidecarPlan::Spawn(mut spec) => {
            spec.envs.push((
                "HERDECK_CONFIG".to_string(),
                config_path.to_string_lossy().into_owned(),
            ));
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                supervise(SupervisorConfig::new(spec), child, stop, move |d| {
                    let view = DiscoveryView::from(&d);
                    register_toggle_hotkey(&handle, &d);
                    if let Some(state) = handle.try_state::<AppState>() {
                        *state.discovery.lock().unwrap() = Some(d);
                    }
                    let _ = handle.emit("discovery", view); // token-free
                });
            });
        }
    }
}
```

- [ ] **Step 7: Read the mode in `run()` and register `get_window_mode`**

Replace the head of `run()` (lib.rs:543-556) — add config-path resolution + mode read + the `window_mode` state, and clone the config path for setup:

```rust
pub fn run() {
    let discovery: Arc<Mutex<Option<Discovery>>> = Arc::new(Mutex::new(None));
    let child: Arc<Mutex<Option<Child>>> = Arc::new(Mutex::new(None));
    let stop = Arc::new(AtomicBool::new(false));

    // Resolve config.toml with the sidecar's existence-check order, then read the
    // window mode BEFORE the window is built (transparent/decorations are
    // creation-time props in Tauri 2).
    let home = PathBuf::from(env::var("HOME").unwrap_or_default());
    let repo_root = repo_root_from_manifest();
    let env_override = env::var("HERDECK_CONFIG").ok();
    let config_path =
        window_mode::resolve_config_path(env_override.as_deref(), &home, &repo_root);
    let mode = window_mode::read_window_mode(&config_path);

    // Clones for the setup closure and the supervisor.
    let setup_discovery = discovery.clone();
    let setup_child = child.clone();
    let setup_stop = stop.clone();
    let setup_config_path = config_path.clone();
    // Clones for the exit handler.
    let exit_child = child.clone();
    let exit_stop = stop.clone();

    let state = AppState {
        discovery,
        window_mode: Arc::new(Mutex::new(mode)),
    };
```

In the `tauri::generate_handler![...]` list (lib.rs:565-582), add `get_window_mode` after `reload_hotkey` (add a comma after `reload_hotkey`):

```rust
            open_config,
            reload_hotkey,
            get_window_mode
        ])
```

- [ ] **Step 8: Build `main` dynamically in `setup()`**

Replace the `.setup(move |app| { ... })` body (lib.rs:583-603). Build `main` FIRST (it's no longer static), then position + close-intercept, then tray + sidecar:

```rust
        .setup(move |app| {
            // `main` is no longer in tauri.conf.json — build it here so its
            // transparent/decorations match the mode. The init script sets
            // `<html data-window-mode>` BEFORE first paint so the borderless CSS
            // applies with no flash of opaque-normal styling (FOUC).
            let app_handle = app.handle().clone();
            let init = format!(
                "document.documentElement.dataset.windowMode='{}'",
                mode.as_str()
            );
            let builder = WebviewWindowBuilder::new(&app_handle, "main", WebviewUrl::default())
                .title("herdeck")
                .shadow(true)
                .initialization_script(init);
            let builder = match mode {
                WindowMode::Normal => builder
                    .decorations(true)
                    .transparent(false)
                    .always_on_top(false)
                    .resizable(true)
                    .inner_size(380.0, 340.0)
                    .skip_taskbar(false),
                WindowMode::Floating => builder
                    .decorations(false)
                    .transparent(true)
                    .always_on_top(false)
                    .resizable(false)
                    .inner_size(360.0, 320.0)
                    .skip_taskbar(true),
                WindowMode::AlwaysOnTop => builder
                    .decorations(false)
                    .transparent(true)
                    .always_on_top(true)
                    .resizable(false)
                    .inner_size(360.0, 320.0)
                    .skip_taskbar(true),
            };
            let main_window = builder.build()?;

            // Borderless modes get the top-right placement; normal opens where the
            // OS puts it and is user-movable via the titlebar.
            if mode.is_borderless() {
                place_floating(&main_window);
            }

            // Normal mode has a close button; intercept close -> hide (like the
            // `config` window) so the tray "Show" brings it back and the app +
            // sidecar keep running. CloseRequested is window-close only — it does
            // NOT fire for app.exit/app.restart, so this never blocks quit/restart.
            {
                let w = main_window.clone();
                main_window.on_window_event(move |event| {
                    if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                        api.prevent_close();
                        let _ = w.hide();
                    }
                });
            }

            build_tray(app)?;

            // The config window is hidden at startup and reopened on demand; if it
            // were allowed to close, Tauri would DESTROY it and open_config would
            // then fail with "config window not found". Intercept close -> hide.
            if let Some(cfg_win) = app.get_webview_window("config") {
                let w = cfg_win.clone();
                cfg_win.on_window_event(move |event| {
                    if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                        api.prevent_close();
                        let _ = w.hide();
                    }
                });
            }
            start_sidecar(app, setup_discovery, setup_child, setup_stop, &setup_config_path);
            Ok(())
        })
```

(Note: `build_tray(app)?` keeps its current one-arg signature in this task; Task 4 changes it to `build_tray(app, mode)?`. `mode` is `Copy`, so it remains usable in Task 4's call.)

- [ ] **Step 9: Build + test**

Run: `(cd /Users/admin/projects/herdeck/desktop/src-tauri && cargo build)`
Expected: compiles cleanly. The build runs `generate_context!`, which parses `tauri.conf.json` — a malformed config (e.g. a dangling comma after removing `main`) fails here.

Run: `(cd /Users/admin/projects/herdeck/desktop/src-tauri && cargo test)`
Expected: all tests pass (Task 2 `window_mode` + `sidecar`). Exit code 0.

- [ ] **Step 10: Commit**

```bash
git add desktop/src-tauri/tauri.conf.json desktop/src-tauri/Cargo.toml desktop/src-tauri/Cargo.lock desktop/src-tauri/capabilities/default.json desktop/src-tauri/src/lib.rs
git commit -m "feat(desktop): build main window per mode + export HERDECK_CONFIG"
```

---

### Task 4: Rust — tray "Window mode" submenu + persist-then-apply

Add a tray submenu with 3 checkable items, persisting the choice to `config.toml` (Rust HTTP, token injected) and applying it: live `set_always_on_top` for floating↔always_on_top, `app.restart()` for any change involving normal. **Gate: `cargo build` + `cargo test`** (the persist/apply path needs a live sidecar to exercise — not unit-testable here; `switch_needs_restart` is already covered in Task 2). Requires Task 1 (`[desktop]` in `BASE_SECTIONS`, else `base.desktop` is filtered out on POST) and Task 3 (`AppState.window_mode`, `mode` in scope in `setup`).

**Files:**
- Modify: `desktop/src-tauri/src/lib.rs` (`build_tray` signature + submenu + handler; new `WmItems`, `set_wm_checks`, `persist_window_mode`, `select_window_mode`; `setup()` call site)

**Interfaces:**
- Consumes: `window_mode::{WindowMode, switch_needs_restart}`, `AppState.window_mode`, `http::{http_get, http_post_json}`, `HDR_TOKEN`, `SIDECAR_TIMEOUT`, `SETUP_CONNECT_TIMEOUT`.

- [ ] **Step 1: Add the `WmItems` holder + check helper**

In `desktop/src-tauri/src/lib.rs`, add (e.g. just above `build_tray`, lib.rs:442):

```rust
/// The three window-mode tray checkboxes. There is no native radio group, so we
/// hold all three handles and drive the checkmarks ourselves (like `autostart_cb`).
#[derive(Clone)]
struct WmItems {
    normal: CheckMenuItem<tauri::Wry>,
    floating: CheckMenuItem<tauri::Wry>,
    aot: CheckMenuItem<tauri::Wry>,
}

/// Check exactly the item for `mode`, uncheck the other two.
fn set_wm_checks(items: &WmItems, mode: WindowMode) {
    let _ = items.normal.set_checked(mode == WindowMode::Normal);
    let _ = items.floating.set_checked(mode == WindowMode::Floating);
    let _ = items.aot.set_checked(mode == WindowMode::AlwaysOnTop);
}
```

- [ ] **Step 2: Add the persist + apply functions**

Add below `set_wm_checks`:

```rust
/// Persist `window_mode = target` to base config via the sidecar. Read-modify-write
/// over the existing `/config` routes (token injected Rust-side, like the editor).
/// Returns `Ok(())` ONLY on a confirmed write: the `/config` contract returns
/// validation failures as HTTP 200 with a non-empty `errors`, writing NOTHING, so
/// success requires HTTP 200 AND `errors == []`. The POST blocks on `_setup_lock`,
/// so it uses the longer `SETUP_CONNECT_TIMEOUT`; a timeout there is a genuine wedge.
fn persist_window_mode(state: &AppState, target: WindowMode) -> Result<(), String> {
    let d = state
        .discovery
        .lock()
        .unwrap()
        .clone()
        .ok_or_else(|| "sidecar not ready".to_string())?;
    let body = http::http_get(
        &d.host,
        d.port,
        &format!("/config?token={}", d.token),
        SIDECAR_TIMEOUT,
    )?;
    let mut cfg: serde_json::Value =
        serde_json::from_str(&body).map_err(|e| format!("invalid /config JSON: {e}"))?;
    {
        let base = cfg
            .get_mut("base")
            .and_then(|b| b.as_object_mut())
            .ok_or_else(|| "config response missing base table".to_string())?;
        let desktop = base
            .entry("desktop")
            .or_insert_with(|| serde_json::json!({}));
        let desktop_obj = desktop
            .as_object_mut()
            .ok_or_else(|| "config desktop is not a table".to_string())?;
        desktop_obj.insert(
            "window_mode".to_string(),
            serde_json::Value::String(target.as_str().to_string()),
        );
    }
    // POST only {base, profiles, local} — the redacted `secrets` field from the GET
    // is display-only and never written back (secret values are one-way).
    let payload = serde_json::json!({
        "base": cfg.get("base").cloned().unwrap_or_else(|| serde_json::json!({})),
        "profiles": cfg.get("profiles").cloned().unwrap_or_else(|| serde_json::json!({})),
        "local": cfg.get("local").cloned().unwrap_or_else(|| serde_json::json!({})),
    });
    let (code, resp) = http::http_post_json(
        &d.host,
        d.port,
        "/config",
        (HDR_TOKEN, &d.token),
        &payload.to_string(),
        SETUP_CONNECT_TIMEOUT,
    )?;
    if code != 200 {
        return Err(format!("POST /config returned HTTP {code}"));
    }
    let parsed: serde_json::Value =
        serde_json::from_str(&resp).map_err(|e| format!("invalid /config response JSON: {e}"))?;
    match parsed.get("errors").and_then(|e| e.as_array()) {
        Some(arr) if arr.is_empty() => Ok(()),
        Some(_) => Err("config rejected (validation errors)".to_string()),
        None => Err("config response missing 'errors' field".to_string()),
    }
}

/// Tray handler for a window-mode choice: persist FIRST, apply only on success.
/// floating↔always_on_top applies live (toggle always_on_top); any change to/from
/// normal restarts (transparent is creation-time). On persist failure: revert the
/// checkmarks, log, do nothing else.
fn select_window_mode(app: &tauri::AppHandle, target: WindowMode, items: &WmItems) {
    let state = match app.try_state::<AppState>() {
        Some(s) => s,
        None => return,
    };
    let current = *state.window_mode.lock().unwrap();
    if target == current {
        set_wm_checks(items, current); // re-assert, no-op
        return;
    }
    if let Err(e) = persist_window_mode(&state, target) {
        eprintln!("window mode: persist failed, not applying: {e}");
        set_wm_checks(items, current); // revert to the real persisted mode
        return;
    }
    if window_mode::switch_needs_restart(current, target) {
        // NOT app.restart(): a tray menu event runs on the MAIN THREAD, where
        // Tauri's restart() skips RunEvent::ExitRequested/Exit and would ORPHAN
        // the sidecar child (its kill lives in that handler). request_restart()
        // routes through the event loop so the exit handler runs before restart.
        app.request_restart();
        return;
    }
    // Reached only for a live borderless↔borderless switch.
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.set_always_on_top(target == WindowMode::AlwaysOnTop);
    }
    *state.window_mode.lock().unwrap() = target;
    set_wm_checks(items, target);
}
```

- [ ] **Step 3: Add the submenu to `build_tray` (and take the current mode)**

Change `build_tray`'s signature (lib.rs:443) from `fn build_tray(app: &tauri::App) -> tauri::Result<()>` to:

```rust
fn build_tray(app: &tauri::App, current_mode: WindowMode) -> tauri::Result<()> {
```

Inside, after the `hide` item is created and before `autostart` (lib.rs:447-448), add the three checkboxes + submenu:

```rust
    let wm_normal = CheckMenuItem::with_id(
        app,
        "wm_normal",
        "Normal",
        true,
        current_mode == WindowMode::Normal,
        None::<&str>,
    )?;
    let wm_floating = CheckMenuItem::with_id(
        app,
        "wm_floating",
        "Floating",
        true,
        current_mode == WindowMode::Floating,
        None::<&str>,
    )?;
    let wm_aot = CheckMenuItem::with_id(
        app,
        "wm_aot",
        "Always on top",
        true,
        current_mode == WindowMode::AlwaysOnTop,
        None::<&str>,
    )?;
    let wm_submenu = tauri::menu::Submenu::with_items(
        app,
        "Window mode",
        true,
        &[&wm_normal, &wm_floating, &wm_aot],
    )?;
    let wm_items = WmItems {
        normal: wm_normal,
        floating: wm_floating,
        aot: wm_aot,
    };
```

Change the menu assembly (lib.rs:458) to include `wm_submenu` (placed after `hide`):

```rust
    let menu = Menu::with_items(
        app,
        &[&settings, &show, &hide, &wm_submenu, &reconnect, &autostart, &quit],
    )?;
```

- [ ] **Step 4: Wire the submenu handler arms**

The handler closure is `.on_menu_event(move |app, event| match event.id.as_ref() { ... })`. It must capture `wm_items`; add the three arms (e.g. right before the `"quit"` arm at lib.rs:499). Because the closure already moves `autostart_cb`, also move `wm_items` (it is `Clone`, captured by the `move`):

```rust
            "wm_normal" => select_window_mode(app, WindowMode::Normal, &wm_items),
            "wm_floating" => select_window_mode(app, WindowMode::Floating, &wm_items),
            "wm_aot" => select_window_mode(app, WindowMode::AlwaysOnTop, &wm_items),
```

(The `app` bound by `on_menu_event` is `&tauri::AppHandle` — exactly what `select_window_mode` expects.)

- [ ] **Step 5: Pass the mode at the `build_tray` call site**

In `setup()` (Task 3 Step 8), change `build_tray(app)?;` to:

```rust
            build_tray(app, mode)?;
```

- [ ] **Step 6: Build + test**

Run: `(cd /Users/admin/projects/herdeck/desktop/src-tauri && cargo build)`
Expected: compiles cleanly (verifies `Submenu::with_items`, `CheckMenuItem<Wry>` types, and that `app.restart()`'s `!` return leaves no unreachable code after the `if`).

Run: `(cd /Users/admin/projects/herdeck/desktop/src-tauri && cargo test)`
Expected: all tests pass. Exit code 0.

- [ ] **Step 7: Commit**

```bash
git add desktop/src-tauri/src/lib.rs
git commit -m "feat(desktop): tray Window mode submenu (persist-then-apply, live aot)"
```

---

### Task 5: Svelte — config helpers + content-fit decision (Vitest)

The two framework-free pieces: `configClient.ts` window-mode helpers (mirror `toggleDeckHotkey`) and a pure `fitDecision` content-measure/anti-feedback guard.

**Files:**
- Modify: `desktop/src/lib/configClient.ts` (add `WINDOW_MODES`, `WindowMode`, `DEFAULT_WINDOW_MODE`, `windowMode`, `setWindowMode`)
- Modify: `desktop/src/lib/configClient.test.ts` (add window-mode tests near the hotkey tests at lines 795-810)
- Create: `desktop/src/lib/windowFit.ts`
- Create: `desktop/src/lib/windowFit.test.ts`

**Interfaces:**
- Produces:
  - `WINDOW_MODES = ["normal","floating","always_on_top"] as const`; `type WindowMode = (typeof WINDOW_MODES)[number]`
  - `DEFAULT_WINDOW_MODE: WindowMode = "normal"`
  - `windowMode(payload: ConfigPayload): WindowMode`
  - `setWindowMode(payload: ConfigPayload, value: WindowMode): ConfigPayload`
  - `fitDecision(scrollHeight, lastRequestedHeight: number | null, width, tolerance?): { apply: boolean; width: number; height: number }`

- [ ] **Step 1: Write failing `fitDecision` tests**

Create `desktop/src/lib/windowFit.test.ts`:

```ts
import { describe, it, expect } from "vitest";
import { fitDecision } from "./windowFit";

describe("fitDecision", () => {
  it("applies on first measure (no previous request) and rounds to integer px", () => {
    expect(fitDecision(320.4, null, 360)).toEqual({ apply: true, width: 360, height: 320 });
    expect(fitDecision(320.6, null, 360)).toEqual({ apply: true, width: 360, height: 321 });
  });

  it("skips when within tolerance of the last requested height (anti-feedback)", () => {
    expect(fitDecision(320.4, 320, 360)).toEqual({ apply: false, width: 360, height: 320 });
    expect(fitDecision(319.7, 320, 360)).toEqual({ apply: false, width: 360, height: 320 });
  });

  it("applies when the change exceeds tolerance", () => {
    expect(fitDecision(340, 320, 360)).toEqual({ apply: true, width: 360, height: 340 });
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Users/admin/projects/herdeck/desktop && npx vitest run src/lib/windowFit.test.ts`
Expected: FAIL — `Failed to resolve import "./windowFit"` (module does not exist yet).

- [ ] **Step 3: Write `windowFit.ts`**

Create `desktop/src/lib/windowFit.ts`:

```ts
/** A content-fit sizing decision for the borderless deck window. */
export interface FitDecision {
  apply: boolean;
  width: number;
  height: number;
}

/**
 * Decide the next window size from the measured intrinsic content height.
 *
 * Rounds to integer logical px and SKIPS (`apply:false`) when the new height is
 * within `tolerance` px of the last requested height — the anti-feedback guard
 * that stops `setSize -> viewport change -> ResizeObserver -> setSize`
 * oscillation. `width` is passed through unchanged (the borderless window has a
 * fixed, non-resizable width).
 */
export function fitDecision(
  scrollHeight: number,
  lastRequestedHeight: number | null,
  width: number,
  tolerance = 1,
): FitDecision {
  const height = Math.round(scrollHeight);
  if (lastRequestedHeight !== null && Math.abs(height - lastRequestedHeight) <= tolerance) {
    return { apply: false, width, height: lastRequestedHeight };
  }
  return { apply: true, width, height };
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /Users/admin/projects/herdeck/desktop && npx vitest run src/lib/windowFit.test.ts`
Expected: PASS (3 tests).

- [ ] **Step 5: Write failing window-mode helper tests**

In `desktop/src/lib/configClient.test.ts`, add the new symbols to the existing import from `"./configClient"` (the file already imports `toggleDeckHotkey`, `setToggleDeckHotkey`, `DEFAULT_TOGGLE_DECK_HOTKEY` — add these alongside):

```ts
  WINDOW_MODES,
  DEFAULT_WINDOW_MODE,
  windowMode,
  setWindowMode,
```

Then add a describe block (the file already has an `emptyPayload()` helper used by the hotkey tests):

```ts
describe("window mode", () => {
  it("defaults to normal when absent", () => {
    expect(windowMode(emptyPayload())).toBe(DEFAULT_WINDOW_MODE);
    expect(DEFAULT_WINDOW_MODE).toBe("normal");
  });

  it("returns a stored valid mode", () => {
    const p = setWindowMode(emptyPayload(), "always_on_top");
    expect(windowMode(p)).toBe("always_on_top");
  });

  it("falls back to default for an unknown stored value", () => {
    const p = setAt(emptyPayload(), "base", "desktop", "window_mode", "bogus");
    expect(windowMode(p)).toBe("normal");
  });

  it("exposes exactly the three modes", () => {
    expect(WINDOW_MODES).toEqual(["normal", "floating", "always_on_top"]);
  });
});
```

(If `setAt` is not already imported in this test file, add it to the `"./configClient"` import.)

- [ ] **Step 6: Run the tests to verify they fail**

Run: `cd /Users/admin/projects/herdeck/desktop && npx vitest run src/lib/configClient.test.ts`
Expected: FAIL — `windowMode`/`setWindowMode`/`WINDOW_MODES`/`DEFAULT_WINDOW_MODE` are not exported.

- [ ] **Step 7: Add the helpers to `configClient.ts`**

In `desktop/src/lib/configClient.ts`, immediately after `setToggleDeckHotkey` (line 849), add:

```ts
/** The three deck window modes (matches Rust `WindowMode::as_str`). */
export const WINDOW_MODES = ["normal", "floating", "always_on_top"] as const;
export type WindowMode = (typeof WINDOW_MODES)[number];

/** Default deck window mode. Mirrors Rust `parse_window_mode` (missing → Normal). */
export const DEFAULT_WINDOW_MODE: WindowMode = "normal";

/** The configured deck window mode. An ABSENT or unknown value → the default,
 *  mirroring the Rust parser (missing/garbage → Normal). */
export function windowMode(payload: ConfigPayload): WindowMode {
  const v = getAt(payload, "base", "desktop", "window_mode");
  return typeof v === "string" && (WINDOW_MODES as readonly string[]).includes(v)
    ? (v as WindowMode)
    : DEFAULT_WINDOW_MODE;
}

/** NEW payload with base.desktop.window_mode set. */
export function setWindowMode(payload: ConfigPayload, value: WindowMode): ConfigPayload {
  return setAt(payload, "base", "desktop", "window_mode", value);
}
```

- [ ] **Step 8: Run the full frontend suite**

Run: `cd /Users/admin/projects/herdeck/desktop && npm test`
Expected: all vitest suites pass (existing + the new window-mode + windowFit tests).

- [ ] **Step 9: Commit**

```bash
git add desktop/src/lib/configClient.ts desktop/src/lib/configClient.test.ts desktop/src/lib/windowFit.ts desktop/src/lib/windowFit.test.ts
git commit -m "feat(desktop): windowMode config helpers + fitDecision content-fit guard"
```

---

### Task 6: Svelte — modes CSS + drag + content-fit + desktop select

Wire the visuals: `App.svelte` reads the pre-paint mode, wraps content in a rounded `.shell` with a drag handle (borderless), runs the content-fit `ResizeObserver`, moves the reonboard ⚙ into flow, and drops `min-height:100vh`; `DeckView`/`Onboarding` drop `min-height:100vh`; `DesktopSection` gains a `window_mode` `<select>`. **Gate: `npm run build` (full Svelte compile of all three components) + `npm test` (the existing `DesktopSection` compile-smoke now exercises the new select + Task 5 unit tests).** No new unit tests — CSS, drag region, and a `ResizeObserver` are not unit-testable; they are covered by the manual macbench gate in the spec.

**Files:**
- Modify: `desktop/src/App.svelte` (mode read, `.shell`, drag handle, ResizeObserver, reonboard into flow, remove `min-height:100vh`)
- Modify: `desktop/src/lib/DeckView.svelte:163` (remove `min-height: 100vh`)
- Modify: `desktop/src/lib/Onboarding.svelte:122` (remove `min-height: 100vh`)
- Modify: `desktop/src/lib/sections/DesktopSection.svelte` (add `window_mode` select)

**Interfaces:**
- Consumes (Task 5): `fitDecision` from `./lib/windowFit`; `WINDOW_MODES`, `windowMode`, `setWindowMode`, `WindowMode` from `../configClient`.

- [ ] **Step 1: Remove `min-height: 100vh` from DeckView**

In `desktop/src/lib/DeckView.svelte`, the `.deck` rule (lines 158-168) has `min-height: 100vh;` (line 163). Delete that one line. The rule becomes:

```css
  .deck {
    display: flex;
    flex-direction: column;
    gap: 8px;
    box-sizing: border-box;
    padding: 10px;
    background: #0b0b0d;
    font: 12px/1.3 system-ui, -apple-system, sans-serif;
    color: #e7ecf3;
  }
```

- [ ] **Step 2: Remove `min-height: 100vh` from Onboarding**

In `desktop/src/lib/Onboarding.svelte`, the `.onboarding` rule (lines 120-130) has `min-height: 100vh;` (line 122). Delete that one line. The rule becomes:

```css
  .onboarding {
    box-sizing: border-box;
    padding: 24px 18px;
    background: #0b0b0d;
    color: #e7ecf3;
    font: 13px/1.4 system-ui, -apple-system, sans-serif;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
```

- [ ] **Step 3: Rewrite `App.svelte` — mode read, shell, drag, content-fit, reonboard in flow**

Replace the entire `desktop/src/App.svelte` with:

```svelte
<script lang="ts">
  import { onMount } from "svelte";
  import { invoke } from "@tauri-apps/api/core";
  import { listen } from "@tauri-apps/api/event";
  import DeckView from "./lib/DeckView.svelte";
  import Onboarding from "./lib/Onboarding.svelte";
  import { asDiscovery, type Discovery } from "./lib/sidecar";
  import { commandTransport } from "./lib/deckClient";
  import { fitDecision } from "./lib/windowFit";
  import {
    setupTransport,
    shouldOnboard,
    type SetupStatus,
  } from "./lib/onboardingClient";

  // Window mode is injected on <html data-window-mode> by Rust BEFORE first paint
  // (initialization_script), so the borderless CSS applies with no FOUC. Falls
  // back to "normal" in a plain browser (no Tauri / no attribute).
  const windowMode =
    (typeof document !== "undefined"
      ? document.documentElement.dataset.windowMode
      : undefined) ?? "normal";
  const borderless = windowMode !== "normal";

  // Borderless width matches the Rust builder inner_size width; the window is
  // non-resizable, so width is constant and only the height is fit to content.
  const BORDERLESS_WIDTH = 360;

  let shell = $state<HTMLElement | undefined>(undefined);

  let discovery = $state<Discovery | null>(null);
  let status = $state<SetupStatus | null>(null);
  // Manual "change connection" override: open the welcome card even when the
  // status would show the deck (so a demo/local-pinned user can re-onboard).
  let reonboard = $state(false);

  const transport = $derived(
    discovery ? commandTransport((cmd, args) => invoke(cmd, args)) : null,
  );
  const setup = $derived(
    discovery ? setupTransport((cmd, args) => invoke(cmd, args)) : null,
  );

  const view = $derived(shouldOnboard(status, reonboard));

  async function pullDiscovery(): Promise<void> {
    try {
      const d = asDiscovery(await invoke("get_discovery"));
      if (d) discovery = d;
    } catch {
      // Not in a Tauri WebView (plain browser): leave null, DeckView goes offline.
    }
  }

  // Content-fit: size the borderless window to the intrinsic content height. Skips
  // redundant calls via fitDecision's anti-feedback guard. No-op (try/catch) when
  // not in a Tauri WebView.
  let lastRequestedHeight: number | null = null;
  async function fitWindow(scrollHeight: number): Promise<void> {
    const d = fitDecision(scrollHeight, lastRequestedHeight, BORDERLESS_WIDTH);
    if (!d.apply) return;
    lastRequestedHeight = d.height;
    try {
      const { getCurrentWindow, LogicalSize } = await import("@tauri-apps/api/window");
      await getCurrentWindow().setSize(new LogicalSize(d.width, d.height));
    } catch {
      /* not in a Tauri WebView */
    }
  }

  onMount(() => {
    let alive = true;

    void listen<Discovery>("discovery", (event) => {
      const d = asDiscovery(event.payload);
      if (d) discovery = d;
    });
    void listen("reonboard", () => {
      reonboard = true;
    });

    void (async () => {
      while (alive && !discovery) {
        await pullDiscovery();
        if (!discovery) await new Promise((r) => setTimeout(r, 400));
      }
    })();

    void (async () => {
      while (alive) {
        if (setup) status = await setup.status();
        await new Promise((r) => setTimeout(r, status ? 2500 : 600));
      }
    })();

    // Borderless content-fit: observe the shell's intrinsic height and resize the
    // window to match. rAF-batched so a burst of mutations triggers one setSize.
    let ro: ResizeObserver | undefined;
    if (borderless && shell && typeof ResizeObserver !== "undefined") {
      let scheduled = false;
      ro = new ResizeObserver(() => {
        if (scheduled) return;
        scheduled = true;
        requestAnimationFrame(() => {
          scheduled = false;
          if (shell) void fitWindow(shell.scrollHeight);
        });
      });
      ro.observe(shell);
    }

    return () => {
      alive = false;
      ro?.disconnect();
    };
  });

  function onConnected(): void {
    reonboard = false;
    void (async () => {
      if (setup) status = await setup.status();
    })();
  }
</script>

<main class:borderless>
  <div class="shell" bind:this={shell}>
    {#if borderless}<div class="drag" data-tauri-drag-region></div>{/if}
    {#if view === "deck"}
      <DeckView {transport} />
      <!-- Re-onboarding affordance, in document flow so content-fit measures it
           and overflow:hidden never clips it. -->
      <div class="tools">
        <button
          class="reonboard"
          title="Změnit připojení"
          aria-label="Změnit připojení"
          onclick={() => (reonboard = true)}>⚙</button
        >
      </div>
    {:else}
      <Onboarding
        {view}
        {status}
        transport={setup}
        {onConnected}
        onDismiss={reonboard ? () => (reonboard = false) : undefined}
      />
    {/if}
  </div>
</main>

<style>
  /* Opaque by default (normal + plain browser); borderless makes the window
     transparent so the rounded .shell is the only painted surface. */
  :global(html, body) {
    margin: 0;
    background: #0b0b0d;
  }
  :global(html[data-window-mode="floating"]),
  :global(html[data-window-mode="floating"] body),
  :global(html[data-window-mode="always_on_top"]),
  :global(html[data-window-mode="always_on_top"] body) {
    background: transparent;
  }

  main {
    position: relative;
    width: 100vw;
    box-sizing: border-box;
  }
  .shell {
    background: #0b0b0d;
  }
  /* Rounded opaque card flush to the (transparent) window edge so the drop shadow
     traces the card silhouette. */
  main.borderless .shell {
    border-radius: 12px;
    overflow: hidden;
  }
  .drag {
    height: 18px;
    width: 100%;
  }
  .tools {
    display: flex;
    justify-content: flex-end;
    padding: 2px 6px 6px;
  }
  .reonboard {
    width: 22px;
    height: 22px;
    padding: 0;
    border: none;
    border-radius: 6px;
    background: #17171b;
    color: #8b97a4;
    font-size: 12px;
    line-height: 22px;
    cursor: pointer;
    opacity: 0.55;
  }
  .reonboard:hover {
    opacity: 1;
  }
</style>
```

- [ ] **Step 4: Add the `window_mode` select to `DesktopSection.svelte`**

Replace `desktop/src/lib/sections/DesktopSection.svelte` with (adds a `SelectField`, renames the hotkey-only `value`/`set` to `hotkey`/`setHotkey`):

```svelte
<script lang="ts">
  import TextField from "../fields/TextField.svelte";
  import SelectField from "../fields/SelectField.svelte";
  import {
    DEFAULT_TOGGLE_DECK_HOTKEY,
    toggleDeckHotkey,
    setToggleDeckHotkey,
    WINDOW_MODES,
    windowMode,
    setWindowMode,
    type ConfigPayload,
    type WindowMode,
  } from "../configClient";

  let { payload = $bindable(), onChange }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void } = $props();

  const hotkey = $derived(toggleDeckHotkey(payload));
  const mode = $derived(windowMode(payload));
  function setHotkey(v: string): void {
    payload = setToggleDeckHotkey(payload, v);
    onChange();
  }
  function setMode(v: string): void {
    payload = setWindowMode(payload, v as WindowMode);
    onChange();
  }
</script>

<h2>Desktop</h2>
<p class="hint">
  Režim plovoucího okna decku: <code>normal</code> = běžné okno s rámečkem,
  <code>floating</code> = bez rámečku, <code>always_on_top</code> = vždy navrchu.
  Změna se projeví po Apply (přechod na/z <code>normal</code> okno restartuje).
</p>
<SelectField label="window_mode" value={mode} options={[...WINDOW_MODES]} onchange={setMode} />
<p class="hint">
  Globální hotkey pro zobrazení/schování decku. Výchozí
  <code>{DEFAULT_TOGGLE_DECK_HOTKEY}</code>; prázdné pole = hotkey vypnutý.
  Změna se projeví po Apply.
</p>
<TextField label="toggle_deck" value={hotkey} oninput={setHotkey} />

<style>
  h2 { margin: 0 0 8px; }
  .hint { color: #888; margin: 0 0 12px; }
  code { color: #aaa; }
</style>
```

- [ ] **Step 5: Compile-build the frontend**

Run: `cd /Users/admin/projects/herdeck/desktop && npm run build`
Expected: `vite build` succeeds — compiles `App.svelte`, `DeckView.svelte`, `Onboarding.svelte`, `DesktopSection.svelte`, `SelectField.svelte` (catches template/type errors). No errors.

- [ ] **Step 6: Run the frontend test suite**

Run: `cd /Users/admin/projects/herdeck/desktop && npm test`
Expected: all vitest suites pass — including `sections.smoke.test.ts` (compiles the new `DesktopSection` with its `SelectField`) and the Task 5 unit tests.

- [ ] **Step 7: Commit**

```bash
git add desktop/src/App.svelte desktop/src/lib/DeckView.svelte desktop/src/lib/Onboarding.svelte desktop/src/lib/sections/DesktopSection.svelte
git commit -m "feat(desktop): rounded shell + drag handle + content-fit + window_mode select"
```

---

## Self-Review

**1. Spec coverage:**
- Komponenta 1 (`[desktop]` passthrough) → Task 1. ✓
- Komponenta 2a (config-path resolve + `HERDECK_CONFIG` export + `parse_window_mode`) → Task 2 (pure logic) + Task 3 (export wiring). ✓
- Komponenta 2b (dynamic `main`, remove from conf, `macOSPrivateApi` + feature, per-mode builder, FOUC init script, setup ordering, `place_floating` primary_monitor + no always_on_top, `get_window_mode` in generate_handler) → Task 3. ✓
- Komponenta 2c (CloseRequested→prevent_close+hide; restart `!`) → Task 3 (close-intercept) + Task 4 (restart in `select_window_mode`). ✓
- Komponenta 3 (tray submenu, 3 CheckMenuItem + mutual set_checked, persist-then-apply ≥15s, 200-with-errors contract, live vs restart) → Task 4. ✓
- Komponenta 4 (data-window-mode CSS, drag handle, content-fit ResizeObserver + anti-feedback guard, remove min-height ×3, reonboard into flow, configClient helpers) → Tasks 5 + 6. ✓
- Capabilities/deps (`toml`, `macos-private-api`, `macOSPrivateApi:true`, `core:window:allow-set-size`) → Tasks 2 + 3. ✓
- Testing table: Python round-trip → Task 1; Rust parse/switch/resolve → Task 2; Svelte mode helpers + content-measure → Task 5; compile-smoke → Task 6; freeze unchanged (not touched). ✓

**2. Placeholder scan:** No TBD/TODO; every code step has complete code and exact commands. ✓

**3. Type consistency:** `WindowMode` (Rust enum / TS union) is `normal`/`floating`/`always_on_top` everywhere; `as_str` (Rust) ↔ `WINDOW_MODES` (TS) agree. `resolve_config_path(env_override, home, repo_root)`, `read_window_mode(path)`, `switch_needs_restart(from, to)`, `fitDecision(scrollHeight, lastRequestedHeight, width, tolerance?)`, `windowMode(payload)`, `setWindowMode(payload, value)`, `get_window_mode` command, `build_tray(app, mode)`, `start_sidecar(..., config_path)`, `WmItems`, `select_window_mode(app, target, items)` — names/signatures match across the tasks that produce and consume them. ✓

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-30-deck-window-ux.md`. Recommended execution: **Subagent-Driven** (superpowers:subagent-driven-development) — fresh subagent per task, task review between, broad review at the end.
