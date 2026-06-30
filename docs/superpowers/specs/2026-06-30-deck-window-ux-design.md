# herdeck desktop app — Deck window UX (configurable mode, rounded, content-fit, draggable) — design

**Status:** design approved (brainstorming) · 2026-06-30 · hardened by adversarial verification (6 agents vs real codebase + Tauri 2.11.3)
**Type:** feature design spec (herdeck desktop app — post-Phase-3 polish on the floating deck window)
**Depends on:** Phase 1 (floating deck window + tray), Phase 3c (onboarding card in the main window), Phase 3d (`[hotkeys]` config passthrough + tray + autostart)

## Problém

Floating deck okno (`main`) je dnes pevné 360×600, borderless (`decorations:false`),
always-on-top, neprůhledné. Tři reálné UX problémy zjištěné při nasazení na macbench:

1. **Prázdná spodní polovina** — `main`, `.deck` i `.onboarding` mají `min-height: 100vh`,
   takže obsah vyplní celé okno, ale deck (grid 5×3) má přirozenou výšku jen ~⅓ → zbytek
   je černá plocha.
2. **Nedá se přesunout** — borderless okno bez `data-tauri-drag-region` nelze tahat.
3. **Žádná volba chování** — uživatel chce volit mezi *always-on-top widgetem*,
   *floating widgetem* a *normálním oknem*; a chce **zaoblené rohy**.

## Cíl

Zavést **3 režimy okna** volitelné v configu + tray, s **content-fit** velikostí
(žádné prázdno), **zaoblenými rohy** a **tahatelností** u borderless režimů.

## Klíčová technická omezení (ověřená proti Tauri 2.11.3 — tvarují architekturu)

1. **`transparent` je creation-time vlastnost** — v Tauri 2 NEEXISTUJE `set_transparent`
   (ověřeno: `transparent()` je jen builder metoda). Runtime toggle nelze.
2. **`.transparent(true)` se na macOS NEZKOMPILUJE bez cargo feature `macos-private-api`** —
   builder metoda je `#[cfg(any(not(target_os="macos"), feature="macos-private-api"))]`.
   Navíc je třeba **`"macOSPrivateApi": true`** v `tauri.conf.json` (`app` klíč; default false).
   Dnešní `Cargo.toml` má jen `features=["tray-icon"]` → **bez obojího borderless režimy
   vůbec nepostaví/nezobrazí**.
3. Zaoblené rohy borderless okna na macOS vyžadují `transparent:true` + CSS rounded
   kontejner (borderless opaque okno má hranaté rohy).
4. **`set_decorations` i `set_always_on_top` jdou měnit za běhu**; `transparent` ne.

Důsledek: přechod **normal↔borderless** mění `transparent` → **restart appky**
(`app.restart()`). Přechod **floating↔always_on_top** mění jen `always_on_top` →
**živě, bez restartu**.

## Režimy okna

| Režim | decorations | transparent | always_on_top | resizable | rohy | tah |
|---|---|---|---|---|---|---|
| **normal** (default) | true | false | false | true | nativní (OS) | nativní titlebar |
| **floating** | false | true | false | false | CSS rounded | drag-handle |
| **always_on_top** | false | true | true | false | CSS rounded | drag-handle |

- **Default = `normal`**. floating a always_on_top se liší jen v `always_on_top`.

---

## Komponenta 1: Config setting (Python passthrough)

```toml
[desktop]
window_mode = "normal"   # "normal" | "floating" | "always_on_top"
```

- **Přidat `"desktop"` do `ConfigService.BASE_SECTIONS`** (`config_service.py`), přesně
  jako 3d přidalo `"hotkeys"` (ověřeno: jednořádková změna; `read()` zahrne `base.desktop`,
  `write()` round-tripuje, mimo `Config` dataclass i `_OVERLAY_SECTIONS` → nula dopadu na
  core; `validate_settings` projde — žádný section-whitelist).
- **Sémantika `window_mode`:** chybějící klíč / neznámá hodnota → **default `normal`**.
  Backend round-tripuje LIBOVOLNÝ string (žádná backend validace) — platnost hodnoty hlídá
  frontend `<select>` (3 hodnoty) a Rust `parse_window_mode` (default `Normal`).
- **Pozn.:** `write()` regeneruje managed config (nezachová ruční komentáře) — stávající
  chování editoru, akceptováno.

---

## Komponenta 2: Rust — okno se vytváří podle režimu

### 2a. Čtení režimu při startu + GARANTOVANÁ shoda cesty se sidecarem

