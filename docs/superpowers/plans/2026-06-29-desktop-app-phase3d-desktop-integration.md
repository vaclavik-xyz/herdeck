# Phase 3d — Desktop integration & polish: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dodat herdeck desktop appce autostart (tray checkbox), globální hotkey pro toggle decku, re-onboarding z tray a herdeck branding (ikony).

**Architecture:** Hotkey je raw `[hotkeys]` tabulka v configu (passthrough přes `ConfigService.BASE_SECTIONS`, mimo `Config` dataclass); Rust si akcelerátor přečte ze sidecaru a registruje globální shortcut (handler toggluje `main` okno). Autostart a re-onboarding jsou položky v tray menu; re-onboarding vyšle event `reonboard` do `main` okna a reuse-uje 3c-ii Onboarding flow. Ikony se generují z `desktop/branding/herdeck-logo.png`.

**Tech Stack:** Python (`herdeck.deckapp.config_service`), Rust/Tauri 2.11.3 (`tauri-plugin-autostart` "2", `tauri-plugin-global-shortcut` "2"), Svelte 5 (runes), Vitest, PIL (icon pre-process), `tauri icon`.

**Spec:** `docs/superpowers/specs/2026-06-29-desktop-app-phase3d-desktop-integration-design.md`

## Global Constraints

- Kód a commit messages **anglicky**; lidská komunikace česky; conventional commits; **žádné `Co-Authored-By`**.
- **Push/PR/merge jen s explicitním souhlasem uživatele** — tasky jen commitují lokálně.
- **Token nikdy v JS**: frontend volá token-free Tauri commandy, Rust injektuje token server-side.
- **Secret hodnoty jednosměrně** — nikdy se nečtou zpět/nelogují/nejdou do TOML.
- **Default hotkey:** `CmdOrCtrl+Shift+D`. **Sémantika `toggle_deck`:** chybějící klíč → default; explicitní `""` → vypnuto (žádná registrace). **Default autostart:** OFF.
- **Cílová platforma 3d = macOS**; plugin volby cross-platform-safe (`CmdOrCtrl`, `MacosLauncher::LaunchAgent`).
- **Testy:** Python `.venv/bin/python -m pytest <file> -v`; lint `.venv/bin/ruff check src tests` (OBĚ složky); Rust `cd desktop/src-tauri && ~/.cargo/bin/cargo test`; Frontend `cd desktop && npm test` + `npm run build`. `.svelte` jen compile-smoke; logika ve framework-free TS/Rust fcích.

---

## File Structure

**Python**
- Modify: `src/herdeck/deckapp/config_service.py` — `"hotkeys"` do `BASE_SECTIONS`.
- Test: `tests/test_config_service.py` — round-trip `[hotkeys]`.

**Rust** (`desktop/src-tauri/`)
- Create: `src/hotkey.rs` — čistá fce `toggle_deck_accelerator(&Value) -> Option<String>`.
- Create: `tests/hotkey.rs` — unit testy té fce.
- Modify: `src/lib.rs` — `pub mod hotkey;`, registrace pluginů, `register_toggle_hotkey`, `toggle_main_window`, `reload_hotkey` command, tray položky.
- Modify: `Cargo.toml` — 2 pluginy.
- Modify: `capabilities/default.json` — 2 permissions.

**Frontend** (`desktop/`)
- Modify: `src/lib/configClient.ts` — `DEFAULT_TOGGLE_DECK_HOTKEY`, `toggleDeckHotkey`, `setToggleDeckHotkey`.
- Test: `src/lib/configClient.test.ts` — ty helpery.
- Create: `src/lib/sections/DesktopSection.svelte` — hotkey field.
- Create: `src/lib/sections/sections.smoke.test.ts` — compile-smoke DesktopSection.
- Modify: `src/ConfigApp.svelte` — „Desktop" sekce + `reload_hotkey` po Apply.
- Modify: `src/App.svelte` — `listen("reonboard", …)`.

**Icons**
- Create: `desktop/scripts/make-icon-source.py` — squircle → průhledné rohy + 1024².
- Modify (generated): `desktop/src-tauri/icons/*`, `desktop/src-tauri/tauri.conf.json`.
- Add: `desktop/branding/herdeck-logo.png` (už existuje), `desktop/src-tauri/icon-source.png`.

---

## Task 1: Config `[hotkeys]` passthrough (Python)

