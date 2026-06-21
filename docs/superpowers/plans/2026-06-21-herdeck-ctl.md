# herdeck-ctl Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `herdeck-ctl`, a Python CLI that lets a lead agent (or a human on mobile) observe and control herdr agents — `ls`, `wait`, `approve`, `deny`, `stop`, `send`, `focus` — as a thin client of the existing herdeck bridge.

**Architecture:** Variant A from the spec — the CLI is a thin client of the existing bridge (WebSocket). It reuses `Connector`, the answer profiles, the config loader, and the wire encoder. Two behaviour-preserving refactors extract shared code into `commands.py` (Command + wire encoder + profile lookup) and `bootstrap.py` (mode resolution + local-bridge config) so both the deck app and the CLI share one source of truth. A `CtlSession` wraps the long-lived `Connector` for single-shot request/response and level-triggered status waits.

**Tech Stack:** Python ≥ 3.12, asyncio, `websockets`, `argparse`, `pytest` + `pytest-asyncio`. Setuptools project managed with `uv`.

## Global Constraints

- Python ≥ 3.12; targets 3.12 and 3.13. `requires-python = ">=3.12"`.
- Runtime dep limited to `websockets>=14`. No new runtime dependencies (CLI uses only stdlib + existing deps).
- Ruff: `line-length = 100`, `select = ["E","F","I","UP","B"]`, `ignore = ["E501","UP042"]`. Keep `Status` as `str`+`Enum`.
- Commit messages: Conventional Commits (`feat`/`fix`/`refactor`/`docs`/`test`/`chore`), English, **no `Co-Authored-By`**.
- One source of truth: answer-profile data lives only in config; `ctl` and the deck must resolve profiles via the same `profile_for`. CLI action semantics must match the deck (`act` guard, `act_force`, `send_text`).
- All existing tests must stay green (baseline: **226 passed**). Run `.venv/bin/pytest -q` after every task.
- Statuses are exactly `working|idle|blocked|done` (see `_AGENT_STATUSES`, `bridge.py:14`), consistent across `ls` and `wait`.
- Exit codes (stable contract): `0` ok · `2` usage · `3` skipped by guard · `4` unknown/ambiguous agent · `5` connection/config/error-frame/snapshot-timeout · `124` `wait` timeout. Settle timeout = exit `0` with `settled:false`.

---

## File Structure

**New:**
- `src/herdeck/commands.py` — `Command` dataclass (moved from `orchestrator.py`), `command_to_msg(cmd, req)` (full wire encoder), `profile_for(config, agent_type)`, `build_action_command(action, agent, profile, *, force, always)`.
- `src/herdeck/bootstrap.py` — `resolve_mode` + `local_config` + `_discover_config_path` (moved from `app.py`), and `resolve_runtime_config(mode, file_config)` (new, async).
- `src/herdeck/ctl.py` — `CtlSession`, target resolution, argparse CLI, `main()`.
- `tests/test_commands.py`, `tests/test_bootstrap.py`, `tests/test_ctl_session.py`, `tests/test_ctl_cli.py`, `tests/test_ctl_e2e.py`.

**Modified:**
- `src/herdeck/orchestrator.py` — `from .commands import Command` (re-export); `_profile_for` delegates to `commands.profile_for`.
- `src/herdeck/app.py` — import `Command`/`command_to_msg` from `commands`; re-export `resolve_mode`/`local_config`/`_discover_config_path` from `bootstrap`; `main` uses `resolve_runtime_config` via a new `_amain`.
- `src/herdeck/pyproject.toml` (`pyproject.toml`) — add `herdeck-ctl = "herdeck.ctl:main"`.
- `tests/test_app.py` — update `_command_to_msg` import + 4 call sites to `command_to_msg(cmd, req)`.
- `README.md` — short `herdeck-ctl` section.

---

## Task 1: `commands.py` — Command, wire encoder, profile lookup, action builder

**Files:**
- Create: `src/herdeck/commands.py`
- Create: `tests/test_commands.py`
- Modify: `src/herdeck/orchestrator.py` (remove `Command` def → import; `_profile_for` delegates)
- Modify: `src/herdeck/app.py:20` (imports), delete `_command_to_msg` (`app.py:229-249`), update caller `app.py:367`
- Modify: `tests/test_app.py:5,9,70-167`

**Interfaces:**
- Produces: `Command(kind, server_id, pane_id=None, source=None, keys=[], text=None)`; `command_to_msg(cmd: Command, req: str | None) -> dict`; `profile_for(config: Config, agent_type: str) -> AnswerProfile`; `build_action_command(action: str, agent: AgentState, profile: AnswerProfile, *, force: bool, always: bool) -> Command`.

- [ ] **Step 1: Write `tests/test_commands.py`**

```python
from herdeck.commands import Command, build_action_command, command_to_msg, profile_for
from herdeck.config import DEFAULT_PROFILES, AnswerProfile, Config
from herdeck.model import AgentKey, AgentState, Status


def _agent(server="dev", pane="p1", agent_type="claude"):
    return AgentState(AgentKey(server, pane), agent_type, "lbl", Status.BLOCKED)


def _config(profiles=None):
    return Config(servers=[], profiles=profiles or dict(DEFAULT_PROFILES),
                  overview_order=[], grid=(5, 3))


def test_command_to_msg_list_has_no_req():
    assert command_to_msg(Command("list", "dev"), None) == {"type": "list"}


def test_command_to_msg_read():
    m = command_to_msg(Command("read", "dev", "p1", source="detection"), "r1")
    assert m == {"type": "read", "req": "r1", "pane_id": "p1", "source": "detection"}


def test_command_to_msg_focus():
    assert command_to_msg(Command("focus", "dev", "p1"), "r2") == {
        "type": "focus", "req": "r2", "pane_id": "p1"}


def test_command_to_msg_send_text():
    m = command_to_msg(Command("send_text", "dev", "p1", text="hi"), "r3")
    assert m == {"type": "send_text", "req": "r3", "pane_id": "p1", "text": "hi"}


def test_command_to_msg_start():
    m = command_to_msg(Command("start", "dev", text="claude", keys=["claude"]), "r4")
    assert m == {"type": "start", "req": "r4", "name": "claude", "argv": ["claude"]}


def test_command_to_msg_act_guard_flags():
    assert command_to_msg(Command("act_if_blocked", "dev", "p1", keys=["1"]), "r5")["guard"] is True
    assert command_to_msg(Command("act_force", "dev", "p1", keys=["ctrl+c"]), "r6")["guard"] is False


def test_command_to_msg_unknown_raises():
    import pytest
    with pytest.raises(ValueError):
        command_to_msg(Command("bogus", "dev"), "r7")


def test_profile_for_known_and_fallback():
    cfg = _config()
    assert profile_for(cfg, "codex").approve == ["y", "enter"]
    assert profile_for(cfg, "nonexistent") is cfg.profiles["default"]


def test_build_action_command_approve_guarded_default():
    cmd = build_action_command("approve", _agent(), profile_for(_config(), "claude"),
                               force=False, always=False)
    assert cmd.kind == "act_if_blocked"
    assert cmd.keys == ["1", "enter"]
    assert cmd.pane_id == "p1" and cmd.server_id == "dev"


def test_build_action_command_approve_always_and_force():
    cmd = build_action_command("approve", _agent(), profile_for(_config(), "claude"),
                               force=True, always=True)
    assert cmd.kind == "act_force"
    assert cmd.keys == ["2", "enter"]


def test_build_action_command_deny_and_stop():
    p = profile_for(_config(), "claude")
    assert build_action_command("deny", _agent(), p, force=False, always=False).kind == "act_if_blocked"
    stop = build_action_command("stop", _agent(), p, force=False, always=False)
    assert stop.kind == "act_force" and stop.keys == ["ctrl+c"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_commands.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'herdeck.commands'`.

