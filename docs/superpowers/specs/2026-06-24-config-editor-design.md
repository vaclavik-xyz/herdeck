# Config editor (desktop app Phase 2) — design

**Status:** design approved (brainstorming) · 2026-06-24
**Type:** feature spec (Phase 2 of the herdeck desktop app)
**Branch:** `feat/config-editor`
**Navazuje na:** `docs/superpowers/specs/2026-06-23-herdeck-desktop-app-overview.md` (Phase 2),
`docs/superpowers/specs/2026-06-24-config-model-unification-design.md` (sjednocený model, který tohle odblokoval)

## Problém

herdeck config se dnes edituje ručně v `config.toml`/`local.toml` nebo přes tlačítka
na decku — bez náhledu, co změna udělá, a bez validace. Phase 1 (floating deck) dodala
desktop app skeleton (Tauri shell + Python sidecar + `DeckView`). Phase 2 přidá druhé
okno: **GUI config editor** celého sjednoceného configu s live preview decku.

Config-model unification (právě smergovaná) byla explicitní precursor: dřív byly dva
rozcházející se formáty a overview proto řadil editaci profilů/answer_profiles mimo
scope. Teď je jeden model (plochá báze + `[profiles.X]` overlaye), takže editor pokryje
**i profily a jejich overlaye** — non-goal z overview (řádek 93) tímto padá.

## Klíčový princip

**Žádná nová logika configu.** Editor je tenká GUI vrstva nad existujícím
`settings`/`config` jádrem — stejně jako floating deck je tenká vrstva nad
`orchestrator`/`icons`. Stejný sidecar, stejný token-auth, stejný Tauri proxy vzor.

## Klíčová rozhodnutí

1. **Rozsah modelu = plný sjednocený model.** Editor edituje *zdrojový* config (TOML):
   plochou bázi všech sekcí + `[profiles.X]` overlaye + `local.toml`. Včetně správy
   profilů (list / aktivace / create / delete) a editace overlay polí profilu.
2. **Apply/reload = A+B přes jeden sdílený watcher.** (A) In-app reload: sidecar po
   zápisu přerenderuje preview/floating deck. (B) File-watch hot-reload: samostatný
   herdeck (`app.py`, fyzický D200 / web sim) sleduje mtime configu a reloadne se přes
   existující `switch_profile`-reload cestu. **Bez IPC** (varianta C zamítnuta — její
   jediná výhoda, cross-machine push, v téhle topologii nefunguje bez config sync, který
   je mimo scope).
3. **Secrets = keychain + jeden sdílený resolver, env-first.** Editor ukládá hodnotu tokenu
   do OS keychainu (`keyring`, service `"herdeck"`) pod názvem `token_env`. **Všichni**
   konzumenti tokenů čtou přes **jeden** sdílený resolver `secrets.get_secret(name)` s
   **env-first** fallbackem (`os.environ.get` → `keyring.get_password`): jak
   `settings._server_config` (server tokeny), `app._build_notifier` (telegram token) i
   `doctor` (`check_config`/`check_notifications`/`_read_config_facts` — token-presence
   diagnostika; dnes čtou přímo `os.environ.get`). Bez toho by token uložený editorem do
   keychainu runtime fungoval, ale `herdeck-doctor` by ho hlásil jako chybějící (a u
   notifications by ho i runtime ignoroval). Existující dev/CI env flow
   (`HERDECK_TOKEN`) zůstává beze změny a může přebít. TOML nese vždy jen `token_env` název,
   nikdy hodnotu.
4. **Onboarding = minimální v Phase 2.** Bez configu editor ukáže prázdný formulář s
   defaulty + inline „přidej první server"; první Apply zapíše čerstvý `config.toml`. Plný
   first-run wizard je Phase 3.

Zafixované z overview: layout = sidebar + formulář + trvalý preview (varianta A) + lehká
klikací vrstva; preview mock-default / živý když je bridge; TOML zápis `tomli-w` s hlavičkou
„Managed by herdeck-config" (komentáře zaniknou, tomlkit = pozdější); stack Tauri + Svelte +
frozen Python sidecar; macOS primárně, Linux druhotně, Windows mimo scope.