Okno se musí vytvořit se správným `transparent`/`decorations` **dřív, než nabíhá sidecar**,
takže Rust přečte režim **přímo z config.toml**. Kritické: deckapp resoluce cesty
(`bootstrap.py` `_discover_config_path`) má **3 větve** — `HERDECK_CONFIG` env →
`~/.config/herdeck/config.toml` (přes `expanduser`, **NEhonoruje `XDG_CONFIG_HOME`**) →
CWD-relativní `./config.toml`. Rust dnes `HERDECK_CONFIG` při spawnu **nenastavuje**, takže
by Rust a sidecar mohly číst **jiný soubor**.

**Řešení (pin cesty):**
- Rust resolvuje cestu k `config.toml` **se stejnými existence-checky jako sidecar**
  (`_discover_config_path`): `HERDECK_CONFIG` env (pokud set & neprázdné → **absolutní**) →
  jinak `$HOME/.config/herdeck/config.toml` **pokud existuje** → jinak `<repo_root>/config.toml`
  **pokud existuje** (dev) → jinak default `$HOME/.config/herdeck/config.toml` (first-run/write
  fallback; nemusí existovat → window_mode `Normal`). **Hardcoded `$HOME/.config/...`, NE
  `XDG_CONFIG_HOME`**, ať se shoduje s deckappem. (Pozn.: pin neexistujícího `$HOME` defaultu
  by v devu ignoroval repo config — proto existence-checky před fallbackem.)
- **Rust tuto resolvovanou absolutní cestu EXPORTUJE jako `HERDECK_CONFIG` do spawnu sidecaru**
  (přidat do `CommandSpec` env v `sidecar.rs`/`choose_spawn`). Tím Rust i sidecar čtou
  **týž soubor** a CWD-relativní větev je mootnutá (důležité hlavně pro frozen `.app`, kde
  je CWD nedeterministické). `local.toml` (`HERDECK_LOCAL_CONFIG`) zůstává beze změny —
  window_mode je base-level v `config.toml`, NIKDY v `local.toml`.
- `parse_window_mode(toml_str: &str) -> WindowMode` — **čistá fce** (test bez FS): `toml`
  crate (nová dep), navigace `desktop.window_mode`, match na enum; **default `Normal` na
  chybějící klíč / neparsovatelný soubor / neznámou hodnotu** (NIKDY nepanikaří). Chybějící
  soubor (první spuštění) → `Normal`.

### 2b. Dynamické vytvoření okna `main`

- **Odebrat `main` ze statických `app.windows` v `tauri.conf.json`** (nechat jen `config`).
  `capabilities/default.json` už `"main"` v `windows[]` má — label match funguje i pro
  dynamicky vytvořené okno.
- **`tauri.conf.json`: přidat `"macOSPrivateApi": true`** (pod `app`); **`Cargo.toml`:
  doplnit `macos-private-api` do features tauri** (vedle `tray-icon`).
- V `setup()` po `read_window_mode()` vytvořit `main` přes
  `WebviewWindowBuilder::new(app, "main", WebviewUrl::default())` (ověřeno: signatura sedí;
  `WebviewUrl::default()` = `App("index.html")` → stejný frontend) s flagy podle režimu:
  - společně: `.title("herdeck").shadow(true)` + **`.initialization_script(...)`** (viz FOUC níže).
  - **normal:** `.decorations(true).transparent(false).always_on_top(false).resizable(true)
    .inner_size(380.0, 340.0).skip_taskbar(false)`.
  - **floating:** `.decorations(false).transparent(true).always_on_top(false).resizable(false)
    .inner_size(360.0, 320.0).skip_taskbar(true)`.
  - **always_on_top:** jako floating + `.always_on_top(true)`.
- **FOUC fix (mód před prvním paintem):** `onMount` běží AŽ po prvním paintu → borderless by
  bliklo opaque-normal CSS. Proto builder dostane
  `.initialization_script("document.documentElement.dataset.windowMode='<mode>'")` (mód
  doplní Rust z `read_window_mode()`), takže `<html data-window-mode>` je nastaveno **před**
  paintem a CSS se řídí podle něj okamžitě. (`get_window_mode()` command zůstává pro JS logiku.)
- **Setup ordering:** stávající blok `get_webview_window("main")` + `place_floating` +
  close-intercept (lib.rs ~584–601) běží PŘED `start_sidecar`; jakmile je `main` dynamické,
  **builder musí proběhnout PRVNÍ** (jinak `get_webview_window("main")` → `None` a
  place_floating/intercept tiše no-opnou). Pořadí: build `main` → place_floating (jen
  borderless) → close-intercept → `start_sidecar` (který teď taky exportuje `HERDECK_CONFIG`).