- [ ] **Step 3: Create `src/herdeck/commands.py`**

```python
from __future__ import annotations

from dataclasses import dataclass, field

from .config import AnswerProfile, Config
from .model import AgentState


@dataclass
class Command:
    kind: str  # list|read|focus|act_if_blocked|act_force|send_text|start
    server_id: str
    pane_id: str | None = None
    source: str | None = None
    keys: list[str] = field(default_factory=list)
    text: str | None = None  # for send_text (macros) / start (agent name)


def command_to_msg(cmd: Command, req: str | None) -> dict:
    """Encode a Command into the bridge wire message. `req` is ignored for `list`."""
    if cmd.kind == "list":
        return {"type": "list"}
    if cmd.kind == "read":
        return {"type": "read", "req": req, "pane_id": cmd.pane_id, "source": cmd.source}
    if cmd.kind == "focus":
        return {"type": "focus", "req": req, "pane_id": cmd.pane_id}
    if cmd.kind == "send_text":
        return {"type": "send_text", "req": req, "pane_id": cmd.pane_id, "text": cmd.text}
    if cmd.kind == "start":
        return {"type": "start", "req": req, "name": cmd.text, "argv": cmd.keys}
    if cmd.kind in ("act_if_blocked", "act_force"):
        return {
            "type": "act",
            "req": req,
            "pane_id": cmd.pane_id,
            "keys": cmd.keys,
            "guard": cmd.kind == "act_if_blocked",
        }
    raise ValueError(f"unknown command kind: {cmd.kind}")


def profile_for(config: Config, agent_type: str) -> AnswerProfile:
    """Pick the answer profile for an agent type, falling back to 'default'."""
    return config.profiles.get(agent_type, config.profiles["default"])


def build_action_command(
    action: str, agent: AgentState, profile: AnswerProfile, *, force: bool, always: bool
) -> Command:
    """Map a high-level action (approve/deny/stop) + agent + profile -> Command."""
    if action == "approve":
        keys = profile.approve_always if always else profile.approve
        kind = "act_force" if force else "act_if_blocked"
    elif action == "deny":
        keys = profile.deny
        kind = "act_force" if force else "act_if_blocked"
    elif action == "stop":
        keys = profile.stop
        kind = "act_force"
    else:
        raise ValueError(f"unknown action: {action}")
    return Command(kind, agent.key.server_id, agent.key.pane_id, keys=list(keys))
```

- [ ] **Step 4: Repoint `orchestrator.py`**

Remove the `@dataclass class Command:` block (`orchestrator.py:20-27`). Add the import near the top (after the existing `from .config import Config`):

```python
from .commands import Command
```

Replace `_profile_for` (`orchestrator.py:289-291`) body:

```python
    def _profile_for(self, key: AgentKey):
        from .commands import profile_for
        return profile_for(self.config, self._agents[key].agent_type)
```

(Keep the existing top-level `from .commands import Command` re-export so `from herdeck.orchestrator import Command` in `tests/test_orchestrator_nav.py:3` and `tests/test_app.py:9` keeps working.)

- [ ] **Step 5: Repoint `app.py`**

Change `app.py:20` from `from .orchestrator import Command, Orchestrator` to:

```python
from .commands import Command, command_to_msg
from .orchestrator import Orchestrator
```

Delete the `_command_to_msg` function (`app.py:229-249`). Update the caller at `app.py:367`:

```python
            asyncio.run_coroutine_threadsafe(conn.send(command_to_msg(cmd, app.next_req_for(cmd))), loop)
```

- [ ] **Step 6: Update `tests/test_app.py`**

Change line 5 from `from herdeck.app import App, _command_to_msg, _guard, _run` to `from herdeck.app import App, _guard, _run`. Add to the `commands` import (reuse line 9 area):

```python
from herdeck.commands import Command, command_to_msg
```

Update the 4 call sites (lines 70-167) to pass an explicit req instead of `app`:

```python
def test_command_to_msg_guard_flags():
    m1 = command_to_msg(Command("act_if_blocked", "dev", "p1", keys=["1"]), "r1")
    assert m1["guard"] is True
    m2 = command_to_msg(Command("act_force", "dev", "p1", keys=["ctrl+c"]), "r2")
    assert m2["guard"] is False


def test_command_to_msg_focus():
    m = command_to_msg(Command("focus", "dev", "p1"), "r1")
    assert m == {"type": "focus", "req": "r1", "pane_id": "p1"}


def test_command_to_msg_start():
    m = command_to_msg(Command("start", "dev", text="claude", keys=["claude"]), "r1")
    assert m == {"type": "start", "req": "r1", "name": "claude", "argv": ["claude"]}
```

(The `app` fixture in these tests may now be unused for those three functions — drop the `app` argument/fixture usage if the linter flags it. The `next_req_for` tests at lines 53-135 are unchanged.)

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS (226 + new `test_commands.py` cases).

- [ ] **Step 8: Lint + commit**

```bash
.venv/bin/ruff check src/herdeck/commands.py src/herdeck/orchestrator.py src/herdeck/app.py tests/test_commands.py tests/test_app.py
git add src/herdeck/commands.py src/herdeck/orchestrator.py src/herdeck/app.py tests/test_commands.py tests/test_app.py
git commit -m "refactor: extract Command, wire encoder and profile lookup into commands.py"
```

---

## Task 2: `bootstrap.py` — shared mode/config resolution

**Files:**
- Create: `src/herdeck/bootstrap.py`
- Create: `tests/test_bootstrap.py`
- Modify: `src/herdeck/app.py` (move `resolve_mode`, `local_config`, `_discover_config_path`; re-export; new `_amain`; `main` tail)

