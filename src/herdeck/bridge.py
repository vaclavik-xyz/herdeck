from __future__ import annotations

import asyncio
import hmac
import json
import os
from typing import Protocol

import websockets

from .protocol import encode


class HerdrClient(Protocol):
    async def list_panes(self) -> list[dict]: ...
    async def get_pane(self, pane_id: str) -> dict: ...
    async def read_pane(self, pane_id: str, source: str) -> str: ...
    async def send_keys(self, pane_id: str, keys: list[str]) -> None: ...


class StubHerdr:
    """In-memory herdr for tests."""

    def __init__(self, panes: list[dict]):
        self.panes = panes
        self.detection: dict[str, str] = {}
        self.sent: list[tuple[str, list[str]]] = []

    async def list_panes(self) -> list[dict]:
        return self.panes

    async def get_pane(self, pane_id: str) -> dict:
        return next(p for p in self.panes if p["pane_id"] == pane_id)

    async def read_pane(self, pane_id: str, source: str) -> str:
        return self.detection.get(pane_id, "")

    async def send_keys(self, pane_id: str, keys: list[str]) -> None:
        self.sent.append((pane_id, keys))


async def handle_client_message(herdr: HerdrClient, server_id: str, raw: str) -> str:
    msg = json.loads(raw)
    kind = msg["type"]
    if kind == "list":
        panes = await herdr.list_panes()
        return encode({"type": "snapshot", "server_id": server_id, "panes": panes})
    if kind == "read":
        text = await herdr.read_pane(msg["pane_id"], msg.get("source", "detection"))
        return encode({"type": "result", "req": msg["req"], "data": {"text": text}})
    if kind == "act":
        pane = await herdr.get_pane(msg["pane_id"])
        if pane.get("status") != "blocked":
            return encode({"type": "result", "req": msg["req"],
                           "data": {"skipped": True}})
        await herdr.send_keys(msg["pane_id"], msg["keys"])
        return encode({"type": "result", "req": msg["req"], "data": {"sent": True}})
    raise ValueError(f"unknown client message: {kind}")


class SocketHerdr:
    """Talks to a real herdr instance over its Unix socket (newline JSON)."""

    def __init__(self, socket_path: str):
        self._path = socket_path
        self._lock = asyncio.Lock()
        self._reader = None
        self._writer = None

    async def _ensure(self):
        if self._writer is None or self._writer.is_closing():
            self._reader, self._writer = await asyncio.open_unix_connection(self._path)

    async def _rpc(self, method: str, params: dict) -> dict:
        async with self._lock:
            # self._lock serializes RPCs, so the fixed request id "b" is safe:
            # only one request/response is ever in flight at a time.
            last_exc = None
            for attempt in range(2):
                try:
                    await self._ensure()
                    self._writer.write((json.dumps(
                        {"id": "b", "method": method, "params": params}) + "\n").encode())
                    await self._writer.drain()
                    line = await self._reader.readline()
                    if not line:                       # EOF: herdr dropped the socket
                        raise ConnectionError("herdr socket closed")
                    return json.loads(line.decode())
                except (OSError, ConnectionError) as exc:
                    last_exc = exc
                    self._reader = None
                    self._writer = None                # force reconnect on retry
            raise last_exc

    async def list_panes(self) -> list[dict]:
        res = await self._rpc("pane.list", {})
        return res.get("result", {}).get("panes", [])

    async def get_pane(self, pane_id: str) -> dict:
        res = await self._rpc("pane.get", {"pane_id": pane_id})
        return res.get("result", {})

    async def read_pane(self, pane_id: str, source: str) -> str:
        res = await self._rpc("pane.read", {"pane_id": pane_id, "source": source})
        return res.get("result", {}).get("text", "")

    async def send_keys(self, pane_id: str, keys: list[str]) -> None:
        await self._rpc("pane.send_keys", {"pane_id": pane_id, "keys": keys})


async def _serve_connection(ws, herdr: HerdrClient, server_id: str, token: str):
    auth = ws.request.headers.get("Authorization", "")
    if not hmac.compare_digest(auth, f"Bearer {token}"):
        await ws.close(code=4401, reason="unauthorized")
        return
    # push initial snapshot, then relay client commands
    panes = await herdr.list_panes()
    await ws.send(encode({"type": "snapshot", "server_id": server_id, "panes": panes}))
    async for raw in ws:
        try:
            out = await handle_client_message(herdr, server_id, raw)
        except Exception as exc:
            # handle_client_message only raises ValueError/KeyError, which never
            # contain the token, so surfacing str(exc) is safe.
            out = encode({"type": "error", "message": str(exc)})
        await ws.send(out)


async def serve(socket_path: str, host: str, port: int, server_id: str, token: str):
    herdr = SocketHerdr(socket_path)

    async def handler(ws):
        await _serve_connection(ws, herdr, server_id, token)

    async with websockets.serve(handler, host, port):
        await asyncio.Future()  # run forever


def main() -> None:
    socket_path = os.environ["HERDR_SOCKET"]
    host = os.environ.get("HERDECK_BIND", "127.0.0.1")  # set to Tailscale IP
    port = int(os.environ.get("HERDECK_PORT", "8788"))
    server_id = os.environ.get("HERDECK_SERVER_ID", "server")
    token = os.environ["HERDECK_TOKEN"]
    asyncio.run(serve(socket_path, host, port, server_id, token))
