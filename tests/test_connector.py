import asyncio
import json

import pytest
import websockets

from herdeck.config import ServerConfig
from herdeck.connector import Connector


@pytest.fixture
async def ws_server():
    received = []
    connections = []

    async def handler(ws):
        connections.append(ws)
        async for msg in ws:
            received.append(msg)

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    yield {"port": port, "received": received, "connections": connections, "server": server}
    server.close()
    await server.wait_closed()


async def test_connector_sends_list_on_connect(ws_server):
    cfg = ServerConfig("workbox", f"ws://127.0.0.1:{ws_server['port']}", "tok")
    snaps = []
    conn = Connector(
        cfg,
        on_snapshot=lambda sid, st: snaps.append(sid),
        on_event=lambda sid, s: None,
        on_connection=lambda sid, up: None,
    )
    task = asyncio.create_task(conn.run())
    # wait until server received the initial "list"
    for _ in range(50):
        if ws_server["received"]:
            break
        await asyncio.sleep(0.02)
    assert any('"list"' in m for m in ws_server["received"])
    conn.stop()
    await asyncio.wait_for(task, timeout=2.0)


async def test_connector_resyncs_after_drop(ws_server):
    cfg = ServerConfig("workbox", f"ws://127.0.0.1:{ws_server['port']}", "tok")
    ups = []
    conn = Connector(
        cfg,
        on_snapshot=lambda sid, st: None,
        on_event=lambda sid, s: None,
        on_connection=lambda sid, up: ups.append(up),
        backoff_base=0.05,
    )
    task = asyncio.create_task(conn.run())
    for _ in range(50):
        if ws_server["connections"]:
            break
        await asyncio.sleep(0.02)
    # drop the first connection -> connector must reconnect and re-send list
    await ws_server["connections"][0].close()
    before = len([m for m in ws_server["received"] if '"list"' in m])
    for _ in range(100):
        after = len([m for m in ws_server["received"] if '"list"' in m])
        if after > before:
            break
        await asyncio.sleep(0.02)
    assert after > before  # resync happened
    assert ups[:3] == [True, False, True]
    conn.stop()
    await asyncio.wait_for(task, timeout=2.0)


async def test_stop_terminates_run_without_cancel(ws_server):
    cfg = ServerConfig("workbox", f"ws://127.0.0.1:{ws_server['port']}", "tok")
    conn = Connector(
        cfg,
        on_snapshot=lambda sid, st: None,
        on_event=lambda sid, s: None,
        on_connection=lambda sid, up: None,
    )
    task = asyncio.create_task(conn.run())
    for _ in range(50):
        if ws_server["connections"]:
            break
        await asyncio.sleep(0.02)
    conn.stop()
    await asyncio.wait_for(task, timeout=2.0)  # must return on its own
    assert task.done()


async def test_stop_during_handshake_exits_without_connecting():
    handshake_started = asyncio.Event()
    release_handshake = asyncio.Event()
    received = []
    connection_states = []

    async def process_request(connection, request):
        handshake_started.set()
        await release_handshake.wait()

    async def handler(ws):
        async for msg in ws:
            received.append(msg)

    server = await websockets.serve(
        handler,
        "127.0.0.1",
        0,
        process_request=process_request,
    )
    port = server.sockets[0].getsockname()[1]
    conn = Connector(
        ServerConfig("workbox", f"ws://127.0.0.1:{port}", "tok"),
        on_snapshot=lambda sid, st: None,
        on_event=lambda sid, s: None,
        on_connection=lambda sid, up: connection_states.append(up),
    )
    task = asyncio.create_task(conn.run())
    try:
        await asyncio.wait_for(handshake_started.wait(), timeout=2.0)
        conn.stop()
        release_handshake.set()
        await asyncio.wait_for(task, timeout=2.0)
        assert received == []
        assert connection_states == []
    finally:
        release_handshake.set()
        conn.stop()
        if not task.done():
            task.cancel()
        server.close()
        await server.wait_closed()


async def test_error_frame_goes_to_on_error(ws_server):
    # server that sends one error frame then idles
    errors = []
    cfg = ServerConfig("workbox", f"ws://127.0.0.1:{ws_server['port']}", "tok")
    conn = Connector(
        cfg,
        on_snapshot=lambda sid, st: None,
        on_event=lambda sid, s: None,
        on_connection=lambda sid, up: None,
        on_error=errors.append,
    )
    # patch the server connection to push an error frame
    task = asyncio.create_task(conn.run())
    for _ in range(50):
        if ws_server["connections"]:
            break
        await asyncio.sleep(0.02)
    await ws_server["connections"][0].send('{"type":"error","message":"boom"}')
    for _ in range(50):
        if errors:
            break
        await asyncio.sleep(0.02)
    assert errors == ["boom"]
    conn.stop()
    await asyncio.wait_for(task, timeout=2.0)