**Interfaces:**
- Produces: `resolve_mode(*, mock, config_path, config_has_servers, socket_path, socket_exists) -> tuple` (unchanged signature, moved); `local_config(port, token, partial=None) -> Config` (moved); `_discover_config_path() -> str | None` (moved); `resolve_runtime_config(mode: tuple, file_config: Config | None) -> tuple[Config, Callable[[], Awaitable[None]]]` (new async — returns `(config, aclose)`).

- [ ] **Step 1: Write `tests/test_bootstrap.py`**

```python
import asyncio

import pytest

from herdeck.bootstrap import resolve_mode, resolve_runtime_config
from herdeck.config import Config, ServerConfig


def _cfg(servers):
    return Config(servers=servers, profiles={}, overview_order=[], grid=(5, 3))


def test_resolve_mode_still_importable_and_remote():
    assert resolve_mode(mock=False, config_path="/c.toml", config_has_servers=True,
                        socket_path="/x.sock", socket_exists=False) == ("remote", "/c.toml")


@pytest.mark.asyncio
async def test_resolve_runtime_config_remote_passthrough():
    cfg = _cfg([ServerConfig("dev", "ws://x", "tok")])
    out, aclose = await resolve_runtime_config(("remote", "/c.toml"), cfg)
    assert out is cfg
    await aclose()  # no-op, must not raise


@pytest.mark.asyncio
async def test_resolve_runtime_config_local_starts_bridge(monkeypatch):
    closed = {"server": False, "task": False}

    class FakeServer:
        def close(self):
            closed["server"] = True

        async def wait_closed(self):
            pass

    class FakeTask:
        def cancel(self):
            closed["task"] = True

    async def fake_start_local_bridge(socket_path):
        return ("127.0.0.1", 5555, "tok", (FakeServer(), FakeTask()))

    monkeypatch.setattr("herdeck.bootstrap.start_local_bridge", fake_start_local_bridge)
    out, aclose = await resolve_runtime_config(("local", "/h.sock"), None)
    assert out.servers[0].url == "ws://127.0.0.1:5555"
    await aclose()
    assert closed == {"server": True, "task": True}
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_bootstrap.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'herdeck.bootstrap'`.

- [ ] **Step 3: Create `src/herdeck/bootstrap.py`**

Move `resolve_mode` (`app.py:272`...), `local_config` (`app.py:451`...), and `_discover_config_path` (`app.py:483`...) verbatim into this file, then add `resolve_runtime_config`:

```python
from __future__ import annotations

import os
from collections.abc import Awaitable, Callable

from .bridge import start_local_bridge
from .config import (
    DEFAULT_MACROS,
    DEFAULT_PROFILES,
    DEFAULT_START_PROFILES,
    Config,
    Notifications,
    ServerConfig,
)


def resolve_mode(*, mock, config_path, config_has_servers, socket_path, socket_exists):
    """Decide how to run from already-gathered facts (pure; no IO)."""
    # ... body moved verbatim from app.py:272-... (mock/remote/local/error tuple) ...


def local_config(port, token, partial=None):
    """Synthesize the config for local mode from the bound bridge port/token."""
    # ... body moved verbatim from app.py:451-473 (no app-local import needed now) ...


def _discover_config_path():
    """HERDECK_CONFIG, then ~/.config/herdeck/config.toml, then ./config.toml."""
    # ... body moved verbatim from app.py:483-495 ...


async def resolve_runtime_config(
    mode: tuple, file_config: Config | None
) -> tuple[Config, Callable[[], Awaitable[None]]]:
    """Produce a connected Config + an async cleanup for the 'remote'/'local' modes.

    'mock' and 'error' are handled by callers, not here.
    """
    async def _noop() -> None:
        return None

    if mode[0] == "remote":
        return file_config, _noop
    if mode[0] == "local":
        _host, port, token, handle = await start_local_bridge(mode[1])
        server, btask = handle

        async def _close() -> None:
            btask.cancel()
            server.close()
            await server.wait_closed()

        return local_config(port, token, file_config), _close
    raise ValueError(f"cannot resolve runtime config for mode {mode!r}")
```

(When moving `local_config`, drop the now-redundant local `from .config import ...` import inside it — the module-level import above covers it.)

- [ ] **Step 4: Update `app.py` to re-export + use bootstrap**

Remove the moved defs (`resolve_mode`, `local_config`, `_discover_config_path`, and the now-unused `_run_local`) from `app.py`. Add near the top imports:

```python
from .bootstrap import (
    _discover_config_path,
    local_config,
    resolve_mode,
    resolve_runtime_config,
)
```

Replace the `main` tail (`app.py:528-534`) and add `_amain`:

```python
    try:
        if mode[0] == "mock":
            asyncio.run(_run_mock(_mock_config(), deck))
        else:
            asyncio.run(_amain(mode, file_config, deck))
    finally:
        # ... existing finally body unchanged ...


async def _amain(mode, file_config, deck) -> None:
    config, aclose = await resolve_runtime_config(mode, file_config)
    try:
        await _run(config, deck)
    finally:
        await aclose()
```

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS. `tests/test_local_mode.py` (imports `local_config`, `resolve_mode`, `_discover_config_path` from `herdeck.app`) passes via the re-exports.

- [ ] **Step 6: Smoke-check the app still boots (mode plumbing)**

Run: `HERDECK_DECK=web HERDECK_WEB_BIND=127.0.0.1 HERDECK_WEB_PORT=8899 HERDR_SOCKET=/nonexistent.sock timeout 3 .venv/bin/herdeck; echo "exit=$?"`
Expected: prints the "no servers / socket" error to stderr and exits `2` (resolve_mode → error path), OR (if a herdr socket exists) boots the web simulator. Either confirms `main`→`resolve_mode` plumbing is intact. (No traceback.)

- [ ] **Step 7: Lint + commit**

```bash
.venv/bin/ruff check src/herdeck/bootstrap.py src/herdeck/app.py tests/test_bootstrap.py
git add src/herdeck/bootstrap.py src/herdeck/app.py tests/test_bootstrap.py
git commit -m "refactor: extract mode/config bootstrap into bootstrap.py"
```

---

## Task 3: `CtlSession` — open / request / close + connection loss

**Files:**
- Create: `src/herdeck/ctl.py`
- Create: `tests/test_ctl_session.py`

**Interfaces:**
- Consumes: `Connector`, `command_to_msg`, `Command`.
- Produces: `class CtlSession(config, *, server_filter=None)` with `async open(*, timeout)`, `async request(cmd, *, timeout) -> dict`, `async close()`; exceptions `ConnectionLost`. A pluggable `connector_factory` kwarg lets tests inject a fake connector.

- [ ] **Step 1: Write `tests/test_ctl_session.py` (request/result + connection loss)**

