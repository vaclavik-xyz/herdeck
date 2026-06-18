# Herdeck Deck v2 — design (Spec 1)

**Date:** 2026-06-18
**Status:** Approved, ready for implementation plan
**Builds on:** the shipped MVP (`feat/implementation`, PR #1)

## 1. Purpose

Turn the rough MVP into a genuinely usable agent dashboard on the Ulanzi D200:
attention-first layout with paging, per-agent icons, clear animated status, a
usable large status panel, and reliable rendering. This is **Spec 1** of three.

### In scope (Spec 1)
- Attention-first ordering + paging (handle more agents than tiles).
- Per-agent icons (Simple Icons → glyph fallback → user override).
- Status visualization incl. a low-rate "working" spinner.
- The large status panel as a real, programmatically-rendered content area:
  overview summary (with paging) and per-agent detail in drill-in.
- Reliability fixes for the freeze bug and rendering robustness.

### Out of scope (later specs)
- **Spec 2:** tap an agent → open its live terminal (`herdr attach`) on the Mac.
- **Spec 3:** usage / remaining limits on the panel (needs a data source —
  separate research).

## 2. Hardware facts (verified on device)

- The deck has **13 renderable tiles**, button indices `0..12`, laid out 5×3
  (row = idx//5, col = idx%5), so tiles occupy rows 0–1 fully and row 2 cols 0–2.
- The bottom-right **large status panel** spans the remaining two row-2 cells,
  grid positions `3_2` and `4_2`, i.e. **button indices 13 and 14**. strmdck
  never addresses them, but `set_buttons` with those indices renders images
  there. Combined width ≈ **392×196 px** (two 196×196 cells).
- By default the panel shows a firmware background (a glitched logo) + a
  CPU/RAM/GPU stats overlay. Sending `set_small_window_data(mode=BACKGROUND)`
  removes the stats overlay, leaving the panel fully ours to render. The panel
  is touch-pressable (reports index 13).
- Rendering is a full ZIP upload via `set_buttons`; `set_buttons(update_only=True)`
  sends only changed buttons (used for the spinner).
- strmdck is asyncio-based; it writes `.build/` and reads `.cache/icons/_generated/`
  **relative to the process CWD** — the source of the MVP freeze (the icon dir
  was deleted under it). v2 must use a stable, app-owned working directory.

## 3. Components

Each unit has one responsibility and a clear interface.

### 3.1 `icons.py` — IconProvider (new)
Resolves an agent type to a tile icon PNG, composited on a status-colored
background.
- **Mapping:** `agent_type` → Simple Icons slug (config-driven map, e.g.
  `claude→claude`, `codex→` *(no SI icon; glyph)*, `cursor→cursor`,
  `copilot→githubcopilot`, `gemini→googlegemini`, `opencode→opencode`).
- **Build:** for each needed icon, fetch the Simple Icons SVG (CDN, cached on
  disk), rasterize to PNG via `cairosvg`, and store in the app icon cache. If the
  slug is missing or offline and no cache exists, fall back to a **generated
  glyph** (first letter on a tinted tile). A user PNG at
  `<config_dir>/icons/<agent_type>.png` overrides everything.
- **Render-time:** `icon_for(agent_type, color, state) -> path` returns a cached
  composited PNG (agent mark on the status background). Results are cached by
  `(agent_type, color, spinner_phase)`.
- **Depends on:** `cairosvg` (rasterize, build-time only), `pillow` (composite),
  network (first build only; cache thereafter).

### 3.2 `layout.py` — overview/paging + panel content (new, pure)
Pure functions that turn agent state into what to show. Extracted from the
orchestrator so ordering/paging/panel logic is testable in isolation.
- `order_agents(agents) -> list[AgentState]`: stable sort by
  **(status priority blocked<working<idle<done<unknown, server order, pane_id)**.
- `page(ordered, page_index, tile_count) -> (tiles_slice, page_count)`.
- `summary(agents) -> Counts`: blocked/working/idle/done totals.
- `panel_overview(counts, page_index, page_count, connection) -> PanelView`.
- `panel_detail(agent, request_text) -> PanelView`.

### 3.3 `orchestrator.py` — state + render model (modified)
- Holds agents, connection, current page, drill target, detection text,
  **spinner phase**.
- `render() -> RenderState` where `RenderState = (tiles: list[TileView], panel: PanelView)`.
  - **Overview:** tiles 0..N-1 filled from the current page (icon + status color +
    project label; working tiles carry `spinner=phase`); panel = `panel_overview(...)`.
  - **Drill-in:** tiles 0..4 = Approve / Approve! / Deny / Stop / Back, rest dim;
    panel = `panel_detail(agent, detection_text)`.
- `on_press(index) -> list[Command]`:
  - Overview: tap agent tile (blocked → drill-in + read; non-blocked → drill-in
    too so its detail/output shows, but action tiles only act when allowed);
    tap the **panel (index 13)** → next page.
  - Drill-in: Approve/Deny/Stop resolve the answer profile (unchanged); Back →
    overview.
- `tick()` advances `spinner_phase`; returns the set of tile indices that are
  `working` (so the app can do a partial re-render).
- System tiles (Next/Refresh/Link) from v1 are **removed** — their roles move to
  the panel (paging + connection/counts) and attention-first ordering (blocked is
  always first).

### 3.4 `driver/base.py` — interface (modified)
- New `PanelView` dataclass: `title: str`, `lines: list[str]`, `color: str`.
- `TileView` gains `agent_type: str | None = None` and `spinner: int | None = None`
  so the driver composites icon + spinner.
- `DeckDriver` gains `render_panel(panel: PanelView) -> None` and
  `tile_count() -> int` (already `slot_count`). `render(tiles)` unchanged in
  signature; the driver uses `IconProvider` to turn a `TileView` into an image.

### 3.5 `driver/d200.py` — hardware (modified)
- **Stable working dir:** create/own `<cache_dir>/herdeck` (e.g.
  `~/.cache/herdeck`), `os.chdir` there (or pass absolute paths) so strmdck's
  `.build`/`.cache` are stable and never collide with the repo.
- **Icon regen:** ensure the icon cache exists before each render; regenerate if
  missing (defends against the freeze bug).
- **Resilient render:** wrap `set_buttons` so an exception is logged and skipped,
  never freezing the loop.
- **Panel:** on init, `set_small_window_data(mode=BACKGROUND)` to drop the stats
  overlay. `render_panel` composites a 392×196 image (title + wrapped lines on a
  color), splits it into left/right 196×196 halves, and renders them to button
  indices **13** and **14**.
- **Spinner:** the app's tick calls a partial render of working tiles
  (`set_buttons(update_only=True)`); the composited icon includes a spinner arc at
  the tile's `spinner` phase.

### 3.6 `driver/fake.py` — test/dev (modified)
Records `last` tiles and `last_panel`; no real rasterization. Lets orchestrator
and app be tested headless.

### 3.7 `app.py` — wiring (modified)
- Builds orchestrator with `deck.tile_count()`.
- Renders both surfaces: `deck.render(state.tiles)` + `deck.render_panel(state.panel)`.
- **Animation tick loop:** every ~0.4 s, `orch.tick()`; if any working tiles,
  re-render just those via the driver's partial path. Runs as a guarded async task
  alongside connectors + the deck reader.

## 4. Data flow

```
herdr → bridge (snapshot/event) → connector → orchestrator state
orchestrator.render() → RenderState(tiles, panel) → deck.render + render_panel
tick timer → orchestrator.tick() → partial re-render of working tiles (spinner)
press → orchestrator.on_press() → Command(s) → connector → bridge → herdr
```

## 5. Status visualization

| State | Tile | Panel accent |
|---|---|---|
| blocked | amber background, bold `!` badge, agent icon | amber |
| working | green background, agent icon, **rotating spinner arc** | green |
| idle | blue background, agent icon | blue |
| done | dim background, agent icon, check | grey |
| disconnected (server) | red background | red |

Spinner: N-frame arc (e.g. 8 phases) advanced ~2–3×/s via partial update. If a
throughput test shows lag, fall back to a 2-phase brightness pulse (same plumbing,
fewer frames).

## 6. Reliability

- Stable app working dir for strmdck artifacts; icons regenerated on demand.
- Render exceptions are caught and logged; the loop keeps running.
- Panel driven programmatically (BACKGROUND mode) — no firmware clock/stats.
- Existing v1 guarantees retained: resync-on-reconnect, snapshot-on-change (incl.
  removals), per-connector isolation, constant-time auth, no double-approve.

## 7. Testing

- **layout.py (pure):** ordering priority, paging math/boundaries, summary counts,
  panel_overview/panel_detail content.
- **orchestrator:** render model for overview vs drill-in, page navigation via
  panel press, tick/spinner phase advance + working-tile set, drill-in actions
  (existing tests adapted to `RenderState`).
- **icons.py:** slug mapping, user-override precedence, glyph fallback when slug
  missing, cache reuse — with a fake rasterizer/HTTP so no network in tests.
- **driver/fake.py:** records tiles + panel; used by app tests.
- **app.py:** snapshot→render(tiles+panel), press routing incl. panel paging,
  tick triggers partial re-render (with fake deck).
- **d200 panel split:** the 392×196→two-halves split is a pure helper, unit-tested
  without hardware.
- **Manual (hardware):** icons render, spinner animates acceptably, panel shows
  overview summary and switches to agent detail on tap, paging works.

## 8. Risks / open items

- **Spinner throughput:** partial-update rate on real hardware unknown → fallback
  to pulse is built into the same plumbing.
- **Simple Icons coverage:** `openai`/`codex` have no SI icon → glyph fallback
  (verified). Slug map is config-extensible.
- **Panel legibility:** 392×196 fits a short title + ~3 wrapped lines; long
  request text is truncated with an ellipsis (full text via Spec 2 terminal-attach).
- **Cell 14 press index** is unconfirmed (only 13 observed); paging uses index 13,
  which is confirmed pressable.
