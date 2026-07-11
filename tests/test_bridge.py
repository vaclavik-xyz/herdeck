import json

import pytest

from herdeck.bridge import (
    StubHerdr,
    _herdr_pane_to_wire,
    _is_agent_pane,
    _tabs_by_id,
    _wire_panes,
    _workspaces_by_id,
    _worktrees_by_workspace,
    handle_client_message,
)


def raw_pane(pane_id="w1:p1", agent="claude", status="blocked", cwd="/home/user/projects/api"):
    """A raw herdr pane (as returned by pane.list)."""
    p = {
        "pane_id": pane_id,
        "workspace_id": "w1",
        "cwd": cwd,
        "foreground_cwd": cwd,
        "agent_status": status,
    }
    if agent is not None:
        p["agent"] = agent
    return p


@pytest.fixture
def herdr():
    return StubHerdr(panes=[raw_pane()])


# --- herdr -> wire mapping ---


def test_herdr_pane_to_wire_maps_fields():
    w = _herdr_pane_to_wire(
        raw_pane(agent="codex", status="working", cwd="/home/user/projects/web")
    )
    assert w == {
        "pane_id": "w1:p1",
        "agent_type": "codex",
        "label": "web",
        "status": "working",
        "project": "web",
        "repo": "web",
        "branch": "",
        "workspace": "",
        "tab": "",
        "custom_status": "",
    }


def test_herdr_pane_to_wire_passes_custom_status_through():
    # herdwatch holds panes via `herdr pane report-agent --custom-status`;
    # the label must reach clients so they can derive the WAITING state.
    raw = raw_pane(agent="claude", status="working", cwd="/x/api")
    raw["custom_status"] = "\u23f3 ci"
    assert _herdr_pane_to_wire(raw)["custom_status"] == "\u23f3 ci"


def test_herdr_pane_to_wire_adds_repo_and_branch_from_worktree():
    raw = raw_pane(agent="claude", status="idle", cwd="/x/macdoktor-crm")
    raw["workspace_id"] = "w1"
    wt_by_ws = _worktrees_by_workspace(
        [{"open_workspace_id": "w1", "label": "macdoktor-crm", "branch": "feat/x"}]
    )
    w = _herdr_pane_to_wire(raw, wt_by_ws)
    assert w["repo"] == "macdoktor-crm" and w["branch"] == "feat/x"


def test_is_agent_pane_filters_plain_shells():
    assert _is_agent_pane(raw_pane()) is True
    # a plain shell pane: no agent, status unknown -> excluded
    shell = {"pane_id": "w9:p1", "agent_status": "unknown", "cwd": "/x"}
    assert _is_agent_pane(shell) is False


def test_wire_panes_filters_and_maps():
    raw = [
        raw_pane("w1:p1", agent="claude", status="blocked"),
        {"pane_id": "w9:p1", "agent_status": "unknown", "cwd": "/x"},
    ]
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
    assert p["agent_type"] == "claude"  # mapped from herdr "agent"
    assert p["status"] == "blocked"  # mapped from "agent_status"
    assert p["label"] == "api"  # derived from cwd


async def test_read_returns_result(herdr):
    herdr.detection["w1:p1"] = "Allow edit to config.py?"
    out = await handle_client_message(
        herdr, "workbox", '{"type":"read","req":"r1","pane_id":"w1:p1","source":"detection"}'
    )
    msg = json.loads(out)
    assert msg["type"] == "result"
    assert msg["req"] == "r1"
    assert msg["data"]["text"] == "Allow edit to config.py?"
    assert msg["data"]["pane_id"] == "w1:p1"


async def test_act_sends_keys_when_blocked(herdr):
    out = await handle_client_message(
        herdr, "workbox", '{"type":"act","req":"r2","pane_id":"w1:p1","keys":["1","enter"]}'
    )
    msg = json.loads(out)
    assert msg["type"] == "result"
    assert herdr.sent == [("w1:p1", ["1", "enter"])]


async def test_act_skipped_when_not_blocked(herdr):
    herdr.panes[0]["agent_status"] = "working"
    out = await handle_client_message(
        herdr, "workbox", '{"type":"act","req":"r3","pane_id":"w1:p1","keys":["1"]}'
    )
    msg = json.loads(out)
    assert msg["data"]["skipped"] is True
    assert herdr.sent == []


