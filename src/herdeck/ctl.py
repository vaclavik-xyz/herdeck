from __future__ import annotations

import asyncio
from collections.abc import Callable

from .commands import Command, build_action_command, command_to_msg, profile_for
from .config import Config
from .connector import Connector
from .model import AgentKey, AgentState, Status


class ConnectionLost(Exception):
    """The bridge connection dropped (or never produced a first snapshot) while waiting."""


class TargetError(Exception):
    """No agent (or more than one) matched a target spec."""

    def __init__(self, message: str, candidates: list[AgentState]):
        super().__init__(message)
        self.candidates = candidates


_GONE = object()  # sentinel: a vanished agent still counts as "left blocked"


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
        self._conn_up: dict[str, bool] = {}
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
        self._conn_up[sid] = up
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
        if cmd.kind == "list":
            raise ValueError("list has no request/response; read agents from open() snapshot")
        if not self._conn_up.get(cmd.server_id, False):
            raise ConnectionLost(f"{cmd.server_id} not connected")
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

    async def wait(self, predicate, *, timeout):
        """Level-triggered: check current state first, then block on changes.

        `_changed` is shared across all status changes (foreign agents too), so
        re-check after every wake. arm(clear) -> re-check -> await ensures no
        wakeup is lost: on_event updates `agents` before set(), and in a single
        loop the callback only runs while we are parked at the await.

        Assumes a single concurrent waiter (the one-shot CLI invariant); not safe
        for parallel waiters due to the shared `_changed.clear()`.
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

    async def settle(self, agent, *, timeout):
        """Wait until `agent` leaves BLOCKED. True if it did, False on timeout."""
        key = agent.key

        def left_blocked():
            a = self.agents.get(key)
            return (a or _GONE) if (a is None or a.status is not Status.BLOCKED) else None

        return await self.wait(left_blocked, timeout=timeout) is not None

    def resolve_target(self, spec: str) -> AgentState:
        """Resolve a spec to one agent: 'server:pane_id' exact, else fuzzy by
        label/repo/branch/pane_id. Raises TargetError on no match or ambiguity."""
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

    async def act(self, action, agent, *, force, always, settle_timeout, request_timeout):
        """Send a profile action; for blocked-clearing actions, settle afterwards.

        Returns {"result": "sent"|"skipped", "settled": bool}.
        """
        profile = profile_for(self.config, agent.agent_type)
        cmd = build_action_command(action, agent, profile, force=force, always=always)
        data = await self.request(cmd, timeout=request_timeout)
        if data.get("skipped"):
            return {"result": "skipped", "settled": True}
        settled = True
        if settle_timeout is not None and action in ("approve", "deny", "stop"):
            settled = await self.settle(agent, timeout=settle_timeout)
        return {"result": "sent", "settled": settled}

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
