"""Regression matrix against T3 v0.0.38 contracts and client lifecycle rules."""
import copy
from datetime import UTC, datetime

import pytest
from test_t3 import connector, thread

from herdeck.config import Config
from herdeck.model import Status
from herdeck.orchestrator import Orchestrator
from herdeck.t3 import T3Error, thread_state
from herdeck.t3_actions import negotiated_features
from herdeck.t3_seen import SeenStore

NOW = datetime(2026, 9, 6, 12, tzinfo=UTC).timestamp()
DONE = {"state": "completed", "completedAt": "2026-09-06T10:00:00Z"}
FEATURES = dict(core=True, extended=True, settle=True, snooze=True)


def state(**kwargs):
    return thread_state("t3", thread(latestTurn=DONE, **kwargs), {}, "epoch", now=NOW, features=FEATURES)


def test_settled_disappears_from_overview_but_keeps_truthful_pin_and_revision():
    active, settled = state(), state(settledOverride="settled")
    assert active.status == Status.DONE
    assert settled.status == Status.IDLE and settled.lifecycle == "settled"
    assert settled.backend_revision != active.backend_revision
    assert "continue" not in settled.capabilities and "unsettle" in settled.capabilities
    orch = Orchestrator(Config(servers=[], profiles={}, overview_order=["t3"], grid=(5, 3)), slots=13)
    orch.apply_snapshot("t3", [settled])
    assert not any(t.repo for t in orch.render().tiles)
    orch.pins = {0: settled.key}
    tile = orch.render().tiles[0]
    assert tile.pinned and tile.status_text == "SETTLED"


@pytest.mark.parametrize("extra,expected", [
    ({}, "snoozed"),
    ({"hasPendingApprovals": True}, "active"),
    ({"hasPendingUserInput": True}, "active"),
    ({"snoozedUntil": "2026-09-06T11:59:00Z"}, "active"),
    ({"session": {"status": "error", "updatedAt": "2026-09-06T11:30:00Z"}}, "active"),
    ({"session": {"status": "error", "updatedAt": "2026-09-06T10:30:00Z"}}, "snoozed"),
    ({"latestTurn": {"state": "completed", "completedAt": "2026-09-06T11:30:00Z"}}, "active"),
])
def test_snooze_wakes_only_for_expiry_or_new_attention(extra, expected):
    t = thread(latestTurn=DONE, snoozedAt="2026-09-06T11:00:00Z", snoozedUntil="2026-09-06T13:00:00Z")
    t.update(extra)
    assert thread_state("t3", t, {}, "e", now=NOW).lifecycle == expected


def test_queued_start_has_bounded_grace_and_prevents_second_start():
    t = thread(latestTurn=DONE, session=None, latestUserMessageAt="2026-09-06T12:00:00Z")
    queued = thread_state("t3", t, {}, "e", now=NOW)
    assert queued.activity == "queued" and queued.status == Status.WORKING
    assert "continue" not in queued.capabilities
    assert thread_state("t3", t, {}, "e", now=NOW + 121).status == Status.DONE


def test_plan_precedes_monitoring_and_error_is_visible():
    plan = state(hasActionableProposedPlan=True, backgroundLiveness="monitoring",
                 proposedPlans=[{"id": "p", "planMarkdown": "Do the work"}])
    assert plan.attention == "plan" and "implement_plan" in plan.capabilities
    assert "continue" not in plan.capabilities
    error = state(session={"status": "error", "lastError": "Provider disconnected"})
    assert error.attention == "error" and "Provider disconnected" in error.preview


