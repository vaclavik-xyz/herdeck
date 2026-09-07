import asyncio

import pytest

from herdeck.config import Config, ServerConfig
from herdeck.model import AgentKey, AgentState, Status
from herdeck.orchestrator import Orchestrator
from herdeck.t3 import T3Connector, T3Error, T3Http, thread_state


def thread(**overrides):
    return {
        "id": "thread-1", "title": "Pilot", "projectId": "project-1",
        "runtimeMode": "approval-required", "interactionMode": "plan",
        "modelSelection": {"provider": "codex", "model": "gpt-5"},
        "session": {"status": "ready", "activeTurnId": None},
        "messages": [], "activities": [], **overrides,
    }


def test_state_identity_and_unknown_status():
    a = thread_state("t3-a", thread(), {}, "epoch")
    b = thread_state("t3-b", thread(), {}, "epoch")
    assert a.key != b.key
    assert a.backend == "t3" and a.terminal_id == ""
    assert a.status == Status.IDLE and "continue" in a.capabilities
    assert thread_state("a", thread(session={"status": "new-state"}), {}, "e").status == Status.UNKNOWN


def test_pending_request_is_structured_and_resolution_removes_actions():
    activity = {"kind": "approval.requested", "payload": {"requestId": "req-1", "detail": "Run tests?"}}
    t = thread(activities=[activity])
    a = thread_state("a", t, {}, "e")
    assert a.status == Status.BLOCKED
    assert [o["id"] for o in a.backend_actions] == ["approve", "deny"]
    assert a.backend_actions[0]["payload"]["requestId"] == "req-1"
    t["activities"].append({"kind": "approval.resolved", "payload": {"requestId": "req-1"}})
    assert thread_state("a", t, {}, "e").backend_actions == []


@pytest.mark.parametrize("url", ["http://example.com", "http://127.0.0.1/?token=secret", "http://u:p@127.0.0.1", "http://127.0.0.1/path"])
def test_endpoint_rejects_unsafe_or_ambiguous_urls(url):
    with pytest.raises(ValueError):
        T3Http(url, "secret")


class FakeHttp:
    def __init__(self, t):
        self.thread = t
        self.projects = []
        self.writes = []
        self.fail_write = False

    def get(self, path):
        if path == "/.well-known/t3/environment":
            return {"serverVersion": "0.0.38", "capabilities": {}}
        if path.endswith("/shell"):
            return {"projects": self.projects, "threads": [self.thread]}
        return {"thread": self.thread}

    def dispatch(self, command):
        self.writes.append(command)
        if self.fail_write:
            raise T3Error("delivery uncertain")
        return {"sequence": 1}


def connector(t):
    results = []
    c = T3Connector(ServerConfig("t3", "http://127.0.0.1:13773", "secret", "t3"),
        on_snapshot=lambda *args: None, on_event=lambda *args: None,
        on_connection=lambda *args: None, on_result=lambda req, data: results.append(data))
    c.http = FakeHttp(t)
    return c, results


@pytest.mark.asyncio
async def test_continue_preserves_modes_and_suppresses_duplicate_press():
    c, results = connector(thread())
    await c.refresh()
    a = c.states["thread-1"]
    msg = {"type": "backend_action", "pane_id": "thread-1", "revision": a.backend_revision,
           "action": "continue", "text": "Continue", "req": "r1"}
    await asyncio.gather(c.send(msg), c.send(msg))
    assert len(c.http.writes) == 1
    command = c.http.writes[0]
    assert command["runtimeMode"] == "approval-required"
    assert command["interactionMode"] == "plan"
    assert command["message"]["text"] == "Continue"
    assert any(r.get("skipped") for r in results)


@pytest.mark.asyncio
async def test_request_resolved_in_t3_rejects_stale_deck_action():
    c, results = connector(thread(activities=[{"kind": "approval.requested", "payload": {"requestId": "req-1"}}]))
    await c.refresh()
    a = c.states["thread-1"]
    c.http.thread = thread()
    await c.send({"type": "backend_action", "pane_id": "thread-1", "revision": a.backend_revision,
                  "action": "approve", "payload": a.backend_actions[0]["payload"], "req": "r"})
    assert not c.http.writes and results[-1]["skipped"]


