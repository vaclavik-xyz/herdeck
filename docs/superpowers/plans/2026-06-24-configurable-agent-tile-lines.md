# Configurable Agent Tile Lines Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user configure what the agent tile's two text lines show, via `[view].tile_primary` / `[view].tile_secondary` lists of tokens (`repo`, `branch`, `workspace`, `tab`, `agent`), reading workspace + tab labels live from herdr.

**Architecture:** Workspace/tab labels flow herdr → `bridge.py` wire pane → `protocol.py` → `AgentState` (and through `connector.py` `_rekey`, which rebuilds `AgentState` when re-stamping the server id). Two pure render helpers in `layout.py` (`compose_line`, `resolve_tile_lines`) turn a token list into the rendered string and resolve per-key fallbacks. Both render paths (`orchestrator.py` overview, `elgato/session.py` slot tile) call the helpers and fill the existing `TileView.repo` / `TileView.branch` slots; `icons.py` is untouched. Config keys are `list[str] | None` so "absent" (None → per-path fallback) is distinct from explicit `[]` (line off).

**Tech Stack:** Python 3.12+ (project manifest: `requires-python = ">=3.12"`, Ruff `target-version = "py312"` — write code compatible with that baseline), dataclasses, pytest, websockets/asyncio (existing). No new dependencies.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-24-configurable-agent-tile-lines-design.md`.
- Worktree (work only here; do NOT touch `/Users/admin/projects/herdeck`): `/Users/admin/.herdr/worktrees/herdeck/worktree-configurable-agent-tile-lines-clean`, branch `feat/configurable-agent-tile-lines-clean`.
- TDD: write the failing test first, run it and watch it fail, write minimal code, run it and watch it pass, commit.
- Run tests with the venv active: `source .venv/bin/activate`; run with `PYTHONPATH=. python -m pytest -q` (some tests import `tests.*`, which needs repo-root on the path).
- Conventional commits, **no `Co-Authored-By`** trailer. After each commit the post-commit hook runs roborev automatically.
- **Tokens (exactly):** `repo`, `branch`, `workspace`, `tab`, `agent`. An unknown token in config raises `ConfigError` at parse time in `settings.py`.
- **Line rendering:** tokens join with `" · "`; empty values are dropped; `tab` renders only when it has a value, as `▸{tab}`; an all-empty line yields `""` (existing render shows nothing).
- **Workspace/tab labels** come from herdr `workspace.list` (`workspaces[].label`) and `tab.list` (`tabs[].label`), looked up by the pane's `workspace_id` / `tab_id`. **Missing lookup or empty label → empty string; never use the raw id as tile text.**
- **Per-render-path fallback when a key is absent (None):** Orchestrator derives it from `tile_fields` (`["repo"]` if `"repo" in tile_fields` else `[]`; `["branch"]` if `"branch" in tile_fields` else `[]`), because that path already honors `tile_fields`. ElgatoSession falls back to fixed `["repo"]` / `["branch"]`, because that path never read `tile_fields` and always shows repo+branch.
- **Explicit value (including `[]`) wins per key** over any fallback; the two keys resolve independently.
- ElgatoSession must keep the selected-agent marker so the approve/deny/stop target stays visually unambiguous, **without violating `[]` semantics**: mark the first non-empty resolved line (primary, else secondary) with the `* ` prefix; an explicitly-empty line (`tile_primary = []`) stays empty and is never rendered as a bare `* `.
- `icons.py` stays unchanged. `TileView.repo` / `TileView.branch` keep their names (now "primary/secondary text slot"); add a clarifying comment, do not rename across the codebase.

---

### Task 1: `AgentState` workspace/tab fields + protocol passthrough

**Files:**
- Modify: `src/herdeck/model.py:21-29` (add fields to `AgentState`)
- Modify: `src/herdeck/protocol.py:20-29` (`_pane_to_state` reads new fields)
- Modify: `src/herdeck/connector.py:106-114` (`_rekey` carries new fields through)
- Test: `tests/test_protocol.py`, `tests/test_connector.py`

**Interfaces:**
- Produces: `AgentState.workspace: str = ""`, `AgentState.tab: str = ""`. `protocol._pane_to_state` reads wire keys `"workspace"` and `"tab"` (default `""`). `connector.Connector._rekey` carries `workspace`/`tab` through when re-stamping the server id (else rekeyed deployments lose them).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_protocol.py`:

