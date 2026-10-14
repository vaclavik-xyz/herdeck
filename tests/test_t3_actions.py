import json
from dataclasses import replace
from pathlib import Path

import pytest

from herdeck.model import Status
from herdeck.t3_actions import Incompatible, build_commands, capabilities, negotiate, thread_actions
from herdeck.t3_state import active_plan, thread_state, timestamp

FIX = Path(__file__).parent / "fixtures" / "t3v2"
NOW = timestamp("2026-10-14T16:00:00.000Z")
ALL = {"settle": True, "snooze": True, "visit": True, "resolved_context": True}
NONE = {"settle": False, "snooze": False, "visit": False, "resolved_context": False}


def fx(name):
    return json.loads((FIX / f"{name}.json").read_text())


def load(name, features=ALL, now=NOW):
    shell = fx(f"shell_{name}")["threads"][0]
    detail = fx(f"detail_{name}")["projection"]
    state = thread_state("s", shell, detail, {}, 0, features=features, now=now)
    return state, shell, detail


def ids(actions):
    return [a["id"] for a in actions]


def test_negotiate_real_descriptor():
    assert negotiate(fx("descriptor")) == ALL


def test_negotiate_flags_default_false():
    assert negotiate({"orchestrationProtocolVersion": 2, "capabilities": {"threadSnooze": True}}) == {
        "settle": False, "snooze": True, "visit": False, "resolved_context": False}
    assert negotiate({"orchestrationProtocolVersion": 2}) == NONE


@pytest.mark.parametrize("desc,n", [({}, 1), ({"orchestrationProtocolVersion": 3}, 3)])
def test_negotiate_incompatible(desc, n):
    with pytest.raises(Incompatible) as e:
        negotiate(desc)
    assert str(e.value) == f"T3 server speaks orchestration protocol {n}; Herdeck needs 2 (T3 0.0.46+)"


def test_approval_actions():
    state, shell, detail = load("approval")
    actions = thread_actions(state, shell, detail, ALL, NOW)
    assert ids(actions)[:3] == ["approve", "approve_session", "deny"]
    by = {a["id"]: a for a in actions}
    assert by["approve"]["payload"] == {"requestId": "req-approval-1", "decision": "accept"}
    assert by["approve"]["confirm"] is False and by["approve"]["subtext"] == ""
    assert by["approve_session"]["confirm"] is True
    assert by["approve_session"]["subtext"] == "Allows this command for the rest of the session"
    assert by["approve_session"]["payload"]["decision"] == "acceptForSession"
    assert by["deny"]["payload"]["decision"] == "decline" and by["deny"]["confirm"] is False
    assert set(by["deny"]) == {"id", "label", "payload", "confirm", "subtext"}
    assert "snooze" not in by


def test_approval_default_options_and_unknown_decision_skipped():
    state, shell, detail = load("approval")
    item = next(i for i in detail["turnItems"] if i["type"] == "approval_request")
    del item["options"]
    a = thread_actions(state, shell, detail, ALL, NOW)
    assert [(x["id"], x["label"]) for x in a if x["id"] in ("approve", "deny")] == [
        ("approve", "Approve"), ("deny", "Deny")]
    item["options"] = [{"decision": "weird", "label": "X"},
                       {"decision": "acceptAlways", "label": "Always"}, {"decision": "cancel", "label": "C"}]
    a = thread_actions(state, shell, detail, ALL, NOW)
    assert ids(a)[:2] == ["approve_always", "deny"]
    assert a[0]["confirm"] is True
    assert a[1]["payload"]["decision"] == "cancel"


def test_non_live_request_offers_nothing():
    state, shell, detail = load("approval")
    detail["runtimeRequests"][0]["responseCapability"] = {"type": "not_resumable"}
    assert thread_actions(state, shell, detail, ALL, NOW) == []
    state, shell, detail = load("input")
    detail["runtimeRequests"] = []
    assert thread_actions(state, shell, detail, ALL, NOW) == []


def test_input_single_question():
    state, shell, detail = load("input")
    actions = thread_actions(state, shell, detail, ALL, NOW)
    assert [a["id"] for a in actions] == ["answer", "answer"]
    assert [a["label"] for a in actions] == ["main", "Feature branch"]
    assert [a["subtext"] for a in actions] == ["Default branch", "Create a new branch"]
    assert actions[0]["payload"] == {"requestId": "req-input-1", "answers": {"q1": "main"}}
    assert actions[1]["payload"]["answers"] == {"q1": "feature"}
    assert all(a["confirm"] is False for a in actions)