```python
import asyncio

import pytest

from herdeck.commands import Command
from herdeck.config import Config, ServerConfig
from herdeck.ctl import ConnectionLost, CtlSession
from herdeck.model import AgentKey, AgentState, Status


class FakeConnector:
    """Drop-in for Connector: records sent msgs, exposes its callbacks to the test."""
    def __init__(self, server, on_snapshot, on_event, on_connection, on_result, on_error):
        self.server = server
        self.on_snapshot, self.on_event = on_snapshot, on_event
        self.on_connection, self.on_result, self.on_error = on_connection, on_result, on_error
        self.sent: list[dict] = []
        self._run = asyncio.Event()

    async def run(self):
        self.on_connection(self.server.id, True)
        await self._run.wait()  # block until stop()

    def stop(self):
        self._run.set()

    async def send(self, msg):
        self.sent.append(msg)


def _config():
    return Config(servers=[ServerConfig("dev", "ws://x", "tok")],
                  profiles={}, overview_order=[], grid=(5, 3))


def _agent(status=Status.BLOCKED):
    return AgentState(AgentKey("dev", "p1"), "claude", "lbl", status)


@pytest.mark.asyncio
async def test_open_waits_for_snapshot_then_request_correlates():
    fc = {}
    def factory(**kw):
        fc["c"] = FakeConnector(**kw)
        return fc["c"]
    sess = CtlSession(_config(), connector_factory=factory)
    open_task = asyncio.create_task(sess.open(timeout=1))
    await asyncio.sleep(0)  # let run() fire on_connection + register
    fc["c"].on_snapshot("dev", [_agent()])
    await open_task
    assert AgentKey("dev", "p1") in sess.agents

    req_task = asyncio.create_task(sess.request(Command("focus", "dev", "p1"), timeout=1))
    await asyncio.sleep(0)
    sent = fc["c"].sent[-1]
    fc["c"].on_result(sent["req"], {"focused": True})
    assert await req_task == {"focused": True}
    await sess.close()


@pytest.mark.asyncio
async def test_open_snapshot_timeout_raises():
    sess = CtlSession(_config(), connector_factory=lambda **kw: FakeConnector(**kw))
    with pytest.raises(ConnectionLost):
        await sess.open(timeout=0.05)
    await sess.close()


@pytest.mark.asyncio
async def test_request_fails_on_connection_drop():
    fc = {}
    sess = CtlSession(_config(),
                      connector_factory=lambda **kw: fc.setdefault("c", FakeConnector(**kw)))
    open_task = asyncio.create_task(sess.open(timeout=1))
    await asyncio.sleep(0)
    fc["c"].on_snapshot("dev", [_agent()])
    await open_task
    req_task = asyncio.create_task(sess.request(Command("focus", "dev", "p1"), timeout=5))
    await asyncio.sleep(0)
    fc["c"].on_connection("dev", False)   # drop before result
    with pytest.raises(ConnectionLost):
        await req_task
    await sess.close()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_ctl_session.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'herdeck.ctl'`.

- [ ] **Step 3: Create `src/herdeck/ctl.py` with `CtlSession` (open/request/close)**

```python
from __future__ import annotations

import asyncio
from collections.abc import Callable

from .commands import Command, command_to_msg
from .config import Config
from .connector import Connector
from .model import AgentKey, AgentState


class ConnectionLost(Exception):
    """The bridge connection dropped (or never produced a first snapshot) while waiting."""


class CtlSession:
    """One-shot request/response + status waits over a long-lived Connector.

    Single-loop asyncio: Connector callbacks run synchronously on this loop and
    mutate state directly (no thread bridging like the deck app).
    """

    def __init__(
        self,
        config: Config,
        *,
        server_filter: str | None = None,
        connector_factory: Callable[..., Connector] = Connector,
    ):
        self.config = config
        self.servers = [s for s in config.servers if server_filter in (None, s.id)]
        self._factory = connector_factory
        self.agents: dict[AgentKey, AgentState] = {}
        self._connectors: dict[str, Connector] = {}
        self._tasks: list[asyncio.Task] = []
        self._pending: dict[str, asyncio.Future] = {}
        self._snapshots: dict[str, asyncio.Event] = {}
        self._changed = asyncio.Event()
        self._req = 0

    # --- Connector callbacks (sync, on this loop) ---
    def _on_snapshot(self, sid: str, states: list[AgentState]) -> None:
        self.agents = {k: v for k, v in self.agents.items() if k.server_id != sid}
        for s in states:
            self.agents[s.key] = s
        self._snapshots.setdefault(sid, asyncio.Event()).set()
        self._changed.set()

    def _on_event(self, sid: str, state: AgentState) -> None:
        self.agents[state.key] = state
        self._changed.set()

    def _on_connection(self, sid: str, up: bool) -> None:
        if not up:
            self._fail_pending(ConnectionLost(f"connection to {sid} lost"))
        self._changed.set()

    def _on_result(self, req: str, data: dict) -> None:
        fut = self._pending.pop(req, None)
        if fut is not None and not fut.done():
            fut.set_result(data)

    def _on_error(self, message: str) -> None:
        self._fail_pending(ConnectionLost(message or "bridge error"))
        self._changed.set()

    def _fail_pending(self, exc: Exception) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    # --- lifecycle ---
    async def open(self, *, timeout: float) -> None:
        for server in self.servers:
            self._snapshots[server.id] = asyncio.Event()
            conn = self._factory(
                server=server,
                on_snapshot=self._on_snapshot,
                on_event=self._on_event,
                on_connection=self._on_connection,
                on_result=self._on_result,
                on_error=self._on_error,
            )
            self._connectors[server.id] = conn
            self._tasks.append(asyncio.create_task(conn.run()))
        try:
            await asyncio.wait_for(
                asyncio.gather(*(self._snapshots[s.id].wait() for s in self.servers)),
                timeout=timeout,
            )
        except TimeoutError as exc:
            raise ConnectionLost("timed out waiting for first snapshot") from exc

    async def request(self, cmd: Command, *, timeout: float) -> dict:
        self._req += 1
        req = f"c{self._req}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req] = fut
        await self._connectors[cmd.server_id].send(command_to_msg(cmd, req))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError as exc:
            self._pending.pop(req, None)
            raise ConnectionLost("timed out waiting for result") from exc

    async def close(self) -> None:
        for conn in self._connectors.values():
            conn.stop()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
```

