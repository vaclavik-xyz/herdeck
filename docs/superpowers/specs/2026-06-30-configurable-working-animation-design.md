# Configurable working-agent animation — design

**Status:** design approved (brainstorming) · 2026-06-30
**Type:** deck-render feature (config-driven; shared across D200 + desktop window + web simulator)
**Depends on:** existing spinner-phase mechanism (orchestrator `_advance_spinner` + `TileView.spinner`), `[view]` config (alongside `tile_primary`/`tile_secondary`), the desktop config editor.

## Problém / cíl

Když agent **pracuje**, jeho dlaždice se animuje — dnes **napevno**: ikona agenta se otáčí (`_compose_agent_tile` dělá `logo.rotate(-spinner * SPIN_DEG)`). Uživatel si chce **zvolit**, jak „working" vypadá. Cíl: jeden globální config klíč `[view].working_animation` s 5 styly, sdílený přes všechny decky.

Vše jede na **stávající spinner fázi** (orchestrator už točí `self._phase` a working dlaždice dostane `spinner=phase`, idle `None`). Měníme jen **vizuál** podle zvoleného stylu; mechanika fáze, tick rate a render pump zůstávají.

## Volby stylu

`[view].working_animation` ∈ (default **`spin`**):

| styl | vizuál (jen když je dlaždice working) |
|---|---|
| `spin` | ikona agenta se otáčí (stávající `logo.rotate(-spinner*SPIN_DEG)`) |
| `comet` | ikona **statická**; kolem ikony obíhá kometní prstenec (světlá hlava + slábnoucí ocas) |
| `pulse` | ikona **„dýchá"** — škáluje mezi ~0.82× a 1.0× podle sinu fáze (pozice vystředěná) |
| `sweep` | ikona statická; spodním **accent pruhem** přejíždí jasný segment podle fáze |
| `none` | ikona statická, žádný prstenec/pohyb (working pozná podle status textu + accent pruhu) |

- **Default + zpětná kompatibilita:** klíč chybí → `spin` (beze změny configu = stávající chování).
- **Globální:** jeden styl pro všechny agenty (žádný per-agent_type). Idle/control/management/launcher dlaždice styl ignorují (animuje se jen agent tile s `spinner is not None`).

## Architektura

### 1. Config (`[view].working_animation`)
- `config.py`: `WORKING_ANIMATIONS: tuple[str, ...] = ("spin", "comet", "pulse", "sweep", "none")`; `ViewConfig` dostane `working_animation: str = "spin"`.
- `settings.py:_view_config`: po vzoru `tile_primary` validace —
  ```python
  if "working_animation" in raw:
      val = raw["working_animation"]
      if val not in WORKING_ANIMATIONS:
          raise ConfigError(f"unknown view.working_animation '{val}'; want one of {WORKING_ANIMATIONS}")
      view.working_animation = val
  ```
  (Nevalidní hodnota → `ConfigError`, stejně jako neznámý tile token. Absence → default `spin`.)

### 2. Vedení kódem — `TileView.working_animation` (varianta A z brainstormu)
- `driver/base.py`: `TileView` dostane `working_animation: str = "spin"` (default drží zpětnou kompatibilitu pro přímé konstrukce TileView v testech/fake).
- `orchestrator.py` (agent tile, ~ř. 224-243): k `spinner=phase` přidat `working_animation=self.config.view.working_animation`. Ostatní (control/idle/launcher) TileView nechat default — animace se jich netýká.
- **Proč ne IconProvider param (varianta B):** styl by se musel protáhnout 3 deck konstruktory (deckapp/d200/web) a cache invalidace řešit zvlášť. Pole na TileView je čistší a přirozeně se dostane do render-cache sig.

### 3. Render — `icons.py` `_compose_agent_tile`
Větvit podle `tile.working_animation`, **jen** když `spinner is not None` (working). Když `spinner is None`, vykreslit statickou ikonu jako dnes — styl nemá efekt.

- **spin** — stávající: `logo = base_glyph.resize(46); logo = logo.rotate(-spinner*SPIN_DEG); composite at (12,12)`.
- **none** — stejné jako spin, ale **bez** `rotate` (statická 46px ikona).
- **comet** — statická 46px ikona + kometní prstenec **kolem loga** (ne kolem celé dlaždice): adaptovat math z `_draw_spinner` (supersample, RING_SPAN ocas, head = `phase*(360/SPINNER_FRAMES)`) na box loga (~12..58 px) s tenkým ringem; composite přes/kolem loga. (Stávající `_draw_spinner` kreslí přes celý ICON_SIZE — refaktorovat na pomocnou fci s parametrem boxu, ať ji sdílí i `icon_for`.)
- **pulse** — statická ikona vyškálovaná faktorem `f = 0.82 + 0.18*(0.5 + 0.5*sin(2*pi*phase/SPINNER_FRAMES))`; výsledek vystředit do 46px boxu na (12,12) (menší velikost → vycentrovat offset). Žádná rotace.
- **sweep** — statická 46px ikona; spodní accent pruh (`[0, ICON_SIZE-8, ICON_SIZE, ICON_SIZE]`) vykreslit **ztlumeně** (accent×0.4) + přes něj jasný segment šířky ~`ICON_SIZE/4`, jehož levý okraj = `(phase/SPINNER_FRAMES)*ICON_SIZE` (přetočí se, wrap přes okraj).

