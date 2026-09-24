"""The connector asks a self_update-capable bridge once per connection whether
it is managed (health probe) and GET /maintenance reports it per server."""

import asyncio
import json

import pytest
import websockets

from herdeck import connector as connector_mod
from herdeck.bridge import StubHerdr, start_local_bridge
from herdeck.config import ServerConfig
from herdeck.connector import Connector
from herdeck.protocol import WIRE_PROTOCOL


def _snapshot(capabilities, protocol=WIRE_PROTOCOL):
    return {
        "type": "snapshot", "server_id": "b", "panes": [], "protocol": protocol,
        "herdeck_version": "0.9.0", "capabilities": capabilities,
    }


class FakeBridge:
    """Sends a snapshot on connect and answers health per ``mode``:
    managed (bool) / "error_req" / "unknown" (old bridge, reqless) / "silent"."""

    def __init__(self, capabilities, mode=True, protocol=WIRE_PROTOCOL):
        self.capabilities = capabilities
        self.mode = mode
        self.protocol = protocol
        self.health_requests: list[dict] = []
        self.connections: list = []

    async def handler(self, ws):
        self.connections.append(ws)
        await ws.send(json.dumps(_snapshot(self.capabilities, self.protocol)))
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") != "health":
                continue
            self.health_requests.append(msg)
            if self.mode == "silent":
                continue
            if self.mode == "unknown":
                await ws.send(json.dumps(
                    {"type": "error", "message": "unknown client message: health"}
                ))
            elif self.mode == "error_req":
                await ws.send(json.dumps(
                    {"type": "error", "req": msg["req"], "message": "nope"}
                ))
            else:
                await ws.send(json.dumps({"type": "result", "req": msg["req"], "data": {
                    "herdeck_version": "0.9.0", "protocol": self.protocol,
                    "herdr_reachable": True, "clients": 1, "managed": self.mode,
                }}))