Note: `Connector.__init__` takes `server` as the first positional arg; the factory is called with `server=` keyword so the real `Connector` and the test `FakeConnector` share one call shape.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_ctl_session.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/herdeck/ctl.py tests/test_ctl_session.py
git commit -m "feat: add CtlSession request/response core over the bridge connector"
```

---

## Task 4: `CtlSession.wait` (level-triggered, W1) + `settle` (W2)

**Files:**
- Modify: `src/herdeck/ctl.py` (add `wait`, `settle`)
- Modify: `tests/test_ctl_session.py` (add wait/settle tests)

**Interfaces:**
- Produces: `async wait(predicate: Callable[[], AgentState | None], *, timeout: float | None) -> AgentState | None`; `async settle(agent: AgentState, *, timeout: float) -> bool` (True = left BLOCKED, False = timed out).

- [ ] **Step 1: Add wait/settle tests to `tests/test_ctl_session.py`**

```python
@pytest.mark.asyncio
async def test_wait_returns_immediately_when_already_satisfied():
    sess = CtlSession(_config())
    sess.agents[AgentKey("dev", "p1")] = _agent(Status.BLOCKED)

    def pred():
        a = sess.agents.get(AgentKey("dev", "p1"))
        return a if a and a.status is Status.BLOCKED else None

    assert await sess.wait(pred, timeout=1) is not None


@pytest.mark.asyncio
async def test_wait_wakes_on_event_and_ignores_foreign_changes():
    sess = CtlSession(_config())
    target = AgentKey("dev", "p1")

    def pred():
        a = sess.agents.get(target)
        return a if a and a.status is Status.BLOCKED else None

    wait_task = asyncio.create_task(sess.wait(pred, timeout=1))
    await asyncio.sleep(0)
    # foreign agent change must NOT satisfy the wait
    sess._on_event("dev", AgentState(AgentKey("dev", "p2"), "claude", "x", Status.BLOCKED))
    await asyncio.sleep(0)
    assert not wait_task.done()
    # target blocks -> wait returns it
    sess._on_event("dev", _agent(Status.BLOCKED))
    assert (await wait_task).key == target


@pytest.mark.asyncio
async def test_wait_times_out_cleanly_to_none():
    sess = CtlSession(_config())
    assert await sess.wait(lambda: None, timeout=0.05) is None


@pytest.mark.asyncio
async def test_settle_true_when_agent_leaves_blocked():
    sess = CtlSession(_config())
    sess.agents[AgentKey("dev", "p1")] = _agent(Status.BLOCKED)
    settle_task = asyncio.create_task(sess.settle(_agent(Status.BLOCKED), timeout=1))
    await asyncio.sleep(0)
    sess._on_event("dev", _agent(Status.WORKING))
    assert await settle_task is True


@pytest.mark.asyncio
async def test_settle_false_on_timeout_when_still_blocked():
    sess = CtlSession(_config())
    sess.agents[AgentKey("dev", "p1")] = _agent(Status.BLOCKED)
    assert await sess.settle(_agent(Status.BLOCKED), timeout=0.05) is False
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_ctl_session.py -k "wait or settle" -q`
Expected: FAIL — `AttributeError: 'CtlSession' object has no attribute 'wait'`.

- [ ] **Step 3: Implement `wait` + `settle` in `ctl.py`**

```python
    async def wait(self, predicate, *, timeout):
        """Level-triggered: check current state first, then block on changes.

        `_changed` is shared across all status changes (foreign agents too), so
        re-check after every wake. arm(clear) -> re-check -> await ensures no
        wakeup is lost: on_event updates `agents` before set(), and in a single
        loop the callback only runs while we are parked at the await.
        """
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while True:
            match = predicate()
            if match:
                return match
            self._changed.clear()
            match = predicate()
            if match:
                return match
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                return None
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=remaining)
            except TimeoutError:
                return None
```

```python
    async def settle(self, agent, *, timeout):
        """Wait until `agent` leaves BLOCKED. True if it did, False on timeout."""
        from .model import Status

        key = agent.key

        def left_blocked():
            a = self.agents.get(key)
            return a or _GONE if (a is None or a.status is not Status.BLOCKED) else None

        return await self.wait(left_blocked, timeout=timeout) is not None
```

Add a module-level sentinel so a vanished agent still counts as "left blocked" (a truthy non-`AgentState` marker):

```python
_GONE = object()
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/test_ctl_session.py -q`
Expected: PASS (all session tests).

- [ ] **Step 5: Commit**

```bash
git add src/herdeck/ctl.py tests/test_ctl_session.py
git commit -m "feat: add level-triggered wait and settle to CtlSession"
```

---

## Task 5: Actions (`act`) + target resolution

**Files:**
- Modify: `src/herdeck/ctl.py` (add `act`, `resolve_target`, `TargetError`)
- Modify: `tests/test_ctl_session.py` (add act/target tests)

**Interfaces:**
- Produces: `resolve_target(spec: str) -> AgentState` (raises `TargetError(message, candidates)`); `async act(action: str, agent: AgentState, *, force: bool, always: bool, settle_timeout: float | None, request_timeout: float) -> dict` returning `{"result": "sent"|"skipped", "settled": bool}`.

- [ ] **Step 1: Add tests to `tests/test_ctl_session.py`**

```python
from herdeck.config import DEFAULT_PROFILES
from herdeck.ctl import TargetError


def _config_p():
    return Config(servers=[ServerConfig("dev", "ws://x", "tok")],
                  profiles=dict(DEFAULT_PROFILES), overview_order=[], grid=(5, 3))


def test_resolve_target_exact_and_fuzzy():
    sess = CtlSession(_config_p())
    a = AgentState(AgentKey("dev", "w2:p3"), "claude", "auth", Status.IDLE, repo="herdeck")
    sess.agents[a.key] = a
    assert sess.resolve_target("dev:w2:p3") is a   # exact server:pane_id
    assert sess.resolve_target("herdeck") is a      # fuzzy by repo
    assert sess.resolve_target("w2:p3") is a         # fuzzy by pane_id


def test_resolve_target_unknown_and_ambiguous():
    sess = CtlSession(_config_p())
    a1 = AgentState(AgentKey("dev", "p1"), "claude", "dup", Status.IDLE)
    a2 = AgentState(AgentKey("dev", "p2"), "claude", "dup", Status.IDLE)
    sess.agents[a1.key] = a1
    sess.agents[a2.key] = a2
    with pytest.raises(TargetError):
        sess.resolve_target("nope")
    with pytest.raises(TargetError):
        sess.resolve_target("dup")  # two labels match


@pytest.mark.asyncio
async def test_act_approve_sent_then_settled():
    fc = {}
    sess = CtlSession(_config_p(),
                      connector_factory=lambda **kw: fc.setdefault("c", FakeConnector(**kw)))
    open_task = asyncio.create_task(sess.open(timeout=1))
    await asyncio.sleep(0)
    fc["c"].on_snapshot("dev", [_agent(Status.BLOCKED)])
    await open_task
    agent = sess.agents[AgentKey("dev", "p1")]
    act_task = asyncio.create_task(
        sess.act("approve", agent, force=False, always=False,
                 settle_timeout=1, request_timeout=1))
    await asyncio.sleep(0)
    fc["c"].on_result(fc["c"].sent[-1]["req"], {"sent": True})
    await asyncio.sleep(0)
    fc["c"].on_event("dev", _agent(Status.WORKING))  # leaves blocked -> settled
    assert await act_task == {"result": "sent", "settled": True}
    assert fc["c"].sent[-1]["keys"] == ["1", "enter"]
    await sess.close()