**Files:**
- Modify: `src/herdeck/deckapp/config_service.py:20-30` (`BASE_SECTIONS`)
- Test: `tests/test_config_service.py`

**Interfaces:**
- Produces: `ConfigService.read()["base"]["hotkeys"]` round-trips; `write()` persistuje `[hotkeys]` do `config.toml`.

- [ ] **Step 1: Write the failing test**

Přidej na konec `tests/test_config_service.py` (reuse `_svc` helper + `_FakeKeyring` z hlavičky souboru):

```python
def test_read_roundtrips_hotkeys_section(tmp_path, monkeypatch):
    monkeypatch.delenv("TOK", raising=False)
    monkeypatch.setattr(secret_store, "_keyring", _FakeKeyring)
    text = (
        '[[servers]]\nid="local"\nurl="ws://x"\ntoken_env="TOK"\n'
        '[hotkeys]\ntoggle_deck = "CmdOrCtrl+Shift+D"\n'
    )
    svc = _svc(tmp_path, text=text)
    assert svc.read()["base"]["hotkeys"] == {"toggle_deck": "CmdOrCtrl+Shift+D"}


def test_write_roundtrips_hotkeys_section(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "real")
    monkeypatch.setattr(secret_store, "_keyring", _FakeKeyring)
    svc = _svc(tmp_path)
    data = svc.read()
    data["base"]["hotkeys"] = {"toggle_deck": "Alt+Space"}
    assert svc.write(data) == []  # no structural errors
    assert _tomllib.loads((tmp_path / "config.toml").read_text())["hotkeys"] == {
        "toggle_deck": "Alt+Space"
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_config_service.py::test_read_roundtrips_hotkeys_section -v`
Expected: FAIL — `read()["base"]` má `hotkeys` vyfiltrované (není v `BASE_SECTIONS`), `KeyError: 'hotkeys'`.

- [ ] **Step 3: Implement — add "hotkeys" to BASE_SECTIONS**

V `src/herdeck/deckapp/config_service.py` rozšiř tuple:

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
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_config_service.py -v`
Expected: PASS (oba nové + všechny existující).

- [ ] **Step 5: Lint + commit**

```bash
.venv/bin/ruff check src tests
git add src/herdeck/deckapp/config_service.py tests/test_config_service.py
git commit -m "feat(deckapp): round-trip [hotkeys] config section"
```

---

## Task 2: configClient hotkey helpers (TS)

**Files:**
- Modify: `desktop/src/lib/configClient.ts` (přidat za `parseActiveChanged`, před `commandTransport`)
- Test: `desktop/src/lib/configClient.test.ts`

**Interfaces:**
- Consumes: `getAt`, `setAt`, `ConfigPayload` (existující v configClient.ts).
- Produces: `DEFAULT_TOGGLE_DECK_HOTKEY: string`; `toggleDeckHotkey(payload): string`; `setToggleDeckHotkey(payload, value): ConfigPayload`.

- [ ] **Step 1: Write the failing test**

Přidej do `desktop/src/lib/configClient.test.ts` (soubor už importuje z `./configClient`; přidej importy helperů):

```typescript
import {
  DEFAULT_TOGGLE_DECK_HOTKEY,
  toggleDeckHotkey,
  setToggleDeckHotkey,
} from "./configClient";

function emptyPayload() {
  return parseConfig({ base: {}, profiles: {}, local: {}, secrets: {} })!;
}

describe("toggle-deck hotkey helpers", () => {
  it("returns the default when the key is absent", () => {
    expect(toggleDeckHotkey(emptyPayload())).toBe(DEFAULT_TOGGLE_DECK_HOTKEY);
  });

  it("returns an explicit empty string (= disabled) verbatim", () => {
    const p = parseConfig({ base: { hotkeys: { toggle_deck: "" } } })!;
    expect(toggleDeckHotkey(p)).toBe("");
  });

  it("returns a configured accelerator verbatim", () => {
    const p = parseConfig({ base: { hotkeys: { toggle_deck: "Alt+Space" } } })!;
    expect(toggleDeckHotkey(p)).toBe("Alt+Space");
  });

  it("writes the accelerator into base.hotkeys.toggle_deck", () => {
    const p = setToggleDeckHotkey(emptyPayload(), "Ctrl+Shift+K");
    expect(toggleDeckHotkey(p)).toBe("Ctrl+Shift+K");
    expect((p.base.hotkeys as Record<string, unknown>).toggle_deck).toBe("Ctrl+Shift+K");
  });
});
```

(`parseConfig` a `describe/it/expect` už soubor importuje — pokud `parseConfig` chybí v importu, doplň ho.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd desktop && npm test -- configClient`
Expected: FAIL — `toggleDeckHotkey is not a function` / import error.

