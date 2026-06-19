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
    async def focus_agent(self, pane_id: str) -> None: ...
    async def send_text(self, pane_id: str, text: str) -> None: ...


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
        self.focused: list[str] = []

    async def list_panes(self) -> list[dict]:
        return self.panes

    async def get_pane(self, pane_id: str) -> dict:
        return next(p for p in self.panes if p["pane_id"] == pane_id)

    async def read_pane(self, pane_id: str, source: str) -> str:
        return self.detection.get(pane_id, "")

    async def send_keys(self, pane_id: str, keys: list[str]) -> None:
        self.sent.append((pane_id, keys))

    async def focus_agent(self, pane_id: str) -> None:
        self.focused.append(pane_id)

    async def send_text(self, pane_id: str, text: str) -> None:
        self.sent.append((pane_id, text))


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
    if kind == "focus":
        await herdr.focus_agent(msg["pane_id"])
        return encode({"type": "result", "req": msg["req"], "data": {"focused": True}})
    if kind == "send_text":
        await herdr.send_text(msg["pane_id"], msg["text"])
        return encode({"type": "result", "req": msg["req"], "data": {"sent": True}})
    raise ValueError(f"unknown client message: {kind}")


# herdr events that change fleet membership (need a status re-subscribe after).
_GLOBAL_EVENT_TYPES = ("pane.created", "pane.closed", "pane.exited",
                       "pane.agent_detected")
_FLEET_EVENT_NAMES = {"pane_created", "pane_closed", "pane_exited",
                      "pane_agent_detected"}


class HerdrEvents:
    """Yields the full agent list whenever it changes.

    The source of truth is a diff of ``pane.list`` (so additions, status changes
    AND removals are all reflected — a closed pane simply drops out). Re-lists are
    triggered immediately by herdr's push events (``events.subscribe``) when a
    socket path is given, with a slow poll as a safety net; without one it falls
    back to pure polling.
    """

    def __init__(self, herdr: HerdrClient, socket_path: str | None = None,
                 poll_interval: float = 5.0):
        self._herdr = herdr
        self._socket_path = socket_path
        self._interval = poll_interval
        self._wake = asyncio.Event()

    async def stream(self):
        listener = (asyncio.create_task(self._listen())
                    if self._socket_path else None)
        prev: list[dict] | None = None
        try:
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
                try:                          # wake on a push event, else slow poll
                    await asyncio.wait_for(self._wake.wait(), timeout=self._interval)
                except (asyncio.TimeoutError, TimeoutError):
                    pass
                self._wake.clear()
        finally:
            if listener is not None:
                listener.cancel()

    async def _listen(self) -> None:
        """Hold a herdr event subscription; wake the stream on every event."""
        while True:
            writer = None
            try:
                reader, writer = await asyncio.open_unix_connection(self._socket_path)
                agent_panes = [p["pane_id"]
                               for p in _wire_panes(await self._herdr.list_panes())]
                subs = [{"type": t} for t in _GLOBAL_EVENT_TYPES]
                subs += [{"type": "pane.agent_status_changed", "pane_id": pid}
                         for pid in agent_panes]
                writer.write((json.dumps({"id": "e", "method": "events.subscribe",
                                          "params": {"subscriptions": subs}})
                              + "\n").encode())
                await writer.drain()
                await reader.readline()       # subscription ack
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    self._wake.set()
                    try:
                        name = json.loads(line).get("event")
                    except Exception:
                        name = None
                    if name in _FLEET_EVENT_NAMES:
                        break                 # fleet changed -> re-subscribe panes
            except Exception:
                pass
            finally:
                if writer is not None:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except Exception:
                        pass
            await asyncio.sleep(0.3)          # brief backoff before re-subscribe


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

    async def _rpc(self, method: str, params: dict, *, retry: bool = True) -> dict:
        # herdr closes the unix socket after each request (one-shot), so we open
        # a fresh connection per RPC instead of reusing one — reuse fails on the
        # second call of a burst (e.g. act = get_pane + send_keys) as the
        # server-side close isn't detected before the next write.
        # self._lock serializes RPCs, so the fixed request id "b" is safe.
        async with self._lock:
            attempts = 2 if retry else 1
            last_exc: Exception | None = None
            for _ in range(attempts):
                reader = writer = None
                try:
                    reader, writer = await asyncio.open_unix_connection(self._path)
                    writer.write((json.dumps(
                        {"id": "b", "method": method, "params": params}) + "\n").encode())
                    await writer.drain()
                    line = await reader.readline()
                    if not line:                       # EOF before a response
                        raise ConnectionError("herdr socket closed")
                    return json.loads(line.decode())
                except (OSError, ConnectionError) as exc:
                    last_exc = exc
                finally:
                    if writer is not None:
                        writer.close()
                        try:
                            await writer.wait_closed()
                        except Exception:
                            pass
            raise last_exc

    async def list_panes(self) -> list[dict]:
        res = await self._rpc("pane.list", {})
        return res.get("result", {}).get("panes", [])

    async def get_pane(self, pane_id: str) -> dict:
        # herdr has no working `pane.get`; derive the pane from the (supported)
        # pane.list so the act guard can check current status.
        for pane in await self.list_panes():
            if pane.get("pane_id") == pane_id:
                return pane
        return {}

    async def read_pane(self, pane_id: str, source: str) -> str:
        res = await self._rpc("pane.read", {"pane_id": pane_id, "source": source})
        return res.get("result", {}).get("read", {}).get("text", "")

    async def send_keys(self, pane_id: str, keys: list[str]) -> None:
        await self._rpc("pane.send_keys", {"pane_id": pane_id, "keys": keys}, retry=False)

    async def focus_agent(self, pane_id: str) -> None:
        # herdr focuses the agent on screen; target is the agent's pane id.
        await self._rpc("agent.focus", {"target": pane_id})

    async def send_text(self, pane_id: str, text: str) -> None:
        # agent.send types the text into the agent's input but does not submit it,
        # so follow with Enter to actually send the message.
        await self._rpc("agent.send", {"target": pane_id, "text": text}, retry=False)
        await self._rpc("pane.send_keys", {"pane_id": pane_id, "keys": ["enter"]},
                        retry=False)


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
    events = HerdrEvents(herdr, socket_path=socket_path)   # push events + slow poll
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
