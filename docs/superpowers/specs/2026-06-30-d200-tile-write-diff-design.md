# D200 spinner-stall fix — per-index tile write-diff

**Status:** design approved · 2026-06-30
**Type:** performance bugfix (D200 USB render path)
**Scope:** single file — `src/herdeck/driver/d200.py` (+ its tests)

## Problém / symptom

Na fyzickém Ulanzi D200 (launchd služba `python -m herdeck.app` na macbench) se animace spinneru u pracujícího agenta **občas na několik sekund zasekne** a pak se rozjede. Není to konstantní — intermitentní, kadenčně blízko ~10 s.

## Root cause (důkazně podložené)

Diagnostické workflow (5 paralelních vyšetřovatelů + měření na macbench + adverzní syntéza) určilo jediný mechanismus, který sedí na **magnitudu (sekundy) i kadenci (~10 s) i intermitenci** zároveň:

1. **Plný refresh každých 10 s.** `app.py:43 FULL_REFRESH_TICKS=25` × `tick_interval=0.4 s` → každých 25 tiků `handle_tick` zavolá `_refresh()`, který přepíše **všech 13 dlaždic** (`d200.py:_write_tiles` → `set_buttons(...)` přes **všechny** dlaždice, bez rozdílu).
2. **D200 driver nemá write-level diffing.** `_tile_buttons` (d200.py:124-133) bezpodmínečně zahrne každou dlaždici; `_write_tiles` (d200.py:162-167) pošle všech 13 i když se 11 z nich nezměnilo. (Kontrast: HTTP cesta `deckapp/server.py:144-146` verzuje a posílá jen změněné — HW cesta žádný analog nemá.)
3. **Velikostní rozdíl 12× (změřeno):** 13 dlaždic = 119 521 B = **118 HID paketů**; 1 dlaždice = 9 209 B = 10 paketů.
4. **strmdck `set_buttons` má neomezenou retry smyčku.** `_prepare_zip` kontroluje „invalid bytes" `[0x00, 0x7c]` na hranicích paketů; při zásahu přidá náhodný balast a **přebuilduje celý zip s `time.sleep(0.05)`** — opakovaně, bez limitu. **Pravděpodobnost zásahu roste s velikostí zipu** → trefuje velký 120 KB rámec mnohem častěji než malé partial updaty. Pár retry kol = stovky ms až sekundy.
5. **Vše běží na jednom worker vlákně RenderPump** (`run_until_complete`, render_pump.py:84). Dokud velký zápis neskončí, spinner se nemá jak posunout (coalescené working frame čekají).

**Vyloučeno jako příčina:** render dlaždic (cairosvg+PIL) — změřeno **~25 ms na všech 13** (≈100× málo). Bridge/event-loop — sub-ms, izolováno přes `call_soon_threadsafe`.

**Klíčové pozorování pro fix:** `render_tile` vrací deterministický filename `tile_<sha1(signature)>.png` (icons.py:396), kde signatura obsahuje veškerý vizuální stav (barva, label, agent_type, spinner fáze, repo, branch, status_text, time_text, working_animation, server tag/accent). Tedy: **stejný vizuál → stejný filename.** Per-index diff na filename je proto přesný a zadarmo — žádné porovnání pixelů.

## Fix

**Per-index write-diff v D200 driveru** (port strategie, kterou HTTP cesta už má). Driver si drží `self._last_icon: dict[int, str]` = poslední zapsaný icon-filename per index. Zápis posílá přes `set_buttons(..., update_only=True)` **jen ty indexy, jejichž filename se od posledního zápisu změnil**. Tím se 10s plný rámec (13 dlaždic / 120 KB / 118 paketů) smrskne na typicky 1–3 dlaždice (~9–28 KB / 10–28 paketů), zip je malý → retry smyčka strmdcku se prakticky netriggeruje → žádný několikasekundový zásek. Zároveň working dlaždice (spinner fáze se mění) jsou vždy „changed", takže se kreslí dál plynule i na refresh tiku.

**Transakční bezpečnost:** `_last_icon` se aktualizuje **až po úspěšném** `set_buttons`. Když zápis vyhodí výjimku, `_last_icon` se nechá beze změny → příští refresh ty dlaždice zopakuje (žádná trvale zaseklá stale dlaždice z polknuté chyby).

**První paint:** dokud je `_last_icon` prázdný (po otevření zařízení), pošle se **plný** `set_buttons(update_only=False)` přes všechny dlaždice, aby se založil layout; teprve další zápisy jedou diffem (`update_only=True`).

### Observabilita (instrumentace)

Logy služby jsou dnes prázdné — žádná runtime data. Přidá se **timing instrumentace** kolem `set_buttons`: změřit dobu zápisu (`time.perf_counter`), zalogovat kanál + počet dlaždic + `update_only` + ms; nad prahem (`_slow_write_ms`, default 250 ms) varovat (`WARNING`), jinak `DEBUG`. Driver navíc vystaví `self._last_write_ms` / `self._last_write_count` pro testovatelnost. Tohle je trvalý guardrail **a** umožní before/after měření na decku (nasadit instrumentační commit samostatně → změřit baseline → nasadit diff → porovnat).

## Komponenty / soubory

Vše v jednom souboru:

