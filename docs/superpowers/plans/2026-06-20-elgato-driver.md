# Elgato Stream Deck driver — design + plan

**Goal:** Add an Elgato Stream Deck driver behind the existing `DeckDriver`
interface so herdeck works on the most common hardware, not just the Ulanzi D200.
Goal: adoption.

## Design decisions (made; implement these)

- **Library:** `streamdeck` (python-elgato-streamdeck). Import lazily *inside*
  methods (like `d200.py` does with `hid`/`strmdck`) so the test suite never needs
  the lib or hardware.
- **Dependency injection for tests:** `ElgatoDriver(device=None, icon_provider=None)`.
  When `device` is None, enumerate+open a real deck; tests pass a **fake device**
  (records `set_key_image` calls, exposes `key_count()`, `set_key_callback`,
  `set_brightness`, `reset`, `close`). No HW needed for tests.
- **Panel handling:** Elgato decks have no separate status window. Reserve the
  **last 2 physical keys** for the panel, mirroring the D200's 13+2 model:
  - `slot_count()` → `device.key_count() - 2`.
  - `render_panel` composes the 392×196 panel image (reuse `icons.compose_panel`
    + `d200.split_panel`) and writes the two halves onto keys
    `slot_count` and `slot_count+1`.
  - A press on those two keys must page the overview.
- **Orchestrator change (required):** `PANEL_INDICES = (13, 14)` is D200-specific.
  Make the panel indices **`(self.slots, self.slots+1)`** so any deck size works.
  This is backward compatible: web/fake/D200 all use `slots == 13`, so the indices
  stay `(13, 14)`. Update `_press_overview` to compare against the computed pair.
- **Images:** icon provider already renders 196×196 PNGs (`render_tile_bytes`).
  Open as PIL, resize to the deck's key size, convert via
  `StreamDeck.ImageHelpers.PILHelper.to_native_format(device, image)`, then
  `device.set_key_image(key, native)`.
- **Presses:** `device.set_key_callback(cb)` where `cb(deck, key, state)`. On
  key-down (`state` True), forward `key` to the orchestrator callback unchanged
  (device key index == orchestrator tile index; panel keys are
  `slot_count`/`slot_count+1`). The app marshals to the loop (same as web).
- **render_working:** partial update — `set_key_image` only for the given tiles.
- **Deck selection (`app.make_deck`):** add `kind == "elgato"` →
  `ElgatoDriver()`. In **auto** mode (kind None) try d200, then **elgato**, then
  fall back to web. Explicit `HERDECK_DECK=elgato` propagates failures (no silent
  web fallback).
- **pyproject:** add an optional extra `elgato = ["streamdeck", "pillow>=10"]`.
  Do NOT touch `[project]` metadata (that's a separate, already-merged PR).

## Files
- Create: `src/herdeck/driver/elgato.py`
- Modify: `src/herdeck/orchestrator.py` (panel indices), `src/herdeck/app.py`
  (`make_deck`), `pyproject.toml` (extra)
- Test: `tests/test_driver_elgato.py` (new), `tests/test_orchestrator_nav.py`
  (panel-index paging for non-13 slot counts)

## TDD tasks (failing test first → minimal code → commit each)

1. **Orchestrator generic panel indices.** Test: with `slots=4`, a press on index
   4 pages the overview (and 13 no longer does). Implement: compute panel indices
   from `self.slots`. Keep the existing `slots=13` behavior green.
2. **ElgatoDriver.slot_count.** Test: fake device `key_count()==15` →
   `slot_count()==13`; `key_count()==32` → `30`. Implement constructor + slot_count
   with an injected fake device.
3. **render writes key images.** Test: `render([TileView(0,...), TileView(1,...)])`
   → fake device recorded `set_key_image` for keys 0 and 1 with non-empty bytes.
   Implement render via icon provider + PILHelper (mock PILHelper/native in test by
   injecting an `icon_provider` returning small PNGs and stubbing the native
   conversion, OR keep conversion thin and assert keys touched).
4. **render_panel writes the two reserved keys.** Test: panel renders onto keys
   `slot_count` and `slot_count+1`. Implement with compose_panel + split_panel.
5. **press callback maps key→orchestrator index.** Test: invoking the registered
   callback with `(deck, 7, True)` calls the herdeck press handler with 7;
   `state=False` (key-up) is ignored. Implement `on_press` + `set_key_callback`.
6. **render_working partial update.** Test: only the given keys get `set_key_image`.
7. **make_deck elgato + auto order.** Test: `make_deck("elgato", 13,
   elgato_factory=...)` returns the elgato deck; auto tries d200 then elgato then
   web (inject factories that raise to assert order). Implement.
8. **pyproject extra + README note.** Add `elgato` extra; one line in README under
   hardware/quick start that Elgato is supported via `pip install ".[elgato]"`.

## Constraints
- Lazy-import `StreamDeck` inside methods; tests must pass without the lib.
- TDD, conventional commits (English, no Co-Authored-By), `.venv/bin/python -m pytest`.
- After each commit check `roborev show <sha>` and fix findings.
- Full suite green at the end. Do NOT touch `[project]` metadata or `web.py`.
