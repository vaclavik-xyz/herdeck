# Herdeck Elgato Plugin — Backend (Python brain) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Python "brain" of the herdeck Elgato Stream Deck plugin — an `elgato-plugin` deck backend that maps live herdr agents onto user-placed Stream Deck keys (sticky slot leasing, global selection, arm-then-confirm Stop) and exposes a local IPC socket a thin TS shell drives. No TS, no hardware needed; fully unit-tested.

**Architecture:** A new `herdeck.elgato` package holds a pure session model (`SlotLeases`, `ElgatoSession`) that consumes herdeck's existing agent-state stream and produces per-key PNG renders + `Command`s, plus an async IPC server speaking a versioned JSON protocol. The runtime reuses herdeck's existing connectors/bridge/commands/answer-profiles/`IconProvider`; only the front-end (session + IPC) is new. This is one more front-end over the core, alongside `d200`/`web`/`fake` — it does NOT reuse the grid-based `Orchestrator`.

**Tech Stack:** Python 3.12, stdlib `asyncio`/`socket`/`json`, existing `herdeck.model`/`commands`/`config`/`connector`/`bootstrap`/`icons`/`layout`, existing pytest/pytest-asyncio.

## Global Constraints

- Python 3.12; stdlib only for the brain (no new runtime dependencies).
- Deck kind is `elgato-plugin`, distinct from the existing HID `elgato` kind.
- IPC `PROTOCOL_VERSION = 1`, sent in `hello`; mismatch → safe error state, never misbehave.
- Approve/Deny are **binary only**: enabled only when the selected agent is blocked, online, prompt read, and classified binary via `layout.parse_options` (no numbered options). Multi-option prompts disable Approve/Deny.
- Stop is **always two-step** (arm-then-confirm), independent of the safety profile; it sends `act_force` (`guard=false`).
- Approve/Deny send `act_if_blocked` (bridge-guarded). Non-idempotent sends are never retried.
- Slots **never reflow**: sticky lease per ordinal, holes backfilled only by newcomers; status drives color/badge, never order.
- All time-based logic (arm timeout) uses an **injectable clock**, defaulting to `time.monotonic`.
- Renders are emitted **only on change** (per-instance), to avoid USB `setImage` spam.

---

### Task 1: IPC Protocol Module

**Files:**
- Create: `src/herdeck/elgato/__init__.py`
- Create: `src/herdeck/elgato/protocol.py`
- Test: `tests/test_elgato_protocol.py`

**Interfaces:**
- Produces: `PROTOCOL_VERSION: int`; `encode(msg: dict) -> bytes` (one JSON line, trailing `\n`); `decode(line: bytes | str) -> dict` (raises `ProtocolError` on bad JSON); `class ProtocolError(Exception)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_elgato_protocol.py`:

```python
import pytest

from herdeck.elgato.protocol import PROTOCOL_VERSION, ProtocolError, decode, encode


def test_protocol_version_is_one():
    assert PROTOCOL_VERSION == 1


def test_encode_is_single_json_line():
    raw = encode({"type": "ready"})
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") == 1
    assert decode(raw) == {"type": "ready"}


def test_decode_accepts_str_and_bytes_without_newline():
    assert decode('{"type":"hello"}') == {"type": "hello"}
    assert decode(b'{"type":"hello"}') == {"type": "hello"}


def test_decode_rejects_garbage():
    with pytest.raises(ProtocolError):
        decode(b"not json")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_elgato_protocol.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'herdeck.elgato'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/herdeck/elgato/__init__.py` (empty file).

Create `src/herdeck/elgato/protocol.py`:

```python
from __future__ import annotations

import json

PROTOCOL_VERSION = 1


class ProtocolError(Exception):
    pass


def encode(msg: dict) -> bytes:
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode()


def decode(line: bytes | str) -> dict:
    if isinstance(line, bytes):
        line = line.decode()
    try:
        value = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError(str(exc)) from exc
    if not isinstance(value, dict):
        raise ProtocolError("message must be a JSON object")
    return value
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_elgato_protocol.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/herdeck/elgato/__init__.py src/herdeck/elgato/protocol.py tests/test_elgato_protocol.py
git commit -m "feat(elgato): add IPC protocol module"
sha=$(git rev-parse HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

If Roborev reports findings, fix them before starting the next task.

---

### Task 2: Slot Leasing

**Files:**
- Create: `src/herdeck/elgato/slots.py`
- Test: `tests/test_elgato_slots.py`

**Interfaces:**
- Consumes: `herdeck.model.AgentKey`.
- Produces: `class SlotLeases` with `update(ordered_present: list[AgentKey]) -> None`, `assignment() -> dict[int, AgentKey]` (sparse ordinal→agent, holes omitted), `ordinal_of(key: AgentKey) -> int | None`. Ordinal space is unbounded (agents at ordinals ≥ slot count are "off-slot"). Existing leases keep their ordinal across updates; vanished agents free their ordinal (hole); newcomers take the lowest free ordinal in the order given.

- [ ] **Step 1: Write the failing test**

Create `tests/test_elgato_slots.py`:

```python
from herdeck.elgato.slots import SlotLeases
from herdeck.model import AgentKey


def k(pane):
    return AgentKey("dev", pane)


def test_initial_assignment_is_reading_order():
    s = SlotLeases()
    s.update([k("p1"), k("p2"), k("p3")])
    assert s.assignment() == {0: k("p1"), 1: k("p2"), 2: k("p3")}


def test_existing_agents_keep_their_ordinal_when_one_vanishes():
    s = SlotLeases()
    s.update([k("p1"), k("p2"), k("p3")])
    s.update([k("p1"), k("p3")])  # p2 gone
    # p1 and p3 must NOT move; ordinal 1 becomes a hole
    assert s.assignment() == {0: k("p1"), 2: k("p3")}


def test_newcomer_fills_lowest_hole_not_the_end():
    s = SlotLeases()
    s.update([k("p1"), k("p2"), k("p3")])
    s.update([k("p1"), k("p3")])  # hole at 1
    s.update([k("p1"), k("p3"), k("p9")])  # p9 is new
    assert s.ordinal_of(k("p9")) == 1
    assert s.ordinal_of(k("p1")) == 0
    assert s.ordinal_of(k("p3")) == 2


def test_overflow_agents_get_offslot_ordinals():
    s = SlotLeases()
    s.update([k(f"p{i}") for i in range(5)])
    assert s.ordinal_of(k("p4")) == 4  # caller decides which ordinals are visible
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_elgato_slots.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

Create `src/herdeck/elgato/slots.py`:

```python
from __future__ import annotations

from ..model import AgentKey


class SlotLeases:
    """Sticky ordinal->agent assignment. Existing agents never move; vanished
    agents leave a hole; newcomers take the lowest free ordinal."""

    def __init__(self) -> None:
        self._lease: dict[int, AgentKey] = {}

    def update(self, ordered_present: list[AgentKey]) -> None:
        present = list(dict.fromkeys(ordered_present))  # de-dup, keep order
        present_set = set(present)
        # 1. release leases whose agent vanished (creates holes, no reflow)
        self._lease = {o: key for o, key in self._lease.items() if key in present_set}
        leased = set(self._lease.values())
        # 2. assign newcomers to the lowest free ordinal, in caller order
        for key in present:
            if key in leased:
                continue
            ordinal = 0
            while ordinal in self._lease:
                ordinal += 1
            self._lease[ordinal] = key
            leased.add(key)

    def assignment(self) -> dict[int, AgentKey]:
        return dict(self._lease)

    def ordinal_of(self, key: AgentKey) -> int | None:
        for ordinal, leased in self._lease.items():
            if leased == key:
                return ordinal
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_elgato_slots.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/herdeck/elgato/slots.py tests/test_elgato_slots.py
git commit -m "feat(elgato): add sticky slot leasing"
sha=$(git rev-parse HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

---

### Task 3: Session Skeleton — Agents, Layout, Slot Render

**Files:**
- Create: `src/herdeck/elgato/session.py`
- Test: `tests/test_elgato_session.py`

**Interfaces:**
- Consumes: `SlotLeases`; `herdeck.config.Config`; `herdeck.model.AgentKey/AgentState/Status`; `herdeck.driver.base.TileView`.
- Produces:
  - `@dataclass KeyRender(image_png: bytes, title: str | None = None)`
  - `class ElgatoSession(config: Config, icons, *, clock=None, arm_timeout: float = 3.0)`
  - inbound: `apply_snapshot(server_id, states)`, `apply_event(server_id, state)`, `set_connection(server_id, up)`
  - layout: `set_slots(instances: list[tuple[str, tuple[int, int]]])` where each is `(instance_id, (col, row))`
  - render: `render_all() -> dict[str, KeyRender]` (instance_id → render). `icons` is anything with `render_tile_bytes(tile: TileView) -> bytes`.
- The stable order fed to `SlotLeases` is `(overview_order index of server, pane_id)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_elgato_session.py`:

```python
from herdeck.config import DEFAULT_PROFILES, Config, ServerConfig
from herdeck.elgato.session import ElgatoSession, KeyRender
from herdeck.model import AgentKey, AgentState, Status