async def test_act_unguarded_sends_even_when_not_blocked(herdr):
    herdr.panes[0]["agent_status"] = "working"
    out = await handle_client_message(
        herdr,
        "workbox",
        '{"type":"act","req":"r9","pane_id":"w1:p1","keys":["ctrl+c"],"guard":false}',
    )
    msg = json.loads(out)
    assert msg["data"]["sent"] is True
    assert herdr.sent == [("w1:p1", ["ctrl+c"])]


# --- broadcast fan-out (snapshots) ---


async def test_broadcast_fans_out_snapshots_to_all_clients():
    from herdeck.bridge import _broadcast

    sent_a, sent_b = [], []

    class FakeWS:
        def __init__(self, log):
            self._log = log

        async def send(self, msg):
            self._log.append(msg)

    clients = {FakeWS(sent_a), FakeWS(sent_b)}

    async def stream():
        yield [
            {
                "pane_id": "w1:p1",
                "agent_type": "claude",
                "label": "api",
                "status": "blocked",
                "project": "api",
            }
        ]

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
        async def send(self, msg):
            good.append(msg)

    class DeadWS:
        async def send(self, msg):
            raise RuntimeError("closed")

    clients = {GoodWS(), DeadWS()}

    async def stream():
        yield [{"pane_id": "p", "status": "working"}]

    await _broadcast(stream(), clients, "s")  # must not raise
    assert len(good) == 1


# --- poll-diff snapshot source ---


async def test_herdr_events_yields_full_list_on_change_and_removal():
    from herdeck.bridge import HerdrEvents

    seq = [
        [
            raw_pane("w1:p1", agent="claude", status="idle"),
            raw_pane("w1:p2", agent="codex", status="working"),
        ],
        [
            raw_pane("w1:p1", agent="claude", status="blocked"),
            raw_pane("w1:p2", agent="codex", status="working"),
        ],  # p1 changed
        [raw_pane("w1:p1", agent="claude", status="blocked")],  # p2 removed
    ]

    class FakeHerdr:
        def __init__(self):
            self.calls = 0

        async def snapshot(self):
            i = min(self.calls, len(seq) - 1)
            self.calls += 1
            return {"agents": seq[i]}

        async def worktrees(self, workspace_ids=None):
            return []

    gen = HerdrEvents(FakeHerdr(), poll_interval=0).stream()
    first = await gen.__anext__()
    assert {p["pane_id"] for p in first} == {"w1:p1", "w1:p2"}
    second = await gen.__anext__()
    assert [p for p in second if p["pane_id"] == "w1:p1"][0]["status"] == "blocked"
    third = await gen.__anext__()
    assert {p["pane_id"] for p in third} == {"w1:p1"}  # removal reflected
    await gen.aclose()


# --- rpc retry semantics ---


async def test_rpc_retries_reads_but_not_send_keys(monkeypatch):
    import asyncio as _asyncio

    from herdeck.bridge import SocketHerdr

    writes = []

    class FakeWriter:
        def write(self, data):
            writes.append(data)

        async def drain(self):
            pass

        def is_closing(self):
            return False

        def close(self):
            pass

    class FakeReader:
        async def readline(self):
            return b""  # always EOF -> triggers retry path

    async def fake_conn(path):
        return FakeReader(), FakeWriter()

    monkeypatch.setattr(_asyncio, "open_unix_connection", fake_conn)

    h = SocketHerdr("/nonexistent.sock")

    writes.clear()
    with pytest.raises(ConnectionError):
        await h.get_pane("w1:p1")  # idempotent -> retried
    assert len(writes) == 2

    writes.clear()
    with pytest.raises(ConnectionError):
        await h.send_keys("w1:p1", ["1"])  # non-idempotent -> NOT retried
    assert len(writes) == 1


async def test_rpc_error_envelope_raises(monkeypatch):
    import asyncio as _asyncio

    from herdeck.bridge import SocketHerdr

    class FakeWriter:
        def write(self, data):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    class FakeReader:
        async def readline(self):
            return b'{"id":"b","error":{"message":"pane not found"}}\n'

    async def fake_conn(path):
        return FakeReader(), FakeWriter()

    monkeypatch.setattr(_asyncio, "open_unix_connection", fake_conn)

    h = SocketHerdr("/tmp/herdr.sock")
    with pytest.raises(RuntimeError, match="pane not found"):
        await h.read_pane("w1:p1", "detection")