@pytest.mark.asyncio
async def test_uncertain_delivery_is_not_replayed():
    c, results = connector(thread())
    await c.refresh()
    c.http.fail_write = True
    msg = {"type": "backend_action", "pane_id": "thread-1", "revision": c.states["thread-1"].backend_revision,
           "action": "continue", "text": "Continue", "req": "r"}
    await c.send(msg)
    await c.send(msg)
    assert len(c.http.writes) == 1
    assert any(r.get("uncertain") for r in results)


@pytest.mark.asyncio
async def test_uncertain_write_unlocks_only_when_its_effect_is_observed():
    c, _ = connector(thread())
    await c.refresh()
    c.http.fail_write = True
    await c.send({"type": "backend_action", "pane_id": "thread-1", "revision": c.states["thread-1"].backend_revision,
                  "action": "continue", "text": "Continue", "req": "r"})
    c._epoch = "reconnected"
    c.http.thread["session"]["updatedAt"] = "later"
    await c.refresh()
    assert "continue" not in c.states["thread-1"].capabilities
    c.http.thread["messages"].append({"role": "user", "id": c.http.writes[0]["message"]["messageId"]})
    await c.refresh()
    assert "continue" in c.states["thread-1"].capabilities


def test_questions_use_provider_values_and_complex_forms_have_no_guessed_buttons():
    q = {"id": "q1", "options": [{"label": "Blue", "value": "blue-id"}]}
    t = thread(activities=[{"kind": "user-input.requested", "payload": {"requestId": "r", "questions": [q]}}])
    a = thread_state("a", t, {}, "e")
    assert a.backend_actions[0]["payload"]["answers"] == {"q1": "blue-id"}
    q["multiSelect"] = True
    assert thread_state("a", t, {}, "e").backend_actions == []


def test_mixed_deck_routes_t3_choice_without_terminal_keys():
    from herdeck.config import DEFAULT_PROFILES, Config
    from herdeck.model import AgentKey, AgentState
    from herdeck.orchestrator import Orchestrator
    config = Config(servers=[ServerConfig("herdr", "ws://old", "x"),
        ServerConfig("t3", "http://127.0.0.1:3773", "x", "t3")],
        profiles=dict(DEFAULT_PROFILES), overview_order=["herdr", "t3"], grid=(5, 3))
    orch = Orchestrator(config)
    t3 = thread_state("t3", thread(activities=[{"kind": "approval.requested",
        "payload": {"requestId": "req", "detail": "Approve?"}}]), {}, "e")
    herdr = AgentState(AgentKey("herdr", "thread-1"), "codex", "Old", Status.IDLE)
    orch.apply_snapshot("herdr", [herdr])
    orch.apply_snapshot("t3", [t3])
    orch._drill = t3.key
    actions, _, _ = orch._drill_layout()
    cmd = actions[0]["make"](t3.key)
    assert cmd.kind == "backend_action" and cmd.server_id == "t3"
    assert cmd.payload == {"requestId": "req", "decision": "accept"}
    assert cmd.keys == [] and cmd.terminal_id is None
    orch.set_connection("t3", False)
    assert orch.on_press(0) == []


def test_http_transport_does_not_follow_redirect_or_echo_secret():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    class Handler(BaseHTTPRequestHandler):
        paths = []
        def do_GET(self):
            self.paths.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/leak")
            self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with pytest.raises(T3Error, match="T3 HTTP 302") as error:
            T3Http(f"http://127.0.0.1:{server.server_port}", "private-token").get("/shell")
        assert "private-token" not in str(error.value)
        assert Handler.paths == ["/shell"]
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


