# Status panel spotlight — design

- **Date:** 2026-06-20
- **Status:** Approved (brainstorming)

## Goal

Replace the low-value overview status panel (`page 1/1` / `B0 W3 I6` / `online`)
with a context-aware **"needs you" spotlight** that surfaces the single most
urgent thing at a glance — directly answering "is anything waiting on me?".

## Scope

Only the **overview** status panel: `layout.panel_overview` plus a small
computation in `orchestrator._render_overview`. The drill-in panel
(`layout.panel_detail`) and the launcher panel are unchanged. This affects every
deck target (D200, Elgato, web, fake) because they all render `PanelView` via
`icons.compose_panel`.

## Behaviour (priority order)

The panel is a title + up to 3 short lines. Exactly one state renders:

1. **Offline** — any configured server is down (`down` non-empty). Highest
   priority because the data may be stale.
   - title `OFFLINE`, line `reconnecting…`, color `red`.
2. **Blocked** — online and ≥1 agent is `BLOCKED`.
   - title `⚠ needs you`
   - line 1 = the **longest-waiting** blocked agent's label
   - line 2 = `blocked {elapsed}` (e.g. `blocked 4m`; if no elapsed is known,
     just `blocked`)
   - color `amber`.
   - "longest-waiting" = the blocked agent that entered `BLOCKED` earliest.
3. **Calm** — online and nothing blocked.
   - title `{N} agents` (N = total agents in the overview)
   - line 1 = `W{working} · I{idle} · D{done}`
   - line 2 = `online`
   - color `grey`.

**Page indicator:** the standalone `page x/y` title is removed. When
`page_count > 1`, append ` · {page}/{count}` to the **last** line of whichever
state is showing. When `page_count == 1`, show no page text.

## Data flow

`orchestrator._render_overview` already owns per-pane status-entry times
(`self._since`) and `self._elapsed_text(key)`. It computes and passes the
spotlight to layout:

- **spotlight**: among `self._agents` with `status is Status.BLOCKED`, pick the
  one with the earliest `self._since[key]` start time; produce
  `(label, self._elapsed_text(key))`. `None` when nothing is blocked.
- **total**: number of agents shown in the overview (`len(ordered)`).
- Existing inputs unchanged: `summary(ordered)` counts, current page index, page
  count, `self._down`.

## Interface

```python
# layout.py
def panel_overview(counts: Counts, page_index: int, page_count: int,
                   down: set[str], total: int,
                   spotlight: tuple[str, str] | None) -> PanelView: ...
```

`spotlight` is `(label, elapsed)` or `None`. Only caller is
`orchestrator._render_overview`, updated to pass `total` and `spotlight`.

## Edge cases

- No `_since` record for the blocked pane → line 2 is `blocked` (no time).
- Long agent label / lines → already truncated to the panel width by
  `icons._truncate` in `compose_panel`; no extra handling needed.
- Multiple servers with some down → offline state (any member in `down`).
- `page_count > 1` combines with any state via the suffix rule above.

## Testing (TDD)

- `tests/test_layout.py` — `panel_overview`:
  - offline (`down={"x"}`) → title `OFFLINE`, color `red`.
  - blocked (`spotlight=("api","4m")`) → title `⚠ needs you`, lines include
    `api` and `blocked 4m`, color `amber`.
  - calm (`spotlight=None`, total=11) → title `11 agents`, a line
    `W3 · I6 · D2`, line `online`, color `grey`.
  - page suffix: `page_count=3` → last line ends with `2/3`; `page_count=1` →
    no page text in any line.
- `tests/test_orchestrator_nav.py` (or `test_orchestrator.py`) — with two
  `BLOCKED` agents that entered the status at different times, the rendered
  overview panel spotlights the **older** one's label.
- `tests/test_app.py` — update `test_snapshot_renders_tiles_and_panel`: a single
  blocked agent now yields the spotlight title (`⚠ needs you`), not
  `page 1/…`.

## Out of scope

- Making the panel actionable (a click still only pages the overview).
- The drill-in and launcher panels.
- History / throughput metrics ("done today", etc.).