class FakeIcons:
    """Renders a tile to deterministic bytes encoding its visible content."""

    def render_tile_bytes(self, tile) -> bytes:
        return f"{tile.label}|{tile.color}|{tile.status_text}|{tile.repo}".encode()


def make_config():
    return Config(
        servers=[ServerConfig("dev", "ws://dev", "t")],
        profiles=dict(DEFAULT_PROFILES),
        overview_order=["dev"],
        grid=(5, 3),
    )


def state(pane, status, label="api"):
    s = AgentState(AgentKey("dev", pane), "claude", label, status)
    s.repo = label
    return s


def test_slots_render_leased_agents_by_ordinal():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_slots([("s1", (0, 0)), ("s0", (1, 0))])  # s0 is column 1, s1 column 0
    sess.apply_snapshot("dev", [state("p1", Status.WORKING, "alpha"),
                                state("p2", Status.BLOCKED, "beta")])

    rendered = sess.render_all()

    # Reading-order: s1 (col 0) = ordinal 0 = alpha; s0 (col 1) = ordinal 1 = beta
    assert isinstance(rendered["s1"], KeyRender)
    assert b"alpha" in rendered["s1"].image_png
    assert b"green" in rendered["s1"].image_png  # working
    assert b"beta" in rendered["s0"].image_png
    assert b"amber" in rendered["s0"].image_png  # blocked


def test_empty_slot_renders_blank():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_slots([("s0", (0, 0)), ("s1", (1, 0))])
    sess.apply_snapshot("dev", [state("p1", Status.IDLE)])
    rendered = sess.render_all()
    assert b"|dim|" in rendered["s1"].image_png  # ordinal 1 unleased -> blank/dim
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_elgato_session.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

Create `src/herdeck/elgato/session.py`:

```python
from __future__ import annotations

import time
from dataclasses import dataclass

from ..config import Config
from ..driver.base import TileView
from ..model import AgentKey, AgentState, Status
from .slots import SlotLeases

_STATUS_COLOR = {
    Status.WORKING: "green",
    Status.IDLE: "blue",
    Status.BLOCKED: "amber",
    Status.DONE: "dim",
}


@dataclass
class KeyRender:
    image_png: bytes
    title: str | None = None


class ElgatoSession:
    def __init__(self, config: Config, icons, *, clock=None, arm_timeout: float = 3.0) -> None:
        self.config = config
        self._icons = icons
        self._clock = clock or time.monotonic
        self._arm_timeout = arm_timeout
        self._agents: dict[AgentKey, AgentState] = {}
        self._down: set[str] = set()
        self._leases = SlotLeases()
        self._slot_order: list[str] = []  # slot instance_ids in reading order
        self._slot_coords: dict[str, tuple[int, int]] = {}

    # --- inbound agent state ---
    def apply_snapshot(self, server_id: str, states: list[AgentState]) -> None:
        self._agents = {k: v for k, v in self._agents.items() if k.server_id != server_id}
        for s in states:
            self._agents[s.key] = s
        self._release()

    def apply_event(self, server_id: str, state: AgentState) -> None:
        self._agents[state.key] = state
        self._release()

    def set_connection(self, server_id: str, up: bool) -> None:
        self._down.discard(server_id) if up else self._down.add(server_id)

    # --- layout ---
    def set_slots(self, instances: list[tuple[str, tuple[int, int]]]) -> None:
        self._slot_coords = {iid: coord for iid, coord in instances}
        self._slot_order = [iid for iid, _ in sorted(instances, key=lambda t: (t[1][1], t[1][0]))]
        self._release()

    # --- internals ---
    def _server_rank(self, server_id: str) -> int:
        try:
            return self.config.overview_order.index(server_id)
        except ValueError:
            return len(self.config.overview_order)

    def _release(self) -> None:
        ordered = sorted(self._agents.values(), key=lambda s: (self._server_rank(s.key.server_id), s.key.pane_id))
        self._leases.update([s.key for s in ordered])

    def _color(self, s: AgentState) -> str:
        if s.key.server_id in self._down:
            return "red"
        return _STATUS_COLOR.get(s.status, "grey")

    def _slot_tile(self, ordinal: int) -> TileView:
        key = self._leases.assignment().get(ordinal)
        if key is None:
            return TileView(ordinal, "", "dim")
        s = self._agents[key]
        down = s.key.server_id in self._down
        return TileView(
            ordinal,
            s.label,
            self._color(s),
            agent_type=s.agent_type,
            repo=s.repo or s.label,
            branch=s.branch or "",
            status_text="OFFLINE" if down else s.status.value.upper(),
        )

    # --- render ---
    def render_all(self) -> dict[str, KeyRender]:
        out: dict[str, KeyRender] = {}
        for ordinal, iid in enumerate(self._slot_order):
            tile = self._slot_tile(ordinal)
            out[iid] = KeyRender(self._icons.render_tile_bytes(tile))
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_elgato_session.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/herdeck/elgato/session.py tests/test_elgato_session.py
git commit -m "feat(elgato): render leased agent slots"
sha=$(git rev-parse HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

---

### Task 4: Global Selection and Auto-Select

**Files:**
- Modify: `src/herdeck/elgato/session.py`
- Test: `tests/test_elgato_session.py`

**Interfaces:**
- Produces (added to `ElgatoSession`): `selected() -> AgentKey | None`; `select(key: AgentKey | None) -> None`. Auto-select rule: if no manual selection and exactly one agent is blocked, the selected agent is that blocked one; a manual selection takes precedence and is cleared when its agent vanishes; auto-selection re-evaluates on every state change.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_elgato_session.py`:

```python
def test_single_blocked_agent_is_auto_selected():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_slots([("s0", (0, 0)), ("s1", (1, 0))])
    sess.apply_snapshot("dev", [state("p1", Status.WORKING), state("p2", Status.BLOCKED)])
    assert sess.selected() == AgentKey("dev", "p2")


def test_manual_selection_beats_auto_and_clears_when_agent_vanishes():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_slots([("s0", (0, 0))])
    sess.apply_snapshot("dev", [state("p1", Status.WORKING), state("p2", Status.BLOCKED)])
    sess.select(AgentKey("dev", "p1"))
    assert sess.selected() == AgentKey("dev", "p1")  # manual beats auto
    sess.apply_snapshot("dev", [state("p2", Status.BLOCKED)])  # p1 gone
    assert sess.selected() == AgentKey("dev", "p2")  # falls back to auto


def test_two_blocked_agents_do_not_auto_select():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_slots([("s0", (0, 0))])
    sess.apply_snapshot("dev", [state("p1", Status.BLOCKED), state("p2", Status.BLOCKED)])
    assert sess.selected() is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_elgato_session.py -k "auto_select or manual_selection or two_blocked" -v`
Expected: FAIL (`AttributeError: 'ElgatoSession' object has no attribute 'selected'`).

- [ ] **Step 3: Write minimal implementation**

In `src/herdeck/elgato/session.py`, add to `__init__`:

```python
        self._manual: AgentKey | None = None
```

Add methods:

```python
    def select(self, key: AgentKey | None) -> None:
        self._manual = key

    def selected(self) -> AgentKey | None:
        if self._manual is not None and self._manual in self._agents:
            return self._manual
        self._manual = None
        blocked = [k for k, s in self._agents.items() if s.status is Status.BLOCKED]
        return blocked[0] if len(blocked) == 1 else None
```