def test_bridge_main_rejects_empty_token(monkeypatch):
    from herdeck import bridge

    async def should_not_start(*args, **kwargs):
        raise AssertionError("serve should not start with empty token")

    monkeypatch.setenv("HERDR_SOCKET", "/tmp/herdr.sock")
    monkeypatch.setenv("HERDECK_TOKEN", "   ")
    monkeypatch.setattr(bridge, "serve", should_not_start)

    with pytest.raises(SystemExit, match="HERDECK_TOKEN must not be empty"):
        bridge.main()


async def test_focus_calls_herdr_focus_agent(herdr):
    out = await handle_client_message(
        herdr, "workbox", '{"type":"focus","req":"f1","pane_id":"w1:p1"}'
    )
    msg = json.loads(out)
    assert msg["data"]["focused"] is True
    assert herdr.focused == ["w1:p1"]


async def test_push_event_wakes_stream_before_poll():
    import asyncio

    from herdeck.bridge import HerdrEvents

    seq = [
        [raw_pane("w1:p1", agent="claude", status="idle")],
        [raw_pane("w1:p1", agent="claude", status="blocked")],
    ]

    class FakeHerdr:
        def __init__(self):
            self.calls = 0

        async def snapshot(self):
            i = min(self.calls, len(seq) - 1)
            self.calls += 1
            return {"agents": seq[i]}

        async def worktrees(self, workspace_ids=None):
            return []

    ev = HerdrEvents(FakeHerdr(), poll_interval=100)  # would block without a wake
    gen = ev.stream()
    first = await gen.__anext__()
    assert first[0]["status"] == "idle"
    ev._wake.set()  # simulate a herdr push event
    second = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert second[0]["status"] == "blocked"
    await gen.aclose()


async def test_send_text_calls_herdr(herdr):
    out = await handle_client_message(
        herdr, "workbox", '{"type":"send_text","req":"s1","pane_id":"w1:p1","text":"continue"}'
    )
    msg = json.loads(out)
    assert msg["data"]["sent"] is True
    assert herdr.sent == [("w1:p1", "continue")]


async def test_start_calls_herdr(herdr):
    out = await handle_client_message(
        herdr, "workbox", '{"type":"start","req":"n1","name":"claude","argv":["claude"]}'
    )
    msg = json.loads(out)
    assert msg["data"]["started"] is True
    assert herdr.started == [("claude", ["claude"])]


def test_herdr_pane_to_wire_adds_workspace_and_tab_labels():
    raw = raw_pane(agent="claude", status="working")
    raw["workspace_id"] = "w2"
    raw["tab_id"] = "w2:t3"
    ws_by_id = _workspaces_by_id([{"workspace_id": "w2", "label": "herdeck"}])
    tab_by_id = _tabs_by_id([{"tab_id": "w2:t3", "label": "2"}])
    w = _herdr_pane_to_wire(raw, None, ws_by_id, tab_by_id)
    assert w["workspace"] == "herdeck"
    assert w["tab"] == "2"


def test_herdr_pane_to_wire_blank_workspace_tab_when_lookup_missing():
    raw = raw_pane(agent="claude", status="idle")
    raw["workspace_id"] = "w9"
    raw["tab_id"] = "w9:t1"
    w = _herdr_pane_to_wire(raw, None, {}, {})
    # never fall back to the raw id
    assert w["workspace"] == ""
    assert w["tab"] == ""


def test_workspaces_and_tabs_by_id_index_label():
    assert _workspaces_by_id([{"workspace_id": "w2", "label": "herdeck"}]) == {"w2": "herdeck"}
    assert _tabs_by_id([{"tab_id": "w2:t1", "label": "1"}]) == {"w2:t1": "1"}
    # entries without an id are skipped
    assert _workspaces_by_id([{"label": "x"}]) == {}
    assert _tabs_by_id([{"label": "x"}]) == {}


