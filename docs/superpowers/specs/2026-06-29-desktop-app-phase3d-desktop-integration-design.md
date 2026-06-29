# herdeck desktop app — Phase 3d: Desktop integration & polish (design)

**Status:** design approved (brainstorming) · 2026-06-29
**Type:** phase design spec (součást [herdeck desktop app overview](2026-06-23-herdeck-desktop-app-overview.md), fáze 3 „Distribuce & polish")
**Depends on:** Phase 1 (floating deck + tray), Phase 2 (config editor), Phase 3a (frozen sidecar), Phase 3c (onboarding — `reonboard` flow)

## Cíl

Z herdeck desktop appky udělat plnohodnotnou nativní aplikaci: spuštění při
přihlášení, globální hotkey pro přivolání decku, herdeck branding (ikony) a
re-onboarding dostupný z tray. Čtyři nezávislé, ale koherentní „OS-integration"
komponenty v jednom řezu.

## Scope

| # | Komponenta | Vrstvy |
|---|---|---|
| 1 | **Autostart** — tray checkbox „Start at login" | Rust (plugin + tray) |
| 2 | **Globální hotkey** — konfigurovatelný toggle decku | Python (config passthrough) + Rust (plugin + register) + Svelte (editor) |
| 3 | **Tray „Change connection…"** — re-onboarding entry point | Rust (tray + event) + Svelte (listener) |
| 4 | **Ikony** — herdeck branding | asset + `tauri.conf.json` |

## Non-goals

- **Signing / notarizace** (3b) — odloženo na konec (chybí placený Apple Developer účet).
- **Linux balení** (3e).
- Per-tile hotkeys / hotkey pro otevření config okna (jen toggle decku).
- Monochromní template tray ikona (tray dědí app ikonu; mono je pozdější polish).
- Zachování runtime stavu hotkey napříč restarty (zdroj je vždy config).

## Global Constraints

Tato pravidla platí pro **každý** task plánu (kopíruj hodnoty verbatim):

- **Komunikace** lidská česky, **kód a commit messages anglicky**; conventional
  commits; **žádné `Co-Authored-By`**; po commitu zkontrolovat `roborev show <sha>`.
- **Push/PR/merge jen s explicitním souhlasem uživatele.**
- **Token nikdy v JS** — všechny dotazy na sidecar jdou přes Rust commands, které
  token injektují server-side; WebView token nikdy nevidí.
- **Secret hodnoty jednosměrně** — nikdy se nečtou zpět, nelogují, nejdou do TOML.
- **Cílová platforma 3d = macOS** (Linux je 3e); ale plugin volby a default
  akcelerátor musí být cross-platform-safe (`CmdOrCtrl`, `MacosLauncher`).
- **Default hotkey:** `CmdOrCtrl+Shift+D`. **Default autostart:** OFF.
- **Ikona = jen čtverečky + rohy** (žádné jiné tvary); zdroj je
  `desktop/branding/herdeck-logo.png` (zamčená varianta „taper-7").
- **Testy:** Python `.venv/bin/python -m pytest`, lint `.venv/bin/ruff check src tests`
  (OBĚ složky); Rust `cd desktop/src-tauri && cargo test`; Frontend
  `cd desktop && npm test` + `npm run build`. Vzor: logika v framework-free
  TS klientech (Vitest), `.svelte` jen compile-smoke (import → `toBeTruthy`).

---

## Komponenta 1: Autostart (tray checkbox)

**Dependency:** `tauri-plugin-autostart = "2"` (Rust) — inicializace přes
`tauri_plugin_autostart::init(MacosLauncher::LaunchAgent, None)`. Plugin
registruje login item mířící na bundle; API přes `ManagerExt::autolaunch()` →
`.enable()`, `.disable()`, `.is_enabled()`.

**Tray:** v `build_tray` (lib.rs) přibude `CheckMenuItem` s id `autostart` a
labelem **„Start at login"**. Jeho zaškrtnutí odráží `app.autolaunch().is_enabled()`.

- **Zdroj pravdy = OS login item** (plugin `is_enabled()`), nikdy ne TOML → žádný drift.
- Při kliknutí: pokud `is_enabled()` → `disable()`, jinak `enable()`; pak set
  checked podle nového stavu. Chyba pluginu → log, checkbox se vrátí na skutečný stav.
- Checked stav se nastaví při startu (z `is_enabled()`) a po každém toggle.
- **Default OFF:** plugin se neinicializuje s auto-enable; čistý profil = login item není.

**Pozn.:** autostart je reálně funkční jen v buildnuté `.app` (login item míří na
bundle path). V `tauri dev` se zaregistruje na dev binárku — chování ověřitelné
jen v manuálním gate na buildnuté appce.

---

## Komponenta 2: Globální hotkey (toggle decku)

**Dependency:** `tauri-plugin-global-shortcut = "2"` (Rust).

### Config (Python)

Nová base-level tabulka v configu:

```toml
[hotkeys]
toggle_deck = "CmdOrCtrl+Shift+D"
```

- Plumbing = **přidat `"hotkeys"` do `ConfigService.BASE_SECTIONS`**
  (`src/herdeck/deckapp/config_service.py`). Tím `read()` zahrne `hotkeys` do
  `base` payloadu a `write()` ho round-tripuje zpět do TOML.
- **Mimo `Config` dataclass i `_OVERLAY_SECTIONS`** — hotkey není per-profil ani
  součást renderu/resolve; `_build_config` neznámé sekce ignoruje, takže nula
  dopadu na core. `validate_settings` projde (extra tabulka je inertní).
- Prázdný / chybějící `toggle_deck` = hotkey vypnutý (žádná registrace).

### Registrace (Rust)

- Po sidecar discovery Rust fetchne `GET /config` (stejně jako `config_read`
  interně přes `http::http_get`), vytáhne `base.hotkeys.toggle_deck`
  (default `CmdOrCtrl+Shift+D` když chybí, žádná registrace když prázdný).
- Zaregistruje globální shortcut přes `tauri-plugin-global-shortcut`; handler
  **toggluje viditelnost `main` okna** (`is_visible()` → `hide()` / `show()+set_focus()`)
  — čistě Rust-side, bez JS round-tripu.
- **Neplatný akcelerátor** → log + fallback na default (a když i ten selže, jen
  log; deck funguje dál bez hotkey).

### Re-registrace po editaci

- Nový command `reload_hotkey(state)` → znovu fetchne `/config`, odregistruje
  starý a zaregistruje nový shortcut. Vrací `Result<(), String>`.
- Config editor (`ConfigApp.svelte`) ho zavolá **po úspěšném** `config_write`.

### Config editor (Svelte)

- Nová **„Desktop" sekce** (`src/lib/sections/DesktopSection.svelte`) s jedním
  text-fieldem pro `base.hotkeys.toggle_deck` (předvyplněný defaultem).
- **Base-only** (žádný profil overlay / tri-state) — hotkey je globální.
- Logika čtení/zápisu pole (snapshot ↔ `base.hotkeys`) ve framework-free helperu
  testovaném Vitestem; `.svelte` jen compile-smoke.
- Validace lehká: neprázdný string; reálná kontrola = Rust při registraci
  (graceful fallback). Po Apply zavolat `reload_hotkey`.

---

## Komponenta 3: Tray „Change connection…"

- V `build_tray` přibude `MenuItem` s id `reconnect`, label **„Change connection…"**.
- Handler: `app.emit_to("main", "reonboard", ())` (token-free, bez payloadu) +
  `main` okno `show()` + `set_focus()`.
- **`App.svelte`** v `onMount` přidá `listen("reonboard", () => (reonboard = true))`
  — vedle existujícího `listen("discovery", …)`. Tím se ukáže welcome karta
  (`Onboarding` komponenta), **reuse celého 3c-ii flow** — žádná nová onboarding logika.
- Backend `/setup/connect` není first-run-gated (3c-i), takže re-onboarding
  funguje i po prvním spuštění (demo/local-pinned uživatel → remote).

---

## Komponenta 4: Ikony (herdeck branding)

**Zdroj:** `desktop/branding/herdeck-logo.png` (1254×1254, zamčená „taper-7" —
beran: Stream Deck grid + štíhlé zatočené rohy, tyrkysová aktivní klávesa ve
středu).

**Pipeline (implementační task):**

1. **Transparentní rohy** — master má squircle na téměř-černém pozadí. Flood-fill
   od 4 rohů (threshold na near-black) → alfa 0, ať squircle maskuje čistě
   (macOS `.icns` nepřidává zaoblení — silueta musí být v artworku). Výstup
   `icon-source.png` (čtvercový, průhledné rohy).
2. **Icon set** — z `icon-source.png` vygenerovat `32x32.png`, `128x128.png`,
   `128x128@2x.png`, `icon.png` (512²) a `icon.icns` (přes `cargo tauri icon`
   nebo `sips`+`iconutil`). Nahradit současné Tauri placeholdery v
   `desktop/src-tauri/icons/`.
3. **`tauri.conf.json`** — `bundle.icon` doplnit o `icons/icon.icns`.
4. **Tray** dědí `app.default_window_icon()` (lib.rs už to dělá) → propadne automaticky.

Icon-test (16/32 px legibilita) byl ověřen při výběru loga.

---

## Capabilities & dependencies

- `desktop/src-tauri/capabilities/default.json` — doplnit permissions
  `global-shortcut:default` a `autostart:default`.
- `Cargo.toml` — `tauri-plugin-autostart = "2"`, `tauri-plugin-global-shortcut = "2"`.
- Oba pluginy registrované v `tauri::Builder` přes `.plugin(...)`.

## Testing

| Vrstva | Co | Jak |
|---|---|---|
| Python | `ConfigService` read/write round-trip `[hotkeys]`; neznámá sekce nerozbije validate | `pytest` |
| Rust | čistá fce: parse `/config` JSON → akcelerátor + default fallback + empty→none; toggle-visibility helper | `cargo test` |
| Svelte | Desktop-section field helper (snapshot ↔ `base.hotkeys`) | Vitest |
| Svelte | `DesktopSection.svelte`, `App.svelte` (reonboard listener) | compile-smoke |
| Freeze | sidecar freeze+smoke beze změny (config passthrough nemění frozen wiring) | `build-sidecar.sh` + `smoke-sidecar.sh` |

## Manuální gate (`tauri build` → `.app`)

- Tray: „Start at login" zapnout → ověřit login item v System Settings; vypnout → zmizí.
- Hotkey `⌘⇧D` → toggle viditelnosti floating decku; změna v editoru + Apply → nový hotkey platí.
- Tray „Change connection…" → `main` okno se ukáže s welcome kartou.
- Nové herdeck ikony v Docku, tray a okně; čisté maskování (průhledné rohy).

## Otevřené otázky

- Žádné blokující. Mono tray template, signing (3b), Linux (3e) jsou vědomě mimo scope.
