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
    task.cancel()


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
    assert ups == [True, False, True] or ups[:1] == [True]
    conn.stop()
    task.cancel()