## Architektura

```
Tauri shell (Rust)
 ├─ Okno "main"   (360×600, always-on-top) → Floating deck   ─┐  oba reuse
 └─ Okno "config" (větší, dekorované)       → Config editor  ─┤  DeckView (preview)
                                                              │  Tauri proxy cmds (token v Rustu)
                                                              ▼
 Python sidecar (jeden, frozen):
   ├─ DeckApp        — render/press (Phase 1, beze změny v podstatě)
   └─ ConfigService  — NOVÁ čistá jednotka: read/validate/write TOML + profily + keychain
        nad settings.{load_settings,resolve_profile,validate_settings,set_active_profile},
        config.*, tomli-w (zápis), keyring (secrets)
```

### Nové / dotčené jednotky

| Vrstva | Soubor | Zodpovědnost |
|---|---|---|
| Python | `src/herdeck/deckapp/config_service.py` (nový) | `ConfigService`: read (redacted) / validate / write (atomic) / profil ops / keychain ops. Čistá, bez HTTP. |
| Python | `src/herdeck/deckapp/server.py` (rozšíření) | config HTTP routes delegující na `ConfigService`; po zápisu spustí in-app reload. |
| Python | `src/herdeck/deckapp/watcher.py` (nový) | `ConfigWatcher`: vlákno, sleduje mtime config/local.toml, volá reload callback. |
| Python | `src/herdeck/secrets.py` (nový) + `settings.py` `_server_config` + `app.py` `_build_notifier` (malé změny) | token: `os.environ.get` (env-first) → `keyring.get_password("herdeck", token_env)` fallback, přes sdílený `secrets.get_secret` — používají ho server tokeny i telegram token. |
| Python | `src/herdeck/app.py` (rozšíření) | napojení `ConfigWatcher` na existující reload entrypoint (hot-reload B pro samostatný deck). |
| Rust | `desktop/src-tauri/tauri.conf.json`, `src/lib.rs` | 2. okno „config"; proxy cmds `config_get/validate/write/set_active/secret_set/secret_clear` (token injektuje Rust). |
| Frontend | `desktop/src/lib/configClient.ts` (nový) | typovaný klient (mirror `deckClient.ts`), čistá logika bez Tauri. |
| Frontend | `desktop/src/ConfigApp.svelte` + `desktop/src/lib/sections/*.svelte` (nové) | editor UI: sidebar + formuláře sekcí + správa profilů + reuse `DeckView`. |

Hranice: `ConfigService` testovatelná čistě v Pythonu bez HTTP/GUI; `configClient.ts` bez
Tauri; UI komponenty nad klientem. Root komponenta se vybírá podle window labelu
(`main` → floating deck, `config` → editor).

## ConfigService API + datový model

Editor edituje **zdroj** (TOML), ne resolvnutý `Config`. Datový tvar mezi sidecarem a UI:

```json
{
  "base":     { "servers": [{"id","url","token_env"}], "deck": {…}, "answer_profiles": {…},
                "macros": [{"label","text"}], "start_profiles": {…}, "notifications": {…},
                "theme": {…}, "view": {…}, "safety": {…} },
  "profiles": { "mobile": { "extends": "default", "servers": ["local"], "view": {…}, … } },
  "local":    { "active_profile": "mobile", "hardware": {…} },
  "secrets":  { "HERDECK_TOKEN": {"set": true, "source": "keychain"}, "TG": {"set": false} }
}
```

Secrets putují **jen jako příznak `set` + `source`** (`env`|`keychain`|null), hodnota nikdy do UI.

**`ConfigService` metody (čisté, bez HTTP):**

- `read() -> dict` — výše uvedený tvar přes `load_settings`; secrets redacted, presence z
  env|keychain.
- `validate(data: dict) -> list[str]` — postaví `SettingsSnapshot` z navrženého dictu (ne
  z disku) a zavolá `validate_settings`. „secret chybí" (`env var … is not set`) je v
  seznamu odlišitelná → UI ji bere jako actionable, ne tvrdou chybu.