@pytest.mark.asyncio
async def test_seen_is_local_persistent_and_next_completion_is_done(tmp_path):
    c, results = connector(thread(latestTurn=copy.deepcopy(DONE)))
    c._seen = SeenStore(tmp_path, "t3")
    await c.refresh()
    s = c.states["thread-1"]
    choice = next(a for a in s.backend_actions if a["id"] == "acknowledge")
    await c.send(dict(type="backend_action", pane_id="thread-1", revision=s.backend_revision,
                     action=choice["id"], payload=choice["payload"]))
    assert results[-1]["local"] and not c.http.writes
    assert c.states["thread-1"].status == Status.IDLE
    c._seen = SeenStore(tmp_path, "t3")
    await c.refresh()
    assert c.states["thread-1"].status == Status.IDLE
    c.http.thread["latestTurn"]["completedAt"] = "2026-09-06T11:00:00Z"
    await c.refresh()
    assert c.states["thread-1"].status == Status.DONE


@pytest.mark.asyncio
async def test_lifecycle_change_rejects_old_continue():
    c, results = connector(thread(latestTurn=DONE))
    await c.refresh()
    revision = c.states["thread-1"].backend_revision
    c.http.thread["settledOverride"] = "settled"
    await c.send(dict(type="backend_action", pane_id="thread-1", revision=revision,
                     action="continue", text="Do more"))
    assert results[-1]["skipped"] and not c.http.writes


@pytest.mark.asyncio
async def test_plan_command_has_reference_and_leaves_plan_mode():
    c, _ = connector(thread(latestTurn=DONE, hasActionableProposedPlan=True,
                            proposedPlans=[dict(id="p", planMarkdown="Verified plan")]))
    await c.refresh()
    s = c.states["thread-1"]
    await c.send(dict(type="backend_action", pane_id="thread-1", revision=s.backend_revision,
                     action="implement_plan", payload={"planId": "p"}))
    cmd = c.http.writes[0]
    assert cmd["interactionMode"] == "default"
    assert cmd["sourceProposedPlan"] == {"threadId": "thread-1", "planId": "p"}
    assert "Verified plan" in cmd["message"]["text"]


def test_persistent_approval_is_gated_and_always_requires_two_presses():
    s = state(activities=[dict(kind="approval.requested", payload=dict(requestId="r", options=[
        dict(decision="acceptAlways", label="Always"), dict(decision="acceptForSession", label="Session")]))])
    cfg = Config(servers=[], profiles={}, overview_order=["t3"], grid=(5, 3))
    cfg.safety.require_confirm_for = []
    cfg.safety.approve_always = False
    orch = Orchestrator(cfg, slots=13)
    orch.apply_snapshot("t3", [s])
    orch._drill = s.key
    actions = orch._drill_layout()[0]
    assert [a["id"] for a in actions] == ["approve_session"]
    assert orch.on_press(0) == []
    assert orch.on_press(0)[0].payload["decision"] == "acceptForSession"


@pytest.mark.asyncio
async def test_cache_reuses_details_but_actions_force_current_read():
    c, _ = connector(thread())
    calls = []
    get = c.http.get
    def tracked(path):
        calls.append(path)
        return copy.deepcopy(get(path))
    c.http.get = tracked
    await c.refresh()
    await c.refresh()
    assert sum("/threads/" in p for p in calls) == 1
    await c.send(dict(type="read", pane_id="thread-1"))
    assert sum("/threads/" in p for p in calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [404, 500, 401])
async def test_one_thread_failure_isolated_but_auth_is_connection_failure(code):
    c, _ = connector(thread())
    other = thread(id="other")
    def get(path):
        if path.endswith("/environment"):
            return dict(serverVersion="0.0.38")
        if path.endswith("/shell"):
            return dict(projects=[], threads=[thread(), other])
        if "thread-1" in path:
            raise T3Error("Test", code)
        return dict(thread=other)
    c.http.get = get
    if code == 401:
        with pytest.raises(T3Error):
            await c.refresh()
        return
    await c.refresh()
    assert "other" in c.states
    if code == 404:
        assert "thread-1" not in c.states
    else:
        assert c.states["thread-1"].capabilities == ("read",)
        assert "Controls disabled" in c.states["thread-1"].preview