def test_input_multi_question_or_multiselect_offers_none():
    state, shell, detail = load("input")
    item = next(i for i in detail["turnItems"] if i["type"] == "user_input_request")
    item["questions"][0]["multiSelect"] = True
    assert thread_actions(state, shell, detail, ALL, NOW) == []
    item["questions"][0]["multiSelect"] = False
    item["questions"].append(dict(item["questions"][0], id="q2"))
    assert thread_actions(state, shell, detail, ALL, NOW) == []


def test_plan_action_and_commands():
    # The live-captured plan shell is "waiting" (no attention); a completed plan turn is "plan".
    shell = dict(fx("shell_plan")["threads"][0], status="completed")
    detail = fx("detail_plan")["projection"]
    state = thread_state("s", shell, detail, {}, 0, features=ALL, now=NOW)
    assert state.attention == "plan"
    actions = thread_actions(state, shell, detail, ALL, NOW)
    plan = next(a for a in actions if a["id"] == "implement_plan")
    assert plan["confirm"] is True
    p = active_plan(detail)
    assert plan["payload"] == {"planId": p["id"]}
    cmds = build_commands("implement_plan", plan["payload"], shell, detail, ALL, now_iso="x")
    tid = shell["id"]
    assert len(cmds) == 2
    assert cmds[0] == {"type": "thread.interaction-mode.set", "threadId": tid, "interactionMode": "default"}
    msg = cmds[1]
    assert msg["type"] == "message.dispatch" and msg["sourcePlanRef"] == {"threadId": tid, "planId": p["id"]}
    assert msg["text"] == "PLEASE IMPLEMENT THIS PLAN:\n" + p["markdown"].strip()
    assert msg["dispatchMode"] == {"type": "start_immediately"} and msg["deliveryIntent"] == "auto"


def test_acknowledge_requires_visit_feature():
    state, shell, detail = load("completed")
    assert state.status == Status.DONE
    assert "acknowledge" in ids(thread_actions(state, shell, detail, ALL, NOW))
    a = next(x for x in thread_actions(state, shell, detail, ALL, NOW) if x["id"] == "acknowledge")
    assert a["label"] == "Mark seen"
    assert a["payload"] == {"completedAt": "2026-10-14T15:05:41.868Z"}
    assert "acknowledge" not in ids(thread_actions(state, shell, detail, dict(ALL, visit=False), NOW))
    cmds = build_commands("acknowledge", a["payload"], shell, detail, ALL, now_iso="x")
    assert cmds == [{"type": "thread.visit", "threadId": shell["id"], "visitedAt": "2026-10-14T15:05:41.868Z"}]


def test_settle_snooze_gating():
    state, shell, detail = load("completed")
    a = ids(thread_actions(state, shell, detail, ALL, NOW))
    assert "settle" in a and "snooze" in a
    a = ids(thread_actions(state, shell, detail, NONE, NOW))
    assert "settle" not in a and "snooze" not in a
    settled = replace(state, lifecycle="settled")
    a = thread_actions(settled, shell, detail, ALL, NOW)
    assert [x["label"] for x in a if x["id"] == "unsettle"] == ["Reopen"]
    assert "settle" not in ids(a)
    snoozed = replace(state, lifecycle="snoozed")
    a = thread_actions(snoozed, shell, detail, ALL, NOW)
    assert [x["label"] for x in a if x["id"] == "unsnooze"] == ["Wake now"]
    assert "snooze" not in ids(a)
    # working thread: no settle; snooze allowed
    state, shell, detail = load("running")
    a = ids(thread_actions(state, shell, detail, ALL, NOW))
    assert "settle" not in a and "snooze" in a
    # queued: no snooze
    assert "snooze" not in ids(thread_actions(replace(state, activity="queued"), shell, detail, ALL, NOW))


@pytest.mark.parametrize("life", ["archived", "deleted"])
def test_no_actions_for_archived_or_deleted(life):
    state, shell, detail = load("completed")
    assert thread_actions(replace(state, lifecycle=life), shell, detail, ALL, NOW) == []


def test_capabilities():
    state, shell, detail = load("running")
    acts = thread_actions(state, shell, detail, ALL, NOW)
    caps = capabilities(state, shell, acts)
    assert caps[:2] == ("read", "stop") and "continue" not in caps and "snooze" in caps
    state, shell, detail = load("completed")
    caps = capabilities(state, shell, thread_actions(state, shell, detail, ALL, NOW))
    assert caps[:2] == ("read", "continue") and "stop" not in caps
    state, shell, detail = load("approval")
    caps = capabilities(state, shell, thread_actions(state, shell, detail, ALL, NOW))
    assert caps == ("read", "stop", "approve", "approve_session", "deny")
    state, shell, detail = load("input")
    caps = capabilities(state, shell, thread_actions(state, shell, detail, ALL, NOW))
    assert caps.count("answer") == 1
    assert capabilities(replace(state, lifecycle="archived"), shell, []) == ("read",)


