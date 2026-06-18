import json

import pytest

from herdeck.bridge import (
    handle_client_message, StubHerdr, _wire_panes, _is_agent_pane,
    _herdr_pane_to_wire,
)


def raw_pane(pane_id="w1:p1", agent="claude", status="blocked",
             cwd="/Users/admin/projects/api"):
    """A raw herdr pane (as returned by pane.list)."""
    p = {"pane_id": pane_id, "workspace_id": "w1", "cwd": cwd,
         "foreground_cwd": cwd, "agent_status": status}
    if agent is not None:
        p["agent"] = agent
    return p


@pytest.fixture
def herdr():
    return StubHerdr(panes=[raw_pane()])


# --- herdr -> wire mapping ---

def test_herdr_pane_to_wire_maps_fields():
    w = _herdr_pane_to_wire(raw_pane(agent="codex", status="working",
                                     cwd="/Users/admin/projects/web"))
    assert w == {"pane_id": "w1:p1", "agent_type": "codex", "label": "web",
                 "status": "working", "project": "web"}


def test_is_agent_pane_filters_plain_shells():
    assert _is_agent_pane(raw_pane()) is True
    # a plain shell pane: no agent, status unknown -> excluded
    shell = {"pane_id": "w9:p1", "agent_status": "unknown", "cwd": "/x"}
    assert _is_agent_pane(shell) is False


def test_wire_panes_filters_and_maps():
    raw = [raw_pane("w1:p1", agent="claude", status="blocked"),
           {"pane_id": "w9:p1", "agent_status": "unknown", "cwd": "/x"}]
    out = _wire_panes(raw)
    assert [p["pane_id"] for p in out] == ["w1:p1"]
    assert out[0]["agent_type"] == "claude"


# --- client message handling ---

async def test_list_returns_mapped_filtered_snapshot(herdr):
    out = await handle_client_message(herdr, "workbox", '{"type":"list"}')
    msg = json.loads(out)
    assert msg["type"] == "snapshot"
    assert msg["server_id"] == "workbox"
    p = msg["panes"][0]
    assert p["pane_id"] == "w1:p1"
    assert p["agent_type"] == "claude"      # mapped from herdr "agent"
    assert p["status"] == "blocked"         # mapped from "agent_status"
    assert p["label"] == "api"              # derived from cwd


async def test_read_returns_result(herdr):
    herdr.detection["w1:p1"] = "Allow edit to config.py?"
    out = await handle_client_message(
        herdr, "workbox",
        '{"type":"read","req":"r1","pane_id":"w1:p1","source":"detection"}')
    msg = json.loads(out)
    assert msg["type"] == "result"
    assert msg["req"] == "r1"
    assert msg["data"]["text"] == "Allow edit to config.py?"
    assert msg["data"]["pane_id"] == "w1:p1"


async def test_act_sends_keys_when_blocked(herdr):
    out = await handle_client_message(
        herdr, "workbox",
        '{"type":"act","req":"r2","pane_id":"w1:p1","keys":["1","enter"]}')
    msg = json.loads(out)
    assert msg["type"] == "result"
    assert herdr.sent == [("w1:p1", ["1", "enter"])]


async def test_act_skipped_when_not_blocked(herdr):
    herdr.panes[0]["agent_status"] = "working"
    out = await handle_client_message(
        herdr, "workbox",
        '{"type":"act","req":"r3","pane_id":"w1:p1","keys":["1"]}')
    msg = json.loads(out)
    assert msg["data"]["skipped"] is True
    assert herdr.sent == []


# --- broadcast fan-out (snapshots) ---

async def test_broadcast_fans_out_snapshots_to_all_clients():
    from herdeck.bridge import _broadcast
    sent_a, sent_b = [], []

    class FakeWS:
        def __init__(self, log): self._log = log
        async def send(self, msg): self._log.append(msg)

    clients = {FakeWS(sent_a), FakeWS(sent_b)}

    async def stream():
        yield [{"pane_id": "w1:p1", "agent_type": "claude", "label": "api",
                "status": "blocked", "project": "api"}]

    await _broadcast(stream(), clients, "workbox")
    assert len(sent_a) == 1 and len(sent_b) == 1
    msg = json.loads(sent_a[0])
    assert msg["type"] == "snapshot"
    assert msg["server_id"] == "workbox"
    assert msg["panes"][0]["pane_id"] == "w1:p1"


async def test_broadcast_survives_a_dead_client():
    from herdeck.bridge import _broadcast
    good = []

    class GoodWS:
        async def send(self, msg): good.append(msg)

    class DeadWS:
        async def send(self, msg): raise RuntimeError("closed")

    clients = {GoodWS(), DeadWS()}

    async def stream():
        yield [{"pane_id": "p", "status": "working"}]

    await _broadcast(stream(), clients, "s")   # must not raise
    assert len(good) == 1


# --- poll-diff snapshot source ---

async def test_herdr_events_yields_full_list_on_change_and_removal():
    from herdeck.bridge import HerdrEvents

    seq = [
        [raw_pane("w1:p1", agent="claude", status="idle"),
         raw_pane("w1:p2", agent="codex", status="working")],
        [raw_pane("w1:p1", agent="claude", status="blocked"),
         raw_pane("w1:p2", agent="codex", status="working")],   # p1 changed
        [raw_pane("w1:p1", agent="claude", status="blocked")],   # p2 removed
    ]

    class FakeHerdr:
        def __init__(self): self.calls = 0
        async def list_panes(self):
            i = min(self.calls, len(seq) - 1)
            self.calls += 1
            return seq[i]

    gen = HerdrEvents(FakeHerdr(), poll_interval=0).stream()
    first = await gen.__anext__()
    assert {p["pane_id"] for p in first} == {"w1:p1", "w1:p2"}
    second = await gen.__anext__()
    assert [p for p in second if p["pane_id"] == "w1:p1"][0]["status"] == "blocked"
    third = await gen.__anext__()
    assert {p["pane_id"] for p in third} == {"w1:p1"}   # removal reflected
    await gen.aclose()


# --- rpc retry semantics ---

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
