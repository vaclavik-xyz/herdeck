import asyncio
import functools

from herdeck.bridge import StubHerdr, start_local_bridge
from herdeck.deckapp.local_bridge import LocalBridgeRunner
from herdeck.deckapp.probe import probe_server


def _probe(url, token, **kw):
    return asyncio.run(probe_server(url, token, **kw))


def test_probe_ok_and_bad_token():
    runner = LocalBridgeRunner(
        "unused.sock", start_bridge=functools.partial(start_local_bridge, herdr=StubHerdr(panes=[]))
    )
    host, port, token = runner.start()
    try:
        url = f"ws://{host}:{port}"
        ok = _probe(url, token)
        assert ok.ok and ok.reason == "ok"
        bad = _probe(url, "wrong-token")
        assert not bad.ok and bad.reason == "bad_token"
    finally:
        runner.close()


def test_probe_unreachable():
    r = _probe("ws://127.0.0.1:1", "t", timeout=0.5)  # port 1: nothing listening
    assert not r.ok and r.reason == "unreachable"
