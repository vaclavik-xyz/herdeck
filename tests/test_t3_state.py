import json
from pathlib import Path

import pytest

from herdeck.model import SUBAGENTS_MAX, Status
from herdeck.t3_state import (
    SUBAGENT_STATUS,
    active_plan,
    last_assistant_text,
    live_request,
    published,
    request_item,
    thread_state,
    thread_subagents,
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
    assert s.preview.startswith("Plan ready — implement it, or reply to refine it.\n# Pl")


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


def test_previews_follow_the_deck_language():
    def cs(sh, detail=None):
        return thread_state("srv", sh, detail, {}, "e1", features={}, now=NOW, lang="cs")
    assert cs(shell(status="failed", lastError="boom")).preview == "Chyba T3: boom"
    assert cs(shell(status="failed")).preview == "Chyba T3: Poslední běh selhal. Podrobnosti najdeš v T3."
    hi = {"turnItems": [{"type": "assistant_message", "text": "hi"}]}
    assert cs(shell(settledOverride="settled"), hi).preview == "Uzavřené vlákno.\nhi"
    assert cs(shell(pendingRuntimeRequest=REQ)).preview == "Žádost o schválení"
    assert cs(shell(pendingRuntimeRequest={**REQ, "kind": "user_input"})).preview == (
        "Odpověz v T3 — podrobnosti nejsou k dispozici.")
    assert cs(shell(title=None)).label == "Konverzace T3"
    sh, _ = fx_shell("input")
    dead = {**fx_detail("input"), "runtimeRequests": []}
    assert cs(sh, dead).preview.startswith("Odpověz v T3 — tento formulář potřebuje celé rozhraní konverzace.\n")
    sh, _ = fx_shell("plan")
    plan = cs({**sh, "status": "completed", "activeRunId": None}, fx_detail("plan"))
    assert plan.preview.startswith("Plán je připravený — spusť ho, nebo ho odpovědí uprav.\n# Pl")
    # English stays the default, and the revision does not depend on the language.
    assert run(shell(status="failed")).backend_revision == cs(shell(status="failed")).backend_revision


# --- subagents ---------------------------------------------------------------

LINEAGE = {"parentThreadId": "t0", "relationshipToParent": "subagent", "rootThreadId": "t0"}


def task(**kw):
    base = {"id": "node:sub-1", "status": "running", "driver": "claudeAgent", "title": "Scan",
            "prompt": "Scan the repo\nthoroughly.", "model": "claude-haiku-5-5",
            "startedAt": "2026-10-14T11:00:00.000Z", "completedAt": None,
            "updatedAt": "2026-10-14T11:00:05.000Z"}
    base.update(kw)
    return base


# OrchestrationV2Subagent.status -> Herdeck Subagent.status. Active work
# (isOrchestrationV2WorkActive) is running; idle is resumable but no longer
# working; interrupted and cancelled ended without a result.
SUBAGENT_STATUSES = [("pending", "running"), ("running", "running"), ("waiting", "running"),
                     ("idle", "done"), ("completed", "done"), ("failed", "failed"),
                     ("cancelled", "failed"), ("interrupted", "failed")]


@pytest.mark.parametrize("t3_status,status", SUBAGENT_STATUSES)
def test_subagent_status_table(t3_status, status):
    assert SUBAGENT_STATUS[t3_status] == status
    (sub,), running, total = thread_subagents({"subagents": [task(status=t3_status)]})
    assert sub.status == status and (running, total) == (int(status == "running"), 1)


def test_subagent_status_table_covers_the_contract():
    assert set(SUBAGENT_STATUS) == {s for s, _ in SUBAGENT_STATUSES}


def test_unknown_or_malformed_subagents_are_dropped():
    subs = thread_subagents({"subagents": [task(status="future"), task(id=None), "junk",
                                           task(startedAt=None, updatedAt=None)]})
    assert subs == ((), 0, 0)
    assert thread_subagents({}) == thread_subagents(None) == thread_subagents({"subagents": "x"}) == ((), 0, 0)


def test_subagent_fields_map_onto_the_herdeck_model():
    (sub,), _, _ = thread_subagents({"subagents": [task()]})
    assert (sub.id, sub.provider, sub.type, sub.description, sub.model, sub.depth) == (
        "node:sub-1", "claudeAgent", "Scan", "Scan the repo", "claude-haiku-5-5", 1)
    assert sub.started_ms == timestamp("2026-10-14T11:00:00.000Z") * 1000 and sub.ended_ms is None
    (done,), _, _ = thread_subagents({"subagents": [task(status="completed",
                                                          completedAt="2026-10-14T11:01:00.000Z")]})
    assert done.ended_ms - done.started_ms == 60_000
    # A pending task has no startedAt yet: its last update stands in.
    (pending,), _, _ = thread_subagents({"subagents": [task(status="pending", startedAt=None, title=None,
                                                             model=None, modelSelection={"model": "m2"})]})
    assert pending.started_ms == timestamp("2026-10-14T11:00:05.000Z") * 1000
    assert (pending.type, pending.model) == ("", "m2")


def test_subagent_text_and_count_bounds_are_the_shared_model_ones():
    tasks = [task(id=f"node:{i}", startedAt=iso(i), title="x" * 500, prompt="‮" + "y" * 500)
             for i in range(SUBAGENTS_MAX + 5)]
    subs, running, total = thread_subagents({"subagents": tasks})
    assert len(subs) == SUBAGENTS_MAX and (running, total) == (SUBAGENTS_MAX + 5, SUBAGENTS_MAX + 5)
    assert subs[0].id == f"node:{SUBAGENTS_MAX + 4}"  # most recent first
    assert len(subs[0].type) == 64 and subs[0].description == "y" * 160


def test_a_long_subagent_id_is_hashed_not_clipped_into_a_collision():
    a, b = "node:" + "a" * 200 + "1", "node:" + "a" * 200 + "2"
    subs, _, _ = thread_subagents({"subagents": [task(id=a), task(id=b, startedAt=iso(1))]})
    assert len({s.id for s in subs}) == 2 and all(len(s.id) <= 128 for s in subs)


def test_parent_tile_carries_its_subagents():
    sh, projects = fx_shell("subagent")
    s = thread_state("srv", sh, fx_detail("subagent"), projects, "e", features={}, now=NOW)
    assert (s.subagents_running, s.subagents_total) == (2, 4)
    assert [x.status for x in s.subagents] == ["running", "running", "done", "failed"]
    assert s.subagents[1].description == "Read the file `tsconfig.json` in the current working directory"


def test_subagents_do_not_churn_the_revision():
    """The revision gates deck presses; no action depends on the subagent list."""
    a = run(shell(status="running"), {"subagents": [task()]})
    b = run(shell(status="running"), {"subagents": [task(status="completed"), task(id="node:2")]})
    assert a.subagents != b.subagents and a.backend_revision == b.backend_revision


def test_subagent_children_are_hidden_unless_they_hold_a_request():
    """T3 hides subagent children (Sidebar.logic.ts isSidebarSubagentThread); a
    delegate_task child waiting on its own approval must stay reachable."""
    assert published(shell(), NOW)
    assert published(shell(lineage={**LINEAGE, "relationshipToParent": "fork"}), NOW)
    assert published(shell(lineage=None), NOW)
    assert not published(shell(lineage=LINEAGE), NOW)
    assert not published(shell(lineage=LINEAGE, status="failed"), NOW)
    assert published(shell(lineage=LINEAGE, pendingRuntimeRequest=REQ), NOW)
    assert not published(shell(lineage=LINEAGE, pendingRuntimeRequest=REQ), NOW, archived=True)
    assert not published(shell(lineage=LINEAGE, pendingRuntimeRequest=REQ, deletedAt=iso(-5)), NOW)
