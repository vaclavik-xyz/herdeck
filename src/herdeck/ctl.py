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