async def test_list_snapshot_includes_workspace_and_tab():
    panes = [{
        "pane_id": "w2:p1", "workspace_id": "w2", "tab_id": "w2:t1",
        "cwd": "/home/user/projects/herdeck",
        "foreground_cwd": "/home/user/projects/herdeck",
        "agent": "claude", "agent_status": "blocked",
    }]
    herdr = StubHerdr(
        panes=panes,
        workspaces=[{"workspace_id": "w2", "label": "herdeck"}],
        tabs=[{"tab_id": "w2:t1", "label": "1"}],
    )
    out = await handle_client_message(herdr, "local", '{"type":"list"}')
    p = json.loads(out)["panes"][0]
    assert p["workspace"] == "herdeck"
    assert p["tab"] == "1"


# --- snapshot RPC concurrency + label caching (audit: bridge-parallel-rpcs) --


async def test_wired_snapshot_reads_labels_from_snapshot():
    pane = raw_pane("w2:p1", agent="claude", status="blocked")
    pane["workspace_id"] = "w2"
    pane["tab_id"] = "w2:t1"
    stub = StubHerdr(
        panes=[pane],
        workspaces=[{"workspace_id": "w2", "label": "herdeck"}],
        tabs=[{"tab_id": "w2:t1", "label": "1"}],
    )
    from herdeck.bridge import _wired_snapshot

    out = await _wired_snapshot(stub)
    assert out[0]["workspace"] == "herdeck"
    assert out[0]["tab"] == "1"


async def test_wired_snapshot_keeps_agent_pane_without_agent_key():
    # herdr's agents list should already be agent-only; the _is_agent_pane belt
    # must keep a row that carries only a live agent_status.
    pane = {"pane_id": "w1:p9", "workspace_id": "w1", "cwd": "/x/api",
            "foreground_cwd": "/x/api", "agent_status": "working"}
    stub = StubHerdr(panes=[pane])
    from herdeck.bridge import _wired_snapshot

    out = await _wired_snapshot(stub)
    assert [p["pane_id"] for p in out] == ["w1:p9"]


async def test_fetch_worktrees_degrades_to_empty_on_error():
    from herdeck.bridge import _fetch_worktrees

    class BrokenHerdr:
        async def worktrees(self, workspace_ids=None):
            raise RuntimeError("worktree.list failed")

    assert await _fetch_worktrees(BrokenHerdr(), ["w1"]) == []


async def test_stream_caches_worktrees_across_status_wakes():
    import asyncio

    from herdeck.bridge import HerdrEvents

    class CountingHerdr:
        def __init__(self):
            self.panes = [raw_pane("w1:p1", agent="claude", status="idle")]
            self.worktree_calls = 0

        async def snapshot(self):
            return {"agents": self.panes}

        async def worktrees(self, workspace_ids=None):
            self.worktree_calls += 1
            return []

    stub = CountingHerdr()
    ev = HerdrEvents(stub, poll_interval=100)  # would block without a wake
    gen = ev.stream()
    await gen.__anext__()
    assert stub.worktree_calls == 1
    # a status-change push wake must NOT refetch the worktree list
    stub.panes = [raw_pane("w1:p1", agent="claude", status="blocked")]
    ev._wake.set()
    await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert stub.worktree_calls == 1
    # a fleet event (what _listen flags) marks worktrees stale -> refetched
    stub.panes = stub.panes + [raw_pane("w1:p2", agent="codex", status="idle")]
    ev._worktrees_stale = True
    ev._wake.set()
    await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert stub.worktree_calls == 2
    await gen.aclose()