def test_respond_commands():
    state, shell, detail = load("approval")
    tid = shell["id"]
    cmd = build_commands("approve_session", {"requestId": "r", "decision": "acceptForSession"},
                         shell, detail, ALL, now_iso="x")
    assert cmd == [{"type": "runtime-request.respond", "threadId": tid, "requestId": "r",
                    "decision": "acceptForSession"}]
    cmd = build_commands("answer", {"requestId": "r", "answers": {"q1": "v"}}, shell, detail, ALL, now_iso="x")
    assert cmd == [{"type": "runtime-request.respond", "threadId": tid, "requestId": "r",
                    "answers": {"q1": "v"}}]


def test_stop_command():
    _, shell, detail = load("running")
    assert build_commands("stop", {}, shell, detail, ALL, now_iso="x") == [
        {"type": "run.interrupt", "threadId": shell["id"], "runId": shell["activeRunId"], "holdQueue": True}]


def test_continue_command_modes():
    _, shell, detail = load("running")
    [msg] = build_commands("continue", {}, shell, detail, ALL, text="  go on ", now_iso="x")
    uid = msg.pop("messageId")
    assert len(uid) == 36
    assert msg == {"type": "message.dispatch", "threadId": shell["id"], "createdBy": "user",
                   "creationSource": "web", "text": "  go on ", "attachments": [],
                   "dispatchMode": {"type": "start_immediately"}, "deliveryIntent": "auto"}
    [msg] = build_commands("continue", {}, shell, detail, dict(ALL, resolved_context=False),
                           text="go", now_iso="x")
    assert msg["dispatchMode"] == {"type": "queue_after_active"} and "deliveryIntent" not in msg
    _, shell, detail = load("completed")
    [msg] = build_commands("continue", {}, shell, detail, dict(ALL, resolved_context=False),
                           text="go", now_iso="x")
    assert msg["dispatchMode"] == {"type": "start_immediately"}


@pytest.mark.parametrize("text,ok", [("", False), ("   ", False), (None, False), ("a", True),
                                     ("a" * 4096, True), ("a" * 4097, False)])
def test_text_bounds(text, ok):
    _, shell, detail = load("completed")
    if ok:
        assert build_commands("continue", {}, shell, detail, ALL, text=text, now_iso="x")
    else:
        with pytest.raises(ValueError, match="T3 message must contain 1–4096 characters"):
            build_commands("continue", {}, shell, detail, ALL, text=text, now_iso="x")


def test_lifecycle_commands():
    _, shell, detail = load("completed")
    tid = shell["id"]
    iso = "2026-10-14T17:00:00.000Z"

    def b(action):
        return build_commands(action, {}, shell, detail, ALL, now_iso="2026-10-14T16:00:00.000Z")

    assert b("settle") == [{"type": "thread.settle", "threadId": tid}]
    assert b("unsettle") == [{"type": "thread.unsettle", "threadId": tid, "reason": "user"}]
    assert b("snooze") == [{"type": "thread.snooze", "threadId": tid, "snoozedUntil": iso}]
    assert b("unsnooze") == [{"type": "thread.unsnooze", "threadId": tid, "reason": "user"}]


def test_no_command_carries_command_id_or_created_at_or_thread_stop():
    shell = dict(fx("shell_plan")["threads"][0], status="completed")
    detail = fx("detail_plan")["projection"]
    for action, payload in [("approve", {"requestId": "r", "decision": "accept"}), ("stop", {}),
                            ("continue", {}), ("implement_plan", {"planId": active_plan(detail)["id"]}),
                            ("acknowledge", {"completedAt": "t"}), ("settle", {}), ("snooze", {})]:
        _, s2, _ = load("running")
        for c in build_commands(action, payload, s2 if action == "stop" else shell, detail, ALL,
                                text="hi", now_iso="2026-10-14T16:00:00.000Z"):
            assert "commandId" not in c and "createdAt" not in c and c["type"] != "thread.stop"
            assert c["threadId"]


def test_unknown_action_raises():
    _, shell, detail = load("plan")
    with pytest.raises(ValueError):
        build_commands("nope", {}, shell, detail, ALL, now_iso="x")
