from __future__ import annotations

import asyncio
import hmac
import json
import os
from typing import Protocol

import websockets

from .protocol import encode

# herdr agent_status values that mark a pane as worth showing on the deck.
_AGENT_STATUSES = {"idle", "working", "blocked", "done"}


class HerdrClient(Protocol):
    async def list_panes(self) -> list[dict]: ...
    async def get_pane(self, pane_id: str) -> dict: ...
    async def read_pane(self, pane_id: str, source: str) -> str: ...
    async def send_keys(self, pane_id: str, keys: list[str]) -> None: ...


def _is_agent_pane(p: dict) -> bool:
    """A raw herdr pane worth showing on the deck hosts a detected agent."""
    return bool(p.get("agent")) or p.get("agent_status") in _AGENT_STATUSES


def _herdr_pane_to_wire(p: dict) -> dict:
    """Map a raw herdr pane to herdeck's wire pane schema.

    herdr uses `agent` / `agent_status` and has no human label, so we derive a
    label/project from the pane's working directory.
    """
    cwd = p.get("foreground_cwd") or p.get("cwd") or ""
    label = os.path.basename(cwd.rstrip("/")) or p.get("workspace_id", "")
    return {
        "pane_id": p["pane_id"],
        "agent_type": p.get("agent", "default"),
        "label": label,
        "status": p.get("agent_status", "unknown"),
        "project": label,
    }


def _wire_panes(raw: list[dict]) -> list[dict]:
    return [_herdr_pane_to_wire(p) for p in raw if _is_agent_pane(p)]


class StubHerdr:
    """In-memory herdr (raw herdr pane shape) for tests."""

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
        panes = _wire_panes(await herdr.list_panes())
        return encode({"type": "snapshot", "server_id": server_id, "panes": panes})
    if kind == "read":
        text = await herdr.read_pane(msg["pane_id"], msg.get("source", "detection"))
        return encode({"type": "result", "req": msg["req"],
                       "data": {"text": text, "pane_id": msg["pane_id"]}})
    if kind == "act":
        guard = msg.get("guard", True)
        if guard:
            pane = await herdr.get_pane(msg["pane_id"])
            if pane.get("agent_status") != "blocked":
                return encode({"type": "result", "req": msg["req"],
                               "data": {"skipped": True}})
        await herdr.send_keys(msg["pane_id"], msg["keys"])
        return encode({"type": "result", "req": msg["req"], "data": {"sent": True}})
    raise ValueError(f"unknown client message: {kind}")


class HerdrEvents:
    """Yields the full agent list whenever it changes, by polling pane.list.

    herdr's `events.subscribe` is per-pane; polling and diffing the whole list is
    simpler and robust for a small fleet. Yielding the FULL list (not per-pane
    deltas) means additions, status changes AND removals are all reflected — a
    pane that closes simply drops out of the next snapshot.
    """

    def __init__(self, herdr: HerdrClient, poll_interval: float = 1.5):
        self._herdr = herdr
        self._interval = poll_interval

    async def stream(self):
        prev: list[dict] | None = None
        while True:
            try:
                raw = await self._herdr.list_panes()
            except Exception:
                raw = None
            if raw is not None:
                cur = _wire_panes(raw)
                if cur != prev:
                    yield cur
                    prev = cur
            await asyncio.sleep(self._interval)


async def _broadcast(snapshot_stream, clients: set, server_id: str) -> None:
    """Forward each changed full agent list to all clients as a snapshot.

    Snapshots (not per-pane events) are used so that removed/finished panes
    disappear from the deck instead of lingering until a manual refresh.
    """
    async for panes in snapshot_stream:
        msg = encode({"type": "snapshot", "server_id": server_id, "panes": panes})
        for ws in list(clients):
            try:
                await ws.send(msg)
            except Exception:
                pass


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

    async def _rpc(self, method: str, params: dict, *, retry: bool = True) -> dict:
        # self._lock serializes RPCs, so the fixed request id "b" is safe.
        async with self._lock:
            attempts = 2 if retry else 1
            last_exc: Exception | None = None
            for _ in range(attempts):
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
                    self._writer = None
            raise last_exc

    async def list_panes(self) -> list[dict]:
        res = await self._rpc("pane.list", {})
        return res.get("result", {}).get("panes", [])

    async def get_pane(self, pane_id: str) -> dict:
        res = await self._rpc("pane.get", {"pane_id": pane_id})
        return res.get("result", {}).get("pane", {})

    async def read_pane(self, pane_id: str, source: str) -> str:
        res = await self._rpc("pane.read", {"pane_id": pane_id, "source": source})
        return res.get("result", {}).get("read", {}).get("text", "")

    async def send_keys(self, pane_id: str, keys: list[str]) -> None:
        await self._rpc("pane.send_keys", {"pane_id": pane_id, "keys": keys}, retry=False)


async def _serve_connection(ws, herdr: HerdrClient, server_id: str, token: str, clients: set):
    auth = ws.request.headers.get("Authorization", "")
    if not hmac.compare_digest(auth, f"Bearer {token}"):
        await ws.close(code=4401, reason="unauthorized")
        return
    panes = _wire_panes(await herdr.list_panes())
    await ws.send(encode({"type": "snapshot", "server_id": server_id, "panes": panes}))
    clients.add(ws)
    try:
        async for raw in ws:
            try:
                out = await handle_client_message(herdr, server_id, raw)
            except Exception as exc:
                out = encode({"type": "error", "message": str(exc)})
            await ws.send(out)
    finally:
        clients.discard(ws)


async def serve(socket_path: str, host: str, port: int, server_id: str, token: str):
    herdr = SocketHerdr(socket_path)
    events = HerdrEvents(herdr)
    clients: set = set()

    async def handler(ws):
        await _serve_connection(ws, herdr, server_id, token, clients)

    async with websockets.serve(handler, host, port):
        await _broadcast(events.stream(), clients, server_id)  # runs forever


def main() -> None:
    socket_path = os.environ["HERDR_SOCKET"]
    host = os.environ.get("HERDECK_BIND", "127.0.0.1")  # set to Tailscale IP
    port = int(os.environ.get("HERDECK_PORT", "8788"))
    server_id = os.environ.get("HERDECK_SERVER_ID", "server")
    token = os.environ["HERDECK_TOKEN"]
    asyncio.run(serve(socket_path, host, port, server_id, token))


if __name__ == "__main__":
    main()