- **`place_floating`:** volat jen pro borderless režimy; **`set_always_on_top` z něj VYJMOUT**
  (řídí builder). Změnit `current_monitor()` → **`primary_monitor()`** (ať „primary top-right"
  sedí na multi-display, jako má macbench 3 displeje).
- `get_window_mode()` **musí být přidán do `tauri::generate_handler![]`** v `run()` (jinak
  `invoke` selže).

### 2c. Close chování `main` + restart

- Normal režim má **zavírací křížek** → **intercept `CloseRequested` → `prevent_close` + hide**
  (vzor `config` okna). Tray „Show"/hotkey vrátí. macOS tray-app: žádné quit-on-last-window
  k potlačení.
- **Restart MUSÍ být `app.request_restart()`, NE `app.restart()`** (ověřeno proti Tauri 2.11.3
  `app.rs`): tray menu handler běží na **main threadu**, kde `restart()` volá `cleanup_before_exit()`
  + `process::restart()` **přímo a PŘESKAKUJE `RunEvent::ExitRequested`/`Exit`** (doc comment: „we
  cannot guarantee the delivery of those events, so we skip them"). Náš sidecar-kill je právě v
  ExitRequested/Exit handleru → `restart()` z traye by sidecar **osiřil** (přesně pozorovaný orphan
  symptom). `request_restart()` projde `request_exit(RESTART_EXIT_CODE)` → event loop spustí
  ExitRequested/Exit (exit handler **zabije sidecar child**, žádný sirotek) → restart. `request_restart()`
  vrací `()` → za ním **musí být `return`** (jinak propadne do live-apply větve). Intercept
  close-to-hide se týká jen user-close (WindowEvent), **nepohltí** RunEvent ExitRequested/Exit.

---

## Komponenta 3: Tray „Window mode" submenu

- V `build_tray` `Submenu` „Window mode" se **3 `CheckMenuItem`** (`wm_normal`/`wm_floating`/
  `wm_aot`); zaškrtnutý = aktuální. **Není nativní radio group** → držet **všechny 3 handly**
  (jako `autostart_cb`) a na výběr `set_checked(true)` na target + `set_checked(false)` na
  ostatní dva.
- Handler při výběru `target` — **persist-then-apply, abort-on-failure**:
  1. **Persist (PREREKVIZITA: Komponenta 1 musí být hotová, jinak `base.desktop` propadne
     filtrem BASE_SECTIONS):** Rust read-modify-write přes existující helpery (ověřeno:
     `http_get`/`http_post_json` v `http.rs`, token v `Discovery`, vzor `config_read`/`config_write`;
     sync blocking, jako `reload_hotkey` — žádné async; tray handler je sync Fn). `GET /config` →
     nastavit `base.desktop.window_mode=target` → `POST /config` s `{base,profiles,local}`
     (active_profile je uvnitř base). Round-trip zachová strukturované sekce (ne komentáře).
     **Timeout:** `POST /config` bere `_setup_lock`, na kterém **blokuje** (sidecar nevrací busy/409);
     souběžný `/setup/connect` ho drží až ~15 s. Proto tray persist použít **dedikovaný timeout
     ≥ 15 s** (strop `/setup/connect`, jako `setup_connect`) — jinak by 3s `SIDECAR_TIMEOUT`
     uťal pomalý-ale-úspěšný zápis a klient by ho mylně považoval za failed, zatímco server
     po uvolnění locku stejně zapíše (divergence). Signál apply/abort = **skutečný výsledek
     `POST /config`**, ne timeout. **Pozor na kontrakt `/config`:** validační chyby vrací
     **HTTP 200 s `{errors:[...]}` a NIC nezapíše** — persist je úspěšný **jen při HTTP 200 A
     `errors == []`** (parsovat tělo). Timeout (>15 s) = genuinní wedge.
  2. **Pokud persist selže** (HTTP 4xx/5xx / **200 s neprázdným `errors`** / nevalidní JSON /
     sidecar down / wedge-timeout) → **NEaplikovat** (žádný restart, žádný `set_always_on_top`),
     vrátit zaškrtnutí na původní + zalogovat.
  3. **Apply (jen po úspěšném persistu):**
     - oba borderless (floating↔always_on_top) → **živě** `main.set_always_on_top(target==aot)`
       + překreslit zaškrtnutí. Bez restartu.
     - jinak (zahrnuje normal) → **`app.request_restart()` + `return`** (NE `app.restart()` — viz 2c:
       main-thread restart přeskočí ExitRequested a osiřil by sidecar; request_restart projde exit handlerem).
