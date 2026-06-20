# Status Panel Spotlight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the overview status panel's low-value `page 1/1` text with a context-aware "needs you" spotlight (offline → blocked → calm).

**Architecture:** `layout.panel_overview` gains `total` + `spotlight` params and renders one of three states by priority. `orchestrator._render_overview` computes the longest-waiting blocked agent (from `self._since`) and the total, and passes them in. The change is coupled (signature + caller + one existing assertion), so it lands as a single atomic task with a TDD step sequence.

**Tech Stack:** Python ≥3.12, `pytest` (pythonpath=src, asyncio_mode=auto — already configured).

## Global Constraints

- Affects only the **overview** panel — do NOT touch `layout.panel_detail`
  (drill) or the launcher panel.
- Priority order is exact: offline > blocked > calm.
- Page indicator appears ONLY when `page_count > 1`, appended as ` · {p}/{n}` to
  the last line.
- TDD: failing tests first, watch them fail, then implement. Conventional commit
  (English, no Co-Authored-By). Run `.venv/bin/python -m pytest`.
- After committing, check `roborev show <sha>` and fix any finding.

---

### Task 1: Context-aware overview panel spotlight

**Files:**
- Modify: `src/herdeck/layout.py` (`panel_overview`)
- Modify: `src/herdeck/orchestrator.py` (`_render_overview`; add `_blocked_spotlight`)
- Test: `tests/test_layout.py` (new panel_overview tests)
- Test: `tests/test_orchestrator_nav.py` (spotlight picks oldest blocked)
- Modify (regression): `tests/test_app.py` (`test_snapshot_renders_tiles_and_panel`)

**Interfaces:**
- Produces: `layout.panel_overview(counts: Counts, page_index: int, page_count: int, down: set[str], total: int, spotlight: tuple[str, str] | None) -> PanelView`
- Produces: `Orchestrator._blocked_spotlight() -> tuple[str, str] | None`
  returning `(label, elapsed_text)` of the longest-waiting BLOCKED agent.

- [ ] **Step 1: Write the failing layout tests**

Add to `tests/test_layout.py`:

```python
from herdeck.layout import panel_overview, Counts


def test_panel_overview_offline_takes_priority():
    pv = panel_overview(Counts(1, 0, 0, 0), 0, 1, {"srv"}, 5, ("api", "2m"))
    assert pv.title == "OFFLINE"
    assert pv.color == "red"


def test_panel_overview_blocked_spotlight():
    pv = panel_overview(Counts(1, 3, 6, 0), 0, 1, set(), 11, ("macdoktor-crm", "4m"))
    assert pv.title == "⚠ needs you"
    assert pv.lines[0] == "macdoktor-crm"
    assert pv.lines[1] == "blocked 4m"
    assert pv.color == "amber"


def test_panel_overview_blocked_without_elapsed():
    pv = panel_overview(Counts(1, 0, 0, 0), 0, 1, set(), 1, ("api", ""))
    assert pv.lines[1] == "blocked"


def test_panel_overview_calm():
    pv = panel_overview(Counts(0, 3, 6, 2), 0, 1, set(), 11, None)
    assert pv.title == "11 agents"
    assert pv.lines[0] == "W3 · I6 · D2"
    assert pv.lines[1] == "online"
    assert pv.color == "grey"


def test_panel_overview_page_suffix_only_when_multipage():
    multi = panel_overview(Counts(0, 1, 0, 0), 1, 3, set(), 5, None)
    assert multi.lines[-1].endswith(" · 2/3")
    single = panel_overview(Counts(0, 1, 0, 0), 0, 1, set(), 5, None)
    assert "/" not in single.lines[-1]
```

- [ ] **Step 2: Run the layout tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_layout.py -k panel_overview -v`
Expected: FAIL — `panel_overview()` currently takes 4 args (TypeError on the
6-arg calls) and emits `page x/y`.

- [ ] **Step 3: Implement the new `panel_overview`**

Replace `panel_overview` in `src/herdeck/layout.py` with:

```python
def panel_overview(counts: Counts, page_index: int, page_count: int,
                   down: set[str], total: int,
                   spotlight: tuple[str, str] | None) -> PanelView:
    if down:
        title, lines, color = "OFFLINE", ["reconnecting…"], "red"
    elif spotlight is not None:
        label, elapsed = spotlight
        title = "⚠ needs you"
        lines = [label, f"blocked {elapsed}".rstrip()]
        color = "amber"
    else:
        title = f"{total} agents"
        lines = [f"W{counts.working} · I{counts.idle} · D{counts.done}", "online"]
        color = "grey"
    if page_count > 1 and lines:
        lines[-1] = f"{lines[-1]} · {page_index + 1}/{page_count}"
    return PanelView(title=title, lines=lines, color=color)
