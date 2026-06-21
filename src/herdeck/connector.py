from __future__ import annotations

import asyncio
from collections.abc import Callable

import websockets

from .config import ServerConfig
from .model import AgentKey, AgentState
from .protocol import Error, Event, Result, Snapshot, decode_inbound, encode


class Connector:
    def __init__(
        self,
        server: ServerConfig,
        on_snapshot: Callable[[str, list[AgentState]], None],
        on_event: Callable[[str, AgentState], None],
        on_connection: Callable[[str, bool], None],
        on_result: Callable[[str, dict], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        backoff_base: float = 0.5,
        backoff_max: float = 30.0,
    ):
        self.server = server
        self._on_snapshot = on_snapshot
        self._on_event = on_event
        self._on_connection = on_connection
        self._on_result = on_result or (lambda req, data: None)
        self._on_error = on_error or (lambda message: None)
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._stop = False
        self._ws = None
        self._loop = None
        self._wake = None
        self._send_lock = asyncio.Lock()

    def stop(self) -> None:
        self._stop = True
        loop = self._loop
        ws = self._ws
        wake = self._wake
        if loop is not None:
            if ws is not None:
                loop.call_soon_threadsafe(lambda: asyncio.ensure_future(ws.close()))
            if wake is not None:
                loop.call_soon_threadsafe(wake.set)

    async def send(self, msg: dict) -> None:
        async with self._send_lock:
            ws = self._ws
            if ws is not None:
                try:
                    await ws.send(encode(msg))
                except websockets.WebSocketException:
                    pass

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        attempt = 0
        while not self._stop:
            connected = False
            try:
                async with websockets.connect(
                    self.server.url,
                    additional_headers={"Authorization": f"Bearer {self.server.token}"},
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
                    self._ws = ws
                    attempt = 0
                    connected = True
                    self._on_connection(self.server.id, True)
                    await ws.send(encode({"type": "list"}))  # resync-on-reconnect
                    async for raw in ws:
                        try:
                            self._dispatch(raw)
                        except Exception as exc:
                            self._on_error(str(exc) or type(exc).__name__)
            except (OSError, websockets.WebSocketException):
                pass
            finally:
                self._ws = None
                if connected:
                    self._on_connection(self.server.id, False)
            if self._stop:
                break
            delay = min(self._backoff_base * (2**attempt), self._backoff_max)
            attempt += 1
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except TimeoutError:
                pass

    def _rekey(self, state: AgentState) -> AgentState:
        """Force the agent key to THIS connector's configured server id.

        The bridge sets its own server_id in frames; routing on the Mac is keyed
        by the config id, so we re-stamp inbound state to keep them consistent
        regardless of the bridge's HERDECK_SERVER_ID.
        """
        if state.key.server_id == self.server.id:
            return state
        return AgentState(
            key=AgentKey(self.server.id, state.key.pane_id),
            agent_type=state.agent_type,
            label=state.label,
            status=state.status,
            project=state.project,
            repo=state.repo,
            branch=state.branch,
        )

    def _dispatch(self, raw: str) -> None:
        msg = decode_inbound(raw)
        if isinstance(msg, Snapshot):
            self._on_snapshot(self.server.id, [self._rekey(s) for s in msg.states])
        elif isinstance(msg, Event):
            self._on_event(self.server.id, self._rekey(msg.state))
        elif isinstance(msg, Result):
            self._on_result(msg.req, msg.data)
        elif isinstance(msg, Error):
            self._on_error(msg.message)
