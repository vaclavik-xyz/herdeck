import json
from pathlib import Path

import pytest

from herdeck.model import Status
from herdeck.t3_state import (
    active_plan,
    last_assistant_text,
    live_request,
    request_item,
    thread_state,
    timestamp,
)

FIX = Path(__file__).parent / "fixtures" / "t3v2"
NOW = timestamp("2026-10-14T12:00:00.000Z")


def iso(delta):
    from datetime import UTC, datetime
    return datetime.fromtimestamp(NOW + delta, UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def shell(**kw):
    base = {"id": "t1", "projectId": "p1", "title": "Pilot", "status": "idle",
            "interactionMode": "default", "runtimeMode": "approval-required",
            "modelSelection": {"instanceId": "claudeAgent", "model": "x"},
            "pendingRuntimeRequest": None, "pendingBackgroundTasks": [],
            "hasActionableProposedPlan": False, "activeRunId": None, "latestRunId": None,
            "updatedAt": "2026-10-14T10:00:00.000Z"}
    base.update(kw)
    return base


def fixture(name):
    return json.loads((FIX / f"{name}.json").read_text())


def fx_shell(name):
    data = fixture(f"shell_{name}")
    return data["threads"][0], {p["id"]: p for p in data["projects"]}


def fx_detail(name):
    return fixture(f"detail_{name}")["projection"]


def run(sh, detail=None, archived=False):
    return thread_state("srv", sh, detail, {}, "e1", features={}, now=NOW, archived=archived)


REQ = {"id": "r", "kind": "command", "createdAt": "2026-10-14T11:00:00.000Z"}
W, B, ID, D, U = Status.WORKING, Status.BLOCKED, Status.IDLE, Status.DONE, Status.UNKNOWN

CASES = [
    ({}, {}, ID, "", ""),
    ({"pendingRuntimeRequest": REQ}, {}, B, "approval", ""),
    ({"pendingRuntimeRequest": {**REQ, "kind": "user_input"}}, {}, B, "input", ""),
    ({"pendingRuntimeRequest": {**REQ, "kind": "auth_refresh"}}, {}, B, "input", ""),
    ({"status": "starting"}, {}, W, "", "CONNECTING"),
    ({"status": "queued"}, {}, W, "", "QUEUED"),
    ({"status": "completed", "latestRunCompletedAt": iso(-600), "latestUserMessageAt": iso(-30)}, {}, W, "", "QUEUED"),
    ({"status": "completed", "latestRunCompletedAt": iso(-600), "latestUserMessageAt": iso(600)}, {}, ID, "", ""),
    ({"status": "running"}, {}, W, "", ""),
    ({"status": "waiting"}, {}, Status.WAITING, "", "WAITING"),
    ({"status": "completed", "pendingBackgroundTasks": [{"taskId": "a", "kind": "subagent"}]}, {},
     Status.WAITING, "", "SUBAGENT"),
    ({"status": "waiting", "pendingBackgroundTasks": [{"kind": "command"}]}, {}, Status.WAITING, "", "COMMAND"),
    # A running command (dev server) does not hold a completed run; upstream
    # backgroundWorkHoldsCompletion. Unknown kinds still hold.
    ({"status": "completed", "pendingBackgroundTasks": [{"kind": "command"}],
      "latestRunCompletedAt": iso(-60), "lastVisitedAt": iso(-300)}, {}, D, "completion", ""),
    ({"status": "completed", "pendingBackgroundTasks": [{"kind": "command"}],
      "latestRunCompletedAt": iso(-300), "lastVisitedAt": iso(-60)}, {}, ID, "", ""),
    ({"status": "completed", "pendingBackgroundTasks": [{"kind": "command"}, {"kind": "subagent"}]}, {},
     Status.WAITING, "", "SUBAGENT"),
    ({"status": "completed", "pendingBackgroundTasks": [{"kind": "future_kind"}]}, {},
     Status.WAITING, "", "WAITING"),
    ({"status": "waiting", "pendingBackgroundTasks": [{"kind": "monitor"}]}, {}, Status.WAITING, "", "MONITOR"),
    ({"status": "waiting", "pendingBackgroundTasks": [{"kind": "background_task"}]}, {},
     Status.WAITING, "", "BACKGROUND"),
    ({"status": "failed", "lastError": "boom"}, {}, U, "error", "ERROR"),
    ({"status": "failed", "lastErrorClass": "usage_limit"}, {}, U, "error", "LIMIT"),
    ({"interactionMode": "plan", "hasActionableProposedPlan": True, "status": "completed"}, {}, B, "plan", "PLAN READY"),
    ({"interactionMode": "plan", "hasActionableProposedPlan": True, "status": "completed", "activeRunId": "run-1"},
     {}, ID, "", ""),
    ({"status": "completed", "latestRunCompletedAt": iso(-60), "lastVisitedAt": iso(-300)}, {}, D, "completion", ""),
    ({"status": "completed", "latestRunCompletedAt": iso(-300), "lastVisitedAt": iso(-60)}, {}, ID, "", ""),
    ({"status": "completed", "latestRunCompletedAt": iso(-60)}, {}, ID, "", ""),
    ({"status": "interrupted"}, {}, ID, "", ""),
    ({"status": "cancelled"}, {}, ID, "", ""),
    ({"status": "rolled_back"}, {}, ID, "", ""),
    ({"deletedAt": iso(-5)}, {}, ID, "", "DELETED"),
    ({"settledOverride": "settled"}, {}, ID, "", "SETTLED"),
    ({"snoozedUntil": iso(3600), "snoozedAt": iso(-3600)}, {}, ID, "", "SNOOZED"),
    ({"snoozedUntil": iso(3600), "snoozedAt": iso(-3600), "status": "completed", "latestRunCompletedAt": iso(-60)},
     {}, ID, "", ""),
    ({"snoozedUntil": iso(-60)}, {}, ID, "", ""),
    ({}, {"archived": True}, ID, "", "ARCHIVED"),
]


@pytest.mark.parametrize("overrides,args,status,attention,label", CASES)
def test_mapping_table(overrides, args, status, attention, label):
    s = run(shell(**overrides), **args)
    assert (s.status, s.attention) == (status, attention)
    assert s.state_labels == ({status.value: label} if label else {})


def test_lifecycle_fields_follow_the_mapping():
    assert run(shell(deletedAt=iso(-5))).lifecycle == "deleted"
    assert run(shell(), archived=True).lifecycle == "archived"
    s = run(shell(snoozedUntil=iso(3600), snoozedAt=iso(-3600), status="completed", latestRunCompletedAt=iso(-60)))
    assert s.lifecycle == "active"


def test_shell_plan_fixture_is_waiting_like_t3_sidebar():
    sh, projects = fx_shell("plan")
    s = thread_state("srv", sh, fx_detail("plan"), projects, "e", features={}, now=NOW)
    assert (s.status, s.attention, s.state_labels) == (Status.WAITING, "", {"waiting": "WAITING"})


def test_fixture_states():
    sh, projects = fx_shell("completed")
    now = timestamp(sh["latestRunCompletedAt"]) + 60
    s = thread_state("srv", sh, fx_detail("completed"), projects, "e", features={}, now=now)
    assert (s.status, s.attention) == (D, "completion")
    assert s.preview == "OK"
    assert s.completed_at == sh["latestRunCompletedAt"]
    assert (s.display_agent, s.backend, s.agent_type) == ("T3 Code", "t3", "claudeAgent")
    assert (s.project, s.repo) == ("proj", "/Users/dev/proj")
    assert s.capabilities == () and s.backend_actions == []
    assert s.key.pane_id == sh["id"]
    sh, projects = fx_shell("failed")
    s = thread_state("srv", sh, fx_detail("failed"), projects, "e", features={}, now=now)
    assert (s.status, s.attention) == (U, "error")
    assert s.preview.startswith("T3 error: ")
    sh, projects = fx_shell("running")
    s = thread_state("srv", sh, fx_detail("running"), projects, "e", features={}, now=now)
    assert s.status == W


def test_approval_fixture_lookup_and_preview():
    sh, projects = fx_shell("approval")
    detail = fx_detail("approval")
    item = request_item(detail, "req-approval-1")
    assert item["type"] == "approval_request"
    assert live_request(detail, "req-approval-1")
    assert request_item(detail, "nope") is None
    assert not live_request(detail, "nope")
    s = thread_state("srv", sh, detail, projects, "e", features={}, now=NOW)
    assert (s.status, s.attention, s.preview) == (B, "approval", "Run `touch approval-probe.txt`?")


def test_request_item_found_via_visible_turn_items_only():
    detail = {"turnItems": [], "visibleTurnItems": [
        {"item": {"type": "approval_request", "requestId": "x", "prompt": "p"}}]}
    assert request_item(detail, "x")["prompt"] == "p"
    assert request_item(None, "x") is None


def test_pending_request_without_runtime_entry_is_not_live():
    detail = fx_detail("approval")
    detail = {**detail, "runtimeRequests": []}
    assert not live_request(detail, "req-approval-1")
    assert not live_request(None, "req-approval-1")
    detail["runtimeRequests"] = [{"id": "req-approval-1", "status": "pending",
                                  "responseCapability": {"type": "none"}}]
    assert not live_request(detail, "req-approval-1")


def test_input_preview_single_question_live_vs_form():
    sh, projects = fx_shell("input")
    detail = fx_detail("input")
    s = thread_state("srv", sh, detail, projects, "e", features={}, now=NOW)
    assert (s.status, s.attention) == (B, "input")
    assert "Branch: Which branch should I use?" in s.preview
    assert "main" in s.preview and "Feature branch" in s.preview
    assert not s.preview.startswith("Answer in T3")
    dead = {**detail, "runtimeRequests": []}
    assert thread_state("srv", sh, dead, projects, "e", features={}, now=NOW).preview.startswith("Answer in T3")
    multi = json.loads(json.dumps(detail))
    for it in multi["turnItems"]:
        if it["type"] == "user_input_request":
            it["questions"][0]["multiSelect"] = True
    assert thread_state("srv", sh, multi, projects, "e", features={}, now=NOW).preview.startswith("Answer in T3")


def test_plan_markdown_from_turn_item_and_preview():
    detail = fx_detail("plan")
    plan = active_plan(detail)
    assert plan["status"] == "active"
    assert plan["markdown"].startswith("# Pl")
    assert active_plan(None) is None
    assert active_plan({"plans": [{"kind": "proposed_plan", "status": "superseded"}]}) is None
    sh, projects = fx_shell("plan")
    sh = {**sh, "status": "completed", "activeRunId": None}
    s = thread_state("srv", sh, detail, projects, "e", features={}, now=NOW)
    assert (s.status, s.attention, s.state_labels) == (B, "plan", {"blocked": "PLAN READY"})
    assert s.preview.startswith("Plan ready — choose how to implement it.\n# Pl")


def test_plan_markdown_via_matching_turn_item_when_plan_has_none():
    detail = fx_detail("plan")
    detail = {**detail, "plans": [{k: v for k, v in p.items() if k != "markdown"} for p in detail["plans"]]}
    assert active_plan(detail)["markdown"].startswith("# Pl")


def test_last_assistant_text_sources():
    assert last_assistant_text(fx_detail("completed")) == "OK"
    assert last_assistant_text(None) == ""
    assert last_assistant_text({"messages": [{"role": "user", "text": "u"}, {"role": "assistant", "text": "a"}]}) == "a"


def test_non_active_preview_prefix_and_cap():
    detail = {"turnItems": [{"type": "assistant_message", "text": "x" * 20000}]}
    s = run(shell(), detail)
    assert len(s.preview) == 12000
    s = run(shell(settledOverride="settled"), {"turnItems": [{"type": "assistant_message", "text": "hi"}]})
    assert s.preview == "Settled thread.\nhi"


def test_error_preview_default_text():
    assert run(shell(status="failed", lastError="boom")).preview == "T3 error: boom"
    assert run(shell(status="failed")).preview == "T3 error: The latest run failed. Inspect T3 for details."


def test_tile_defaults():
    s = run(shell(title=None, modelSelection=None, providerInstanceId=None))
    assert (s.label, s.title, s.agent_type, s.branch) == ("T3 conversation", "T3 conversation", "default", "")


def test_revision_stability_and_sensitivity():
    a = run(shell(status="completed", lastVisitedAt=iso(-10)))
    assert a.backend_revision == run(shell(status="completed", lastVisitedAt=iso(-10))).backend_revision
    assert a.backend_revision != run(shell(status="completed", lastVisitedAt=iso(-5))).backend_revision
    other = thread_state("srv", shell(status="completed", lastVisitedAt=iso(-10)), None, {}, "e2", features={}, now=NOW)
    assert a.backend_revision != other.backend_revision
    feat = thread_state("srv", shell(status="completed", lastVisitedAt=iso(-10)), None, {}, "e1",
                        features={"x": True}, now=NOW)
    assert a.backend_revision != feat.backend_revision


def test_plan_fallback_ignores_unrelated_items_without_node_id():
    detail = {"plans": [{"id": "p1", "kind": "proposed_plan", "status": "active"}],
              "turnItems": [{"type": "proposed_plan", "planId": "other", "markdown": "unrelated"}]}
    assert active_plan(detail)["markdown"] == ""


def test_plan_fallback_first_matching_item_wins():
    detail = {"plans": [{"id": "p1", "kind": "proposed_plan", "status": "active"}],
              "turnItems": [{"type": "proposed_plan", "planId": "p1", "markdown": "first"},
                            {"type": "proposed_plan", "planId": "p1", "markdown": "second"}]}
    assert active_plan(detail)["markdown"] == "first"