```

- [ ] **Step 4: Run the layout tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_layout.py -k panel_overview -v`
Expected: PASS.

- [ ] **Step 5: Write the failing orchestrator test**

Add to `tests/test_orchestrator_nav.py` (reuse that file's existing config/agent
helpers; this snippet builds its own to be self-contained):

```python
def test_overview_panel_spotlights_oldest_blocked():
    from herdeck.config import AnswerProfile, Config
    from herdeck.model import AgentKey, AgentState, Status
    from herdeck.orchestrator import Orchestrator

    cfg = Config(servers=[], profiles={"default": AnswerProfile(["enter"], ["esc"],
                 ["ctrl+c"], ["enter"])}, overview_order=["s"], grid=(5, 3))
    now = [0.0]
    orch = Orchestrator(cfg, slots=13, clock=lambda: now[0])
    now[0] = 100.0
    orch.apply_event("s", AgentState(AgentKey("s", "p1"), "claude", "older", Status.BLOCKED))
    now[0] = 200.0
    orch.apply_event("s", AgentState(AgentKey("s", "p2"), "claude", "newer", Status.BLOCKED))
    now[0] = 260.0
    panel = orch.render().panel
    assert panel.title == "⚠ needs you"
    assert panel.lines[0] == "older"          # entered BLOCKED earliest
```

- [ ] **Step 6: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_orchestrator_nav.py::test_overview_panel_spotlights_oldest_blocked -v`
Expected: FAIL — `_render_overview` still calls the old 4-arg `panel_overview`
(TypeError) and renders `page x/y`.

- [ ] **Step 7: Wire the orchestrator**

In `src/herdeck/orchestrator.py`, add a helper (near the other render helpers):

```python
def _blocked_spotlight(self) -> tuple[str, str] | None:
    """The longest-waiting BLOCKED agent as (label, elapsed), or None."""
    blocked = [s for s in self._agents.values() if s.status is Status.BLOCKED]
    if not blocked:
        return None
    def started(s):
        rec = self._since.get(s.key)
        return rec[1] if rec else 0.0
    oldest = min(blocked, key=started)
    return (oldest.label, self._elapsed_text(oldest.key))
```

In `_render_overview`, replace the panel line:

```python
        panel = layout.panel_overview(layout.summary(ordered), self._page % pages,
                                      pages, self._down, len(ordered),
                                      self._blocked_spotlight())
```

- [ ] **Step 8: Update the existing app regression test**

In `tests/test_app.py`, `test_snapshot_renders_tiles_and_panel` currently asserts
`deck.last_panel.title.startswith("page 1/")`. The single agent there is BLOCKED,
so it now spotlights. Change that assertion to:

```python
    assert deck.last_panel is not None and deck.last_panel.title == "⚠ needs you"
```

- [ ] **Step 9: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS (all). No `page 1/` regressions elsewhere.

- [ ] **Step 10: Commit**

```bash
git add src/herdeck/layout.py src/herdeck/orchestrator.py \
        tests/test_layout.py tests/test_orchestrator_nav.py tests/test_app.py
git commit -m "feat(panel): context-aware needs-you spotlight in overview status"
```

- [ ] **Step 11: Roborev**

Run `roborev show <sha>` for the commit; if it reports a finding, fix it (TDD)
and amend/commit.

## Self-Review

**Spec coverage:**
- Offline/blocked/calm states + colors → Step 3 + layout tests (Steps 1, 4).
- Longest-waiting blocked selection → Step 7 `_blocked_spotlight` + Step 5 test.
- Page suffix only when >1 page → Step 3 + `test_panel_overview_page_suffix_only_when_multipage`.
- Data flow (total, spotlight from orchestrator) → Step 7.
- Regression on existing panel assertion → Step 8.
- Scope limited to overview panel → only `panel_overview` + `_render_overview`
  touched; `panel_detail`/launcher untouched.

**Placeholder scan:** none — all steps carry full code/commands.

**Type consistency:** `panel_overview(counts, page_index, page_count, down, total, spotlight)`
identical in Step 3 def and Step 7 call. `_blocked_spotlight() -> (label, elapsed) | None`
matches its use in Step 7. `Counts(blocked, working, idle, done)` field order used
consistently in tests.
