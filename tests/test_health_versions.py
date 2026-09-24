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
            "managed": False,
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
        assert health["ever_connected"] is True
        assert health["bridge_version"] == "0.8.1"
        assert health["protocol"] == 3
        assert health["protocol_supported"] is True
        assert health["last_error"] is None
        assert health["attempt"] == 0
        assert isinstance(health["since"], int) and health["since"] > 0
    finally:
        await _stop(conn, task)
    assert conn.health()["connected"] is False
    assert conn.health()["ever_connected"] is True  # a real outage from here on


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
    assert health["ever_connected"] is False  # never answered: not an outage
    assert health["last_error"]


# --- runtime /health --------------------------------------------------------


def test_runtime_health_explains_a_dark_deck():
    from test_deckapp_live import FakeRunner, make_live

    app, src, server, _ = make_live()

    class StubConnector:
        def health(self):
            return {
                "connected": True,  # the source's own flag wins
                "last_error": "token rejected (close 4401)",
                "since": 123,
                "attempt": 4,
                "bridge_version": "0.7.9",
                "protocol": 3,
                "protocol_supported": True,
            }

    src.attach_runner(FakeRunner(StubConnector()), server.id)

    class StubSink:
        def deliver(self, frame):
            pass

        def close(self):
            pass

        def health(self):
            return {"connected": False, "since": 1, "last_frame_at": None,
                    "last_error": "disconnected", "lock_owner": 42}

    app.add_sink(StubSink())
    src._notify_feed.push("t", "b", False)
    health = app._health()
    assert health["version"] == __version__
    assert health["protocol"] == WIRE_PROTOCOL
    assert isinstance(health["pid"], int) and health["uptime_s"] >= 0
    assert health["servers"][server.id] == {
        "connected": False,
        "last_error": "token rejected (close 4401)",
        "since": 123,
        "attempt": 4,
        "bridge_version": "0.7.9",
        "protocol": 3,
        "protocol_supported": True,
    }
    assert health["d200"]["lock_owner"] == 42
    assert health["notifications"] == {
        "queued": 1, "acked": 0, "fallback": 0, "dropped": 0, "pending": 1,
    }
    app.close()


def test_notification_stats_count_acks_and_fallbacks():
    from herdeck.notify import NotificationFeed

    feed = NotificationFeed()
    first = feed.push("a", "b", False)
    second = feed.push("c", "d", False)
    assert feed.ack(first["generation"], first["seq"])
    assert feed.fallback(second["generation"], second["seq"], lambda *a: None)
    assert feed.stats() == {
        "queued": 2, "acked": 2, "fallback": 1, "dropped": 0, "pending": 0,
    }


def test_d200_sink_health_reports_frames_errors_and_lock_owner(tmp_path):
    import os

    from test_d200_sink import _RS, _held_driver, _Tile, _wait

    from herdeck.deckapp.device_lock import DeviceLock
    from herdeck.deckapp.sinks import ReconnectingD200Sink, RenderFrame

    path = str(tmp_path / "d200.lock")
    opens = []
    driver = _held_driver()

    def factory():
        opens.append(1)
        if len(opens) == 1:
            raise OSError("device busy")
        return driver

    sink = ReconnectingD200Sink(
        factory, on_press=lambda i: None, slots=13, retry_interval=0.01,
        device_lock=DeviceLock(path),
    )
    other = None
    try:
        assert _wait(lambda: sink.health()["connected"])
        health = sink.health()
        assert health["last_error"] is None and health["lock_owner"] is None
        assert health["last_frame_at"] is None  # nothing rendered yet
        ticker = RenderFrame(render=_RS([_Tile(0)]), working=None, full=True, ticker=True)
        sink.deliver(ticker)  # dropped by D200Sink: no USB write
        assert sink.health()["last_frame_at"] is None
        sink.deliver(RenderFrame(render=_RS([_Tile(0)]), working=None, full=True))
        assert isinstance(sink.health()["last_frame_at"], int)

        other = ReconnectingD200Sink(
            _held_driver, on_press=lambda i: None, slots=13,
            device_lock=DeviceLock(path), lock_retry_interval=0.02,
        )
        assert _wait(lambda: other.health()["last_error"] is not None)
        blocked = other.health()
        assert blocked["connected"] is False
        assert blocked["lock_owner"] == os.getpid()
        assert "another herdeck runtime" in blocked["last_error"]
    finally:
        sink.close()
        if other is not None:
            other.close()


# --- herdeck-doctor ---------------------------------------------------------


def test_doctor_reports_runtime_and_bridge_versions(tmp_path, monkeypatch):
    from herdeck.doctor import check_runtime

    monkeypatch.setenv("HERDECK_RUNTIME_DIR", str(tmp_path))
    info = {"url": "http://127.0.0.1:9", "token": "t"}
    same = {"version": __version__, "servers": {
        "box": {"connected": True, "bridge_version": __version__, "protocol": 3},
    }}
    check = check_runtime(lambda p: info, lambda u, t: same)
    assert check.ok and f"runtime {__version__}" in check.detail
    assert f"'box' connected: bridge {__version__}" in check.detail

    old_bridge = {"version": __version__, "servers": {
        "box": {"connected": False, "last_error": "token rejected",
                "bridge_version": "0.0.1", "protocol": WIRE_PROTOCOL + 1},
    }}
    check = check_runtime(lambda p: info, lambda u, t: old_bridge)
    assert not check.ok
    assert f"bridge 0.0.1 ≠ {__version__}" in check.detail
    assert "down (token rejected)" in check.detail
    assert "unsupported wire protocol" in check.detail

    stale_runtime = check_runtime(lambda p: info, lambda u, t: {"version": "0.0.1"})
    assert not stale_runtime.ok and "restart it" in stale_runtime.detail


def test_doctor_server_check_uses_the_bridge_health():
    from herdeck.config import ServerConfig
    from herdeck.doctor import check_servers

    server = ServerConfig("box", "ws://a:8788", "t")
    healthy = {"herdeck_version": __version__, "protocol": 3,
               "herdr_reachable": True, "clients": 2}
    check = check_servers([server], lambda u, t: healthy)[0]
    assert check.ok and "herdr reachable, 2 client(s)" in check.detail
    check = check_servers([server], lambda u, t: {**healthy, "herdr_reachable": False})[0]
    assert not check.ok and "herdr socket not reachable" in check.detail


async def test_doctor_probe_reads_version_and_health_from_a_real_bridge():
    from herdeck.doctor import _probe_server_ws

    host, port, token, (server, btask) = await start_local_bridge(
        "unused.sock", herdr=StubHerdr(panes=[])
    )
    try:
        info = await _probe_server_ws(f"ws://{host}:{port}", token)
    finally:
        btask.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await btask
        server.close()
        await server.wait_closed()
    assert info == {
        "herdeck_version": __version__,
        "protocol": WIRE_PROTOCOL,
        "herdr_reachable": True,
        "clients": 1,
        "managed": False,
    }


def test_d200_sink_health_keeps_the_open_error():
    from test_d200_sink import _wait

    from herdeck.deckapp.sinks import ReconnectingD200Sink

    def factory():
        raise OSError("device busy")

    sink = ReconnectingD200Sink(factory, on_press=lambda i: None, slots=13, retry_interval=0.01)
    try:
        assert _wait(lambda: sink.health()["last_error"] == "device busy")
        assert sink.health()["connected"] is False
    finally:
        sink.close()