```python
def test_decode_snapshot_preserves_workspace_and_tab():
    raw = (
        '{"type":"snapshot","server_id":"workbox","panes":'
        '[{"pane_id":"w2:p1","agent_type":"claude","label":"herdeck",'
        '"status":"working","project":"herdeck","repo":"herdeck",'
        '"branch":"main","workspace":"herdeck","tab":"2"}]}'
    )
    msg = decode_inbound(raw)
    assert msg.states[0].workspace == "herdeck"
    assert msg.states[0].tab == "2"


def test_decode_snapshot_defaults_workspace_and_tab_to_empty():
    raw = (
        '{"type":"snapshot","server_id":"workbox","panes":'
        '[{"pane_id":"w1:p1","agent_type":"claude","label":"api","status":"idle"}]}'
    )
    msg = decode_inbound(raw)
    assert msg.states[0].workspace == ""
    assert msg.states[0].tab == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python -m pytest tests/test_protocol.py::test_decode_snapshot_preserves_workspace_and_tab -v`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'workspace'` or `AttributeError: 'AgentState' object has no attribute 'workspace'`.

- [ ] **Step 3: Add fields to `AgentState`**

In `src/herdeck/model.py`, extend the dataclass (append after `branch`):

```python
@dataclass
class AgentState:
    key: AgentKey
    agent_type: str
    label: str
    status: Status
    project: str = ""
    repo: str = ""  # git repo name (from herdr worktree label)
    branch: str = ""  # git branch (from herdr worktree)
    workspace: str = ""  # herdr workspace label (workspace.list)
    tab: str = ""  # herdr tab label (tab.list)