- `src/herdeck/driver/d200.py` (změna):
  - `import logging`, `import time`; modul-logger `log = logging.getLogger(__name__)`.
  - `__init__`: `self._last_icon: dict[int, str] = {}`, `self._slow_write_ms = ...`, `self._last_write_ms`/`_last_write_count`.
  - **Task 1 — instrumentace:** `_timed_set_buttons(channel, buttons, *, update_only) -> bool` — změří + zaloguje `set_buttons`, vrátí True/False podle úspěchu; chyba se loguje (WARNING) a polkne (vrátí False). `_write_tiles`/`_write_working`/`_write_panel` přes něj routují, chování beze změny (pořád posílají všechno).
  - **Task 2 — diff:** `_diff(buttons)` vrátí jen indexy se změněným filename vs `_last_icon`; `_write_tiles`/`_write_working` diffují a routují přes `_timed_set_buttons`; `_last_icon` se aktualizuje jen při úspěšném zápisu. První paint (`not self._last_icon`) = plný `update_only=False`.
- `tests/test_d200_panel.py` (rozšíření; existující `_FakeDev`/`_FakeIcons`/`_make_driver` harness).

## Data flow (po fixu)

1. **refresh tik (každých 10 s):** `_refresh()` → `_write_tiles(all 13, fáze P)` → `_diff` vybere jen změněné (vč. working dlaždic na fázi P) → `set_buttons(changed, update_only=True)` → malý zápis. Spinner ukáže fázi P.
2. **normální tik:** `render_working(working tiles, fáze P+1)` → `_diff` (working jsou vždy changed) → malý partial zápis. Spinner plynule pokračuje.
3. **idle dlaždice** (control/launcher/profile/idle agent): jejich filename se mezi refreshi nemění → diff je vždy přeskočí → 0 USB zápisů.

## Error handling

- **`set_buttons` selže** (USB glitch) → `_timed_set_buttons` vrátí False, `_last_icon` se neaktualizuje → retry na příštím refreshi. WARNING v logu.
- **Zařízení drží stale stav** — vyloučeno, protože každý index 0–12 má v každém renderu definovanou dlaždici (orchestrátor renderuje všechny sloty); není „remove button" případ. Jediný reset `_last_icon` je při otevření zařízení (= konstrukce driveru); driver se při výpadku rekonstruuje, takže další reset-pointy (brightness/panel-mode jsou taky jen v `__init__`) nejsou potřeba.
- **Panel (cell 13,14)** se kreslí zvlášť přes `_write_panel(update_only=True)` — nedotčeno diffem dlaždic; routuje jen přes instrumentaci.

## Testy

**Task 1 (instrumentace):**
- po zápisu je `driver._last_write_ms` nastavené a `_last_write_count` == počet poslaných dlaždic;
- blokující `_FakeDev` (přes existující `block` Event) → write nad prahem → WARNING v caplogu s počtem dlaždic + `update_only`;
- selhání `set_buttons` (fake vyhodí) → polknuto, WARNING, worker žije dál.

**Task 2 (diff):**
- **první paint = plný:** první `render(tiles)` → `set_buttons` voláno s `update_only=False` a všemi indexy;
- **beze změny → 0 zápisů:** druhý `render` se stejnými dlaždicemi → žádné další `set_buttons` volání (filename identický);
- **jen změněná dlaždice:** fake icons s filename závislým na obsahu (`f"icon_{i}_{tile.time_text}.png"`); změna `time_text` jedné dlaždice → `set_buttons` voláno jen s tím indexem, `update_only=True`;
- **working aktualizuje `_last_icon`:** `render_working` jednoho indexu → následný `render` (full) ten index přeskočí, pokud se filename nezměnil;
- **transakčnost:** `set_buttons` při prvním pokusu vyhodí → `_last_icon` prázdný → příští `render` ty dlaždice pošle znovu.

**Recyklace:** existující `test_d200_panel.py` + `test_render_pump.py` zůstávají zelené (offload/close/panel/brightness chování nezměněné — jen `_write_tiles`/`_write_working` interně diffují).

**Manuální gate (macbench D200):** nasadit instrumentační commit (Task 1) samostatně → ve `~/.cache/herdeck/herdeck-app.err.log` sledovat periodický (~10 s) `_write_tiles` zápis a jeho ms (baseline = pomalý, mnoho dlaždic); pak nasadit diff (Task 2) → log ukáže malé rychlé zápisy (1–3 dlaždice, desítky ms) a **spinner se přestane sekat**. Rozhodující cross-check už není nutný (instrumentace přímo měří).

## Non-goals (YAGNI / odloženo)

- **Pump „tiles drops working" změna + `handle_tick` emit-on-refresh (původní bod #2).** S diffem už refresh-tik kreslí posunutou spinner fázi levně přes diffnuté tiles; pump-drop pak ovlivňuje jen vzácné incidentní coalescení tiles+working (možný 1-frame stutter, **nikdy** reportovaný několikasekundový zásek) a změna se subtilně potýká s novou `_last_icon` evidencí (riziko 1-frame oscilace). Mimo tento slice; revidovat jen pokud manuální gate i po diffu ukáže stutter.
- **Zhrubení `_elapsed_text` / vyhození `time_text` ze signatury.** S diffem je idle dlaždice přeskočena tak jako tak; per-second elapsed u working dlaždic je žádané UX. Samostatné product rozhodnutí.
- **Zvýšení `FULL_REFRESH_TICKS`.** S diffem je plný refresh levný → frekvence nevadí.
- **Eviction render-cache** (4146 PNG / 46 MB na macbench, bez eviction). Pomáhá device-side prepare cost sekundárně; samostatná linie.
- **`handle_tick` tick-delta logging** v app.py — device-side write timing v d200 logu kadenci ukáže sám; není třeba sahat mimo driver.
- **Panel diffing** — panel = 2 buňky, malý zápis, není bottleneck.
```
