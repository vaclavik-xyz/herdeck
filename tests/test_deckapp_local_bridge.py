import asyncio
import functools

import websockets

from herdeck.bridge import StubHerdr, start_local_bridge
from herdeck.deckapp.local_bridge import LocalBridgeRunner


def test_runner_binds_and_serves_a_snapshot():
    herdr = StubHerdr(panes=[])
    runner = LocalBridgeRunner(
        "unused.sock", start_bridge=functools.partial(start_local_bridge, herdr=herdr)
    )
    host, port, token = runner.start()
    try:
        assert host == "127.0.0.1" and isinstance(port, int) and port > 0 and token

        async def _client():
            async with websockets.connect(
                f"ws://{host}:{port}",
                additional_headers={"Authorization": f"Bearer {token}"},
            ) as ws:
                return await asyncio.wait_for(ws.recv(), timeout=3)

        first = asyncio.run(_client())
        assert "snapshot" in first
    finally:
        runner.close()


def test_close_is_idempotent():
    herdr = StubHerdr(panes=[])
    runner = LocalBridgeRunner(
        "unused.sock", start_bridge=functools.partial(start_local_bridge, herdr=herdr)
    )
    runner.start()
    runner.close()
    runner.close()  # no error on second close