def test_dispatch_routes_terminal_messages_with_configured_server_id():
    from herdeck.protocol import TermClosed, TermFrame

    seen = []
    conn = Connector(
        ServerConfig("configured", "ws://example.invalid", "tok"),
        on_snapshot=lambda sid, states: None,
        on_event=lambda sid, state: None,
        on_connection=lambda sid, up: None,
        on_term=lambda sid, msg: seen.append((sid, msg)),
    )
    conn._dispatch(
        '{"type":"term_frame","req":"t1","seq":1,"full":true,"cols":80,"rows":24,"data":"QQ=="}'
    )
    conn._dispatch('{"type":"term_closed","req":"t1","reason":"done"}')
    assert seen == [
        ("configured", TermFrame("t1", 1, True, 80, 24, "QQ==")),
        ("configured", TermClosed("t1", "done")),
    ]


async def test_malformed_terminal_frame_stops_remote_and_ignores_late_frames():
    from herdeck.protocol import TermClosed

    seen = []
    sent = []
    conn = Connector(
        ServerConfig("configured", "ws://example.invalid", "tok"),
        on_snapshot=lambda sid, states: None,
        on_event=lambda sid, state: None,
        on_connection=lambda sid, up: None,
        on_term=lambda sid, msg: seen.append((sid, msg)),
    )

    async def record_send(msg):
        sent.append(msg)

    conn.send = record_send
    conn._dispatch(
        '{"type":"term_frame","req":"bad","seq":"oops","full":true,'
        '"cols":80,"rows":24,"data":"QQ=="}'
    )
    conn._dispatch(
        '{"type":"term_frame","req":"bad","seq":2,"full":false,"cols":80,"rows":24,"data":"Qg=="}'
    )
    await asyncio.sleep(0)
    assert sent == [{"type": "observe_stop", "req": "bad"}]
    assert seen == [("configured", TermClosed("bad", "invalid terminal frame"))]


async def test_stop_during_backoff_returns_quickly():
    cfg = ServerConfig("x", "ws://127.0.0.1:9", "t")  # port 9: connection refused
    conn = Connector(
        cfg,
        on_snapshot=lambda sid, st: None,
        on_event=lambda sid, s: None,
        on_connection=lambda sid, up: None,
        backoff_base=10.0,
    )  # would sleep 10s if not interrupted
    task = asyncio.create_task(conn.run())
    await asyncio.sleep(0.2)  # let one connect attempt fail -> enter backoff
    conn.stop()
    await asyncio.wait_for(task, timeout=2.0)  # must return well under the 10s backoff
    assert task.done()


async def test_result_frame_goes_to_on_result(ws_server):
    results = []
    cfg = ServerConfig("workbox", f"ws://127.0.0.1:{ws_server['port']}", "tok")
    conn = Connector(
        cfg,
        on_snapshot=lambda sid, st: None,
        on_event=lambda sid, s: None,
        on_connection=lambda sid, up: None,
        on_result=lambda req, data: results.append((req, data)),
    )
    task = asyncio.create_task(conn.run())
    for _ in range(50):
        if ws_server["connections"]:
            break
        await asyncio.sleep(0.02)
    await ws_server["connections"][0].send('{"type":"result","req":"r1","data":{"skipped":true}}')
    for _ in range(50):
        if results:
            break
        await asyncio.sleep(0.02)
    assert results == [("r1", {"skipped": True})]
    conn.stop()
    await asyncio.wait_for(task, timeout=2.0)


async def test_malformed_frame_reports_error_and_reconnects(ws_server):
    errors = []
    cfg = ServerConfig("workbox", f"ws://127.0.0.1:{ws_server['port']}", "tok")
    conn = Connector(
        cfg,
        on_snapshot=lambda sid, st: None,
        on_event=lambda sid, s: None,
        on_connection=lambda sid, up: None,
        on_error=errors.append,
        backoff_base=0.05,
    )
    task = asyncio.create_task(conn.run())
    for _ in range(50):
        if ws_server["connections"]:
            break
        await asyncio.sleep(0.02)
    await ws_server["connections"][0].send("not-json")
    for _ in range(50):
        if errors:
            break
        await asyncio.sleep(0.02)
    assert errors

    await ws_server["connections"][0].close()
    before = len([m for m in ws_server["received"] if '"list"' in m])
    for _ in range(100):
        after = len([m for m in ws_server["received"] if '"list"' in m])
        if after > before:
            break
        await asyncio.sleep(0.02)
    assert after > before
    conn.stop()
    await asyncio.wait_for(task, timeout=2.0)


def test_dispatch_rekeys_inbound_to_config_server_id():
    # bridge reports its own server_id; the connector must re-stamp inbound
    # state to the configured id so command routing stays consistent.
    cfg = ServerConfig("dev", "ws://x", "t")
    seen = {}
    conn = Connector(
        cfg,
        on_snapshot=lambda sid, st: seen.update(sid=sid, states=st),
        on_event=lambda sid, s: None,
        on_connection=lambda sid, up: None,
    )
    raw = (
        '{"type":"snapshot","server_id":"some-bridge-id","panes":'
        '[{"pane_id":"w1:p1","agent_type":"claude","label":"api",'
        '"status":"blocked","project":"api"}]}'
    )
    conn._dispatch(raw)
    assert seen["sid"] == "dev"
    assert seen["states"][0].key.server_id == "dev"
    assert seen["states"][0].agent_type == "claude"