- **Známý transient cost:** úspěšný `POST /config` spustí `app.reload()` → u remote decku
  **přestaví+znovupřipojí live source** (server.py reload→build_live_source→close old), i když
  je window_mode pro render irelevantní. Pro normal↔borderless je to mootnuté (stejně restart);
  pro floating↔always_on_top to znamená **krátký reconnect/re-render** při každém přepnutí —
  akceptujeme jako známý náklad.
- Stávající tray položky (Settings/Show/Hide/Start at login/Change connection/Quit) zůstávají.

---

## Komponenta 4: Svelte — zaoblení + content-fit + drag

Mód se na `<html data-window-mode>` injektuje **před paintem** (2b initialization_script);
`App.svelte` ho čte synchronně + má `get_window_mode()` pro logiku. `borderless = mode !== "normal"`.

### 4a. Zaoblení + průhlednost (borderless)

- **borderless:** `:global(html, body) { background: transparent }`; root wrapper
  `.shell { border-radius: 12px; overflow: hidden; background: #0b0b0d }` — zaoblená neprůhledná
  karta na průhledném okně. `.shell` musí být **flush k okraji okna** (žádný transparentní
  margin), ať zaoblený stín sedí (stín kopíruje opaque siluetu `.shell`; `shadow:true` OK,
  `macos-private-api` pro stín netřeba).
- **normal:** `:global(html, body) { background: #0b0b0d }`, `.shell` bez zaoblení (OS rámeček).
- Volba CSS přes `[data-window-mode]` na `<html>` (`:global([data-window-mode="floating"]) ...`).

### 4b. Drag-handle (borderless)

- Slim `<div class="drag" data-tauri-drag-region>` (~18 px) **nahoře v `.shell`**, jen borderless.
  Deck/onboarding pod ním zůstávají klikací (drag region je per-element — tiles jsou `<button>`
  mimo proužek; ověřeno). Pozor: atribut **nedávat na rodiče tiles**; interaktivní prvky uvnitř
  proužku by potřebovaly `data-tauri-drag-region="none"`.
- normal: žádný drag-handle (tah dělá nativní titlebar).

### 4c. Content-fit (žádné prázdno) — anti-feedback

- **Odebrat `min-height: 100vh` ze VŠECH TŘÍ:** `App.svelte` (`main`), `DeckView.svelte`
  (`.deck`), `Onboarding.svelte` (`.onboarding`) — jinak content-fit nefunguje a ResizeObserver
  by měřil 100vh. (V normal režimu nech obsah top-aligned; okno řídí uživatel.)
- **borderless content-fit:** efekt s `ResizeObserver` na `.shell` měří **intrinsic
  `scrollHeight`** content karty (NE `offsetHeight` 100vh prvku) a volá
  `getCurrentWindow().setSize(new LogicalSize(w, h))` (`@tauri-apps/api/window`, v2.11.1 OK).
  **Anti-feedback guard (jinak by setSize→viewport→observer oscilovalo):** (a) měřit
  viewport-nezávislou výšku content karty; (b) **skip setSize, pokud je nová velikost do ~1 px
  od poslední vyžádané**; (c) zaokrouhlit na celé logické px; (d) rAF-batch callbacků. Logika
  měření/guardu ve framework-free helperu (Vitest); `setSize` volání v `.svelte`. `setSize`
  drží levý-horní roh (po primary-top-right umístění OK).
- **Guard běhu:** resize logika jen v okně `main` a jen v borderless; `getCurrentWindow()` mimo
  WebView hází → obalit try/catch (jako `main.ts`).
- **normal:** žádný auto-resize; okno se otevře ve fit-default (2b) a je resizovatelné.
- **reonboard ⚙ tlačítko:** dnes `position:fixed; left:8px; bottom:8px` → mimo content flow
  (neměří se, `overflow:hidden` na `.shell` by ho ořízl, koliduje s deck footerem na left:8px).
  **Přesunout do document flow** (relativně, do footer oblasti), ať je součástí měřené výšky a
  neořezává se.

---

## Capabilities & dependencies (ověřeno)