@pytest.mark.asyncio
async def test_act_skipped_by_guard_no_settle():
    fc = {}
    sess = CtlSession(_config_p(),
                      connector_factory=lambda **kw: fc.setdefault("c", FakeConnector(**kw)))
    open_task = asyncio.create_task(sess.open(timeout=1))
    await asyncio.sleep(0)
    fc["c"].on_snapshot("dev", [_agent(Status.IDLE)])
    await open_task
    agent = sess.agents[AgentKey("dev", "p1")]
    act_task = asyncio.create_task(
        sess.act("approve", agent, force=False, always=False,
                 settle_timeout=1, request_timeout=1))
    await asyncio.sleep(0)
    fc["c"].on_result(fc["c"].sent[-1]["req"], {"skipped": True})
    assert await act_task == {"result": "skipped", "settled": True}
    await sess.close()
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_ctl_session.py -k "target or act_" -q`
Expected: FAIL — `ImportError: cannot import name 'TargetError'`.

- [ ] **Step 3: Implement `TargetError`, `resolve_target`, `act` in `ctl.py`**

```python
class TargetError(Exception):
    def __init__(self, message: str, candidates: list[AgentState]):
        super().__init__(message)
        self.candidates = candidates
```

```python
    def resolve_target(self, spec: str) -> AgentState:
        if ":" in spec:
            sid, pid = spec.split(":", 1)
            exact = self.agents.get(AgentKey(sid, pid))
            if exact is not None:
                return exact
        matches = [
            a for a in self.agents.values()
            if spec in (a.label, a.repo, a.branch, a.key.pane_id)
        ]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise TargetError(f"no agent matching {spec!r}", list(self.agents.values()))
        raise TargetError(f"ambiguous agent {spec!r}", matches)
```

```python
    async def act(self, action, agent, *, force, always, settle_timeout, request_timeout):
        from .commands import build_action_command, profile_for

        profile = profile_for(self.config, agent.agent_type)
        cmd = build_action_command(action, agent, profile, force=force, always=always)
        data = await self.request(cmd, timeout=request_timeout)
        if data.get("skipped"):
            return {"result": "skipped", "settled": True}
        settled = True
        if settle_timeout is not None and action in ("approve", "deny", "stop"):
            settled = await self.settle(agent, timeout=settle_timeout)
        return {"result": "sent", "settled": settled}
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/test_ctl_session.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/herdeck/ctl.py tests/test_ctl_session.py
git commit -m "feat: add CtlSession actions and target resolution"
```

---

## Task 6: CLI surface — argparse, dispatch, output, exit codes, `main`

**Files:**
- Modify: `src/herdeck/ctl.py` (add `build_parser`, `dispatch`, output helpers, `main`, `EXIT_*`)
- Create: `tests/test_ctl_cli.py`

**Interfaces:**
- Consumes: `CtlSession`, `resolve_mode`, `resolve_runtime_config`, `_discover_config_path`, `load_config`.
- Produces: `build_parser() -> argparse.ArgumentParser`; `async dispatch(args, session) -> int`; `main(argv=None) -> int`. Exit constants: `EXIT_OK=0, EXIT_USAGE=2, EXIT_SKIPPED=3, EXIT_TARGET=4, EXIT_CONN=5, EXIT_WAIT_TIMEOUT=124`.

- [ ] **Step 1: Write `tests/test_ctl_cli.py`**

```python
import json

import pytest

from herdeck.config import DEFAULT_PROFILES, Config, ServerConfig
from herdeck.ctl import EXIT_OK, EXIT_SKIPPED, EXIT_TARGET, build_parser, dispatch
from herdeck.model import AgentKey, AgentState, Status


def _config():
    return Config(servers=[ServerConfig("dev", "ws://x", "tok")],
                  profiles=dict(DEFAULT_PROFILES), overview_order=[], grid=(5, 3))


class StubSession:
    """Stands in for an opened CtlSession for dispatch-level tests."""
    def __init__(self, agents):
        self.config = _config()
        self.agents = {a.key: a for a in agents}
        self.acted = None

    def resolve_target(self, spec):
        from herdeck.ctl import CtlSession
        return CtlSession.resolve_target(self, spec)

    async def act(self, action, agent, **kw):
        self.acted = (action, agent.key.pane_id, kw)
        return {"result": "sent", "settled": True}

    async def wait(self, predicate, *, timeout):
        return predicate()  # already-satisfied path


def test_parser_rejects_unknown_status():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["wait", "--any", "--until", "bogus"])


@pytest.mark.asyncio
async def test_dispatch_ls_json(capsys):
    a = AgentState(AgentKey("dev", "p1"), "claude", "auth", Status.BLOCKED, repo="herdeck")
    args = build_parser().parse_args(["ls", "--json"])
    rc = await dispatch(args, StubSession([a]))
    assert rc == EXIT_OK
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["pane_id"] == "p1" and rows[0]["status"] == "blocked"


@pytest.mark.asyncio
async def test_dispatch_approve_calls_act(capsys):
    a = AgentState(AgentKey("dev", "p1"), "claude", "auth", Status.BLOCKED)
    args = build_parser().parse_args(["approve", "dev:p1"])
    sess = StubSession([a])
    assert await dispatch(args, sess) == EXIT_OK
    assert sess.acted[0] == "approve"


@pytest.mark.asyncio
async def test_dispatch_unknown_target_exit4(capsys):
    args = build_parser().parse_args(["approve", "ghost"])
    assert await dispatch(args, StubSession([])) == EXIT_TARGET
    assert "no agent" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_dispatch_skipped_exit3(capsys):
    a = AgentState(AgentKey("dev", "p1"), "claude", "auth", Status.IDLE)

    class SkipSession(StubSession):
        async def act(self, action, agent, **kw):
            return {"result": "skipped", "settled": True}

    args = build_parser().parse_args(["approve", "dev:p1"])
    assert await dispatch(args, SkipSession([a])) == EXIT_SKIPPED
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_ctl_cli.py -q`
Expected: FAIL — `ImportError: cannot import name 'build_parser'`.

- [ ] **Step 3: Implement the CLI in `ctl.py`**

```python
import argparse
import json
import os
import sys

from .bootstrap import _discover_config_path, resolve_mode, resolve_runtime_config
from .config import ConfigError, load_config
from .model import Status

