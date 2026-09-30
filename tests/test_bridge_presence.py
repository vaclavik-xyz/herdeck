import asyncio
import contextlib
import json

import websockets

from herdeck.bridge import StubHerdr, _serve_connection
from herdeck.presence_hub import PRESENCE_CAPABILITY, PresenceHub


def test_aggregate_is_min_idle_aged_and_skips_stale():
    now = [100.0]
    hub = PresenceHub(clock=lambda: now[0])
    hub.report("a", 600.0)
    hub.report("b", 5.0)
    hub.report("c", None)
    now[0] = 110.0
    assert hub.aggregate() == (15.0, 3)
    now[0] = 200.0  # a and b are 100 s old -> stale
    hub.report("c", None)
    assert hub.aggregate() == (None, 1)


def test_drop_forgets_a_reporter():
    hub = PresenceHub(clock=lambda: 0.0)
    hub.report("a", 1.0)
    assert hub.drop("a") is True and hub.drop("a") is False
    assert hub.aggregate() == (None, 0)


def test_negative_or_bogus_idle_counts_as_unknown():
    hub = PresenceHub(clock=lambda: 0.0)
    hub.report("a", -5)
    hub.report("b", float("nan"))
    assert hub.aggregate() == (None, 2)


@contextlib.asynccontextmanager
async def _bridge(presence):
    clients: dict = {}

    async def handler(ws):
        await _serve_connection(
            ws, StubHerdr(panes=[]), "s", "tok", clients, "/unused.sock",
            readonly_token="view", presence=presence,
        )

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


async def _connect(url, token):
    ws = await websockets.connect(url, additional_headers={"Authorization": f"Bearer {token}"})
    snap = json.loads(await asyncio.wait_for(ws.recv(), 3))
    return ws, snap


async def test_report_broadcasts_aggregate_to_reporters_only():
    hub = PresenceHub()
    async with _bridge(hub) as url:
        a, snap = await _connect(url, "tok")
        assert PRESENCE_CAPABILITY in snap["capabilities"]
        b, _ = await _connect(url, "tok")
        await a.send(json.dumps({"type": "presence", "idle_s": 42.0}))
        frame = json.loads(await asyncio.wait_for(a.recv(), 3))
        assert frame["type"] == "presence" and frame["server_id"] == "s"
        assert frame["clients"] == 1 and 42.0 <= frame["idle_s"] < 43.0
        with contextlib.suppress(TimeoutError):
            extra = await asyncio.wait_for(b.recv(), 0.3)
            assert json.loads(extra)["type"] != "presence"  # b never reported
        await b.send(json.dumps({"type": "presence", "idle_s": 3.0}))
        for ws in (a, b):
            frame = json.loads(await asyncio.wait_for(ws.recv(), 3))
            assert frame["clients"] == 2 and frame["idle_s"] < 4.0
        await b.close()
        frame = json.loads(await asyncio.wait_for(a.recv(), 3))
        assert frame["clients"] == 1 and frame["idle_s"] >= 42.0  # b dropped
        await a.close()


async def test_readonly_client_cannot_report_presence():
    hub = PresenceHub()
    async with _bridge(hub) as url:
        ws, _ = await _connect(url, "view")
        await ws.send(json.dumps({"type": "presence", "idle_s": 1.0}))
        # Silently ignored: an error frame (empty req) would fail in-flight
        # card reads on the runtime, which reports presence every 30 s.
        with contextlib.suppress(TimeoutError):
            frame = await asyncio.wait_for(ws.recv(), 0.3)
            raise AssertionError(f"unexpected frame {frame!r}")
        assert hub.aggregate() == (None, 0)
        await ws.close()


async def test_bridge_without_hub_does_not_advertise_presence():
    async with _bridge(None) as url:
        ws, snap = await _connect(url, "tok")
        assert PRESENCE_CAPABILITY not in snap["capabilities"]
        await ws.close()
