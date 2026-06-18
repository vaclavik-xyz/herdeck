import asyncio

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
    yield {"port": port, "received": received, "connections": connections,
           "server": server}
    server.close()
    await server.wait_closed()


async def test_connector_sends_list_on_connect(ws_server):
    cfg = ServerConfig("workbox", f"ws://127.0.0.1:{ws_server['port']}", "tok")
    snaps = []
    conn = Connector(cfg, on_snapshot=lambda sid, st: snaps.append(sid),
                     on_event=lambda sid, s: None,
                     on_connection=lambda sid, up: None)
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
    conn = Connector(cfg, on_snapshot=lambda sid, st: None,
                     on_event=lambda sid, s: None,
                     on_connection=lambda sid, up: ups.append(up),
                     backoff_base=0.05)
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
    assert after > before          # resync happened
    assert ups[:3] == [True, False, True]
    conn.stop()
    await asyncio.wait_for(task, timeout=2.0)


async def test_stop_terminates_run_without_cancel(ws_server):
    cfg = ServerConfig("workbox", f"ws://127.0.0.1:{ws_server['port']}", "tok")
    conn = Connector(cfg, on_snapshot=lambda sid, st: None,
                     on_event=lambda sid, s: None,
                     on_connection=lambda sid, up: None)
    task = asyncio.create_task(conn.run())
    for _ in range(50):
        if ws_server["connections"]:
            break
        await asyncio.sleep(0.02)
    conn.stop()
    await asyncio.wait_for(task, timeout=2.0)   # must return on its own
    assert task.done()


async def test_error_frame_goes_to_on_error(ws_server):
    # server that sends one error frame then idles
    errors = []
    cfg = ServerConfig("workbox", f"ws://127.0.0.1:{ws_server['port']}", "tok")
    conn = Connector(cfg, on_snapshot=lambda sid, st: None,
                     on_event=lambda sid, s: None,
                     on_connection=lambda sid, up: None,
                     on_error=errors.append)
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


async def test_stop_during_backoff_returns_quickly():
    cfg = ServerConfig("x", "ws://127.0.0.1:9", "t")   # port 9: connection refused
    conn = Connector(cfg, on_snapshot=lambda sid, st: None,
                     on_event=lambda sid, s: None,
                     on_connection=lambda sid, up: None,
                     backoff_base=10.0)                  # would sleep 10s if not interrupted
    task = asyncio.create_task(conn.run())
    await asyncio.sleep(0.2)        # let one connect attempt fail -> enter backoff
    conn.stop()
    await asyncio.wait_for(task, timeout=2.0)   # must return well under the 10s backoff
    assert task.done()


async def test_result_frame_goes_to_on_result(ws_server):
    results = []
    cfg = ServerConfig("workbox", f"ws://127.0.0.1:{ws_server['port']}", "tok")
    conn = Connector(cfg, on_snapshot=lambda sid, st: None,
                     on_event=lambda sid, s: None,
                     on_connection=lambda sid, up: None,
                     on_result=lambda req, data: results.append((req, data)))
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
    raw = ('{"type":"snapshot","server_id":"some-bridge-id","panes":'
           '[{"pane_id":"w1:p1","agent_type":"claude","label":"api",'
           '"status":"blocked","project":"api"}]}')
    conn._dispatch(raw)
    assert seen["sid"] == "dev"
    assert seen["states"][0].key.server_id == "dev"
    assert seen["states"][0].agent_type == "claude"
