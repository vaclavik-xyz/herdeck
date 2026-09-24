"""Version handshake + bridge health: the bridge announces its herdeck
version in every snapshot and answers an authenticated ``health`` message;
the connector remembers both and flags a protocol it does not speak."""

import asyncio
import contextlib
import json
import logging

import pytest
import websockets

from herdeck import __version__
from herdeck.bridge import StubHerdr, handle_client_message, start_local_bridge
from herdeck.config import ServerConfig
from herdeck.connector import Connector
from herdeck.protocol import WIRE_PROTOCOL, Snapshot, decode_inbound


async def test_snapshot_carries_herdeck_version():
    out = await handle_client_message(StubHerdr(panes=[]), "s", '{"type":"list"}')
    msg = json.loads(out)
    assert msg["herdeck_version"] == __version__
    assert msg["protocol"] == WIRE_PROTOCOL


def test_decode_snapshot_reads_optional_version():
    snap = decode_inbound(
        json.dumps({"type": "snapshot", "server_id": "s", "panes": [], "herdeck_version": "9.9.9"})
    )
    assert isinstance(snap, Snapshot) and snap.herdeck_version == "9.9.9"
    old = decode_inbound(json.dumps({"type": "snapshot", "server_id": "s", "panes": []}))
    assert old.herdeck_version is None  # an old bridge simply omits it
    bad = decode_inbound(
        json.dumps({"type": "snapshot", "server_id": "s", "panes": [], "herdeck_version": 3})
    )
    assert bad.herdeck_version is None


@contextlib.asynccontextmanager
async def _bridge(herdr=None):
    host, port, token, (server, btask) = await start_local_bridge(
        "unused.sock", herdr=herdr or StubHerdr(panes=[])
    )
    try:
        yield f"ws://{host}:{port}", {"Authorization": f"Bearer {token}"}
    finally:
        btask.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await btask
        server.close()
        await server.wait_closed()


async def test_health_message_reports_version_protocol_herdr_and_clients():
    async with _bridge() as (url, headers):
        async with websockets.connect(url, additional_headers=headers) as ws:
            assert json.loads(await ws.recv())["type"] == "snapshot"
            await ws.send(json.dumps({"type": "health", "req": "h1"}))
            reply = json.loads(await asyncio.wait_for(ws.recv(), 3))
    assert reply == {
        "type": "result",
        "req": "h1",
        "data": {
            "herdeck_version": __version__,
            "protocol": WIRE_PROTOCOL,
            "herdr_reachable": True,
            "clients": 1,
        },
    }


async def test_health_message_reports_unreachable_herdr():
    class DeadHerdr(StubHerdr):
        async def snapshot(self):
            raise OSError("no socket")

    herdr = StubHerdr(panes=[])
    async with _bridge(herdr) as (url, headers):
        async with websockets.connect(url, additional_headers=headers) as ws:
            await ws.recv()
            herdr.__class__ = DeadHerdr  # herdr goes away after the greeting
            await ws.send(json.dumps({"type": "health", "req": "h2"}))
            reply = json.loads(await asyncio.wait_for(ws.recv(), 3))
    assert reply["data"]["herdr_reachable"] is False


async def test_health_message_requires_the_token():
    async with _bridge() as (url, _headers):
        async with websockets.connect(
            url, additional_headers={"Authorization": "Bearer nope"}
        ) as ws:
            with pytest.raises(websockets.ConnectionClosed) as info:
                await ws.send(json.dumps({"type": "health", "req": "h3"}))
                await asyncio.wait_for(ws.recv(), 3)
    assert info.value.rcvd.code == 4401


# --- connector side ---------------------------------------------------------


@pytest.fixture
async def fake_bridge():
    frames: list[dict] = []

    async def handler(ws):
        for frame in frames:
            await ws.send(json.dumps(frame))
        async for _ in ws:
            pass

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    yield port, frames
    server.close()
    await server.wait_closed()


async def _connect_until_snapshot(port):
    snaps = []
    conn = Connector(
        ServerConfig("box", f"ws://127.0.0.1:{port}", "tok"),
        on_snapshot=lambda sid, states: snaps.append(sid),
        on_event=lambda sid, s: None,
        on_connection=lambda sid, up: None,
    )
    task = asyncio.create_task(conn.run())
    for _ in range(100):
        if snaps:
            break
        await asyncio.sleep(0.02)
    return conn, task


async def _stop(conn, task):
    conn.stop()
    await asyncio.wait_for(task, timeout=2.0)


async def test_connector_remembers_bridge_version_and_state(fake_bridge):
    port, frames = fake_bridge
    frames.append(
        {"type": "snapshot", "server_id": "b", "panes": [], "protocol": 3, "herdeck_version": "0.8.1"}
    )
    conn, task = await _connect_until_snapshot(port)
    try:
        health = conn.health()
        assert health["connected"] is True
        assert health["bridge_version"] == "0.8.1"
        assert health["protocol"] == 3
        assert health["protocol_supported"] is True
        assert health["last_error"] is None
        assert health["attempt"] == 0
        assert isinstance(health["since"], int) and health["since"] > 0
    finally:
        await _stop(conn, task)
    assert conn.health()["connected"] is False


async def test_connector_warns_on_unsupported_protocol(fake_bridge, caplog):
    port, frames = fake_bridge
    frames.append(
        {"type": "snapshot", "server_id": "b", "panes": [], "protocol": WIRE_PROTOCOL + 1}
    )
    with caplog.at_level(logging.WARNING, logger="herdeck.connector"):
        conn, task = await _connect_until_snapshot(port)
        try:
            assert conn.health()["protocol_supported"] is False
        finally:
            await _stop(conn, task)
    assert any("unsupported wire protocol" in r.getMessage() for r in caplog.records)


async def test_connector_counts_failed_attempts():
    conn = Connector(
        ServerConfig("box", "ws://127.0.0.1:9", "tok"),
        on_snapshot=lambda sid, states: None,
        on_event=lambda sid, s: None,
        on_connection=lambda sid, up: None,
        backoff_base=0.01,
        backoff_max=0.01,
    )
    task = asyncio.create_task(conn.run())
    for _ in range(100):
        if conn.health()["attempt"] >= 2:
            break
        await asyncio.sleep(0.02)
    await _stop(conn, task)
    health = conn.health()
    assert health["attempt"] >= 2
    assert health["connected"] is False
    assert health["last_error"]
