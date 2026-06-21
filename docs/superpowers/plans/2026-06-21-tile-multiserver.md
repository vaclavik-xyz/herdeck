# F6 — Tile / multi-server polish — Implementation Plan

> **For agentic workers:** strict TDD (failing test → minimal code → commit). Conventional commits (English, no Co-Authored-By). After each commit run `roborev show <sha>` and fix findings.

**Goal:** In multi-server setups, visually distinguish which server a tile's agent
belongs to, without changing the single-server look at all.

**Files:**
- Modify: `src/herdeck/driver/base.py` (`TileView`: add optional fields)
- Modify: `src/herdeck/orchestrator.py` (`_render_overview`: populate the new fields only when >1 server)
- Modify: `src/herdeck/icons.py` (`_compose_agent_tile`: draw the server tag when present)
- Test: `tests/test_orchestrator.py` (or `test_layout.py`), `tests/test_icons.py`

## Global constraints
- Work in your assigned worktree (lead gives path); verify branch first.
- venv: `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`. Tests: `.venv/bin/python -m pytest`.
- **Single-server output MUST be byte-for-byte unchanged.** New `TileView` fields default `None`; when only one server is configured/visible, leave them `None` so nothing renders differently.
- Do NOT touch `driver/web.py`, the status panel, or `[project]` metadata.
- Stable color per server id (hash the id to a palette); distinct from status colors.

---

### Task 1: TileView optional server fields
- [ ] **Failing test** (`tests/test_layout.py` or a new `tests/test_tileview.py`):
```python
from herdeck.driver.base import TileView
def test_tileview_server_fields_default_none():
    t = TileView(0, "x", "blue")
    assert t.server_tag is None and t.server_accent is None
```
- [ ] Run → FAIL (`TypeError`/`AttributeError`).
- [ ] Implement: add to the `TileView` dataclass:
```python
    server_tag: str | None = None      # short server label, shown only in multi-server
    server_accent: str | None = None   # color name for the server tag
```
- [ ] Run → PASS. Run full suite → still green (defaults don't affect anyone).
- [ ] Commit: `feat(tileview): optional server_tag/server_accent fields (default None)`.
- [ ] `roborev show <sha>`; fix findings.

### Task 2: orchestrator populates server fields only when multi-server
- [ ] **Failing test** (`tests/test_orchestrator.py`): build a config with TWO servers
  and agents on each; assert rendered overview tiles carry a non-None `server_tag`
  distinct per server. Then build a single-server config and assert tiles have
  `server_tag is None`.
```python
def test_multi_server_tiles_get_server_tag():
    from herdeck.config import AnswerProfile, Config
    from herdeck.model import AgentKey, AgentState, Status
    from herdeck.orchestrator import Orchestrator
    prof = {"default": AnswerProfile(["enter"],["esc"],["ctrl+c"],["enter"])}
    cfg = Config(servers=[], profiles=prof, overview_order=["a","b"], grid=(5,3))
    orch = Orchestrator(cfg, slots=13)
    orch.apply_snapshot("a", [AgentState(AgentKey("a","p1"),"claude","ra",Status.IDLE)])
    orch.apply_event("b", AgentState(AgentKey("b","p1"),"codex","rb",Status.IDLE))
    tags = {t.server_tag for t in orch.render().tiles if t.repo}
    assert None not in tags and len(tags) >= 2

def test_single_server_tiles_have_no_tag():
    from herdeck.config import AnswerProfile, Config
    from herdeck.model import AgentKey, AgentState, Status
    from herdeck.orchestrator import Orchestrator
    prof = {"default": AnswerProfile(["enter"],["esc"],["ctrl+c"],["enter"])}
    cfg = Config(servers=[], profiles=prof, overview_order=["a"], grid=(5,3))
    orch = Orchestrator(cfg, slots=13)
    orch.apply_snapshot("a", [AgentState(AgentKey("a","p1"),"claude","ra",Status.IDLE)])
    assert all(t.server_tag is None for t in orch.render().tiles)
```
- [ ] Run → FAIL.
- [ ] Implement in `_render_overview`: compute the set of distinct server ids among
  shown agents (or `len(overview_order)`); if `> 1`, set `server_tag` to a short label
  (e.g. first 3 chars of the server id, uppercased) and `server_accent` to a stable
  color chosen by hashing the server id into a small palette
  (`["teal","violet","orange","pink","lime"]`). When `<= 1` distinct server, leave None.
  Add a module-level helper `server_accent(server_id) -> str` (pure) so it's testable.
- [ ] Run → PASS, full suite green.
- [ ] Commit: `feat(orchestrator): tag tiles with server in multi-server mode`.
- [ ] `roborev show <sha>`; fix findings.

### Task 3: icons render the server tag
- [ ] **Failing test** (`tests/test_icons.py`): render a tile with `server_tag="WBX"`
  and assert it differs from the same tile without a tag (e.g. bright-pixel count or
  that a render path runs without error and produces a PNG). Keep it robust, not
  pixel-exact:
```python
def test_agent_tile_with_server_tag_renders():
    # reuse this file's existing IconProvider/tile fixtures
    base = render_tile_png(TileView(0,"",("blue"),agent_type="claude",repo="api",branch="x",
                            status_text="IDLE",time_text="1m"))
    tagged = render_tile_png(TileView(0,"",("blue"),agent_type="claude",repo="api",branch="x",
                            status_text="IDLE",time_text="1m",server_tag="WBX",server_accent="teal"))
    assert base != tagged
```
  (Adapt `render_tile_png` to however `tests/test_icons.py` already renders tiles.)
- [ ] Run → FAIL (tag not drawn yet → identical bytes).
- [ ] Implement in `_compose_agent_tile`: when `tile.server_tag`, draw the short tag as
  a small chip (server_accent background or text) in a free corner (e.g. top-right under
  the status word, or bottom-left), small font, not overlapping repo/branch. Keep layout
  stable when absent.
- [ ] Run → PASS, full suite green.
- [ ] Commit: `feat(icons): draw server tag chip on multi-server tiles`.
- [ ] `roborev show <sha>`; fix findings.

### Done
- Full suite green; single-server visuals unchanged. Short summary (commits + result).
  If stuck, describe and stop.