def test_dispatch_rekey_preserves_workspace_and_tab():
    cfg = ServerConfig("dev", "ws://x", "t")
    seen = {}
    conn = Connector(
        cfg,
        on_snapshot=lambda sid, st: seen.update(states=st),
        on_event=lambda sid, s: None,
        on_connection=lambda sid, up: None,
    )
    raw = (
        '{"type":"snapshot","server_id":"some-bridge-id","panes":'
        '[{"pane_id":"w2:p1","agent_type":"claude","label":"herdeck",'
        '"status":"working","project":"herdeck","repo":"herdeck",'
        '"branch":"main","workspace":"herdeck","tab":"2"}]}'
    )
    conn._dispatch(raw)
    assert seen["states"][0].key.server_id == "dev"  # rekeyed to configured id
    assert seen["states"][0].workspace == "herdeck"  # carried through rekey
    assert seen["states"][0].tab == "2"


def test_dispatch_rekey_preserves_custom_status_and_waiting():
    # A server-id mismatch (bridge "some-bridge-id" vs config "dev") triggers
    # the rekey reconstruction; it must carry custom_status through, else a
    # herdwatch-held pane rendered WAITING with no holder label (fell back to
    # the generic word). dataclasses.replace copies every field.
    from herdeck.model import Status

    cfg = ServerConfig("dev", "ws://x", "t")
    seen = {}
    conn = Connector(
        cfg,
        on_snapshot=lambda sid, st: seen.update(states=st),
        on_event=lambda sid, s: None,
        on_connection=lambda sid, up: None,
    )
    raw = (
        '{"type":"snapshot","server_id":"some-bridge-id","panes":'
        '[{"pane_id":"w2:p1","agent_type":"claude","label":"cli",'
        '"status":"working","custom_status":"\\u23f3 ci"}]}'
    )
    conn._dispatch(raw)
    s = seen["states"][0]
    assert s.key.server_id == "dev"  # rekeyed
    assert s.status is Status.WAITING  # derived from working + custom_status
    assert s.custom_status == "⏳ ci"  # carried through rekey (was dropped)


async def test_connector_serializes_concurrent_sends_in_call_order():
    class SlowWs:
        def __init__(self):
            self.sent = []

        async def send(self, msg):
            if '"focus"' in msg:
                await asyncio.sleep(0.01)
            self.sent.append(msg)

    cfg = ServerConfig("dev", "ws://x", "t")
    conn = Connector(
        cfg,
        on_snapshot=lambda sid, st: None,
        on_event=lambda sid, s: None,
        on_connection=lambda sid, up: None,
    )
    conn._ws = SlowWs()

    await asyncio.gather(
        conn.send({"type": "focus", "req": "r1", "pane_id": "p1"}),
        conn.send({"type": "read", "req": "r2", "pane_id": "p1", "source": "detection"}),
    )

    assert [json.loads(msg)["type"] for msg in conn._ws.sent] == ["focus", "read"]


# --- connect-failure surfacing (audit: connector-error-surface) ---------------


async def test_connect_refused_sets_last_error_and_logs_once(caplog):
    import logging

    cfg = ServerConfig("workbox", "ws://127.0.0.1:1", "tok")  # nothing listens
    conn = Connector(
        cfg,
        on_snapshot=lambda sid, st: None,
        on_event=lambda sid, s: None,
        on_connection=lambda sid, up: None,
        backoff_base=0.02,
        backoff_max=0.02,
    )
    with caplog.at_level(logging.WARNING, logger="herdeck.connector"):
        task = asyncio.create_task(conn.run())
        for _ in range(100):
            if conn.last_connect_error:
                break
            await asyncio.sleep(0.02)
        # let it fail a few more times: identical reason must not re-log
        await asyncio.sleep(0.15)
        conn.stop()
        await asyncio.wait_for(task, timeout=2.0)
    assert conn.last_connect_error  # reason retained for ctl's timeout message
    warnings = [r for r in caplog.records if "workbox" in r.getMessage()]
    assert len(warnings) == 1  # once per distinct failure, no spam


async def test_bridge_4401_close_reads_as_token_rejection():
    async def reject(ws):
        await ws.close(code=4401, reason="unauthorized")

    server = await websockets.serve(reject, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    cfg = ServerConfig("workbox", f"ws://127.0.0.1:{port}", "bad")
    conn = Connector(
        cfg,
        on_snapshot=lambda sid, st: None,
        on_event=lambda sid, s: None,
        on_connection=lambda sid, up: None,
        backoff_base=0.02,
        backoff_max=0.02,
    )
    try:
        task = asyncio.create_task(conn.run())
        for _ in range(100):
            if conn.last_connect_error and "token rejected" in conn.last_connect_error:
                break
            await asyncio.sleep(0.02)
        conn.stop()
        await asyncio.wait_for(task, timeout=2.0)
        assert "token rejected" in (conn.last_connect_error or "")
    finally:
        server.close()
        await server.wait_closed()