def test_unknown_contract_is_read_only():
    features = negotiated_features(dict(serverVersion="1.0.0", capabilities=dict(threadSettlement=True)))
    s = thread_state("t3", thread(), {}, "e", features=features)
    assert s.capabilities == ("read",) and not s.backend_actions


def test_uncertain_snooze_reconciles_equivalent_iso_timestamps():
    from herdeck.t3 import _observed_effect
    from herdeck.t3_actions import semantic_command
    command = {"type": "thread.snooze", "snoozedUntil": "2026-09-06T13:00:00.123+00:00"}
    assert _observed_effect({"snoozedUntil": "2026-09-06T13:00:00.123Z"}, command)
    assert not _observed_effect({"snoozedUntil": "2026-09-06T13:00:00.124Z"}, command)
    assert not _observed_effect({"snoozedUntil": None}, command)
    sent = semantic_command("snooze", {}, {})["snoozedUntil"]
    assert len(sent.split(".")[1].split("+")[0]) == 3


def test_missing_pinned_thread_is_distinct_from_offline_server():
    from herdeck.model import AgentKey
    cfg = Config(servers=[], profiles={}, overview_order=["t3"], grid=(5, 3))
    cfg.view.language = "en"
    orch = Orchestrator(cfg, slots=13)
    orch.pins = {0: AgentKey("t3", "deleted-thread")}
    orch.apply_snapshot("t3", [])
    orch.set_connection("t3", True)
    assert orch.render().tiles[0].label == "Pinned · missing"
    orch.set_connection("t3", False)
    assert orch.render().tiles[0].label == "Pinned · offline"


@pytest.mark.parametrize("age,expected", [(-10, Status.DONE), (0, Status.DONE), (299, Status.DONE), (300, Status.IDLE), (36000, Status.IDLE)])
def test_temporary_done_timeout_uses_completion_time(age, expected):
    t = thread(latestTurn={"state": "completed", "completedAt": "2026-09-06T12:00:00Z"})
    s = thread_state("t3", t, {}, "e", now=NOW + age, done_ttl=300)
    assert s.status == expected
    assert ("acknowledge" in s.capabilities) == (expected == Status.DONE)


def test_done_timeout_disabled_and_other_attention_is_preserved():
    t = thread(latestTurn=DONE)
    assert thread_state("t3", t, {}, "e", now=NOW, done_ttl=0).status == Status.DONE
    for extra, expected in [
        ({"hasPendingApprovals": True}, Status.BLOCKED),
        ({"hasPendingUserInput": True}, Status.BLOCKED),
        ({"hasActionableProposedPlan": True}, Status.BLOCKED),
        ({"backgroundLiveness": "working"}, Status.WORKING),
        ({"backgroundLiveness": "monitoring"}, Status.WAITING),
        ({"session": {"status": "error"}}, Status.UNKNOWN),
    ]:
        assert thread_state("t3", {**t, **extra}, {}, "e", now=NOW, done_ttl=300).status == expected


@pytest.mark.asyncio
async def test_done_expires_with_cached_detail_without_persisting_seen(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDECK_T3_DONE_TTL_SECONDS", "300")
    c, _ = connector(thread(latestTurn=DONE))
    c._seen = SeenStore(tmp_path, "ttl")
    await c.refresh()
    assert c.states["thread-1"].status == Status.IDLE
    assert not c._seen.path.exists()
    # Expiry is derived each refresh, not stored in cached thread details.
    c.http.thread["latestTurn"] = {"state": "completed", "completedAt": datetime.now(UTC).isoformat()}
    await c.refresh()
    previous = c.states["thread-1"].backend_revision
    assert c.states["thread-1"].status == Status.DONE
    c._done_ttl = 0.000001
    await c.refresh()
    assert c.states["thread-1"].status == Status.IDLE
    assert c.states["thread-1"].backend_revision != previous
    assert not c._seen.path.exists()