EXIT_OK, EXIT_USAGE, EXIT_SKIPPED = 0, 2, 3
EXIT_TARGET, EXIT_CONN, EXIT_WAIT_TIMEOUT = 4, 5, 124

_STATUSES = ["blocked", "working", "idle", "done"]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="herdeck-ctl", description="Control herdr agents.")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--server", help="restrict to one configured server id")
    p.add_argument("--config", help="config path (default: $HERDECK_CONFIG / discovery)")
    p.add_argument("--timeout", type=float, default=10.0, help="connect/request timeout (s)")
    sub = p.add_subparsers(dest="cmd", required=True)

    ls = sub.add_parser("ls", help="list agents")
    ls.add_argument("--status", choices=_STATUSES)

    w = sub.add_parser("wait", help="block until an agent reaches a status")
    w.add_argument("agent", nargs="?")
    w.add_argument("--any", dest="any_agent", action="store_true")
    w.add_argument("--until", choices=_STATUSES, required=True)

    for name in ("approve", "deny"):
        a = sub.add_parser(name, help=f"{name} a blocked agent")
        a.add_argument("agent")
        a.add_argument("--force", action="store_true", help="ignore the blocked guard")
        a.add_argument("--settle", type=float, default=3.0)
        a.add_argument("--no-settle", dest="settle", action="store_const", const=None)
        if name == "approve":
            a.add_argument("--always", action="store_true", help="approve-always profile")

    st = sub.add_parser("stop", help="stop an agent (unconditional)")
    st.add_argument("agent")
    st.add_argument("--settle", type=float, default=3.0)
    st.add_argument("--no-settle", dest="settle", action="store_const", const=None)

    se = sub.add_parser("send", help="send text to an agent (submits immediately)")
    se.add_argument("agent")
    se.add_argument("text")

    fo = sub.add_parser("focus", help="bring an agent's pane to the foreground")
    fo.add_argument("agent")
    return p


def _agent_row(a) -> dict:
    return {"server": a.key.server_id, "pane_id": a.key.pane_id, "label": a.label,
            "status": a.status.value, "agent_type": a.agent_type,
            "repo": a.repo, "branch": a.branch}


def _emit(args, payload: dict | list) -> None:
    if args.json:
        print(json.dumps(payload))
    elif isinstance(payload, list):
        for r in payload:
            print(f"{r['server']}:{r['pane_id']}  {r['status']:<8} {r['repo'] or r['label']} "
                  f"{r['branch']}".rstrip())
    else:
        print(payload.get("result") or payload.get("status") or json.dumps(payload))


async def dispatch(args, session) -> int:
    if args.cmd == "ls":
        rows = [_agent_row(a) for a in session.agents.values()
                if not args.status or a.status.value == args.status]
        _emit(args, rows)
        return EXIT_OK

    if args.cmd == "wait":
        target_status = Status(args.until)
        if args.any_agent:
            def pred():
                return next((a for a in session.agents.values()
                             if a.status is target_status), None)
        else:
            try:
                fixed = session.resolve_target(args.agent)
            except TargetError as e:
                return _target_error(args, e)

            def pred():
                a = session.agents.get(fixed.key)
                return a if a and a.status is target_status else None
        match = await session.wait(pred, timeout=args.timeout)
        if match is None:
            print("wait timed out", file=sys.stderr)
            return EXIT_WAIT_TIMEOUT
        _emit(args, {"agent": f"{match.key.server_id}:{match.key.pane_id}",
                     "status": match.status.value})
        return EXIT_OK

    # actions
    try:
        agent = session.resolve_target(args.agent)
    except TargetError as e:
        return _target_error(args, e)

    if args.cmd == "send":
        await session.request(Command("send_text", agent.key.server_id, agent.key.pane_id,
                                      text=args.text), timeout=args.timeout)
        _emit(args, {"result": "sent"})
        return EXIT_OK
    if args.cmd == "focus":
        await session.request(Command("focus", agent.key.server_id, agent.key.pane_id),
                              timeout=args.timeout)
        _emit(args, {"result": "focused"})
        return EXIT_OK

    result = await session.act(
        args.cmd, agent,
        force=getattr(args, "force", False),
        always=getattr(args, "always", False),
        settle_timeout=args.settle,
        request_timeout=args.timeout,
    )
    if not result.get("settled", True):
        print(f"warning: {agent.key.pane_id} still blocked after settle", file=sys.stderr)
    _emit(args, {"agent": f"{agent.key.server_id}:{agent.key.pane_id}", **result})
    return EXIT_SKIPPED if result["result"] == "skipped" else EXIT_OK


def _target_error(args, exc: TargetError) -> int:
    print(str(exc), file=sys.stderr)
    for a in exc.candidates:
        print(f"  {a.key.server_id}:{a.key.pane_id}  {a.label} {a.repo}".rstrip(), file=sys.stderr)
    return EXIT_TARGET


async def _amain(args) -> int:
    config_path = args.config or _discover_config_path()
    file_config = load_config(config_path) if config_path else None
    socket_path = os.path.expanduser(
        os.environ.get("HERDR_SOCKET", "~/.config/herdr/herdr.sock"))
    mode = resolve_mode(mock=False, config_path=config_path,
                        config_has_servers=bool(file_config and file_config.servers),
                        socket_path=socket_path, socket_exists=os.path.exists(socket_path))
    if mode[0] == "error":
        print(mode[1], file=sys.stderr)
        return EXIT_CONN
    config, aclose = await resolve_runtime_config(mode, file_config)
    session = CtlSession(config, server_filter=args.server)
    try:
        await session.open(timeout=args.timeout)
        return await dispatch(args, session)
    except ConnectionLost as e:
        print(f"connection error: {e}", file=sys.stderr)
        return EXIT_CONN
    finally:
        await session.close()
        await aclose()


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_amain(args))
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        return EXIT_CONN
```

(Place `EXIT_*`, `build_parser`, `_agent_row`, `_emit`, `dispatch`, `_target_error`, `_amain`, `main` after the `CtlSession` class. The `argparse`/`json`/`os`/`sys` imports go at the top of the module alongside the existing imports.)

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_ctl_cli.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/herdeck/ctl.py tests/test_ctl_cli.py
git commit -m "feat: add herdeck-ctl CLI surface, dispatch and exit codes"
```

---

## Task 7: Wire entry point + end-to-end integration

**Files:**
- Modify: `pyproject.toml` (`[project.scripts]`)
- Create: `tests/test_ctl_e2e.py`

**Interfaces:**
- Consumes: `start_local_bridge`, the bridge's `FakeHerdrClient` test double pattern (see `tests/test_local_mode.py` / `bridge.py`), real `Connector`, `CtlSession`.

- [ ] **Step 1: Add the entry point to `pyproject.toml`**