Mark the selected agent's slot so the deck shows which agent the action keys target. The rich tile renderer draws **`repo`** (not `label`) for agent tiles, so mark `repo`: in `_slot_tile`, compute `repo = s.repo or s.label`, then `if key == self.selected(): repo = f"* {repo}"`, and pass that `repo` to the `TileView`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_elgato_session.py -k "auto_select or manual_selection or two_blocked" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/herdeck/elgato/session.py tests/test_elgato_session.py
git commit -m "feat(elgato): global selection with auto-select"
sha=$(git rev-parse HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

---

### Task 5: Action Keys — Registration, Enablement, Render

**Files:**
- Modify: `src/herdeck/elgato/session.py`
- Test: `tests/test_elgato_session.py`

**Interfaces:**
- Consumes: `herdeck.layout.parse_options`.
- Produces (added to `ElgatoSession`): `set_action_keys(instances: list[tuple[str, str, tuple[int, int]]])` where each is `(instance_id, type, coord)` and `type ∈ {"approve","deny","stop","pager"}`; `set_detection(key: AgentKey, text: str)`; `action_enabled(type: str) -> bool`. `render_all()` now also renders action-key instances showing the selected target's identity and enabled/disabled state. Enablement: `approve`/`deny` iff selected agent is blocked, its server online, prompt read (`set_detection` called for it) and **binary** (`parse_options` empty); `stop` iff a selected agent exists and its server online; `pager` always enabled.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_elgato_session.py`:

```python
from herdeck.layout import parse_options  # noqa: F401  (ensures dependency exists)


def test_approve_disabled_until_prompt_read_then_enabled_for_binary():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_slots([("s0", (0, 0))])
    sess.set_action_keys([("a", "approve", (0, 2)), ("d", "deny", (1, 2)), ("t", "stop", (2, 2))])
    sess.apply_snapshot("dev", [state("p1", Status.BLOCKED)])  # auto-selected, prompt unread

    assert sess.action_enabled("approve") is False  # unread
    sess.set_detection(AgentKey("dev", "p1"), "Proceed? (y/n)")
    assert sess.action_enabled("approve") is True
    assert sess.action_enabled("deny") is True
    assert sess.action_enabled("stop") is True  # selected + online


def test_approve_disabled_for_multi_option_prompt():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_action_keys([("a", "approve", (0, 2))])
    sess.apply_snapshot("dev", [state("p1", Status.BLOCKED)])
    sess.set_detection(AgentKey("dev", "p1"), "Pick:\n1. Yes\n2. No\n3. Maybe")
    assert sess.action_enabled("approve") is False  # multi-option -> deck cannot answer


def test_stop_disabled_when_server_offline():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_action_keys([("t", "stop", (2, 2))])
    sess.apply_snapshot("dev", [state("p1", Status.WORKING)])
    sess.select(AgentKey("dev", "p1"))
    assert sess.action_enabled("stop") is True
    sess.set_connection("dev", False)
    assert sess.action_enabled("stop") is False


def test_action_key_render_shows_target_identity():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_action_keys([("a", "approve", (0, 2))])
    sess.apply_snapshot("dev", [state("p1", Status.BLOCKED, "myrepo")])
    sess.set_detection(AgentKey("dev", "p1"), "y/n")
    assert b"myrepo" in sess.render_all()["a"].image_png


def test_stale_prompt_does_not_re_enable_approve_after_status_change():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_action_keys([("a", "approve", (0, 2))])
    sess.apply_snapshot("dev", [state("p1", Status.BLOCKED)])
    sess.set_detection(AgentKey("dev", "p1"), "y/n")
    assert sess.action_enabled("approve") is True
    sess.apply_event("dev", state("p1", Status.WORKING))  # left blocked -> prune
    sess.apply_event("dev", state("p1", Status.BLOCKED))  # re-blocked, prompt unread
    assert sess.action_enabled("approve") is False  # stale "y/n" must not re-enable it


def test_detection_cleared_on_disconnect_so_reconnect_needs_fresh_read():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_action_keys([("a", "approve", (0, 2))])
    sess.apply_snapshot("dev", [state("p1", Status.BLOCKED)])
    sess.set_detection(AgentKey("dev", "p1"), "y/n")
    assert sess.action_enabled("approve") is True
    sess.set_connection("dev", False)  # server drops
    sess.set_connection("dev", True)   # ...and reconnects
    assert sess.action_enabled("approve") is False  # stale prompt gone; awaits a fresh read


def test_blocked_without_detection_skips_offline_servers():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.apply_snapshot("dev", [state("p1", Status.BLOCKED)])
    assert sess.blocked_without_detection() == [AgentKey("dev", "p1")]
    sess.set_connection("dev", False)
    assert sess.blocked_without_detection() == []  # no proactive read for a dead server
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_elgato_session.py -k "approve or stop_disabled or action_key_render" -v`
Expected: FAIL (`set_action_keys`/`set_detection`/`action_enabled` missing).

- [ ] **Step 3: Write minimal implementation**

In `src/herdeck/elgato/session.py`, add imports and `__init__` fields:

```python
from .. import layout
```

```python
        self._action_keys: list[tuple[str, str]] = []   # (instance_id, type)
        self._detection: dict[AgentKey, str] = {}
        self._block_gen: dict[AgentKey, int] = {}  # +1 each time an agent enters BLOCKED
        self._pending_act: AgentKey | None = None  # an act is in flight for this agent
```

Add to `set_connection`/`apply_snapshot`/`apply_event` nothing special; add layout setter and helpers:

```python
    def set_action_keys(self, instances: list[tuple[str, str, tuple[int, int]]]) -> None:
        self._action_keys = [(iid, kind) for iid, kind, _ in instances]

    def set_detection(self, key: AgentKey, text: str) -> None:
        # Only trust a prompt read for an agent that is present and currently blocked.
        agent = self._agents.get(key)
        if agent is not None and agent.status is Status.BLOCKED:
            self._detection[key] = text

    def _prune_detection(self) -> None:
        # Drop cached prompts whose agent vanished or is no longer blocked, so stale
        # prompt text can never re-enable Approve/Deny for a changed/recreated agent.
        self._detection = {
            k: v
            for k, v in self._detection.items()
            if k in self._agents and self._agents[k].status is Status.BLOCKED
        }

    def block_generation(self, key: AgentKey) -> int:
        return self._block_gen.get(key, 0)

    def blocked_without_detection(self) -> list[AgentKey]:
        # Blocked agents on an ONLINE server whose prompt has not been read yet — the
        # runtime issues a proactive read for each so Approve can enable without a
        # slot press. Offline servers are skipped: reading a dead connector would just
        # leave a pending read that suppresses the real read after reconnect.
        return [
            k for k, s in self._agents.items()
            if s.status is Status.BLOCKED
            and k.server_id not in self._down
            and k not in self._detection
        ]

    def _bump_block_gen(self, incoming: list[AgentState]) -> None:
        # A fresh BLOCKED episode increments the generation so the runtime read
        # correlator can reject a read that was issued for an earlier episode.
        for s in incoming:
            prev = self._agents.get(s.key)
            if s.status is Status.BLOCKED and (prev is None or prev.status is not Status.BLOCKED):
                self._block_gen[s.key] = self._block_gen.get(s.key, 0) + 1

    def _target(self) -> AgentState | None:
        key = self.selected()
        return self._agents.get(key) if key is not None else None

    def action_enabled(self, kind: str) -> bool:
        if kind == "pager":
            return True
        target = self._target()
        if target is None or target.key.server_id in self._down:
            return False
        if kind == "stop":
            return True
        if kind in ("approve", "deny"):
            if target.status is not Status.BLOCKED:
                return False
            text = self._detection.get(target.key)
            if not text:
                return False
            return not layout.parse_options(text)
        return False

    def _action_tile(self, instance_id: str, kind: str) -> TileView:
        enabled = self.action_enabled(kind)
        target = self._target()
        labels = {"approve": "Approve", "deny": "Deny", "stop": "Stop", "pager": "Next"}
        ident = (target.repo or target.label) if (target is not None and kind != "pager") else ""
        color = {"approve": "green", "deny": "amber", "stop": "red", "pager": "blue"}[kind]
        if kind != "pager" and target is not None and target.key == self._pending_act:
            return TileView(0, labels[kind], "dim", repo=ident or None, status_text="PENDING")
        return TileView(
            0,
            labels[kind],
            color if enabled else "dim",
            repo=ident or None,
            status_text=labels[kind].upper(),
        )
```

Call `self._bump_block_gen(states)` / `self._bump_block_gen([state])` at the **start** of `apply_snapshot` / `apply_event` (before mutating `self._agents`, so it can see the previous status), and call `self._prune_detection()` at the **end** (next to the `_reconcile_arm()` call added in Task 6) so a stale prompt is dropped as soon as its agent changes status or vanishes. Also clear the in-flight act marker when its agent gets a new state: in `apply_event`, `if state.key == self._pending_act: self._pending_act = None`; in `apply_snapshot`, `if self._pending_act is not None and self._pending_act.server_id == server_id: self._pending_act = None`. And in `set_connection`, when `up` is False, drop cached detection for that server (`self._detection = {k: v for k, v in self._detection.items() if k.server_id != server_id}`) so a stale prompt cannot re-enable Approve/Deny after the server reconnects — the proactive read re-populates it on the next snapshot.

Extend `render_all` to also render action keys (append before `return out`):

```python
        for iid, kind in self._action_keys:
            out[iid] = KeyRender(self._icons.render_tile_bytes(self._action_tile(iid, kind)))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_elgato_session.py -k "approve or stop_disabled or action_key_render" -v`
Expected: PASS.

- [ ] **Step 5: Run full session suite**

Run: `.venv/bin/python -m pytest tests/test_elgato_session.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/herdeck/elgato/session.py tests/test_elgato_session.py
git commit -m "feat(elgato): action key enablement and identity render"
sha=$(git rev-parse HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

---

### Task 6: Stop Arm-Then-Confirm State Machine

**Files:**
- Modify: `src/herdeck/elgato/session.py`
- Test: `tests/test_elgato_session.py`

**Interfaces:**
- Produces (added to `ElgatoSession`): internal arm state with `tick() -> None` (disarms when `clock() - armed_at > arm_timeout`); `is_armed() -> bool`. The arm is bound to the selected target; selecting a different agent (or the target vanishing) disarms. Firing is handled in Task 7.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_elgato_session.py`:

```python
class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_arm_times_out():
    clk = FakeClock()
    sess = ElgatoSession(make_config(), FakeIcons(), clock=clk, arm_timeout=3.0)
    sess.apply_snapshot("dev", [state("p1", Status.WORKING)])
    sess.select(AgentKey("dev", "p1"))
    sess._arm()  # internal: first Stop press arms (Task 7 wires the press)
    assert sess.is_armed() is True
    clk.now = 3.5
    sess.tick()
    assert sess.is_armed() is False


def test_changing_selection_disarms():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.apply_snapshot("dev", [state("p1", Status.WORKING), state("p2", Status.WORKING)])
    sess.select(AgentKey("dev", "p1"))
    sess._arm()
    assert sess.is_armed() is True
    sess.select(AgentKey("dev", "p2"))
    assert sess.is_armed() is False


def test_is_armed_expires_without_explicit_tick():
    clk = FakeClock()
    sess = ElgatoSession(make_config(), FakeIcons(), clock=clk, arm_timeout=3.0)
    sess.apply_snapshot("dev", [state("p1", Status.WORKING)])
    sess.select(AgentKey("dev", "p1"))
    sess._arm()
    clk.now = 4.0
    assert sess.is_armed() is False  # lazy expiry, no tick() called


def test_arm_cleared_when_target_vanishes_and_does_not_resurrect():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.apply_snapshot("dev", [state("p1", Status.WORKING)])
    sess.select(AgentKey("dev", "p1"))
    sess._arm()
    assert sess.is_armed() is True
    sess.apply_snapshot("dev", [])  # p1 gone -> reconcile clears the arm
    assert sess.is_armed() is False
    sess.apply_snapshot("dev", [state("p1", Status.WORKING)])  # same key reappears
    assert sess.is_armed() is False  # stale arm must not resurrect


def test_armed_stop_key_renders_confirm_state():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_action_keys([("t", "stop", (2, 2))])
    sess.apply_snapshot("dev", [state("p1", Status.WORKING)])
    sess.select(AgentKey("dev", "p1"))
    sess._arm()
    assert b"STOP?" in sess.render_all()["t"].image_png  # armed shows the confirm prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_elgato_session.py -k "arm_times_out or changing_selection" -v`
Expected: FAIL (`_arm`/`is_armed`/`tick` missing).

- [ ] **Step 3: Write minimal implementation**

In `__init__` add:

```python
        self._armed_for: AgentKey | None = None
        self._armed_at: float = 0.0
```

Update `select` to reconcile the arm on change:

```python
    def select(self, key: AgentKey | None) -> None:
        self._manual = key
        self._reconcile_arm()
```

Add the arm methods. `is_armed()` folds in the timeout so a confirm is safe even if no `tick()` ran; `_reconcile_arm()` drops a stale arm when the effective target changes or vanishes:

```python
    def _arm(self) -> None:
        self._armed_for = self.selected()
        self._armed_at = self._clock()

    def is_armed(self) -> bool:
        return (
            self._armed_for is not None
            and self._armed_for == self.selected()
            and (self._clock() - self._armed_at) <= self._arm_timeout
        )

    def _reconcile_arm(self) -> None:
        if self._armed_for is not None and self._armed_for != self.selected():
            self._armed_for = None

    def tick(self) -> None:
        if self._armed_for is not None and self._clock() - self._armed_at > self._arm_timeout:
            self._armed_for = None
```

Call `self._reconcile_arm()` at the end of `apply_snapshot`, `apply_event`, and `set_connection`, so an arm never survives its target changing or vanishing via live state (not only manual `select`).

Now that `is_armed()` exists, render the armed Stop key — add this as the **first** branch of `_action_tile` (before the pending branch from Task 5):

```python
        if kind == "stop" and self.is_armed():
            return TileView(0, "Stop", "red", repo=ident or None, status_text="STOP?")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_elgato_session.py -k "arm_times_out or changing_selection" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/herdeck/elgato/session.py tests/test_elgato_session.py
git commit -m "feat(elgato): stop arm-then-confirm state machine"
sha=$(git rev-parse HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

---

### Task 7: Press Handling → Commands

**Files:**
- Modify: `src/herdeck/elgato/session.py`
- Test: `tests/test_elgato_session.py`

**Interfaces:**
- Consumes: `herdeck.commands.Command`, `herdeck.commands.profile_for`.
- Produces (added to `ElgatoSession`): `key_up(instance_id: str) -> list[Command]`. Behavior: pressing a slot → select+focus that agent and read its prompt (`[focus, read]`); `approve`/`deny` → guarded `act_if_blocked` with the agent's answer-profile keys (empty if not enabled); `stop` → first press arms (returns `[]`), press while armed fires `act_force` (and disarms); `pager` → advance selection to the next blocked agent (returns `[]`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_elgato_session.py`:

```python
from herdeck.commands import Command


def test_pressing_slot_selects_and_reads():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_slots([("s0", (0, 0))])
    sess.apply_snapshot("dev", [state("p1", Status.WORKING)])
    cmds = sess.key_up("s0")
    assert cmds == [
        Command("focus", "dev", "p1"),
        Command("read", "dev", "p1", source="detection"),
    ]
    assert sess.selected() == AgentKey("dev", "p1")


def test_approve_emits_guarded_act_if_blocked():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_action_keys([("a", "approve", (0, 2))])
    sess.apply_snapshot("dev", [state("p1", Status.BLOCKED)])
    sess.set_detection(AgentKey("dev", "p1"), "y/n")
    cmds = sess.key_up("a")
    assert cmds == [Command("act_if_blocked", "dev", "p1", keys=["1", "enter"])]


def test_stop_requires_arm_then_confirm():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_action_keys([("t", "stop", (2, 2))])
    sess.apply_snapshot("dev", [state("p1", Status.WORKING)])
    sess.select(AgentKey("dev", "p1"))
    assert sess.key_up("t") == []          # first press arms
    assert sess.is_armed() is True
    assert sess.key_up("t") == [Command("act_force", "dev", "p1", keys=["ctrl+c"])]
    assert sess.is_armed() is False         # fired -> disarmed


def test_pager_advances_selection_through_blocked():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_action_keys([("p", "pager", (3, 2))])
    sess.apply_snapshot("dev", [state("p1", Status.BLOCKED), state("p2", Status.BLOCKED)])
    assert sess.selected() is None  # two blocked, no auto
    sess.key_up("p")
    first = sess.selected()
    sess.key_up("p")
    assert sess.selected() != first  # cycled to the other blocked agent


def test_stop_confirm_after_timeout_rearms_instead_of_firing():
    clk = FakeClock()
    sess = ElgatoSession(make_config(), FakeIcons(), clock=clk, arm_timeout=3.0)
    sess.set_action_keys([("t", "stop", (2, 2))])
    sess.apply_snapshot("dev", [state("p1", Status.WORKING)])
    sess.select(AgentKey("dev", "p1"))
    assert sess.key_up("t") == []  # first press arms
    clk.now = 4.0                  # confirm window expired, no manual tick()
    assert sess.key_up("t") == []  # must re-arm, NOT fire act_force
    assert sess.is_armed() is True


def test_repeated_approve_while_pending_does_not_double_send():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_action_keys([("a", "approve", (0, 2))])
    sess.apply_snapshot("dev", [state("p1", Status.BLOCKED)])
    sess.set_detection(AgentKey("dev", "p1"), "y/n")
    first = sess.key_up("a")
    second = sess.key_up("a")  # pressed again before any state update
    assert len(first) == 1 and second == []  # no duplicate act_if_blocked


def test_pending_act_clears_on_next_snapshot():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_action_keys([("a", "approve", (0, 2))])
    sess.apply_snapshot("dev", [state("p1", Status.BLOCKED)])
    sess.set_detection(AgentKey("dev", "p1"), "y/n")
    sess.key_up("a")              # emits act, sets pending
    assert sess.key_up("a") == []  # suppressed while pending
    sess.apply_snapshot("dev", [state("p1", Status.IDLE)])  # the act's re-list result
    assert sess._pending_act is None  # pending cleared, key not stuck
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_elgato_session.py -k "pressing_slot or approve_emits or stop_requires or pager_advances" -v`
Expected: FAIL (`key_up` missing).

- [ ] **Step 3: Write minimal implementation**

Add import at top: `from ..commands import Command, build_action_command, profile_for` (`build_action_command` is herdeck's single source of truth for action→`Command` mapping, including `act_if_blocked` vs `act_force`).

Add to `ElgatoSession`:

```python
    def _slot_instance_key(self, instance_id: str) -> AgentKey | None:
        if instance_id not in self._slot_order:
            return None
        ordinal = self._slot_order.index(instance_id)
        return self._leases.assignment().get(ordinal)

    def _action_kind(self, instance_id: str) -> str | None:
        for iid, kind in self._action_keys:
            if iid == instance_id:
                return kind
        return None

    def key_up(self, instance_id: str) -> list[Command]:
        key = self._slot_instance_key(instance_id)
        if key is not None:
            self.select(key)
            return [
                Command("focus", key.server_id, key.pane_id),
                Command("read", key.server_id, key.pane_id, source="detection"),
            ]
        kind = self._action_kind(instance_id)
        if kind == "pager":
            self._page_blocked()
            return []
        if kind is None or not self.action_enabled(kind):
            return []
        target = self._target()
        if self._pending_act == target.key:
            return []  # an act is already in flight for this agent — never double-send
        profile = profile_for(self.config, target.agent_type)
        if kind in ("approve", "deny"):
            self._pending_act = target.key  # show pending until the next state update
            return [build_action_command(kind, target, profile, force=False, always=False)]
        if kind == "stop":
            if not self.is_armed():
                self._arm()
                return []
            self._armed_for = None
            self._pending_act = target.key
            return [build_action_command("stop", target, profile, force=True, always=False)]
        return []

    def _page_blocked(self) -> None:
        blocked = sorted(
            (k for k, s in self._agents.items() if s.status is Status.BLOCKED),
            key=lambda k: (self._server_rank(k.server_id), k.pane_id),
        )
        if not blocked:
            return
        cur = self.selected()
        idx = blocked.index(cur) + 1 if cur in blocked else 0
        self.select(blocked[idx % len(blocked)])
```

Note: `pager` is enabled (Task 5) so the early `not action_enabled` branch never short-circuits it; the explicit `_page_blocked()` calls cover both paths.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_elgato_session.py -k "pressing_slot or approve_emits or stop_requires or pager_advances" -v`
Expected: PASS.

- [ ] **Step 5: Run full session suite**

Run: `.venv/bin/python -m pytest tests/test_elgato_session.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/herdeck/elgato/session.py tests/test_elgato_session.py
git commit -m "feat(elgato): map presses to guarded commands"
sha=$(git rev-parse HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

---

### Task 8: Render Diffing (Only-On-Change)

**Files:**
- Modify: `src/herdeck/elgato/session.py`
- Test: `tests/test_elgato_session.py`

**Interfaces:**
- Produces (added to `ElgatoSession`): `take_render_diff() -> dict[str, KeyRender]` — returns only the instances whose rendered bytes changed since the previous call (first call returns everything currently placed). Reuses the per-instance version idea from the web driver.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_elgato_session.py`:

```python
def test_render_diff_returns_only_changed_instances():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_slots([("s0", (0, 0)), ("s1", (1, 0))])
    sess.apply_snapshot("dev", [state("p1", Status.WORKING, "a"), state("p2", Status.IDLE, "b")])

    first = sess.take_render_diff()
    assert set(first) == {"s0", "s1"}  # first call: everything

    assert sess.take_render_diff() == {}  # nothing changed

    sess.apply_event("dev", state("p1", Status.BLOCKED, "a"))  # p1 changed only
    diff = sess.take_render_diff()
    assert set(diff) == {"s0"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_elgato_session.py::test_render_diff_returns_only_changed_instances -v`
Expected: FAIL (`take_render_diff` missing).

- [ ] **Step 3: Write minimal implementation**

In `__init__` add: `self._last_bytes: dict[str, bytes] = {}`.

Add:

```python
    def take_render_diff(self) -> dict[str, KeyRender]:
        current = self.render_all()
        diff: dict[str, KeyRender] = {}
        for iid, render in current.items():
            if self._last_bytes.get(iid) != render.image_png:
                diff[iid] = render
        self._last_bytes = {iid: r.image_png for iid, r in current.items()}
        return diff
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_elgato_session.py::test_render_diff_returns_only_changed_instances -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/herdeck/elgato/session.py tests/test_elgato_session.py
git commit -m "feat(elgato): emit render diffs only on change"
sha=$(git rev-parse HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

---

### Task 9: IPC Server

**Files:**
- Create: `src/herdeck/elgato/ipc.py`
- Test: `tests/test_elgato_ipc.py`

**Interfaces:**
- Consumes: `protocol.PROTOCOL_VERSION/encode/decode`; an object with the `ElgatoSession` press/layout/render methods; a `token: str`.
- Produces: `class IpcServer(session, token, *, on_commands)` with `async handle(reader, writer) -> None` driving one TS connection. Inbound `hello` (checks `protocol_version` + `token`), `slots`, `action_keys`, `keyDown` (ignored server-side), `keyUp` (→ `on_commands(cmds)` + push diff), `bye`. After `hello` it sends `ready` and a full render. `on_commands(cmds: list[Command]) -> None` hands commands to the runtime. Also exposes `async push_diff() -> None`, which the runtime calls after herdr state changes to push a render diff to the active connection **without** a key press. Renders are base64-encoded in the `render` message: `{"type":"render","keys":{iid:{"image": <b64>, "title": null}}}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_elgato_ipc.py`:

```python
import asyncio
import base64

import pytest

from herdeck.config import DEFAULT_PROFILES, Config, ServerConfig
from herdeck.elgato.ipc import IpcServer
from herdeck.elgato.protocol import PROTOCOL_VERSION, decode, encode
from herdeck.elgato.session import ElgatoSession
from herdeck.model import AgentKey, AgentState, Status


class FakeIcons:
    def render_tile_bytes(self, tile) -> bytes:
        return f"{tile.label}|{tile.color}|{tile.repo}".encode()


def make_session():
    cfg = Config(servers=[ServerConfig("dev", "ws://dev", "t")], profiles=dict(DEFAULT_PROFILES),
                 overview_order=["dev"], grid=(5, 3))
    return ElgatoSession(cfg, FakeIcons())


class Pipe:
    """In-memory reader/writer pair good enough for the line protocol."""

    def __init__(self):
        self._buf = asyncio.Queue()
        self.sent = []

    async def readline(self):
        return await self._buf.get()

    def feed(self, line: bytes):
        self._buf.put_nowait(line)

    def write(self, data: bytes):
        self.sent.append(data)

    async def drain(self):
        pass

    def close(self):
        pass


@pytest.mark.asyncio
async def test_hello_with_wrong_token_is_rejected():
    sess = make_session()
    server = IpcServer(sess, token="secret", on_commands=lambda c: None)
    pipe = Pipe()
    pipe.feed(encode({"type": "hello", "protocol_version": PROTOCOL_VERSION, "token": "wrong"}))
    await asyncio.wait_for(server.handle(pipe, pipe), timeout=1)
    types = [decode(b)["type"] for b in pipe.sent]
    assert "ready" not in types
    assert "error" in types


@pytest.mark.asyncio
async def test_hello_with_wrong_protocol_version_is_rejected():
    sess = make_session()
    server = IpcServer(sess, token="secret", on_commands=lambda c: None)
    pipe = Pipe()
    pipe.feed(encode({"type": "hello", "protocol_version": PROTOCOL_VERSION + 1, "token": "secret"}))
    await asyncio.wait_for(server.handle(pipe, pipe), timeout=1)
    types = [decode(b)["type"] for b in pipe.sent]
    assert "ready" not in types and "error" in types


@pytest.mark.asyncio
async def test_hello_with_non_string_token_is_rejected_not_crashed():
    sess = make_session()
    server = IpcServer(sess, token="secret", on_commands=lambda c: None)
    pipe = Pipe()
    pipe.feed(encode({"type": "hello", "protocol_version": PROTOCOL_VERSION, "token": None}))
    await asyncio.wait_for(server.handle(pipe, pipe), timeout=1)
    types = [decode(b)["type"] for b in pipe.sent]
    assert "ready" not in types and "error" in types  # malformed token rejected, not crashed


@pytest.mark.asyncio
async def test_keyup_runs_command_and_pushes_render():
    sess = make_session()
    sess.set_slots([("s0", (0, 0))])
    sess.apply_snapshot("dev", [AgentState(AgentKey("dev", "p1"), "claude", "api", Status.WORKING)])
    got = []
    server = IpcServer(sess, token="secret", on_commands=got.append)
    pipe = Pipe()
    pipe.feed(encode({"type": "hello", "protocol_version": PROTOCOL_VERSION, "token": "secret"}))
    pipe.feed(encode({"type": "slots", "slots": [{"instanceId": "s0", "coord": {"col": 0, "row": 0}}]}))
    pipe.feed(encode({"type": "keyUp", "instanceId": "s0"}))
    pipe.feed(b"")  # EOF
    await asyncio.wait_for(server.handle(pipe, pipe), timeout=1)

    assert got and got[0][0].kind == "focus"
    renders = [decode(b) for b in pipe.sent if decode(b)["type"] == "render"]
    assert renders, "expected a render push after keyUp"
    payload = renders[-1]["keys"]["s0"]
    # keyUp selected p1, so its slot's repo now carries the "* " marker (the field the
    # real renderer draws) — proving the press changed the render, not just the slots msg.
    assert base64.b64decode(payload["image"]) == b"api|green|* api"


@pytest.mark.asyncio
async def test_push_diff_sends_render_on_state_change_without_keypress():
    sess = make_session()
    sess.set_slots([("s0", (0, 0))])
    sess.apply_snapshot("dev", [AgentState(AgentKey("dev", "p1"), "claude", "api", Status.WORKING)])
    server = IpcServer(sess, token="secret", on_commands=lambda c: None)
    pipe = Pipe()
    server._writer = pipe          # simulate an authed connection
    sess.take_render_diff()        # prime the diff baseline
    sess.apply_event("dev", AgentState(AgentKey("dev", "p1"), "claude", "api", Status.BLOCKED))
    await server.push_diff()       # runtime calls this after a live state change
    renders = [decode(b) for b in pipe.sent if decode(b)["type"] == "render"]
    assert renders and "s0" in renders[-1]["keys"]  # pushed with no keyUp


@pytest.mark.asyncio
async def test_push_diff_does_nothing_before_authenticated_hello():
    sess = make_session()
    server = IpcServer(sess, token="secret", on_commands=lambda c: None)
    await server.push_diff()       # nobody has sent a valid hello yet
    assert server._writer is None  # writer is registered only after auth


@pytest.mark.asyncio
async def test_bye_closes_connection_and_clears_writer():
    sess = make_session()
    server = IpcServer(sess, token="secret", on_commands=lambda c: None)
    pipe = Pipe()
    pipe.feed(encode({"type": "hello", "protocol_version": PROTOCOL_VERSION, "token": "secret"}))
    pipe.feed(encode({"type": "bye"}))  # graceful close without an EOF
    await asyncio.wait_for(server.handle(pipe, pipe), timeout=1)
    assert server._writer is None  # bye returned from handle() and cleared the writer
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_elgato_ipc.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'herdeck.elgato.ipc'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/herdeck/elgato/ipc.py`:

```python
from __future__ import annotations

import base64
import hmac
from collections.abc import Callable

from ..commands import Command
from .protocol import PROTOCOL_VERSION, ProtocolError, decode, encode
from .session import ElgatoSession, KeyRender


class IpcServer:
    def __init__(self, session: ElgatoSession, token: str, *, on_commands: Callable[[list[Command]], None]) -> None:
        self._session = session
        self._token = token
        self._on_commands = on_commands
        self._writer = None  # active TS connection (single client in v1)

    async def handle(self, reader, writer) -> None:
        authed = False
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                try:
                    msg = decode(line)
                except ProtocolError:
                    continue
                authed = await self._dispatch(msg, writer, authed)
                if authed is None:  # fatal (bad hello)
                    return
        finally:
            if self._writer is writer:
                self._writer = None
            writer.close()

    async def _dispatch(self, msg: dict, writer, authed: bool):
        kind = msg.get("type")
        if not authed:
            if kind != "hello":
                return authed
            if msg.get("protocol_version") != PROTOCOL_VERSION or not self._valid(msg.get("token", "")):
                await self._send(writer, {"type": "error", "reason": "auth or version mismatch"})
                return None
            self._writer = writer  # register the active connection ONLY after auth
            await self._send(writer, {"type": "ready"})
            await self._push(writer, self._session.render_all())
            return True

        if kind == "slots":
            self._session.set_slots([(s["instanceId"], (s["coord"]["col"], s["coord"]["row"]))
                                     for s in msg.get("slots", [])])
            await self._push(writer, self._session.take_render_diff())
        elif kind == "action_keys":
            self._session.set_action_keys([(s["instanceId"], s["type"], (s["coord"]["col"], s["coord"]["row"]))
                                           for s in msg.get("action_keys", [])])
            await self._push(writer, self._session.take_render_diff())
        elif kind == "keyUp":
            cmds = self._session.key_up(msg.get("instanceId", ""))
            if cmds:
                self._on_commands(cmds)
            await self._push(writer, self._session.take_render_diff())
        elif kind == "keyDown":
            pass
        elif kind == "bye":
            return None  # graceful close: handle() returns and clears the active writer
        return True

    def _valid(self, token) -> bool:
        return isinstance(token, str) and hmac.compare_digest(token.encode(), self._token.encode())

    async def push_diff(self) -> None:
        """Server-initiated render push, called by the runtime after herdr state
        changes so the deck updates without a key press."""
        if self._writer is not None:
            await self._push(self._writer, self._session.take_render_diff())

    async def _push(self, writer, renders: dict[str, KeyRender]) -> None:
        if not renders:
            return
        keys = {iid: {"image": base64.b64encode(r.image_png).decode(), "title": r.title}
                for iid, r in renders.items()}
        await self._send(writer, {"type": "render", "keys": keys})

    async def _send(self, writer, msg: dict) -> None:
        writer.write(encode(msg))
        await writer.drain()
```

Note: the first full render after `hello` uses `render_all()`; subsequent pushes use `take_render_diff()`. To keep the diff baseline consistent, prime it once after the initial `render_all` by discarding the first diff:

```python
            await self._push(writer, self._session.render_all())
            self._session.take_render_diff()  # prime diff baseline
            return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_elgato_ipc.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/herdeck/elgato/ipc.py tests/test_elgato_ipc.py
git commit -m "feat(elgato): IPC server over local socket protocol"
sha=$(git rev-parse HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

---

### Task 10: Runtime Wiring and `elgato-plugin` Deck Kind

**Files:**
- Create: `src/herdeck/elgato/runtime.py`
- Modify: `src/herdeck/app.py`
- Test: `tests/test_elgato_runtime.py`

**Interfaces:**
- Consumes: `ElgatoSession`, `IpcServer`, `herdeck.app.ConnectorManager`, `herdeck.bootstrap.resolve_runtime_config`, `herdeck.commands.command_to_msg`.
- Produces: `discover_ipc(getenv=os.environ.get) -> tuple[str, str]` returning `(socket_path, token)` from `HERDECK_ELGATO_SOCK` / `HERDECK_ELGATO_TOKEN` (raising `ConfigError` if unset); `async serve_elgato(config, *, socket_path, token, make_session=...) -> None` wiring connector callbacks → session and an `asyncio` Unix socket server running `IpcServer.handle`, sending session commands through the connectors. `app.main()` routes deck kind `elgato-plugin` to this path instead of building a `DeckDriver`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_elgato_runtime.py`:

```python
import pytest

from herdeck.config import ConfigError
from herdeck.elgato.runtime import discover_ipc


def test_discover_ipc_reads_env(monkeypatch):
    monkeypatch.setenv("HERDECK_ELGATO_SOCK", "/tmp/h.sock")
    monkeypatch.setenv("HERDECK_ELGATO_TOKEN", "abc")
    assert discover_ipc() == ("/tmp/h.sock", "abc")


def test_discover_ipc_requires_both(monkeypatch):
    monkeypatch.delenv("HERDECK_ELGATO_SOCK", raising=False)
    monkeypatch.setenv("HERDECK_ELGATO_TOKEN", "abc")
    with pytest.raises(ConfigError):
        discover_ipc()
```

Append a wiring test to the same file:

```python
def test_session_commands_route_to_connectors():
    # The runtime hands session commands to a sender keyed by server_id.
    from herdeck.commands import Command
    from herdeck.elgato.runtime import build_command_sender

    sent = []
    sender = build_command_sender(send=lambda cmd: sent.append((cmd.kind, cmd.server_id)))
    sender([Command("focus", "dev", "p1"), Command("act_force", "dev", "p1", keys=["ctrl+c"])])
    assert sent == [("focus", "dev"), ("act_force", "dev")]
```

Append read-correlation tests to the same file:

```python
def _icons():
    class I:
        def render_tile_bytes(self, tile):
            return b""

    return I()


def _cfg(servers=("dev",)):
    from herdeck.config import DEFAULT_PROFILES, Config, ServerConfig

    return Config(
        servers=[ServerConfig(s, f"ws://{s}", "t") for s in servers],
        profiles=dict(DEFAULT_PROFILES),
        overview_order=list(servers),
        grid=(5, 3),
    )


def test_read_correlator_rejects_stale_read_after_reblock():
    from herdeck.elgato.runtime import ReadCorrelator
    from herdeck.elgato.session import ElgatoSession
    from herdeck.model import AgentKey, AgentState, Status

    sess = ElgatoSession(_cfg(), _icons())
    k = AgentKey("dev", "p1")
    sess.apply_snapshot("dev", [AgentState(k, "claude", "api", Status.BLOCKED)])  # gen 1
    corr = ReadCorrelator(sess)
    corr.issued(k, "r1")
    sess.apply_event("dev", AgentState(k, "claude", "api", Status.WORKING))
    sess.apply_event("dev", AgentState(k, "claude", "api", Status.BLOCKED))  # gen 2
    assert corr.result(k, "r1", "old prompt") is False  # stale read rejected


def test_read_correlator_is_keyed_per_server_not_pane():
    from herdeck.elgato.runtime import ReadCorrelator
    from herdeck.elgato.session import ElgatoSession
    from herdeck.model import AgentKey, AgentState, Status

    sess = ElgatoSession(_cfg(servers=("a", "b")), _icons())
    ka, kb = AgentKey("a", "p1"), AgentKey("b", "p1")  # identical pane id, two servers
    sess.apply_snapshot("a", [AgentState(ka, "claude", "api", Status.BLOCKED)])
    sess.apply_snapshot("b", [AgentState(kb, "claude", "api", Status.BLOCKED)])
    corr = ReadCorrelator(sess)
    corr.issued(ka, "r1")
    corr.issued(kb, "r2")
    assert corr.result(kb, "r2", "B prompt") is True  # server a's read never clobbers b


def test_read_correlator_clear_server_drops_pending():
    from herdeck.elgato.runtime import ReadCorrelator
    from herdeck.elgato.session import ElgatoSession
    from herdeck.model import AgentKey, AgentState, Status

    sess = ElgatoSession(_cfg(), _icons())
    k = AgentKey("dev", "p1")
    sess.apply_snapshot("dev", [AgentState(k, "claude", "api", Status.BLOCKED)])
    corr = ReadCorrelator(sess)
    corr.issued(k, "r1")
    assert corr.has_pending(k) is True
    corr.clear_server("dev")
    assert corr.has_pending(k) is False  # so reconnect can issue a fresh read


def test_default_session_passes_icons_dir_override(tmp_path):
    from herdeck.config import HardwareConfig
    from herdeck.elgato.runtime import _default_session

    cfg = _cfg()
    cfg.hardware = HardwareConfig(icons_dir=str(tmp_path / "icons"))
    sess = _default_session(cfg)
    assert sess._icons._overrides_dir == str(tmp_path / "icons")  # honors custom icon dir
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_elgato_runtime.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

Create `src/herdeck/elgato/runtime.py`:

```python
from __future__ import annotations

import asyncio
import os
import stat
from collections.abc import Callable

from ..commands import Command, command_to_msg
from ..config import Config, ConfigError
from ..connector import Connector
from ..icons import DEFAULT_AGENT_SLUGS, IconProvider
from ..model import AgentKey
from .ipc import IpcServer
from .session import ElgatoSession


def discover_ipc(getenv=os.environ.get) -> tuple[str, str]:
    sock = getenv("HERDECK_ELGATO_SOCK")
    token = getenv("HERDECK_ELGATO_TOKEN")
    if not sock or not token:
        raise ConfigError("HERDECK_ELGATO_SOCK and HERDECK_ELGATO_TOKEN must both be set")
    return sock, token


class ReadCorrelator:
    """Accepts a prompt read only for the request that issued it and the agent's
    current block generation — rejects a stale read that lands after the agent left
    and re-entered BLOCKED, and keys by AgentKey so two servers' identical pane ids
    never cross-wire."""

    def __init__(self, session: ElgatoSession) -> None:
        self._session = session
        self._pending: dict[AgentKey, tuple[str, int]] = {}

    def issued(self, key: AgentKey, req_id: str) -> None:
        self._pending[key] = (req_id, self._session.block_generation(key))

    def has_pending(self, key: AgentKey) -> bool:
        p = self._pending.get(key)
        return p is not None and p[1] == self._session.block_generation(key)

    def result(self, key: AgentKey, req_id: str, text: str) -> bool:
        if self._pending.get(key) == (req_id, self._session.block_generation(key)):
            self._session.set_detection(key, text)
            del self._pending[key]
            return True
        return False

    def clear_server(self, server_id: str) -> None:
        # Drop pending reads for a server on disconnect so reconnect re-reads.
        self._pending = {k: v for k, v in self._pending.items() if k.server_id != server_id}


def build_command_sender(send: Callable[[Command], None]) -> Callable[[list[Command]], None]:
    def run(cmds: list[Command]) -> None:
        for cmd in cmds:
            send(cmd)
    return run


def _default_session(config: Config) -> ElgatoSession:
    import tempfile

    cache = os.path.join(tempfile.gettempdir(), "herdeck-elgato-icons")
    overrides = (
        os.path.abspath(os.path.expanduser(config.hardware.icons_dir))
        if config.hardware.icons_dir
        else None
    )
    icons = IconProvider(cache_dir=cache, slug_map=DEFAULT_AGENT_SLUGS, overrides_dir=overrides)
    return ElgatoSession(config, icons)


async def serve_elgato(config: Config, *, socket_path: str, token: str, make_session=_default_session) -> None:
    if not config.servers:
        raise ConfigError("no servers configured for elgato-plugin run")
    loop = asyncio.get_running_loop()
    session = make_session(config)

    req = {"n": 0}
    correlator = ReadCorrelator(session)

    def _next_req() -> str:
        req["n"] += 1
        return f"r{req['n']}"

    def send(cmd: Command) -> None:
        conn = connectors.get(cmd.server_id)
        if conn is None:
            return
        req_id = _next_req()
        if cmd.kind == "read" and cmd.pane_id is not None:
            correlator.issued(AgentKey(cmd.server_id, cmd.pane_id), req_id)
        loop.create_task(conn.send(command_to_msg(cmd, req_id)))

    def on_result(server_id: str, req_id: str, data: dict) -> None:
        pane, text = data.get("pane_id"), data.get("text")
        if text is not None and pane is not None:
            correlator.result(AgentKey(server_id, pane), req_id, text)  # read result -> detection
        elif text is None:
            send(Command("list", server_id))  # act/focus result -> re-list (clears pending state)

    sender = build_command_sender(send)
    server = IpcServer(session, token, on_commands=sender)

    def _proactive_reads() -> None:
        for key in session.blocked_without_detection():
            if not correlator.has_pending(key):
                send(Command("read", key.server_id, key.pane_id, source="detection"))

    def _apply(fn, *args) -> None:
        # snapshot / event / read-result: mutate the session, proactively read any
        # blocked agent without a fresh prompt (so an auto-selected agent enables
        # Approve without a slot press), then push a render diff.
        fn(*args)
        _proactive_reads()
        asyncio.create_task(server.push_diff())

    def _on_connection(server_id: str, up: bool) -> None:
        # Connection-up arrives BEFORE the resync snapshot, while self._agents still
        # holds pre-disconnect panes — so do NOT proactive-read here; the snapshot
        # that immediately follows runs _apply against fresh state. Just update+push.
        session.set_connection(server_id, up)
        if not up:
            correlator.clear_server(server_id)  # drop pending so reconnect re-reads
        asyncio.create_task(server.push_diff())

    connectors: dict[str, Connector] = {}
    for sc in config.servers:
        conn = Connector(
            sc,
            on_snapshot=lambda sid, st: loop.call_soon_threadsafe(_apply, session.apply_snapshot, sid, st),
            on_event=lambda sid, s: loop.call_soon_threadsafe(_apply, session.apply_event, sid, s),
            on_connection=lambda sid, up: loop.call_soon_threadsafe(_on_connection, sid, up),
            on_result=lambda req_id, data, sid=sc.id: loop.call_soon_threadsafe(
                _apply, on_result, sid, req_id, data
            ),
        )
        connectors[sc.id] = conn

    async def _guarded(c):
        try:
            await c.run()
        except Exception:
            pass

    async def _ticker() -> None:
        # Enforces the Stop arm timeout and reverts the armed key visual even when
        # no other event arrives.
        while True:
            await asyncio.sleep(0.5)
            session.tick()
            await server.push_diff()

    if os.path.lexists(socket_path):
        if not stat.S_ISSOCK(os.lstat(socket_path).st_mode):
            raise ConfigError(f"HERDECK_ELGATO_SOCK {socket_path!r} exists and is not a socket")
        os.unlink(socket_path)  # only ever remove a stale socket, never a real file
    ipc = await asyncio.start_unix_server(server.handle, path=socket_path)
    tasks = [asyncio.create_task(_guarded(c)) for c in connectors.values()]
    tasks.append(asyncio.create_task(_ticker()))
    async with ipc:
        await asyncio.gather(ipc.serve_forever(), *tasks)
```

In `src/herdeck/app.py`, add an `_amain_elgato` wrapper next to `_amain` (it reuses `resolve_runtime_config`, which already synthesizes remote vs local-bridge config):

```python
async def _amain_elgato(mode, file_config, socket_path, token) -> None:
    from .elgato.runtime import serve_elgato

    config, aclose = await resolve_runtime_config(mode, file_config)
    try:
        await serve_elgato(config, socket_path=socket_path, token=token)
    finally:
        await aclose()
```

Then in `main()`, after `kind` is resolved and before building the deck, route the new kind early:

```python
    if kind == "elgato-plugin":
        from .elgato.runtime import discover_ipc

        sock, token = discover_ipc()
        asyncio.run(_amain_elgato(mode, file_config, sock, token))
        return
```

Other deck kinds keep the existing `_amain` / `make_deck` path unchanged.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_elgato_runtime.py -v`
Expected: PASS.

- [ ] **Step 5: Run elgato + app suites**

Run: `.venv/bin/python -m pytest tests/test_elgato_runtime.py tests/test_app.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/herdeck/elgato/runtime.py src/herdeck/app.py tests/test_elgato_runtime.py
git commit -m "feat(elgato): wire runtime and elgato-plugin deck kind"
sha=$(git rev-parse HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

---

### Task 11: Docs and Full Verification

**Files:**
- Modify: `README.md`
- Modify: any files needing verification fixes.

- [ ] **Step 1: Document the backend**

Add a `Stream Deck (Elgato) plugin backend` section to `README.md` covering: the `elgato-plugin` deck kind, the `HERDECK_ELGATO_SOCK` / `HERDECK_ELGATO_TOKEN` env contract (the TS shell spawns the backend and passes these), binary approve/deny scope (multi-option → focus into the TUI), and that the TS shell + packaging live in a separate follow-up plan.

- [ ] **Step 2: Run full tests**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass (347 prior + new elgato tests).

- [ ] **Step 3: Run Ruff**

Run: `.venv/bin/ruff check src tests`
Expected: PASS.

- [ ] **Step 4: Import smoke**

Run:

```bash
.venv/bin/python - <<'PY'
from herdeck.elgato.session import ElgatoSession, KeyRender
from herdeck.elgato.ipc import IpcServer
from herdeck.elgato.runtime import discover_ipc, serve_elgato
from herdeck.elgato.protocol import PROTOCOL_VERSION
print("ok", PROTOCOL_VERSION)
PY
```

Expected: prints `ok 1`.

- [ ] **Step 5: Commit any verification fixes**

```bash
git add -A
git commit -m "docs: document elgato-plugin backend"
sha=$(git rev-parse HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

---

## Self-Review Checklist

- Spec coverage:
  - Architecture (brain = new front-end over core, not Orchestrator): Tasks 3, 10.
  - IPC contract (protocol_version, keyDown/keyUp, render coalesce/diff): Tasks 1, 8, 9.
  - Live render push on herdr state change (server-initiated, no key press): Tasks 9 (`push_diff`), 10 (`_apply`).
  - Slot leasing / no reflow / reachability: Tasks 2, 3, 7 (Pager).
  - Selection + auto-select: Task 4.
  - Action enablement + binary-only Approve/Deny + identity render: Task 5.
  - Stop arm-then-confirm (always, injectable clock): Tasks 6, 7.
  - Guarded acts + no-retry + action mapping reuse: Task 7 (`build_action_command`, `act_if_blocked` bridge guard).
  - Explicit pending visual (distinct from disabled): Tasks 5 (`_pending_act` render), 7 (act emit sets it), cleared on next state update.
  - One-press Approve after auto-select (proactive read of blocked agents): Task 10 `_apply` + Task 5 `blocked_without_detection`.
  - Stale-read correlation (per `AgentKey` + block generation): Task 5 `block_generation`, Task 10 `ReadCorrelator`.
  - Selected-agent visibility: Task 4 (slot `* ` marker) + Task 5 (action keys show target identity).
  - Process lifecycle / discovery + auth: Tasks 9 (token), 10 (env contract).
  - Rendering via `IconProvider`: Tasks 3, 5, 10.
  - Testing without hardware: every task is unit-tested; IPC tested against a fake pipe.
- Out of scope (correctly absent): TS shell, packaging, multi-option rendering, send-text/launch/profile-switch, raise-terminal.
- Type consistency:
  - `KeyRender(image_png: bytes, title: str | None)` — Tasks 3, 9.
  - `ElgatoSession` press surface: `key_up(instance_id) -> list[Command]` — Tasks 7, 9.
  - `SlotLeases.assignment() -> dict[int, AgentKey]` — Tasks 2, 3, 7.
  - IPC messages: `hello`/`slots`/`action_keys`/`keyDown`/`keyUp`/`bye` → `ready`/`render`/`error` — Task 9.
