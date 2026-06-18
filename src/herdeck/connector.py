from __future__ import annotations

import asyncio
from collections.abc import Callable

import websockets

from .config import ServerConfig
from .model import AgentState
from .protocol import decode_inbound, encode, Event, Result, Snapshot


class Connector:
    def __init__(
        self,
        server: ServerConfig,
        on_snapshot: Callable[[str, list[AgentState]], None],
        on_event: Callable[[str, AgentState], None],
        on_connection: Callable[[str, bool], None],
        on_result: Callable[[str, dict], None] | None = None,
        backoff_base: float = 0.5,
        backoff_max: float = 30.0,
    ):
        self.server = server
        self._on_snapshot = on_snapshot
        self._on_event = on_event
        self._on_connection = on_connection
        self._on_result = on_result or (lambda req, data: None)
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._stop = False
        self._ws = None
        self._loop = None

    def stop(self) -> None:
        self._stop = True
        ws = self._ws
        loop = self._loop
        if ws is not None and loop is not None:
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(ws.close()))

    async def send(self, msg: dict) -> None:
        ws = self._ws
        if ws is not None:
            try:
                await ws.send(encode(msg))
            except websockets.WebSocketException:
                pass

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
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
                    await ws.send(encode({"type": "list"}))   # resync-on-reconnect
                    async for raw in ws:
                        self._dispatch(raw)
            except (OSError, websockets.WebSocketException):
                pass
            finally:
                self._ws = None
                if connected:
                    self._on_connection(self.server.id, False)
            if self._stop:
                break
            delay = min(self._backoff_base * (2 ** attempt), self._backoff_max)
            attempt += 1
            await asyncio.sleep(delay)

    def _dispatch(self, raw: str) -> None:
        msg = decode_inbound(raw)
        if isinstance(msg, Snapshot):
            self._on_snapshot(msg.server_id, msg.states)
        elif isinstance(msg, Event):
            self._on_event(msg.server_id, msg.state)
        elif isinstance(msg, Result):
            self._on_result(msg.req, msg.data)
