import asyncio

import pytest

from herdeck.config import ServerConfig
from herdeck.model import Status
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
        self.writes = []
        self.fail_write = False

    def get(self, path):
        if path.endswith("/shell"):
            return {"projects": [], "threads": [self.thread]}
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