async def _serve(bridge):
    server = await websockets.serve(bridge.handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def _connector(port, **kw):
    seen = {"errors": [], "results": [], "up": []}
    conn = Connector(
        ServerConfig("box", f"ws://127.0.0.1:{port}", "tok"),
        on_snapshot=lambda sid, states: None,
        on_event=lambda sid, s: None,
        on_connection=lambda sid, up: seen["up"].append(up),
        on_result=lambda req, data: seen["results"].append(req),
        on_error=lambda message: seen["errors"].append(message),
        backoff_base=0.01,
        backoff_max=0.01,
        **kw,
    )
    return conn, seen


async def _until(pred, timeout=3.0):
    for _ in range(int(timeout / 0.02)):
        if pred():
            return True
        await asyncio.sleep(0.02)
    return pred()


async def _run(bridge, check, **kw):
    server, port = await _serve(bridge)
    conn, seen = _connector(port, **kw)
    task = asyncio.create_task(conn.run())
    try:
        await check(conn, seen, server)
    finally:
        conn.stop()
        await asyncio.wait_for(task, timeout=2.0)
        server.close()
        await server.wait_closed()
    return conn


@pytest.mark.parametrize("managed", [True, False])
async def test_probe_reports_managed_once_per_connection(managed):
    bridge = FakeBridge(["self_update"], mode=managed)

    async def check(conn, seen, server):
        assert await _until(lambda: conn.health()["managed"] is managed)
        await asyncio.sleep(0.1)
        assert len(bridge.health_requests) == 1  # cached, not re-asked per snapshot
        assert conn.health()["self_update"] is True
        assert seen["results"] == [] and seen["errors"] == []  # claimed, never surfaced

    conn = await _run(bridge, check)
    assert conn.health()["managed"] is None  # reset on disconnect


async def test_probe_resets_and_reasks_on_reconnect():
    bridge = FakeBridge(["self_update"], mode=True)

    async def check(conn, seen, server):
        assert await _until(lambda: conn.health()["managed"] is True)
        bridge.mode = False
        await bridge.connections[0].close()
        assert await _until(lambda: len(bridge.health_requests) == 2)
        assert await _until(lambda: conn.health()["managed"] is False)

    await _run(bridge, check)


@pytest.mark.parametrize(
    ("capabilities", "protocol"),
    [([], WIRE_PROTOCOL), (["self_update"], WIRE_PROTOCOL + 1)],
)
async def test_no_probe_without_the_capability_or_protocol_support(capabilities, protocol):
    bridge = FakeBridge(capabilities, mode=True, protocol=protocol)

    async def check(conn, seen, server):
        assert await _until(lambda: conn.health()["connected"])
        await asyncio.sleep(0.15)
        assert bridge.health_requests == []
        assert conn.health()["managed"] is None
        assert conn.health()["self_update"] is ("self_update" in capabilities)

    await _run(bridge, check)


@pytest.mark.parametrize("mode", ["unknown", "error_req"])
async def test_error_answers_leave_managed_unknown_and_are_not_surfaced(mode):
    bridge = FakeBridge(["self_update"], mode=mode)

    async def check(conn, seen, server):
        assert await _until(lambda: len(bridge.health_requests) == 1)
        await asyncio.sleep(0.1)
        assert conn.health()["managed"] is None
        assert conn._health_waiter is None
        assert seen["errors"] == []

    await _run(bridge, check)


async def test_unanswered_probe_times_out_to_unknown(monkeypatch):
    monkeypatch.setattr(connector_mod, "HEALTH_PROBE_TIMEOUT_S", 0.1)
    bridge = FakeBridge(["self_update"], mode="silent")

    async def check(conn, seen, server):
        assert await _until(lambda: len(bridge.health_requests) == 1)
        assert await _until(lambda: conn._health_waiter is None)
        assert conn.health()["managed"] is None
        assert conn.health()["connected"] is True  # the connection is unaffected

    await _run(bridge, check)


async def test_real_bridge_answers_the_probe():
    host, port, token, (server, btask) = await start_local_bridge(
        "unused.sock", herdr=StubHerdr(panes=[])
    )
    conn = Connector(
        ServerConfig("box", f"ws://{host}:{port}", token),
        on_snapshot=lambda sid, states: None,
        on_event=lambda sid, s: None,
        on_connection=lambda sid, up: None,
    )
    task = asyncio.create_task(conn.run())
    try:
        assert await _until(lambda: conn.health()["managed"] is False)
        assert conn.health()["self_update"] is True
    finally:
        conn.stop()
        await asyncio.wait_for(task, timeout=2.0)
        btask.cancel()
        try:
            await btask
        except asyncio.CancelledError:
            pass
        server.close()
        await server.wait_closed()


# --- GET /maintenance -------------------------------------------------------


class _App:
    def __init__(self, facts):
        self._sinks = []
        self._started_at = 0.0
        self._source = type("S", (), {"server_health": lambda _self: facts})()


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        ({"connected": True, "self_update": True, "managed": True}, (True, True)),
        ({"connected": True, "self_update": True, "managed": False}, (True, False)),
        ({"connected": True, "self_update": True, "managed": None}, (True, None)),
        ({"connected": True, "self_update": False, "managed": None}, (False, None)),
        ({"connected": True}, (False, None)),  # T3 / a connector without the facts
        ({"connected": True, "self_update": "yes", "managed": "yes"}, (False, None)),
    ],
)
def test_maintenance_reports_self_update_and_managed(facts, expected, tmp_path, monkeypatch):
    from herdeck.deckapp import maintenance as mt

    monkeypatch.setattr(mt.Maintenance, "d200_status", lambda self: {})
    status = mt.Maintenance(_App({"box": facts}), home=tmp_path, platform="linux").status()
    server = status["servers"]["box"]
    assert (server["self_update"], server["managed"]) == expected
    assert server["connected"] is True