- **Rust dep:** `toml` (parsování window_mode při startu).
- **Tauri feature:** **`macos-private-api`** doplnit do `features` tauri v `Cargo.toml` +
  **`"macOSPrivateApi": true`** v `tauri.conf.json` — JINAK borderless transparent nepostaví.
- **Capability:** **doplnit `"core:window:allow-set-size"`** do `capabilities/default.json`
  permissions (ne wildcard) — `core:default` je read-only a `setSize` z WebView by jinak ACL
  zamítl. Builder/`set_always_on_top`/`set_decorations`/`restart`/tray jsou Rust-side (ne ACL).
- **JS:** `@tauri-apps/api/window` (`getCurrentWindow`, `LogicalSize`) už k dispozici.
- **configClient.ts:** přidat helpery `DEFAULT_WINDOW_MODE="normal"`,
  `windowMode(payload)` (`getAt base/desktop/window_mode`, typeof-guard → default),
  `setWindowMode(payload, v)` (`setAt base/desktop/window_mode`) — vzor jako
  `toggleDeckHotkey`/`setToggleDeckHotkey`.

## Testing

| Vrstva | Co | Jak |
|---|---|---|
| Python | `ConfigService` round-trip `[desktop].window_mode`; inertní `[desktop]` nerozbije validate | `pytest` (mirror 3d hotkey testů) |
| Rust | `parse_window_mode(&str)` (normal/floating/aot/missing/garbage→Normal); `switch_needs_restart(from,to)` (borderless↔borderless=false, jinak true); config-path resolve (HERDECK_CONFIG→`$HOME/.config/...`→repo) | `cargo test` |
| Svelte | mode→borderless rozhodnutí; content-measure+guard helper (DOM výška → LogicalSize, skip-within-1px, round); configClient window_mode helpery | Vitest |
| Svelte | `App.svelte` (drag-handle, data-window-mode, get_window_mode, ResizeObserver), `DesktopSection.svelte` (window_mode select) | compile-smoke |
| Freeze | sidecar freeze+smoke beze změny | `build-sidecar.sh` + `smoke-sidecar.sh` |

## Manuální gate (redeploy na macbench)

Rebuild `.app` → rsync na macbench → relaunch. Ověřit:
1. **Default normal:** dekorované okno, titlebar tah/resize/zavřít, OS zaoblení; zavření → hide, tray Show vrátí.
2. **Tray → Window mode → Floating:** restart → borderless, **zaoblené rohy bez bliknutí** (FOUC fix), drag-handle tahá, **žádné prázdno** (okno obaluje deck), nedrží nad ostatními.
3. **→ Always-on-top:** **bez restartu** přepne na drží-nad-vším (krátký reconnect OK).
4. **→ Normal:** restart zpět na dekorované.
5. Config editor „Desktop" sekce ukazuje/mění `window_mode` (Apply → projeví se po restartu).
6. Onboarding karta v borderless taky obalená + tahatelná (drag-handle/rounded/content-fit ji obalí).

## Non-goals

- Windows/Linux window-mode specifika (řeší balení; design je cross-platform-safe).
- Resizovatelné borderless okno (borderless = auto-fit; resize jen normal).
- Živý přechod normal↔borderless bez restartu (recreate okna) — vědomě restartem.
- Drag celého pozadí decku (jen handle proužek).
- **Non-5×3 deck layouty:** grid je hardcoded `repeat(5,1fr)` (`DeckView`) a default `inner_size`
  předpokládá 5×3 (současný deck JE 5×3, `grid="5x3"`). Borderless ResizeObserver dorovná
  velikost, ale sloupce/normal-default zůstávají 5×3-only — **vědomé omezení**, ne fix v tomto řezu.

## Vyřešené technické body (z verifikace)

- Config cesta: Rust resolvuje + **exportuje `HERDECK_CONFIG`** sidecaru (garantovaná shoda);
  hardcoded `$HOME/.config/herdeck/config.toml`, NE `XDG_CONFIG_HOME`; absolutní cesty.
- `macos-private-api` feature + `macOSPrivateApi:true` + `core:window:allow-set-size` capability.
- FOUC: mód injektován na `<html>` přes `initialization_script` před paintem.
- min-height:100vh odebrat ze 3 souborů; ResizeObserver anti-feedback guard.
- Tray: 3 handly + mutual set_checked; persist-then-apply, abort-on-failure; POST /config →
  reload→reconnect je známý transient cost.
- setup ordering (build main první); place_floating → primary_monitor + bez set_always_on_top;
  get_window_mode v generate_handler; reonboard ⚙ do flow.