- [ ] **Step 3: Implement the helpers**

V `desktop/src/lib/configClient.ts` přidej (např. hned za `parseActiveChanged`):

```typescript
/** The default deck-toggle global hotkey. Cross-platform: CmdOrCtrl maps to
 *  Cmd on macOS and Ctrl elsewhere. */
export const DEFAULT_TOGGLE_DECK_HOTKEY = "CmdOrCtrl+Shift+D";

/** The configured deck-toggle accelerator for the editor field. An ABSENT key
 *  shows the default; an explicit "" (disabled) is returned verbatim so the
 *  field can show it empty. Mirrors the Rust semantics (missing=default, ""=off). */
export function toggleDeckHotkey(payload: ConfigPayload): string {
  const v = getAt(payload, "base", "hotkeys", "toggle_deck");
  return typeof v === "string" ? v : DEFAULT_TOGGLE_DECK_HOTKEY;
}

/** NEW payload with base.hotkeys.toggle_deck set (incl. "" to disable). */
export function setToggleDeckHotkey(payload: ConfigPayload, value: string): ConfigPayload {
  return setAt(payload, "base", "hotkeys", "toggle_deck", value);
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd desktop && npm test -- configClient`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add desktop/src/lib/configClient.ts desktop/src/lib/configClient.test.ts
git commit -m "feat(desktop): configClient toggle-deck hotkey helpers"
```

---

## Task 3: DesktopSection.svelte + ConfigApp wiring

**Files:**
- Create: `desktop/src/lib/sections/DesktopSection.svelte`
- Create: `desktop/src/lib/sections/sections.smoke.test.ts`
- Modify: `desktop/src/ConfigApp.svelte` (SECTIONS list + render arm)

**Interfaces:**
- Consumes: `toggleDeckHotkey`, `setToggleDeckHotkey` (Task 2), `TextField.svelte`, `ConfigPayload`.
- Produces: „Desktop" sekce v editoru editující `base.hotkeys.toggle_deck`.

- [ ] **Step 1: Write the failing compile-smoke test**

Create `desktop/src/lib/sections/sections.smoke.test.ts`:

```typescript
import { describe, it, expect } from "vitest";
import DesktopSection from "./DesktopSection.svelte";