- `write(data: dict) -> list[str]` — **gate na strukturální chyby** (cyklus, neznámý
  server/profil ref, špatný grid, rezervovaný `[profiles.default]`, malformed); vrátí je
  bez zápisu. Missing-secret zápis **neblokuje**. Atomický zápis: serialize base+profiles
  přes `tomli-w`, hlavička `# Managed by herdeck-config — generated, manual comments are not preserved`,
  zapsat do temp + `os.replace`. `local.toml` (active_profile + hardware) zvlášť, stejně
  atomicky. Nikdy nezapisuje secret hodnoty.
- `set_active(name: str) -> bool` — = `settings.set_active_profile` (píše local.toml
  active_profile, respektuje env-lock → False).
- `create_profile(name)` / `delete_profile(name)` — úprava `[profiles.*]` v base datech (+
  následný `write`); `name` nesmí být `"default"`, create nesmí kolidovat.
- `set_secret(token_env, value)` / `clear_secret(token_env)` / `has_secret(token_env) -> bool`
  — přes `keyring` (service `"herdeck"`); `set/clear` nikdy nelogováno; `has` = env || keychain.

**HTTP routes** (v `server.py`, stejný token-auth jako deck routes; GET = query-token,
mutace = header-token `X-Herdeck-Token`):

| Metoda | Cesta | Akce |
|---|---|---|
| GET | `/config` | `read()` (redacted) |
| POST | `/config/validate` | body = navržený data → `validate()` errors |
| POST | `/config` | `validate` strukturu → `write()` → in-app reload; vrátí ok / errors |
| POST | `/profiles/active` | `{name}` → `set_active()` |
| POST | `/secret` | `{token_env, value}` → `set_secret()` |
| DELETE | `/secret/{token_env}` | `clear_secret()` |

Secret hodnota jde přes loopback (127.0.0.1) + token; `log_message` je v handleru už umlčený.

## Reload (A+B) přes sdílený watcher

`ConfigWatcher` (vlákno, mtime/FSEvents poll ~1 s) volá reload callback při změně
config/local.toml:

- **Sidecar (A):** callback = znovu vybrat source (`select_live`/mock) + re-render → preview
  i floating deck odráží nový config. Pokrývá i externí editaci souboru.
- **Samostatný herdeck (B):** v `app.py` se watcher napojí na **existující** reload
  entrypoint (`switch_profile`-reload cesta, dnes spouštěná tlačítkem na decku) → re-resolve
  + re-render; když se změní servery/tokeny, reconnect. Watcher jen volá existující cestu,
  žádná nová reload logika.

## Frontend UX

**Layout (varianta A):** sidebar (nav sekcí) + formulář aktuální sekce + trvalý preview
(`DeckView`) + lehká klikací vrstva (klik na dlaždici v preview → skok do relevantní sekce).

**Sekce v sidebaru** (zrcadlí model): Servers · Deck · View · Theme · Macros · Start
profiles · Notifications · Safety · Answer profiles · Profiles.

**Přepínač profilu nahoře** řídí dva režimy:

- **Base mód** (`default (báze)`): formuláře editují bázi přímo.
- **Overlay mód** (vybraný pojmenovaný profil): každé pole ukazuje zděděnou bázovou hodnotu
  jako placeholder (dimmed); přepsaná pole zvýrazněná; per-field toggle **override / clear
  override** (clear odebere overlay pole → vrátí se na zděděné). `extends` a výběr `servers`
  profilu jsou v sekci Profiles.

**Preview = saved-state (v1):** ukazuje resolvnutý config pro vybraný profil (mock|živý).
Tok: edit → Apply → watcher reload → preview se obnoví. Disk = jediný zdroj pravdy, žádná
druhá in-memory render cesta. Live preview *neuložených* editů (`/config/preview` endpoint)
= pozdější vylepšení.

## Error handling

- **Validace:** inline u sekce + souhrnný banner. Apply blokován na **strukturálních**
  chybách; **missing-secret** jen varuje a linkuje na „nastav token".