async def test_stream_refreshes_worktrees_by_age_when_wakes_starve_the_poll(monkeypatch):
    """Constant status wakes keep the slow-poll timeout from firing; the cached
    worktrees must still refresh once older than poll_interval (a branch switch
    inside an existing worktree emits no event)."""
    import asyncio

    from herdeck import bridge as bridge_mod
    from herdeck.bridge import HerdrEvents

    t = [0.0]
    monkeypatch.setattr(bridge_mod, "_monotonic", lambda: t[0])

    class CountingHerdr:
        def __init__(self):
            self.panes = [raw_pane("w1:p1", agent="claude", status="idle")]
            self.worktree_calls = 0

        async def snapshot(self):
            return {"agents": self.panes}

        async def worktrees(self, workspace_ids=None):
            self.worktree_calls += 1
            return []

    stub = CountingHerdr()
    ev = HerdrEvents(stub, poll_interval=5.0)
    gen = ev.stream()
    await gen.__anext__()  # worktrees fetched at t=0
    assert stub.worktree_calls == 1
    stub.panes = [raw_pane("w1:p1", agent="claude", status="blocked")]
    t[0] = 1.0
    ev._wake.set()  # young cache + status wake -> no refetch
    await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert stub.worktree_calls == 1
    stub.panes = [raw_pane("w1:p1", agent="claude", status="working")]
    t[0] = 6.0  # cache older than poll_interval; wake is still a status event
    ev._wake.set()
    await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert stub.worktree_calls == 2
    await gen.aclose()


async def test_broadcast_stalled_client_does_not_starve_others():
    """One backpressured client must not delay snapshots for everyone else
    (audit: bridge-broadcast-isolate)."""
    import asyncio

    from herdeck.bridge import _broadcast

    got = []

    class FastWs:
        async def send(self, msg):
            got.append(msg)

    class StalledWs:
        def __init__(self):
            self.closed = False

        async def send(self, msg):
            await asyncio.sleep(3600)  # full TCP buffer: send never returns

        async def close(self, code=None, reason=None):
            self.closed = True

    async def stream():
        yield [{"pane_id": "p1"}]
        yield [{"pane_id": "p2"}]

    stalled = StalledWs()
    clients = {FastWs(), stalled}
    # patch the per-client timeout small for the test
    import herdeck.bridge as bridge_mod

    orig = bridge_mod._BROADCAST_SEND_TIMEOUT
    bridge_mod._BROADCAST_SEND_TIMEOUT = 0.05
    try:
        await asyncio.wait_for(_broadcast(stream(), clients, "s"), timeout=5.0)
    finally:
        bridge_mod._BROADCAST_SEND_TIMEOUT = orig
    assert len(got) == 2  # the healthy client received every snapshot
    assert stalled.closed  # the stalled one was dropped to reconnect cleanly


async def test_wired_snapshot_queries_worktrees_for_agent_workspaces():
    from herdeck.bridge import StubHerdr, _wired_snapshot

    pane_a = raw_pane("wA:p1", agent="claude", status="working")
    pane_a["workspace_id"] = "wA"
    pane_b = raw_pane("wB:p1", agent="codex", status="idle")
    pane_b["workspace_id"] = "wB"
    stub = StubHerdr(
        [pane_a, pane_b],
        worktrees=[
            {"open_workspace_id": "wA", "label": "repo-a", "branch": "main"},
            {"open_workspace_id": "wB", "label": "repo-b", "branch": "fix/x"},
        ],
    )
    panes = await _wired_snapshot(stub)
    # worktrees were asked about exactly the agent workspaces (sorted, unique)
    assert stub.worktree_queries == [["wA", "wB"]]
    by_id = {p["pane_id"]: p for p in panes}
    assert by_id["wA:p1"]["branch"] == "main"
    assert by_id["wB:p1"]["branch"] == "fix/x"


async def test_socket_herdr_merges_per_workspace_worktrees():
    from herdeck.bridge import SocketHerdr

    herdr = SocketHerdr("/nonexistent")
    calls = []

    async def fake_rpc(method, params, *, retry=True):
        calls.append((method, params))
        ws = params.get("workspace_id")
        # the same repo open in two workspaces returns the same worktree rows
        shared = {"path": "/r/a", "open_workspace_id": "wA", "branch": "main"}
        per_ws = {
            "wA": [shared],
            "wB": [shared, {"path": "/r/b", "open_workspace_id": "wB", "branch": "dev"}],
        }
        return {"result": {"worktrees": per_ws.get(ws, [])}}

    herdr._rpc = fake_rpc
    merged = await herdr.worktrees(["wA", "wB"])
    assert calls == [
        ("worktree.list", {"workspace_id": "wA"}),
        ("worktree.list", {"workspace_id": "wB"}),
    ]
    assert len(merged) == 2  # the shared row is de-duplicated
    assert {w["branch"] for w in merged} == {"main", "dev"}