@pytest.mark.asyncio
async def test_project_and_thread_renames_reach_mixed_deck_tiles():
    c, _ = connector(thread(title="Fix the checkout"))
    c.http.projects = [{"id": "project-1", "title": "My shop",
                        "workspaceRoot": "/Users/admin/projects/shop"}]
    o = Orchestrator(Config(servers=[], profiles={}, overview_order=["t3", "local"], grid=(5, 3)), slots=13)
    c._on_snapshot = o.apply_snapshot
    o.apply_snapshot("local", [AgentState(AgentKey("local", "p1"), "codex", "api", Status.IDLE)])
    await c.refresh()
    tiles = [t for t in o.render().tiles if t.repo]
    assert [(t.repo, t.branch, t.server_tag) for t in tiles] == [
        ("My shop", "Fix the checkout", "T3"), ("api", "", "HERDR")]
    c.http.projects[0]["title"] = "Renamed shop"
    c.http.thread["title"] = "Checkout fixed"
    await c.refresh()
    tile = o.render().tiles[0]
    assert (tile.repo, tile.branch) == ("Renamed shop", "Checkout fixed")
    assert c.states["thread-1"].repo == "/Users/admin/projects/shop"


def test_t3_tile_honors_explicit_secondary_layout():
    cfg = Config(servers=[], profiles={}, overview_order=["t3"], grid=(5, 3))
    cfg.view.tile_secondary = ["branch"]
    o = Orchestrator(cfg, slots=13)
    o.apply_snapshot("t3", [thread_state("t3", thread(branch="feature/shop"), {}, "e")])
    assert o.render().tiles[0].branch == "feature/shop"


@pytest.mark.parametrize("turn_state,completed_at,expected", [
    ("completed", "2026-09-06T16:00:00Z", Status.DONE),
    ("completed", None, Status.IDLE),
    ("interrupted", "2026-09-06T16:00:00Z", Status.IDLE),
    ("error", "2026-09-06T16:00:00Z", Status.UNKNOWN),
    ("running", None, Status.IDLE),
])
def test_done_requires_explicit_successful_turn(turn_state, completed_at, expected):
    s = thread_state("t3", thread(latestTurn={"state": turn_state, "completedAt": completed_at}), {}, "e")
    assert s.status == expected


def test_active_turn_and_pending_requests_override_previous_completion():
    completed = {"state": "completed", "completedAt": "2026-09-06T16:00:00Z"}
    assert thread_state("t3", thread(latestTurn=completed,
        session={"status": "running", "activeTurnId": "new-turn"}), {}, "e").status == Status.WORKING
    assert thread_state("t3", thread(latestTurn=completed,
        hasPendingApprovals=True), {}, "e").status == Status.BLOCKED
    assert thread_state("t3", thread(latestTurn=completed,
        session={"status": "error"}), {}, "e").status == Status.UNKNOWN


@pytest.mark.asyncio
async def test_completed_turn_keeps_continue_available():
    c, results = connector(thread(latestTurn={"state": "completed",
        "turnId": "finished-turn", "completedAt": "2026-09-06T16:00:00Z"}))
    await c.refresh()
    s = c.states["thread-1"]
    assert s.status == Status.DONE
    await c.send({"type": "backend_action", "pane_id": "thread-1", "revision": s.backend_revision,
                  "action": "continue", "text": "Next step", "req": "next"})
    assert results[-1]["accepted"] and len(c.http.writes) == 1


@pytest.mark.parametrize("liveness,expected", [("working", Status.WORKING), ("monitoring", Status.WAITING)])
def test_background_work_overrides_completed_foreground_turn(liveness, expected):
    s = thread_state("t3", thread(latestTurn={"state": "completed",
        "completedAt": "2026-09-06T16:00:00Z"}, backgroundLiveness=liveness), {}, "e")
    assert s.status == expected
    assert "continue" not in s.capabilities


@pytest.mark.asyncio
@pytest.mark.parametrize('code', [400, 401, 403, 409, 422, 429, 408, 500, None])
async def test_rejected_dispatch_is_retryable_but_ambiguous_delivery_is_locked(code):
    c, results = connector(thread())
    await c.refresh()
    calls = []
    def dispatch(command):
        calls.append(command)
        raise T3Error('sanitized rejection', code)
    c.http.dispatch = dispatch
    msg = {'type': 'backend_action', 'pane_id': 'thread-1',
           'revision': c.states['thread-1'].backend_revision,
           'action': 'continue', 'text': 'Continue', 'req': 'r'}
    await c.send(msg)
    await c.send(msg)
    ambiguous = code in (408, 500, None)
    assert len(calls) == (1 if ambiguous else 2)
    assert bool(c._uncertain) == ambiguous
    assert results[0].get('uncertain' if ambiguous else 'rejected') is True