```

- [ ] **Step 4: Read new fields in `_pane_to_state`**

In `src/herdeck/protocol.py`, update `_pane_to_state` to pass the two keys:

```python
def _pane_to_state(server_id: str, pane: dict) -> AgentState:
    return AgentState(
        key=AgentKey(server_id, pane["pane_id"]),
        agent_type=pane.get("agent_type", "default"),
        label=pane.get("label", ""),
        status=_status(pane.get("status", "unknown")),
        project=pane.get("project", ""),
        repo=pane.get("repo", ""),
        branch=pane.get("branch", ""),
        workspace=pane.get("workspace", ""),
        tab=pane.get("tab", ""),
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=. python -m pytest tests/test_protocol.py -v`
Expected: PASS (all protocol tests, including the two new ones).

- [ ] **Step 6: Write the failing connector test**

`connector.Connector._rekey` rebuilds `AgentState` (re-stamping the server id) by copying fields explicitly, so it must carry the two new fields or rekeyed snapshots/events drop them. Add to `tests/test_connector.py` (`Connector` and `ServerConfig` are already imported there):

```python
def test_dispatch_rekey_preserves_workspace_and_tab():
    cfg = ServerConfig("dev", "ws://x", "t")
    seen = {}
    conn = Connector(
        cfg,
        on_snapshot=lambda sid, st: seen.update(states=st),
        on_event=lambda sid, s: None,
        on_connection=lambda sid, up: None,
    )
    raw = (
        '{"type":"snapshot","server_id":"some-bridge-id","panes":'
        '[{"pane_id":"w2:p1","agent_type":"claude","label":"herdeck",'
        '"status":"working","project":"herdeck","repo":"herdeck",'
        '"branch":"main","workspace":"herdeck","tab":"2"}]}'
    )
    conn._dispatch(raw)
    assert seen["states"][0].key.server_id == "dev"   # rekeyed to configured id
    assert seen["states"][0].workspace == "herdeck"   # carried through rekey
    assert seen["states"][0].tab == "2"
```

- [ ] **Step 7: Run the connector test to verify it fails**

Run: `PYTHONPATH=. python -m pytest tests/test_connector.py::test_dispatch_rekey_preserves_workspace_and_tab -v`
Expected: FAIL — `assert "" == "herdeck"` (the rekeyed copy drops `workspace`/`tab`).

- [ ] **Step 8: Carry the fields through `_rekey`**

In `src/herdeck/connector.py`, add the two fields to the rebuilt `AgentState`:

```python
        return AgentState(
            key=AgentKey(self.server.id, state.key.pane_id),
            agent_type=state.agent_type,
            label=state.label,
            status=state.status,
            project=state.project,
            repo=state.repo,
            branch=state.branch,
            workspace=state.workspace,
            tab=state.tab,
        )
```

- [ ] **Step 9: Run the connector tests to verify they pass**

Run: `PYTHONPATH=. python -m pytest tests/test_connector.py -v`
Expected: PASS (existing rekey test + the new one).

- [ ] **Step 10: Commit**

```bash
git add src/herdeck/model.py src/herdeck/protocol.py src/herdeck/connector.py tests/test_protocol.py tests/test_connector.py
git commit -m "feat(model): add workspace/tab labels to AgentState, protocol + connector passthrough"
```

---

### Task 2: `bridge.py` — fetch workspace/tab labels into the wire pane

**Files:**
- Modify: `src/herdeck/bridge.py` (`HerdrClient`, `StubHerdr`, `SocketHerdr`, `_herdr_pane_to_wire`, `_wire_panes`, `_wired_snapshot`, new index helpers)
- Test: `tests/test_bridge.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `bridge._workspaces_by_id(workspaces: list[dict]) -> dict[str, str]` — `{workspace_id: label}`.
  - `bridge._tabs_by_id(tabs: list[dict]) -> dict[str, str]` — `{tab_id: label}`.
  - `_herdr_pane_to_wire(p, wt_by_ws=None, ws_by_id=None, tab_by_id=None)` now adds wire keys `"workspace"` and `"tab"`.
  - `HerdrClient.workspaces() -> list[dict]`, `HerdrClient.tabs() -> list[dict]`; implemented by `SocketHerdr` (RPC `workspace.list` → `result.workspaces`, `tab.list` → `result.tabs`) and `StubHerdr`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_bridge.py` (note `raw_pane` currently has no `tab_id`; pass one explicitly where needed):

```python
from herdeck.bridge import _tabs_by_id, _workspaces_by_id  # add to existing imports


def test_herdr_pane_to_wire_adds_workspace_and_tab_labels():
    raw = raw_pane(agent="claude", status="working")
    raw["workspace_id"] = "w2"
    raw["tab_id"] = "w2:t3"
    ws_by_id = _workspaces_by_id([{"workspace_id": "w2", "label": "herdeck"}])
    tab_by_id = _tabs_by_id([{"tab_id": "w2:t3", "label": "2"}])
    w = _herdr_pane_to_wire(raw, None, ws_by_id, tab_by_id)
    assert w["workspace"] == "herdeck"
    assert w["tab"] == "2"


def test_herdr_pane_to_wire_blank_workspace_tab_when_lookup_missing():
    raw = raw_pane(agent="claude", status="idle")
    raw["workspace_id"] = "w9"
    raw["tab_id"] = "w9:t1"
    w = _herdr_pane_to_wire(raw, None, {}, {})
    # never fall back to the raw id
    assert w["workspace"] == ""
    assert w["tab"] == ""


def test_workspaces_and_tabs_by_id_index_label():
    assert _workspaces_by_id([{"workspace_id": "w2", "label": "herdeck"}]) == {"w2": "herdeck"}
    assert _tabs_by_id([{"tab_id": "w2:t1", "label": "1"}]) == {"w2:t1": "1"}
    # entries without an id are skipped
    assert _workspaces_by_id([{"label": "x"}]) == {}


async def test_list_snapshot_includes_workspace_and_tab():
    panes = [{
        "pane_id": "w2:p1", "workspace_id": "w2", "tab_id": "w2:t1",
        "cwd": "/Users/admin/projects/herdeck",
        "foreground_cwd": "/Users/admin/projects/herdeck",
        "agent": "claude", "agent_status": "blocked",
    }]
    herdr = StubHerdr(
        panes=panes,
        workspaces=[{"workspace_id": "w2", "label": "herdeck"}],
        tabs=[{"tab_id": "w2:t1", "label": "1"}],
    )
    out = await handle_client_message(herdr, "local", '{"type":"list"}')
    p = json.loads(out)["panes"][0]
    assert p["workspace"] == "herdeck"
    assert p["tab"] == "1"
```

Also **update the existing** `test_herdr_pane_to_wire_maps_fields` exact-equality assertion (it asserts the whole dict, so it breaks once `_herdr_pane_to_wire` adds the two keys). Add `"workspace": ""` and `"tab": ""`:

```python
    assert w == {
        "pane_id": "w1:p1",
        "agent_type": "codex",
        "label": "web",
        "status": "working",
        "project": "web",
        "repo": "web",
        "branch": "",
        "workspace": "",
        "tab": "",
    }
```

(`test_herdr_pane_to_wire_adds_repo_and_branch_from_worktree` asserts individual keys, so it needs no change.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. python -m pytest tests/test_bridge.py::test_herdr_pane_to_wire_adds_workspace_and_tab_labels -v`
Expected: FAIL with `ImportError: cannot import name '_workspaces_by_id'`.

- [ ] **Step 3: Add index helpers + wire mapping**

In `src/herdeck/bridge.py`, add the helpers next to `_worktrees_by_workspace`:

```python
def _workspaces_by_id(workspaces: list[dict]) -> dict[str, str]:
    """Index herdr workspaces (workspace.list) as {workspace_id: label}."""
    return {w["workspace_id"]: w.get("label", "") for w in (workspaces or []) if w.get("workspace_id")}


def _tabs_by_id(tabs: list[dict]) -> dict[str, str]:
    """Index herdr tabs (tab.list) as {tab_id: label}."""
    return {t["tab_id"]: t.get("label", "") for t in (tabs or []) if t.get("tab_id")}
```

Update `_herdr_pane_to_wire` to accept the indexes and add the two wire keys:

```python
def _herdr_pane_to_wire(
    p: dict,
    wt_by_ws: dict[str, dict] | None = None,
    ws_by_id: dict[str, str] | None = None,
    tab_by_id: dict[str, str] | None = None,
) -> dict:
    """Map a raw herdr pane to herdeck's wire pane schema.

    herdr uses `agent` / `agent_status` and has no human label. We derive repo +
    branch from the pane's open worktree (herdr `worktree.list`), falling back to
    the working-directory basename when no worktree info is available. The
    workspace/tab labels come from `workspace.list` / `tab.list`; a missing lookup
    or empty label stays empty (the raw id is never used as tile text).
    """
    cwd = p.get("foreground_cwd") or p.get("cwd") or ""
    label = os.path.basename(cwd.rstrip("/")) or p.get("workspace_id", "")
    wt = (wt_by_ws or {}).get(p.get("workspace_id", ""), {})
    repo = wt.get("label") or label
    branch = wt.get("branch") or ""
    return {
        "pane_id": p["pane_id"],
        "agent_type": p.get("agent", "default"),
        "label": label,
        "status": p.get("agent_status", "unknown"),
        "project": label,
        "repo": repo,
        "branch": branch,
        "workspace": (ws_by_id or {}).get(p.get("workspace_id", ""), ""),
        "tab": (tab_by_id or {}).get(p.get("tab_id", ""), ""),
    }
```

- [ ] **Step 4: Thread indexes through `_wire_panes` and `_wired_snapshot`**

Update `_wire_panes` and `_wired_snapshot` in `src/herdeck/bridge.py`:

```python
def _wire_panes(
    raw: list[dict],
    worktrees: list[dict] | None = None,
    workspaces: list[dict] | None = None,
    tabs: list[dict] | None = None,
) -> list[dict]:
    wt_by_ws = _worktrees_by_workspace(worktrees or [])
    ws_by_id = _workspaces_by_id(workspaces or [])
    tab_by_id = _tabs_by_id(tabs or [])
    return [_herdr_pane_to_wire(p, wt_by_ws, ws_by_id, tab_by_id) for p in raw if _is_agent_pane(p)]


async def _wired_snapshot(herdr: HerdrClient) -> list[dict]:
    """Fetch panes + worktrees + workspaces + tabs from herdr and build the wire snapshot."""
    raw = await herdr.list_panes()
    try:
        worktrees = await herdr.worktrees()
    except Exception:
        worktrees = []
    try:
        workspaces = await herdr.workspaces()
    except Exception:
        workspaces = []
    try:
        tabs = await herdr.tabs()
    except Exception:
        tabs = []
    return _wire_panes(raw, worktrees, workspaces, tabs)
```

- [ ] **Step 5: Extend the `HerdrClient` protocol and both implementations**

In `src/herdeck/bridge.py`, add to the `HerdrClient` Protocol (next to `worktrees`):

```python
    async def worktrees(self) -> list[dict]: ...
    async def workspaces(self) -> list[dict]: ...
    async def tabs(self) -> list[dict]: ...
```

Add to `SocketHerdr` (next to its `worktrees` method):

```python
    async def workspaces(self) -> list[dict]:
        res = await self._rpc("workspace.list", {})
        return res.get("result", {}).get("workspaces", [])

    async def tabs(self) -> list[dict]:
        res = await self._rpc("tab.list", {})
        return res.get("result", {}).get("tabs", [])
```

Update `StubHerdr.__init__` and add the methods:

```python
    def __init__(
        self,
        panes: list[dict],
        worktrees: list[dict] | None = None,
        workspaces: list[dict] | None = None,
        tabs: list[dict] | None = None,
    ):
        self.panes = panes
        self._worktrees = worktrees or []
        self._workspaces = workspaces or []
        self._tabs = tabs or []
        self.detection: dict[str, str] = {}
        self.sent: list[tuple[str, list[str]]] = []
        self.focused: list[str] = []
        self.started: list[tuple[str, list[str]]] = []
```

```python
    async def workspaces(self) -> list[dict]:
        return self._workspaces

    async def tabs(self) -> list[dict]:
        return self._tabs
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `PYTHONPATH=. python -m pytest tests/test_bridge.py -v`
Expected: PASS (existing bridge tests — with the updated `maps_fields` assertion — plus the four new ones).

- [ ] **Step 7: Commit**

```bash
git add src/herdeck/bridge.py tests/test_bridge.py
git commit -m "feat(bridge): fetch workspace/tab labels from herdr into wire pane"
```

---

### Task 3: `ViewConfig` keys + settings parsing/validation + `[profiles.X.view]` merge

**Files:**
- Modify: `src/herdeck/config.py:70-77` (`ViewConfig` fields) + add `TILE_LINE_TOKENS` constant
- Modify: `src/herdeck/settings.py:192-204` (`_view_config` parse + validate); import `TILE_LINE_TOKENS`
- Test: `tests/test_config.py`, `tests/test_settings.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `config.TILE_LINE_TOKENS: tuple[str, ...] = ("repo", "branch", "workspace", "tab", "agent")`.
  - `ViewConfig.tile_primary: list[str] | None = None`, `ViewConfig.tile_secondary: list[str] | None = None` (None = absent → per-path fallback; `[]` = explicit off).
  - `settings._view_config` parses the two keys when present, validates every token against `TILE_LINE_TOKENS`, raises `ConfigError` otherwise.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py` (inside the existing default-config test, after the `tile_fields` assert):

```python
    assert cfg.view.tile_primary is None
    assert cfg.view.tile_secondary is None
```

Add to `tests/test_settings.py` (import `_view_config` and `ConfigError`):

```python
from herdeck.config import ConfigError  # if not already imported
from herdeck.settings import _view_config  # add to existing imports


def test_view_config_parses_tile_lines():
    view = _view_config({"tile_primary": ["workspace"], "tile_secondary": ["tab", "branch"]})
    assert view.tile_primary == ["workspace"]
    assert view.tile_secondary == ["tab", "branch"]


def test_view_config_defaults_tile_lines_to_none():
    view = _view_config({})
    assert view.tile_primary is None
    assert view.tile_secondary is None


def test_view_config_keeps_explicit_empty_list():
    view = _view_config({"tile_primary": []})
    assert view.tile_primary == []
    assert view.tile_secondary is None


def test_view_config_rejects_unknown_tile_token():
    with pytest.raises(ConfigError, match="unknown tile token 'bogus'"):
        _view_config({"tile_secondary": ["branch", "bogus"]})


def test_profile_view_overlay_merges_tile_primary():
    data = {
        "view": {"tile_fields": ["repo"]},
        "profiles": {"solo": {"view": {"tile_primary": ["workspace"]}}},
    }
    merged, _ = _merged_sections(data, "solo")
    assert merged["view"] == {"tile_fields": ["repo"], "tile_primary": ["workspace"]}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. python -m pytest tests/test_settings.py::test_view_config_rejects_unknown_tile_token -v`
Expected: FAIL with `AttributeError: 'ViewConfig' object has no attribute 'tile_primary'` (and the validation test fails because no `ConfigError` is raised).

- [ ] **Step 3: Add the constant and fields in `config.py`**

In `src/herdeck/config.py`, add the token constant near `DEFAULT_TILE_FIELDS` (line ~60):

```python
TILE_LINE_TOKENS: tuple[str, ...] = ("repo", "branch", "workspace", "tab", "agent")
```

Extend `ViewConfig`:

```python
@dataclass
class ViewConfig:
    management: str = "launcher_menu"
    bottom_row: list[str] = field(default_factory=lambda: list(DEFAULT_BOTTOM_ROW))
    show_profile_on_panel: bool = False
    agent_slots: str = "max"
    tile_fields: list[str] = field(default_factory=lambda: list(DEFAULT_TILE_FIELDS))
    # None = key absent (each render path supplies its own fallback);
    # [] = explicitly empty (that text line is off). A non-empty list is a
    # token list rendered by layout.compose_line.
    tile_primary: list[str] | None = None
    tile_secondary: list[str] | None = None
```

- [ ] **Step 4: Parse + validate in `settings._view_config`**

In `src/herdeck/settings.py`, import the constant (add `TILE_LINE_TOKENS` to the existing `from .config import (...)` block) and extend `_view_config`:

```python
def _view_config(raw: dict | None) -> ViewConfig:
    raw = raw or {}
    view = ViewConfig()
    for key in ("management", "agent_slots"):
        if key in raw:
            setattr(view, key, raw[key])
    if "bottom_row" in raw:
        view.bottom_row = list(raw["bottom_row"])
    if "tile_fields" in raw:
        view.tile_fields = list(raw["tile_fields"])
    for key in ("tile_primary", "tile_secondary"):
        if key in raw:
            tokens = list(raw[key])
            for tok in tokens:
                if tok not in TILE_LINE_TOKENS:
                    raise ConfigError(f"unknown tile token '{tok}' in view.{key}")
            setattr(view, key, tokens)
    if "show_profile_on_panel" in raw:
        view.show_profile_on_panel = bool(raw["show_profile_on_panel"])
    return view
```

(`ConfigError` is already imported in `settings.py`; if not, add `ConfigError` to the `from .config import (...)` block.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=. python -m pytest tests/test_config.py tests/test_settings.py -v`
Expected: PASS (defaults None, parsing, explicit `[]` kept, unknown token raises, profile overlay merges).

- [ ] **Step 6: Commit**

```bash
git add src/herdeck/config.py src/herdeck/settings.py tests/test_config.py tests/test_settings.py
git commit -m "feat(config): add view.tile_primary/tile_secondary with token validation"
```

---

### Task 4: `layout.py` render helpers — `compose_line` + `resolve_tile_lines`

**Files:**
- Modify: `src/herdeck/layout.py` (add two pure functions)
- Test: `tests/test_layout.py`

**Interfaces:**
- Consumes: `AgentState` (with `workspace`/`tab` from Task 1), `ViewConfig` (with `tile_primary`/`tile_secondary` from Task 3).
- Produces:
  - `layout.compose_line(state: AgentState, tokens: list[str]) -> str` — maps tokens to values (`repo`→`repo or label`, `branch`→`branch`, `workspace`→`workspace`, `tab`→`▸{tab}` only when non-empty, `agent`→`agent_type`), drops empties, joins with `" · "`. Unknown token contributes nothing.
  - `layout.resolve_tile_lines(view, fallback_primary: list[str], fallback_secondary: list[str]) -> tuple[list[str], list[str]]` — per key: explicit value (incl. `[]`) wins; `None` → the given fallback.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_layout.py`:

```python
from herdeck.config import TILE_LINE_TOKENS, ViewConfig
from herdeck.layout import compose_line, resolve_tile_lines
from herdeck.model import AgentKey, AgentState, Status


def _astate(repo="herdeck", branch="main", workspace="herdeck", tab="2", label="herdeck", agent="claude"):
    s = AgentState(AgentKey("dev", "w2:p1"), agent, label, Status.WORKING)
    s.repo, s.branch, s.workspace, s.tab = repo, branch, workspace, tab
    return s


def test_compose_line_joins_tokens_with_separator():
    assert compose_line(_astate(), ["tab", "branch"]) == "▸2 · main"


def test_compose_line_omits_empty_values():
    s = _astate(branch="")
    assert compose_line(s, ["repo", "branch"]) == "herdeck"


def test_compose_line_tab_only_when_present():
    assert compose_line(_astate(tab=""), ["tab", "branch"]) == "main"
    assert compose_line(_astate(tab="3"), ["tab"]) == "▸3"


def test_compose_line_repo_falls_back_to_label():
    s = _astate(repo="", label="api")
    assert compose_line(s, ["repo"]) == "api"


def test_compose_line_empty_when_all_values_empty():
    s = _astate(workspace="", tab="")
    assert compose_line(s, ["workspace", "tab"]) == ""


def test_compose_line_handles_every_valid_token():
    s = _astate()
    for tok in TILE_LINE_TOKENS:
        # must not raise and must return a string
        assert isinstance(compose_line(s, [tok]), str)


def test_resolve_tile_lines_uses_fallback_when_none():
    view = ViewConfig()  # tile_primary/secondary default None
    primary, secondary = resolve_tile_lines(view, ["repo"], ["branch"])
    assert primary == ["repo"]
    assert secondary == ["branch"]


def test_resolve_tile_lines_explicit_wins_per_key_including_empty():
    view = ViewConfig()
    view.tile_primary = ["workspace"]
    view.tile_secondary = []  # explicit off
    primary, secondary = resolve_tile_lines(view, ["repo"], ["branch"])
    assert primary == ["workspace"]  # explicit wins
    assert secondary == []           # explicit [] wins over fallback


def test_resolve_tile_lines_partial_override_one_key():
    view = ViewConfig()
    view.tile_primary = ["workspace"]  # secondary stays None -> fallback
    primary, secondary = resolve_tile_lines(view, ["repo"], ["branch"])
    assert primary == ["workspace"]
    assert secondary == ["branch"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. python -m pytest tests/test_layout.py::test_compose_line_joins_tokens_with_separator -v`
Expected: FAIL with `ImportError: cannot import name 'compose_line'`.

- [ ] **Step 3: Implement the helpers**

In `src/herdeck/layout.py`, add (after `status_color`):

```python
def compose_line(state: AgentState, tokens: list[str]) -> str:
    """Render an agent-tile text line from a token list.

    Tokens map to AgentState values; empty values are dropped and the rest are
    joined with " · ". `tab` is shown only when present, prefixed with ▸.
    """
    parts: list[str] = []
    for token in tokens:
        if token == "repo":
            value = state.repo or state.label
        elif token == "branch":
            value = state.branch
        elif token == "workspace":
            value = state.workspace
        elif token == "tab":
            value = f"▸{state.tab}" if state.tab else ""
        elif token == "agent":
            value = state.agent_type
        else:
            value = ""
        if value:
            parts.append(value)
    return " · ".join(parts)


def resolve_tile_lines(view, fallback_primary: list[str], fallback_secondary: list[str]):
    """Resolve (primary, secondary) token lists.

    Per key: an explicit config value (including an empty list) wins; an absent
    key (None) uses the render path's fallback.
    """
    primary = view.tile_primary if view.tile_primary is not None else fallback_primary
    secondary = view.tile_secondary if view.tile_secondary is not None else fallback_secondary
    return primary, secondary
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. python -m pytest tests/test_layout.py -v`
Expected: PASS (all layout tests, including the new ones).

- [ ] **Step 5: Commit**

```bash
git add src/herdeck/layout.py tests/test_layout.py
git commit -m "feat(layout): add compose_line + resolve_tile_lines tile helpers"
```

---

### Task 5: Orchestrator render path uses the helpers (fallback from `tile_fields`)

**Files:**
- Modify: `src/herdeck/orchestrator.py:190-233` (`_render_overview`)
- Test: `tests/test_orchestrator.py`

**Interfaces:**
- Consumes: `layout.compose_line`, `layout.resolve_tile_lines` (Task 4); `AgentState.workspace/tab` (Task 1); `view.tile_primary/secondary` (Task 3).
- Produces: agent tiles whose `repo`/`branch` slots come from the resolved token lines; Orchestrator fallback is derived from `tile_fields`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_orchestrator.py`:

```python
def test_overview_renders_configured_tile_lines():
    cfg = make_config()
    cfg.view.tile_primary = ["workspace"]
    cfg.view.tile_secondary = ["tab", "branch"]
    o = Orchestrator(cfg, slots=13)
    # distinct repo vs workspace so a stale "render repo as primary" impl fails
    s = AgentState(AgentKey("dev", "w2:p1"), "claude", "herdeck", Status.WORKING)
    s.repo, s.branch, s.workspace, s.tab = "api", "main", "herdeck", "2"
    o.apply_snapshot("dev", [s])

    tile = o.render().tiles[0]

    assert tile.repo == "herdeck"        # primary = workspace, NOT repo "api"
    assert tile.branch == "▸2 · main"    # secondary = tab + branch


def test_overview_tile_lines_fall_back_to_tile_fields():
    # No new keys set; tile_fields=["repo"] must still hide branch (today's behavior).
    cfg = make_multi_config()
    cfg.view.tile_fields = ["repo"]
    o = Orchestrator(cfg, slots=13)
    s = AgentState(AgentKey("alpha", "p1"), "claude", "api", Status.IDLE)
    s.repo, s.branch = "repo", "feat/x"
    o.apply_snapshot("alpha", [s])

    tile = o.render().tiles[0]

    assert tile.repo == "repo"
    assert tile.branch == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. python -m pytest tests/test_orchestrator.py::test_overview_renders_configured_tile_lines -v`
Expected: FAIL — `tile.repo == "repo"` (old code uses `s.repo or s.label`, ignoring `tile_primary`), and `tile.branch == "main"` not `"▸2 · main"`.

- [ ] **Step 3: Wire the helpers into `_render_overview`**

In `src/herdeck/orchestrator.py` `_render_overview`, after `fields = self.config.view.tile_fields` (line ~194) compute the resolved token lines once (they don't depend on the agent):

```python
        fields = self.config.view.tile_fields
        fb_primary = ["repo"] if "repo" in fields else []
        fb_secondary = ["branch"] if "branch" in fields else []
        primary_tokens, secondary_tokens = layout.resolve_tile_lines(
            self.config.view, fb_primary, fb_secondary
        )
        show_server_tags = "server" in fields and len({s.key.server_id for s in ordered}) > 1
```

Then in the agent-tile branch, replace the `repo=` / `branch=` arguments (lines ~222-223):

```python
                        repo=layout.compose_line(s, primary_tokens),
                        branch=layout.compose_line(s, secondary_tokens),
```

Leave `status_text`, `time_text`, `server_tag`, `server_accent`, `agent_type`, `spinner` unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. python -m pytest tests/test_orchestrator.py -v`
Expected: PASS — including the existing `test_tile_fields_can_hide_branch_status_time_and_server_tag` (proves backward compatibility).

- [ ] **Step 5: Commit**

```bash
git add src/herdeck/orchestrator.py tests/test_orchestrator.py
git commit -m "feat(orchestrator): render configurable tile lines (fallback from tile_fields)"
```

---

### Task 6: ElgatoSession slot tile uses the helpers (fixed repo+branch fallback) + keeps selected marker

**Files:**
- Modify: `src/herdeck/elgato/session.py:284-301` (`_slot_tile`)
- Test: `tests/test_elgato_session.py`

**Interfaces:**
- Consumes: `layout.compose_line`, `layout.resolve_tile_lines` (Task 4); `view.tile_primary/secondary` (Task 3). `session.py` already imports `from .. import layout`.
- Produces: slot tiles whose `repo`/`branch` slots come from the resolved token lines; ElgatoSession fallback is fixed `["repo"]` / `["branch"]`; the selected agent keeps a `* ` marker on the first non-empty line (primary, else secondary). If both resolved lines are empty, no bare marker is rendered.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_elgato_session.py` (these read the `TileView` directly via the internal `_slot_tile`, since `FakeIcons` does not encode `branch`):

```python
def test_slot_tile_renders_configured_lines():
    cfg = make_config()
    cfg.view.tile_primary = ["workspace"]
    cfg.view.tile_secondary = ["tab", "branch"]
    sess = ElgatoSession(cfg, FakeIcons())
    sess.set_slots([("s0", (0, 0))])
    s = AgentState(AgentKey("dev", "w2:p1"), "claude", "herdeck", Status.WORKING)
    # distinct repo vs workspace so a stale "render repo as primary" impl fails
    s.repo, s.branch, s.workspace, s.tab = "api", "main", "herdeck", "2"
    sess.apply_snapshot("dev", [s])

    tile = sess._slot_tile(0)

    assert tile.repo == "herdeck"        # primary = workspace, NOT repo "api"
    assert tile.branch == "▸2 · main"    # secondary = tab + branch


def test_slot_tile_fallback_is_fixed_repo_branch_ignoring_tile_fields():
    # Elgato path never honored tile_fields: even tile_fields=["repo"] keeps branch.
    cfg = make_config()
    cfg.view.tile_fields = ["repo"]
    sess = ElgatoSession(cfg, FakeIcons())
    sess.set_slots([("s0", (0, 0))])
    s = state("p1", Status.WORKING, "api")
    s.branch = "feat/x"
    sess.apply_snapshot("dev", [s])

    tile = sess._slot_tile(0)

    assert tile.repo == "api"
    assert tile.branch == "feat/x"


def test_slot_tile_keeps_selected_marker_on_primary():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_slots([("s0", (0, 0))])
    sess.apply_snapshot("dev", [state("p1", Status.BLOCKED, "api")])  # single blocked -> auto-selected

    tile = sess._slot_tile(0)

    assert sess.selected() == AgentKey("dev", "p1")
    assert tile.repo == "* api"


def test_slot_tile_selected_marker_moves_to_secondary_when_primary_off():
    # Explicit tile_primary=[] must stay empty (not become a bare "* ");
    # the selected marker moves to the next non-empty line instead.
    cfg = make_config()
    cfg.view.tile_primary = []
    cfg.view.tile_secondary = ["branch"]
    sess = ElgatoSession(cfg, FakeIcons())
    sess.set_slots([("s0", (0, 0))])
    s = state("p1", Status.BLOCKED, "api")  # single blocked -> auto-selected
    s.branch = "feat/x"
    sess.apply_snapshot("dev", [s])

    tile = sess._slot_tile(0)

    assert sess.selected() == AgentKey("dev", "p1")
    assert tile.repo == ""            # explicit [] stays empty, never "* "
    assert tile.branch == "* feat/x"  # marker moved to secondary


def test_slot_tile_selected_marker_hidden_when_both_lines_off():
    cfg = make_config()
    cfg.view.tile_primary = []
    cfg.view.tile_secondary = []
    sess = ElgatoSession(cfg, FakeIcons())
    sess.set_slots([("s0", (0, 0))])
    sess.apply_snapshot("dev", [state("p1", Status.BLOCKED, "api")])  # single blocked -> auto-selected

    tile = sess._slot_tile(0)

    assert sess.selected() == AgentKey("dev", "p1")
    assert tile.repo == ""
    assert tile.branch == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. python -m pytest tests/test_elgato_session.py::test_slot_tile_renders_configured_lines -v`
Expected: FAIL — `tile.repo == "api"` (old code uses `s.repo or s.label`) and `tile.branch == "main"` not `"▸2 · main"`.

- [ ] **Step 3: Wire the helpers into `_slot_tile`**

In `src/herdeck/elgato/session.py`, replace the body of `_slot_tile` after fetching `s`:

```python
    def _slot_tile(self, ordinal: int) -> TileView:
        key = self._leases.assignment().get(ordinal)
        if key is None:
            return TileView(ordinal, "", "dim")
        s = self._agents[key]
        down = s.key.server_id in self._down
        # Elgato never honored tile_fields -> fixed repo/branch fallback.
        primary_tokens, secondary_tokens = layout.resolve_tile_lines(
            self.config.view, ["repo"], ["branch"]
        )
        primary = layout.compose_line(s, primary_tokens)
        secondary = layout.compose_line(s, secondary_tokens)
        if key == self.selected():
            # Mark the first non-empty line so the act target stays identifiable
            # without turning an explicitly-empty line into a bare "* ".
            if primary:
                primary = f"* {primary}"
            elif secondary:
                secondary = f"* {secondary}"
        return TileView(
            ordinal,
            s.label,
            self._color(s),
            agent_type=s.agent_type,
            repo=primary,
            branch=secondary,
            status_text="OFFLINE" if down else s.status.value.upper(),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. python -m pytest tests/test_elgato_session.py -v`
Expected: PASS — including existing slot tests (selected marker + leasing unchanged).

- [ ] **Step 5: Run the full suite**

Run: `PYTHONPATH=. python -m pytest -q`
Expected: PASS (whole suite green).

- [ ] **Step 6: Commit**

```bash
git add src/herdeck/elgato/session.py tests/test_elgato_session.py
git commit -m "feat(elgato): render configurable tile lines, keep selected marker"
```

---

## Self-Review

**Spec coverage** (spec §→task):
- §4 config schema (`tile_primary`/`tile_secondary`, list, tokens, `ConfigError`) → Task 3.
- §4.1 line rendering (`▸` tab, `" · "`, drop empties) → Task 4 (`compose_line`).
- §4.2 per-key, per-render-path fallback (Orchestrator from `tile_fields`, Elgato fixed; explicit `[]` wins) → Task 4 (`resolve_tile_lines`) + Task 5 (Orchestrator fallback) + Task 6 (Elgato fallback).
- §4 `[profiles.X.view]` merge → Task 3 (`test_profile_view_overlay_merges_tile_primary`; uses existing `_merged_sections`, no code change needed).
- §5 data path (bridge → protocol → model; missing/empty → empty, never raw id) → Task 1 (model + protocol + connector `_rekey`) + Task 2 (bridge).
- §6 render in both paths; `TileView` names kept; `icons.py` untouched; Elgato selected marker → Tasks 5, 6.
- §7 edge cases (empty line, unknown token, missing label, tab-only, truncation unchanged, `workspace==repo` allowed) → Tasks 3, 4 (truncation/`workspace==repo` need no code; existing render + user choice).
- §8 tests → every task's test step. §3 out-of-scope (action tiles, badges, pane name) → untouched by design.

**Placeholder scan:** No TBD/TODO; every code/test step shows complete code and exact commands.

**Type consistency:** `compose_line(state, tokens)->str` and `resolve_tile_lines(view, fallback_primary, fallback_secondary)->tuple[list,list]` used identically in Tasks 4/5/6. Wire keys `"workspace"`/`"tab"` produced in Task 2, consumed in Task 1's `_pane_to_state`. `TILE_LINE_TOKENS` defined in Task 3 (`config.py`), consumed in Task 3 (`settings.py`) and Task 4 (test invariant). `ViewConfig.tile_primary/secondary: list[str] | None` consistent across Tasks 3/4/5/6.

**Note (no code, by design):** `[profiles.X.view]` merging already works via `_OVERLAY_SECTIONS` containing `"view"`; Task 3 only adds a test asserting it. Truncation/wrap stays in `icons.py` unchanged.