```toml
[project.scripts]
herdeck = "herdeck.app:main"
herdeck-bridge = "herdeck.bridge:main"
herdeck-doctor = "herdeck.doctor:main"
herdeck-ctl = "herdeck.ctl:main"
```

- [ ] **Step 2: Reinstall so the console script appears**

Run: `uv pip install -q -e ".[dev]"`
Then: `.venv/bin/herdeck-ctl --help`
Expected: usage text listing `ls`, `wait`, `approve`, `deny`, `stop`, `send`, `focus`. Exit `0`.

- [ ] **Step 3: Write `tests/test_ctl_e2e.py`**

Mirror the fake-herdr setup used in the existing bridge tests. Start a real loopback bridge with `start_local_bridge(socket_path, herdr=FakeHerdr(...))`, build a real `Connector` through `CtlSession`, and assert the fake herdr received the right `send_keys`.

```python
import asyncio

import pytest

from herdeck.bridge import start_local_bridge
from herdeck.commands import Command
from herdeck.config import DEFAULT_PROFILES, Config, ServerConfig
from herdeck.ctl import CtlSession
from herdeck.model import AgentKey


class FakeHerdr:
    """Minimal herdr double: one blocked claude pane; records send_keys."""
    def __init__(self):
        self.sent: list[tuple[str, list[str]]] = []
        self._status = "blocked"

    async def list_panes(self):
        return [{"pane_id": "w1:p1", "agent": "claude", "label": "auth",
                 "agent_status": self._status, "project": "", "repo": "herdeck", "branch": "x"}]

    async def get_pane(self, pane_id):
        return {"pane_id": pane_id, "agent_status": self._status}

    async def send_keys(self, pane_id, keys):
        self.sent.append((pane_id, keys))
        self._status = "working"  # leaving blocked, so settle resolves

    async def read_pane(self, pane_id, source):
        return ""

    async def focus_agent(self, pane_id):
        pass

    async def send_text(self, pane_id, text):
        self.sent.append((pane_id, [text]))

    async def start_agent(self, name, argv):
        pass


def _config(port, token):
    return Config(servers=[ServerConfig("local", f"ws://127.0.0.1:{port}", token)],
                  profiles=dict(DEFAULT_PROFILES), overview_order=["local"], grid=(5, 3))


@pytest.mark.asyncio
async def test_e2e_ls_and_approve():
    herdr = FakeHerdr()
    host, port, token, (server, btask) = await start_local_bridge("/unused.sock", herdr=herdr)
    try:
        sess = CtlSession(_config(port, token))
        await sess.open(timeout=5)
        assert AgentKey("local", "w1:p1") in sess.agents

        agent = sess.resolve_target("local:w1:p1")
        out = await sess.act("approve", agent, force=False, always=False,
                             settle_timeout=5, request_timeout=5)
        assert out["result"] == "sent"
        # claude approve profile = ["1","enter"]
        assert herdr.sent == [("w1:p1", ["1", "enter"])]
        await sess.close()
    finally:
        btask.cancel()
        server.close()
        await server.wait_closed()
```

(Match `FakeHerdr`'s method names to the `HerdrClient` protocol in `bridge.py:18-25` — adjust if the protocol differs from the names above; the existing `bridge.py` `FakeHerdrClient` in tests is the reference shape.)

- [ ] **Step 4: Run the e2e test**

Run: `.venv/bin/pytest tests/test_ctl_e2e.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml tests/test_ctl_e2e.py
git commit -m "feat: register herdeck-ctl entry point and add e2e coverage"
```

---

## Task 8: Docs + final verification

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add a `herdeck-ctl` section to `README.md`**

Insert after the "Quick start (local)" section:

```markdown
## Controlling agents from the CLI (`herdeck-ctl`)

`herdeck-ctl` drives agents from a terminal (for scripting or orchestration by a
lead agent), using the same bridge and answer profiles as the deck.

```bash
herdeck-ctl ls --json                          # list agents + status
herdeck-ctl wait --any --until blocked --json  # block until one needs input
herdeck-ctl approve local:w1:p1                # approve a blocked agent
herdeck-ctl focus local:w1:p1                  # bring its pane to the foreground
herdeck-ctl send local:w1:p1 "run the tests"   # send text (submits immediately)
```

Exit codes: `0` ok · `2` usage · `3` skipped (agent not blocked) · `4` unknown/
ambiguous agent · `5` connection/config error · `124` `wait` timed out. Actions
that clear a block (`approve`/`deny`/`stop`) wait until the agent leaves
`blocked` before returning (`--settle S`, `--no-settle`).
```

- [ ] **Step 2: Full suite + lint + format**

```bash
.venv/bin/pytest -q
.venv/bin/ruff check src tests
```
Expected: all green, no lint errors. (Baseline 226 + all new tests pass.)

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: document herdeck-ctl usage and exit codes"
```

- [ ] **Step 4: Open the PR (only when the user asks)**

When the user requests it, push `feat/herdeck-ctl` and open a PR referencing the spec. Do not squash-merge; preserve individual commits.

---

## Self-Review

**Spec coverage:**
- Variant A bridge client → Tasks 3–7 (CtlSession over Connector). ✔
- Commands `ls`/`wait`/`approve`/`deny`/`stop`/`send`/`focus` → Task 6 dispatch. ✔
- `wait` level-triggered (W1) + clean timeout (note a) → Task 4. ✔
- Settle anti-double-fire (W2) → Tasks 4–5 (`settle` + `act`). ✔
- Reconnect/connection-loss mid-request (note b) → Task 3 (`_on_connection` fails pending → exit 5). ✔
- Profiles single source of truth → Task 1 `profile_for` (orchestrator delegates). ✔
- Wire encoder full incl. read/start (R1) → Task 1. ✔
- Bootstrap extraction + cycle fix (move `resolve_mode`+`local_config` to bootstrap) → Task 2. ✔
- Target resolution `server:pane_id` + fuzzy → Task 5. ✔
- Exit-code contract → Task 6 (`EXIT_*`). ✔
- `--no-submit` / spawn explicitly out of v1 → not implemented (documented as future in spec). ✔

**Placeholder scan:** No `TBD`/`TODO`; every code step shows complete code. Two "match the reference shape" notes (test_app `app` fixture cleanup; e2e `FakeHerdr` method names vs `HerdrClient` protocol) point at concrete existing references, not deferred work.

**Type consistency:** `Command(kind, server_id, pane_id, source, keys, text)`, `command_to_msg(cmd, req)`, `profile_for(config, agent_type)`, `build_action_command(action, agent, profile, *, force, always)`, `CtlSession.open(timeout=)`, `request(cmd, timeout=)`, `wait(predicate, timeout=)`, `settle(agent, timeout=)`, `act(action, agent, *, force, always, settle_timeout, request_timeout)`, `resolve_target(spec)`, `EXIT_*` — names match across tasks.