// Compile-smoke only: importing a .svelte compiles it (catches syntax/compile
// errors) without a render harness.
describe("section compile-smoke", () => {
  it("compiles DesktopSection", () => {
    expect(DesktopSection).toBeTruthy();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd desktop && npm test -- sections.smoke`
Expected: FAIL — `Failed to resolve import "./DesktopSection.svelte"` (soubor neexistuje).

- [ ] **Step 3: Create DesktopSection.svelte**

Create `desktop/src/lib/sections/DesktopSection.svelte`:

```svelte
<script lang="ts">
  import TextField from "../fields/TextField.svelte";
  import {
    DEFAULT_TOGGLE_DECK_HOTKEY,
    toggleDeckHotkey,
    setToggleDeckHotkey,
    type ConfigPayload,
  } from "../configClient";

  let { payload = $bindable(), onChange }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void } = $props();

  const value = $derived(toggleDeckHotkey(payload));
  function set(v: string): void {
    payload = setToggleDeckHotkey(payload, v);
    onChange();
  }
</script>

<h2>Desktop</h2>
<p class="hint">
  Globální hotkey pro zobrazení/schování plovoucího decku. Výchozí
  <code>{DEFAULT_TOGGLE_DECK_HOTKEY}</code>; prázdné pole = hotkey vypnutý.
  Změna se projeví po Apply.
</p>
<TextField label="toggle_deck" value={value} oninput={set} />

<style>
  h2 { margin: 0 0 8px; }
  .hint { color: #888; margin: 0 0 12px; }
  code { color: #aaa; }
</style>
```

- [ ] **Step 4: Wire into ConfigApp.svelte**

V `desktop/src/ConfigApp.svelte`:

(a) přidej import vedle ostatních section importů (po `ProfilesSection`):

```svelte
  import DesktopSection from "./lib/sections/DesktopSection.svelte";
```

(b) přidej `"Desktop"` na konec `SECTIONS`:

```javascript
  const SECTIONS = [
    "Servers", "Deck", "View", "Theme", "Macros",
    "Start profiles", "Notifications", "Safety", "Answer profiles", "Profiles", "Desktop",
  ];
```

(c) přidej render arm před `{:else}` (za `{:else if active === "Profiles"}` blok):

```svelte
      {:else if active === "Desktop"}
        <DesktopSection bind:payload onChange={markDirty} onError={(m) => setBanner("error", m)} />
```

- [ ] **Step 5: Run smoke test + build to verify they pass**

Run: `cd desktop && npm test -- sections.smoke && npm run build`
Expected: smoke PASS; `npm run build` succeeds (compiles ConfigApp + DesktopSection).

- [ ] **Step 6: Commit**

```bash
git add desktop/src/lib/sections/DesktopSection.svelte desktop/src/lib/sections/sections.smoke.test.ts desktop/src/ConfigApp.svelte
git commit -m "feat(desktop): Desktop config section for the deck-toggle hotkey"
```

---

## Task 4: Rust hotkey accelerator (pure fn)

**Files:**
- Create: `desktop/src-tauri/src/hotkey.rs`
- Create: `desktop/src-tauri/tests/hotkey.rs`
- Modify: `desktop/src-tauri/src/lib.rs` (add `pub mod hotkey;`)

**Interfaces:**
- Produces: `hotkey::DEFAULT_TOGGLE_DECK: &str`; `hotkey::toggle_deck_accelerator(&serde_json::Value) -> Option<String>` — `None` = disabled.

- [ ] **Step 1: Write the failing test**

Create `desktop/src-tauri/tests/hotkey.rs`:

```rust
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd desktop/src-tauri && ~/.cargo/bin/cargo test --test hotkey`
Expected: FAIL — `unresolved import herdeck_desktop_lib::hotkey` (modul neexistuje).

- [ ] **Step 3: Implement hotkey.rs + register the module**

Create `desktop/src-tauri/src/hotkey.rs`:

```rust
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
```

In `desktop/src-tauri/src/lib.rs`, add the module declaration next to the existing ones (near `pub mod http;` / `pub mod sidecar;`):

```rust
pub mod hotkey;
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd desktop/src-tauri && ~/.cargo/bin/cargo test --test hotkey`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add desktop/src-tauri/src/hotkey.rs desktop/src-tauri/tests/hotkey.rs desktop/src-tauri/src/lib.rs
git commit -m "feat(desktop): pure deck-toggle accelerator extraction from /config"
```

---

## Task 5: Global-shortcut plugin + registration + reload_hotkey

**Files:**
- Modify: `desktop/src-tauri/Cargo.toml` (dep)
- Modify: `desktop/src-tauri/capabilities/default.json` (permission)
- Modify: `desktop/src-tauri/src/lib.rs` (plugin, helpers, command, wiring)
- Modify: `desktop/src/ConfigApp.svelte` (`reload_hotkey` po Apply)

**Interfaces:**
- Consumes: `hotkey::toggle_deck_accelerator` (Task 4), `Discovery`, `http::http_get`, `SIDECAR_TIMEOUT`, `current_discovery` (vše v lib.rs).
- Produces: `reload_hotkey` Tauri command; globální shortcut registrovaný po discovery + po editaci configu.

> **Pozn. k API:** kód cílí na `tauri-plugin-global-shortcut` v2. Pokud se přesný název metody/pole liší (např. `event.state` vs `event.state()`), uprav podle toho, co vyžaduje `cargo build` proti nainstalovanému crate — deliverable je ověřený `cargo build` + `cargo test` (existující) + manuální gate, ne nová unit logika (ta je v Task 4).

- [ ] **Step 1: Add the dependency**

V `desktop/src-tauri/Cargo.toml`, do `[dependencies]`:

```toml
tauri-plugin-global-shortcut = "2"
```

- [ ] **Step 2: Add the capability permission**

V `desktop/src-tauri/capabilities/default.json` přidej do `permissions`:

```json
{
  "identifier": "default",
  "description": "Default capability for the floating deck window (core APIs: events for discovery, window controls).",
  "windows": ["main", "config"],
  "permissions": ["core:default", "global-shortcut:default"]
}
```

- [ ] **Step 3: Add the toggle helper + registration fn + command (lib.rs)**

V `desktop/src-tauri/src/lib.rs` přidej helpery (např. nad `build_tray`):

```rust
/// Show/hide the floating `main` window — the deck-toggle hotkey action.
fn toggle_main_window(app: &tauri::AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        if w.is_visible().unwrap_or(false) {
            let _ = w.hide();
        } else {
            let _ = w.show();
            let _ = w.set_focus();
        }
    }
}

/// (Re)register the deck-toggle global shortcut from the sidecar's `/config`.
/// Best-effort: any failure is logged and leaves the deck usable without a hotkey.
fn register_toggle_hotkey(app: &tauri::AppHandle, d: &Discovery) {
    use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};

    let gs = app.global_shortcut();
    let _ = gs.unregister_all();

    let body = match http::http_get(
        &d.host,
        d.port,
        &format!("/config?token={}", d.token),
        SIDECAR_TIMEOUT,
    ) {
        Ok(b) => b,
        Err(e) => {
            eprintln!("hotkey: /config fetch failed: {e}");
            return;
        }
    };
    let cfg: serde_json::Value = match serde_json::from_str(&body) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("hotkey: invalid /config JSON: {e}");
            return;
        }
    };
    let accel = match hotkey::toggle_deck_accelerator(&cfg) {
        Some(a) => a,
        None => return, // explicitly disabled
    };

    let app_for_cb = app.clone();
    let handler = move |_app: &tauri::AppHandle, _sc: &_, event: tauri_plugin_global_shortcut::ShortcutEvent| {
        if event.state == ShortcutState::Pressed {
            toggle_main_window(&app_for_cb);
        }
    };
    if let Err(e) = gs.on_shortcut(accel.as_str(), handler) {
        eprintln!("hotkey: register '{accel}' failed: {e}");
        if accel != hotkey::DEFAULT_TOGGLE_DECK {
            let app_for_fb = app.clone();
            let _ = gs.on_shortcut(
                hotkey::DEFAULT_TOGGLE_DECK,
                move |_app: &tauri::AppHandle, _sc: &_, event: tauri_plugin_global_shortcut::ShortcutEvent| {
                    if event.state == ShortcutState::Pressed {
                        toggle_main_window(&app_for_fb);
                    }
                },
            );
        }
    }
}

/// Re-read `/config` and re-register the deck-toggle hotkey (the editor calls
/// this after a successful config write so a changed accelerator takes effect).
#[tauri::command]
fn reload_hotkey(app: tauri::AppHandle, state: tauri::State<'_, AppState>) -> Result<(), String> {
    let d = current_discovery(&state)?;
    register_toggle_hotkey(&app, &d);
    Ok(())
}
```

- [ ] **Step 4: Register the plugin, the command, and call register on discovery**

V `lib.rs` `run()`:

(a) přidej plugin do builderu (za `.manage(state)`):

```rust
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
```

(b) přidej `reload_hotkey` do `generate_handler![...]` (za `open_config`):

```rust
            open_config,
            reload_hotkey
```

(c) ve `start_sidecar` zaregistruj hotkey, jakmile dorazí discovery — v OBOU větvích, PŘED tím, než se `d` přesune do `discovery`:

External větev:
```rust
        SidecarPlan::External(d) => {
            let view = DiscoveryView::from(&d);
            register_toggle_hotkey(&app.handle(), &d);
            *discovery.lock().unwrap() = Some(d);
            let _ = app.handle().emit("discovery", view); // token-free
        }
```

Spawn větev (uvnitř `supervise(... move |d| { ... })`):
```rust
                supervise(SupervisorConfig::new(spec), child, stop, move |d| {
                    let view = DiscoveryView::from(&d);
                    register_toggle_hotkey(&handle, &d);
                    if let Some(state) = handle.try_state::<AppState>() {
                        *state.discovery.lock().unwrap() = Some(d);
                    }
                    let _ = handle.emit("discovery", view); // token-free
                });
```

- [ ] **Step 5: Wire the editor to reload after Apply (ConfigApp.svelte)**

V `desktop/src/ConfigApp.svelte`, ve `apply()`, v úspěšné větvi `if (res.length === 0)` hned po `await load();` přidej:

```javascript
        dirty = false;
        await load(); // re-read saved state (preview refreshes itself via its own poll)
        // A changed [hotkeys] accelerator only takes effect once Rust re-registers it.
        void invoke("reload_hotkey").catch(() => {});
```

- [ ] **Step 6: Build + verify existing tests still pass**

Run: `cd desktop/src-tauri && ~/.cargo/bin/cargo build && ~/.cargo/bin/cargo test`
Expected: build succeeds; existing tests (http, spawn, hotkey) PASS.
Run: `cd desktop && npm run build`
Expected: succeeds (ConfigApp compiles with the new invoke).

- [ ] **Step 7: Commit**

```bash
git add desktop/src-tauri/Cargo.toml desktop/src-tauri/Cargo.lock desktop/src-tauri/capabilities/default.json desktop/src-tauri/src/lib.rs desktop/src/ConfigApp.svelte
git commit -m "feat(desktop): register deck-toggle global hotkey from config"
```

---

## Task 6: Autostart plugin + tray checkbox

**Files:**
- Modify: `desktop/src-tauri/Cargo.toml` (dep)
- Modify: `desktop/src-tauri/capabilities/default.json` (permission)
- Modify: `desktop/src-tauri/src/lib.rs` (plugin + `CheckMenuItem`)

**Interfaces:**
- Consumes: existing `build_tray`, `Menu`, `MenuItem` (lib.rs).
- Produces: tray „Start at login" checkbox reflektující/togglující `app.autolaunch()`.

> **Pozn. k API:** cílí na `tauri-plugin-autostart` v2 (`ManagerExt::autolaunch()` → `is_enabled/enable/disable`, `MacosLauncher::LaunchAgent`). Uprav názvy podle crate, pokud `cargo build` vyžaduje.

- [ ] **Step 1: Add the dependency**

V `desktop/src-tauri/Cargo.toml` `[dependencies]`:

```toml
tauri-plugin-autostart = "2"
```

- [ ] **Step 2: Add the capability permission**

V `desktop/src-tauri/capabilities/default.json` rozšiř `permissions` o `"autostart:default"`:

```json
  "permissions": ["core:default", "global-shortcut:default", "autostart:default"]
```

- [ ] **Step 3: Register the plugin**

V `lib.rs` `run()`, do builder řetězce (za global-shortcut plugin z Tasku 5):

```rust
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            None,
        ))
```

- [ ] **Step 4: Add the CheckMenuItem to the tray**

V `build_tray` (`lib.rs`) uprav importy a menu. Nahoře v souboru rozšiř menu import:

```rust
use tauri::menu::{CheckMenuItem, Menu, MenuItem};
```

Ve `build_tray`, mezi vytvořením položek a `Menu::with_items`:

```rust
    use tauri_plugin_autostart::ManagerExt;
    let settings = MenuItem::with_id(app, "settings", "Settings…", true, None::<&str>)?;
    let show = MenuItem::with_id(app, "show", "Show", true, None::<&str>)?;
    let hide = MenuItem::with_id(app, "hide", "Hide", true, None::<&str>)?;
    let autostart = CheckMenuItem::with_id(
        app,
        "autostart",
        "Start at login",
        true,
        app.autolaunch().is_enabled().unwrap_or(false),
        None::<&str>,
    )?;
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&settings, &show, &hide, &autostart, &quit])?;
    let autostart_cb = autostart.clone();
```

V `.on_menu_event(|app, event| match event.id.as_ref() { ... })` přidej arm (a closure musí být `move`, ať pobere `autostart_cb`):

```rust
        .on_menu_event(move |app, event| match event.id.as_ref() {
            // ...existing settings/show/hide arms unchanged...
            "autostart" => {
                use tauri_plugin_autostart::ManagerExt;
                let mgr = app.autolaunch();
                let now = mgr.is_enabled().unwrap_or(false);
                let res = if now { mgr.disable() } else { mgr.enable() };
                if let Err(e) = res {
                    eprintln!("autostart toggle failed: {e}");
                }
                let _ = autostart_cb.set_checked(mgr.is_enabled().unwrap_or(false));
            }
            "quit" => app.exit(0),
            _ => {}
        });
```

- [ ] **Step 5: Build + verify existing tests still pass**

Run: `cd desktop/src-tauri && ~/.cargo/bin/cargo build && ~/.cargo/bin/cargo test`
Expected: build succeeds; existing tests PASS.

- [ ] **Step 6: Commit**

```bash
git add desktop/src-tauri/Cargo.toml desktop/src-tauri/Cargo.lock desktop/src-tauri/capabilities/default.json desktop/src-tauri/src/lib.rs
git commit -m "feat(desktop): tray 'Start at login' autostart checkbox"
```

---

## Task 7: Tray „Change connection…" + App.svelte listener

**Files:**
- Modify: `desktop/src-tauri/src/lib.rs` (tray item + emit)
- Modify: `desktop/src/App.svelte` (`listen("reonboard", …)`)

**Interfaces:**
- Consumes: `build_tray`, `tauri::Emitter` (už importováno v lib.rs), App.svelte `reonboard` `$state` (existuje z 3c-ii).
- Produces: tray „Change connection…" → event `reonboard` → welcome karta.

- [ ] **Step 1: Add the tray MenuItem + handler (lib.rs)**

Ve `build_tray`, přidej položku `reconnect` (mezi `hide` a `autostart`):

```rust
    let reconnect = MenuItem::with_id(app, "reconnect", "Change connection…", true, None::<&str>)?;
```

a zařaď ji do menu:

```rust
    let menu = Menu::with_items(app, &[&settings, &show, &hide, &reconnect, &autostart, &quit])?;
```

V `.on_menu_event(...)` přidej arm:

```rust
            "reconnect" => {
                let _ = app.emit_to("main", "reonboard", ());
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.show();
                    let _ = w.set_focus();
                }
            }
```

- [ ] **Step 2: Add the App.svelte listener**

V `desktop/src/App.svelte`, v `onMount`, vedle existujícího `listen<Discovery>("discovery", …)` přidej:

```typescript
    void listen("reonboard", () => {
      reonboard = true;
    });
```

- [ ] **Step 3: Build + verify**

Run: `cd desktop/src-tauri && ~/.cargo/bin/cargo build`
Expected: build succeeds.
Run: `cd desktop && npm run build && npm test`
Expected: build succeeds; all frontend tests PASS (no regression).

- [ ] **Step 4: Commit**

```bash
git add desktop/src-tauri/src/lib.rs desktop/src/App.svelte
git commit -m "feat(desktop): tray 'Change connection…' re-onboarding entry point"
```

---

## Task 8: herdeck icon set

**Files:**
- Create: `desktop/scripts/make-icon-source.py`
- Add: `desktop/branding/herdeck-logo.png` (už ve working tree), `desktop/src-tauri/icon-source.png`
- Modify (generated): `desktop/src-tauri/icons/*`
- Modify: `desktop/src-tauri/tauri.conf.json` (`bundle.icon`)

**Interfaces:**
- Consumes: `desktop/branding/herdeck-logo.png` (master, 1254² RGB squircle).
- Produces: icon set v `desktop/src-tauri/icons/` (vč. `icon.icns`) + `bundle.icon` na něj odkazuje.

> Asset task — není TDD; deliverable se ověřuje kontrolami v Step 4 + manuálním gate.

- [ ] **Step 1: Write the icon-source pre-processor**

Create `desktop/scripts/make-icon-source.py`:

```python
"""Turn the herdeck logo master (a dark squircle on a near-black frame) into a
clean macOS icon source: transparent OUTSIDE the squircle, 1024x1024.

macOS .icns does NOT auto-round icons — the squircle silhouette must live in the
artwork with transparent corners. We take the bounding box of the non-near-black
(the squircle) and apply a rounded-rectangle alpha mask at that bbox, so the
corners become transparent regardless of the gradient.
"""
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent  # desktop/
SRC = ROOT / "branding" / "herdeck-logo.png"
OUT = ROOT / "src-tauri" / "icon-source.png"

im = Image.open(SRC).convert("RGBA")
px = im.load()
w, h = im.size

# bbox of the squircle = pixels brighter than the near-black frame (~ (7,9,14))
xs, ys = [], []
for y in range(h):
    for x in range(w):
        r, g, b, _ = px[x, y]
        if max(r, g, b) > 22:
            xs.append(x)
            ys.append(y)
x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)

mask = Image.new("L", (w, h), 0)
radius = int(0.22 * (x1 - x0))  # macOS-squircle-ish corner radius
ImageDraw.Draw(mask).rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=255)
im.putalpha(mask)

im = im.resize((1024, 1024), Image.LANCZOS)
im.save(OUT)
print(f"wrote {OUT} ({im.size[0]}x{im.size[1]} RGBA)")
```

- [ ] **Step 2: Generate the icon source**

Run: `.venv/bin/python desktop/scripts/make-icon-source.py`
Expected: `wrote .../desktop/src-tauri/icon-source.png (1024x1024 RGBA)`.

- [ ] **Step 3: Generate the icon set + update the bundle config**

Run: `cd desktop && npm run tauri -- icon src-tauri/icon-source.png`
Expected: regenerates `src-tauri/icons/` (32x32.png, 128x128.png, 128x128@2x.png, icon.png, **icon.icns**, icon.ico, Square*Logo*.png).

Then in `desktop/src-tauri/tauri.conf.json`, add `icons/icon.icns` to `bundle.icon`:

```json
    "icon": [
      "icons/32x32.png",
      "icons/128x128.png",
      "icons/128x128@2x.png",
      "icons/icon.png",
      "icons/icon.icns"
    ]
```

- [ ] **Step 4: Verify the deliverable**

Run:
```bash
cd /Users/admin/projects/herdeck/desktop
test -f src-tauri/icons/icon.icns && echo "icns OK"
sips -g pixelWidth -g pixelHeight src-tauri/icons/128x128.png | grep -E 'pixel(Width|Height)'
python3 -c "import json; d=json.load(open('src-tauri/tauri.conf.json')); assert 'icons/icon.icns' in d['bundle']['icon'], 'icns not in bundle.icon'; print('bundle.icon OK')"
python3 -c "from PIL import Image; im=Image.open('src-tauri/icons/128x128.png'); assert im.mode=='RGBA' and im.getextrema()[3][0]==0, 'corners not transparent'; print('transparent corners OK')"
```
Expected: `icns OK`; pixelWidth/Height = 128; `bundle.icon OK`; `transparent corners OK`.

- [ ] **Step 5: Commit**

```bash
cd /Users/admin/projects/herdeck
git add desktop/branding/herdeck-logo.png desktop/scripts/make-icon-source.py desktop/src-tauri/icon-source.png desktop/src-tauri/icons desktop/src-tauri/tauri.conf.json
git commit -m "feat(desktop): herdeck app + tray icon set (ram + deck grid)"
```

---

## Manuální gate (po SDD, na buildnuté `.app`)

Spustit `cd desktop && npm run tauri -- build` a na výsledné `.app` ověřit:
- Tray „Start at login" zapnout → login item v System Settings; vypnout → zmizí.
- Hotkey `⌘⇧D` → toggle viditelnosti floating decku; změna v editoru („Desktop" sekce) + Apply → nový hotkey platí (starý ne).
- Tray „Change connection…" → `main` okno se ukáže s welcome kartou.
- Nové herdeck ikony v Docku, tray a okně; průhledné rohy maskují čistě.

---

## Self-Review

**Spec coverage:**
- Autostart (tray checkbox, default OFF, plugin-authoritative) → Task 6. ✓
- Hotkey config `[hotkeys]` passthrough → Task 1; helpers → Task 2; editor sekce → Task 3; pure extract → Task 4; registrace + reload → Task 5. ✓ (sémantika missing=default / ""=off konzistentní v Task 2 helperu, Task 4 fci a editoru.)
- Tray „Change connection…" + App listener → Task 7. ✓
- Ikony z `desktop/branding/herdeck-logo.png` + průhledné rohy + `tauri icon` + conf → Task 8. ✓
- Capabilities (`global-shortcut:default`, `autostart:default`) → Task 5 + Task 6. ✓
- Testy: Python (T1), Vitest helpers (T2), compile-smoke (T3), Rust unit (T4), build/existing (T5–T7), asset verify (T8). ✓

**Type consistency:** `toggle_deck` semantics (absent→default, ""→off) shodné v `toggleDeckHotkey` (T2), `toggle_deck_accelerator` (T4), editoru (T3). `DEFAULT_TOGGLE_DECK_HOTKEY` (TS) == `DEFAULT_TOGGLE_DECK` (Rust) == `"CmdOrCtrl+Shift+D"`. `reload_hotkey` command (T5) ↔ `invoke("reload_hotkey")` (T5 ConfigApp). `register_toggle_hotkey`/`toggle_main_window` definované v T5, volané v T5.

**Placeholder scan:** žádné TBD/„handle errors"; každý code step má konkrétní kód. Tauri plugin API má explicitní „adapt to crate" pozn. u T5/T6 (integration tasky gated buildem), ne placeholder.