- **Missing secret:** inline prompt v Servers/Notifications → zadání hodnoty → keychain.
- **Sidecar nedostupný / write selže:** ne-destruktivní toast; in-memory edity zůstanou
  (žádná ztráta dat).
- **env-locked profil** (`HERDECK_PROFILE`): přepínač zamčený, set_active disabled s
  vysvětlením.
- **No-config:** prázdný formulář s defaulty + inline „přidej první server"; první Apply
  zapíše čerstvý `config.toml`.

## Testing (TDD)

- **Python `ConfigService`:** read redakce (secret hodnota nikdy ve výstupu); validate
  navrženého dictu (strukturální vs. missing-secret odlišení); atomic write round-trip
  (tomli-w → tomllib zpět == vstup, hlavička přítomná, secret hodnota nikde); profil
  create/delete/set_active; keychain set/has/clear přes fake `keyring` backend.
- **Python HTTP routes:** token-auth (403 bez tokenu), `/config` read/validate/write happy
  + error path, secret endpointy nikdy neuniknou hodnotu (a nelogují).
- **Python watcher:** mtime změna → reload callback zavolán; žádná změna → ticho.
- **Core `_server_config`:** env-first (env nastavený → použije env, keychain se nečte);
  keychain fallback (env prázdné → keyring); obojí prázdné → `ConfigError` (zachovat).
- **Frontend `configClient.ts`:** čisté unit testy (mirror `deckClient.test.ts`, bez Tauri):
  serialize/parse config, override/clear-override logika, secret-presence mapping,
  validate-call shaping.
- **Rust:** proxy cmd testy (token injection) mirroring `tests/http.rs`/`spawn.rs`.
- Reálné chování, ne mocky: skutečný tomllib/tomli-w round-trip, keyring test backend.

## Implementační řezy (pro writing-plans)

1. **Backend core:** `ConfigService` (read/validate/write/profily/secrets) + keychain +
   `config._server_config` env-first/keychain fallback. (+ `tomli-w`, `keyring` deps.)
2. **Backend HTTP + reload:** config routes v `server.py` + in-app reload + `ConfigWatcher`
   + napojení B do `app.py`.
3. **Frontend transport:** `configClient.ts` + 2. Tauri okno „config" + proxy cmds (token v Rustu).
4. **Frontend UI:** `ConfigApp.svelte` + sekce formuláře + profil/overlay UX + preview reuse
   + minimální onboarding.

## Závislosti

- **Nové:** `tomli-w` (zápis TOML; čtení = stdlib `tomllib`), `keyring` (cross-platform OS
  keychain). `keyring` musí být dostupný tam, kde se resolvuje config (sidecar i core pro
  fyzický deck) — přidat do příslušné deps group + do frozen bundle (PyInstaller hidden imports).

## Non-goals (Phase 2)

- Zachování komentářů v TOML (tomlkit) — pozdější vylepšení.
- Plný onboarding wizard — Phase 3.
- Live preview neuložených editů (`/config/preview`) — pozdější vylepšení.
- Cross-machine config sync / IPC reload (varianta C).
- File-watch hot-reload (B) v **local/mock módu** samostatného herdecku — jen remote mód.
  V local módu `resolve_runtime_config` syntetizuje lokální bridge server/token, který by
  re-aplikace surového file configu odstranila; local/mock deck nabere edity při restartu.
  (Sidecar in-app reload A tím netrpí — jede přes bridge/mock, ne přes lokální herdr socket.)
- Windows build; jemná direct-manipulace na decku; signing/distribuce (Phase 3).

## Otevřené otázky (k vyřešení v plánu / implementaci)

- Přesný název existujícího reload entrypointu v `app.py`, na který se watcher napojí
  (`switch_profile` cesta) — ověřit při řezu 2.
- `keyring` backend na headless Linuxu (Secret Service nemusí běžet) — fallback na env je
  pojistka; případně doplnit hlášku.
- Debounce watcheru, ať atomický `os.replace` nespustí dvojitý reload (mtime + rename).
