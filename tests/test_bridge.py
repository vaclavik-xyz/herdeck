import json

import pytest

from herdeck.bridge import handle_client_message, StubHerdr


@pytest.fixture
def herdr():
    return StubHerdr(panes=[
        {"pane_id": "w1:p1", "agent_type": "claude", "label": "api",
         "status": "blocked", "project": "api"},
    ])


async def test_list_returns_snapshot(herdr):
    out = await handle_client_message(herdr, "workbox", '{"type":"list"}')
    msg = json.loads(out)
    assert msg["type"] == "snapshot"
    assert msg["server_id"] == "workbox"
    assert msg["panes"][0]["pane_id"] == "w1:p1"


async def test_read_returns_result(herdr):
    herdr.detection["w1:p1"] = "Allow edit to config.py?"
    out = await handle_client_message(
        herdr, "workbox",
        '{"type":"read","req":"r1","pane_id":"w1:p1","source":"detection"}')
    msg = json.loads(out)
    assert msg["type"] == "result"
    assert msg["req"] == "r1"
    assert msg["data"]["text"] == "Allow edit to config.py?"


async def test_act_if_blocked_sends_keys_when_blocked(herdr):
    out = await handle_client_message(
        herdr, "workbox",
        '{"type":"act","req":"r2","pane_id":"w1:p1","keys":["1","enter"]}')
    msg = json.loads(out)
    assert msg["type"] == "result"
    assert herdr.sent == [("w1:p1", ["1", "enter"])]


async def test_act_skipped_when_not_blocked(herdr):
    herdr.panes[0]["status"] = "working"
    out = await handle_client_message(
        herdr, "workbox",
        '{"type":"act","req":"r3","pane_id":"w1:p1","keys":["1"]}')
    msg = json.loads(out)
    assert msg["data"]["skipped"] is True
    assert herdr.sent == []


async def test_broadcast_fans_out_events_to_all_clients():
    from herdeck.bridge import _broadcast

    sent_a, sent_b = [], []

    class FakeWS:
        def __init__(self, log): self._log = log
        async def send(self, msg): self._log.append(msg)

    clients = {FakeWS(sent_a), FakeWS(sent_b)}

    async def stream():
        yield {"pane_id": "w1:p1", "agent_type": "claude", "label": "api",
               "status": "blocked", "project": "api"}

    await _broadcast(stream(), clients, "workbox")
    assert len(sent_a) == 1 and len(sent_b) == 1
    import json as _json
    msg = _json.loads(sent_a[0])
    assert msg["type"] == "event"
    assert msg["server_id"] == "workbox"
    assert msg["pane"]["pane_id"] == "w1:p1"


async def test_broadcast_survives_a_dead_client():
    from herdeck.bridge import _broadcast

    good = []

    class GoodWS:
        async def send(self, msg): good.append(msg)

    class DeadWS:
        async def send(self, msg): raise RuntimeError("closed")

    clients = {GoodWS(), DeadWS()}

    async def stream():
        yield {"pane_id": "p", "status": "working"}

    await _broadcast(stream(), clients, "s")   # must not raise
    assert len(good) == 1


def test_event_to_pane_extracts_fields():
    from herdeck.bridge import _event_to_pane
    pane = _event_to_pane({"pane": {"pane_id": "w1:p2", "agent_type": "codex",
                                    "status": "working"}})
    assert pane == {"pane_id": "w1:p2", "agent_type": "codex", "label": "",
                    "status": "working", "project": ""}
    assert _event_to_pane({"params": {}}) is None


async def test_rpc_retries_reads_but_not_send_keys(monkeypatch):
    import asyncio as _asyncio
    from herdeck.bridge import SocketHerdr

    writes = []

    class FakeWriter:
        def write(self, data): writes.append(data)
        async def drain(self): pass
        def is_closing(self): return False
        def close(self): pass

    class FakeReader:
        async def readline(self): return b""   # always EOF -> triggers retry path

    async def fake_conn(path):
        return FakeReader(), FakeWriter()

    monkeypatch.setattr(_asyncio, "open_unix_connection", fake_conn)

    h = SocketHerdr("/nonexistent.sock")

    writes.clear()
    with pytest.raises(Exception):
        await h.get_pane("w1:p1")          # idempotent -> retried
    assert len(writes) == 2

    writes.clear()
    with pytest.raises(Exception):
        await h.send_keys("w1:p1", ["1"])  # non-idempotent -> NOT retried
    assert len(writes) == 1