# --- session.snapshot client method ---


async def test_socket_herdr_snapshot_returns_snapshot():
    from herdeck.bridge import SocketHerdr

    h = SocketHerdr("/nonexistent")
    calls = []

    async def fake_rpc(method, params, *, retry=True):
        calls.append((method, params))
        return {"result": {"snapshot": {"agents": [], "workspaces": [], "tabs": []}}}

    h._rpc = fake_rpc
    snap = await h.snapshot()
    assert calls == [("session.snapshot", {})]
    assert snap == {"agents": [], "workspaces": [], "tabs": []}


async def test_socket_herdr_snapshot_maps_unknown_method_to_version_hint():
    from herdeck.bridge import SocketHerdr

    h = SocketHerdr("/nonexistent")

    async def fake_rpc(method, params, *, retry=True):
        raise RuntimeError(
            "herdr RPC session.snapshot failed: invalid request: "
            "unknown variant `session.snapshot`, expected one of `ping`, `server.stop`"
        )

    h._rpc = fake_rpc
    with pytest.raises(RuntimeError, match="herdr >= 0.7.2"):
        await h.snapshot()


async def test_stub_herdr_snapshot_composes_lists():
    stub = StubHerdr(
        panes=[raw_pane()],
        workspaces=[{"workspace_id": "w1", "label": "api"}],
        tabs=[{"tab_id": "w1:t1", "label": "1"}],
    )
    snap = await stub.snapshot()
    assert snap["agents"][0]["pane_id"] == "w1:p1"
    assert snap["workspaces"] == [{"workspace_id": "w1", "label": "api"}]
    assert snap["tabs"] == [{"tab_id": "w1:t1", "label": "1"}]


# --- push-event digestion (labels ride the snapshot; worktrees need staling) ---


def test_note_event_stales_worktrees_and_flags_fleet_changes():
    from herdeck.bridge import HerdrEvents

    ev = HerdrEvents(StubHerdr(panes=[]), poll_interval=100)
    ev._worktrees_stale = False
    # label renames ride the next snapshot: wake-only, no staling, no re-subscribe
    assert ev._note_event("tab_renamed") is False
    assert ev._note_event("workspace_renamed") is False
    assert ev._note_event("workspace_updated") is False
    assert ev._worktrees_stale is False
    # worktree membership changed: branch labels must refetch
    assert ev._note_event("worktree_opened") is False
    assert ev._worktrees_stale is True
    ev._worktrees_stale = False
    assert ev._note_event("worktree_removed") is False
    assert ev._worktrees_stale is True
    # fleet change: re-subscribe per-pane status subs AND refetch worktrees
    ev._worktrees_stale = False
    assert ev._note_event("pane_created") is True
    assert ev._worktrees_stale is True
    # unknown/None events are inert
    ev._worktrees_stale = False
    assert ev._note_event(None) is False
    assert ev._worktrees_stale is False


async def test_tab_rename_wake_delivers_fresh_labels_without_worktree_refetch():
    import asyncio

    from herdeck.bridge import HerdrEvents

    pane = raw_pane("w1:p1", agent="claude", status="idle")
    pane["tab_id"] = "w1:t1"
    stub = StubHerdr(panes=[pane], tabs=[{"tab_id": "w1:t1", "label": "1"}])
    ev = HerdrEvents(stub, poll_interval=100)
    gen = ev.stream()
    first = await gen.__anext__()
    assert first[0]["tab"] == "1"
    stub._tabs = [{"tab_id": "w1:t1", "label": "review"}]  # herdr-side rename
    ev._wake.set()  # what a tab.renamed push event does
    second = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert second[0]["tab"] == "review"
    assert stub.worktree_queries == [["w1"]]  # worktrees were NOT refetched
    await gen.aclose()


def test_listen_subscribes_to_label_and_worktree_events():
    from herdeck.bridge import _GLOBAL_EVENT_TYPES, _LABEL_EVENT_TYPES

    assert set(_LABEL_EVENT_TYPES) == {
        "tab.renamed", "workspace.renamed", "workspace.updated",
        "worktree.created", "worktree.opened", "worktree.removed",
    }
    assert not set(_LABEL_EVENT_TYPES) & set(_GLOBAL_EVENT_TYPES)