Všechny styly používají `spinner % SPINNER_FRAMES` (cache bounded na fixní set rámců — stávající invariant).

### 4. Render-cache sig
- `render_tile` `sig_parts`: přidat `tile.working_animation` **jen když `tile.spinner is not None`** (idle dlaždice se nepřeklíčují při změně stylu — žádná zbytečná cache churn; jejich výstup je na stylu nezávislý). Tím změna stylu přerenderuje jen working dlaždice. (Nezávislé na nedávném asset-fingerprint cache fixu v `icons.py`.)

### 5. Config editor (desktop)
- `[view]` sekce editoru dostane **dropdown `working_animation`** (5 voleb, default spin). Frontendová sekce ViewConfig (Svelte) + payload mapping; validace běží serverově přes `config_service` → `settings` (`ConfigError` na nevalidní = stávající chování editoru). Detaily UI ladí plán; sdílí vzor s ostatními `[view]` poli.

## Komponenty / soubory
- `src/herdeck/config.py` — `WORKING_ANIMATIONS` + `ViewConfig.working_animation`.
- `src/herdeck/settings.py` — `_view_config` validace + parse.
- `src/herdeck/driver/base.py` — `TileView.working_animation`.
- `src/herdeck/orchestrator.py` — předat `working_animation` na agent TileView.
- `src/herdeck/icons.py` — `_compose_agent_tile` větvení + per-styl render + sdílená comet-ring pomocná fce; `render_tile` sig.
- `desktop/src/lib/...` (ViewConfig sekce editoru) + případně `config_service` allow-listy — dropdown.
- Testy: `tests/test_icons.py`, `tests/test_settings*.py` / `tests/test_config*.py`, orchestrator test, desktop vitest.

## Testy
- **Config (pytest):** `working_animation="pulse"` se naparsuje; neznámá hodnota → `ConfigError`; absence → `"spin"`.
- **Orchestrator (pytest):** working agent TileView nese `working_animation` z configu; idle/control TileView má default a styl je ignorován (spinner None).
- **Render (pytest):** pro working tile (spinner set) každý z 5 stylů dá **jiné byty** než statická ikona a **navzájem různé** (alespoň spin/comet/pulse/sweep/none párově odlišné); `none` == statická bez ringu (≠ spin). Idle tile (spinner None) renderuje **stejně** pro všechny styly.
- **Cache sig (pytest):** dva working tiles lišící se jen `working_animation` → různé `render_tile` filename; dva **idle** tiles lišící se jen stylem → **stejné** filename (styl není v sig pro idle).
- **Editor (vitest):** ViewConfig sekce nabídne 5 voleb; payload mapuje `working_animation`; nevalidní → server validační chyba.
- **Manuální gate (macbench D200):** přepnout `[view].working_animation` na každý styl → restart deck → working agenti animují daným stylem (spin/comet/pulse/sweep), `none` statický; idle beze změny.

## Non-goals
- Per-agent_type animace (jen globální).
- Animace idle/blocked/done stavů (jen „working").
- Nové animační styly nad těch 5 (rozšiřitelné později přidáním do `WORKING_ANIMATIONS` + větve).
- Konfigurovatelná rychlost/FPS animace (řídí stávající tick rate + SPINNER_FRAMES).
- Tray/keyboard přepínání za běhu (jen config + editor; projeví se po reload/restartu jako ostatní `[view]` změny).

## Vyřešené technické body
- **Sdílí se přes všechny decky** automaticky — `_compose_agent_tile` je jediná agent-tile render cesta (D200 `render_tile`, web `render_tile_bytes`, deckapp tiles). Žádná duplikace.
- **Zpětná kompatibilita:** absence klíče = `spin` = stávající vizuál; `TileView.working_animation` default `"spin"` chrání přímé konstrukce.
- **Cache:** styl v sig jen pro working dlaždice → změna stylu přerenderuje pouze je; idle beze změny.
- **Validace:** `ConfigError` na nevalidní hodnotu (vzor `tile_primary` neznámý token); deckapp `load_settings` ji chytá (→ mock + hint), editor ji ukáže inline.
- **comet** reuse: `_draw_spinner` math se refaktoruje na pomocnou fci s boxem, ať ji sdílí `icon_for` (celá dlaždice) i `_compose_agent_tile` (kolem loga).
