"""T3Connector on orchestration protocol 2: reads, caching, delivery rules."""
import copy
import json
import logging
import uuid
from pathlib import Path

import pytest

from herdeck import t3 as t3mod
from herdeck.config import Config, ServerConfig
from herdeck.model import AgentKey, AgentState, Status
from herdeck.orchestrator import Orchestrator
from herdeck.t3 import T3Connector, T3Error, T3Http
from herdeck.t3_client import T3Rejected
from herdeck.t3_state import timestamp

FIX = Path(__file__).parent / "fixtures" / "t3v2"
NOW = timestamp("2026-10-14T16:00:00.000Z")
OPERATE = "T3 credential lacks orchestration:operate — actions disabled.\n"


def fx(name):
    return json.loads((FIX / f"{name}.json").read_text())


def thread(name, tid=None, **overrides):
    """(shell thread, bounded projection) of a fixture, optionally re-identified."""
    shell = fx(f"shell_{name}")["threads"][0]
    detail = fx(f"detail_{name}")["projection"]
    if tid:
        shell["id"] = tid
        detail["thread"]["id"] = tid
    shell.update(overrides)
    return shell, detail


class FakeClient:
    """In-memory T3Client: scripted descriptor/shell/detail and dispatch outcomes."""

    def __init__(self, *threads, archived=()):
        self.desc = fx("descriptor")
        self.projects = fx("shell_completed")["projects"]
        self.threads = [s for s, _ in threads]
        self.archived = [s for s, _ in archived]
        self.details = {s["id"]: d for s, d in [*threads, *archived]}
        self.full = {}
        self.descriptor_calls = 0
        self.detail_calls = []
        self.full_calls = []
        self.commands = []
        self.outcomes = []
        self.shell_error = None
        self.shell_script = []  # per-call errors (None = success), then shell_error
        self.detail_errors = {}

    def descriptor(self):
        self.descriptor_calls += 1
        return copy.deepcopy(self.desc)

    def shell(self):
        error = self.shell_script.pop(0) if self.shell_script else self.shell_error
        if error:
            raise error
        return copy.deepcopy({"schemaVersion": 2, "snapshotSequence": 1, "threads": self.threads,
                              "archivedThreads": self.archived, "projects": self.projects})

    def thread_detail(self, tid):
        self.detail_calls.append(tid)
        if tid in self.detail_errors:
            raise self.detail_errors[tid]
        return copy.deepcopy(self.details[tid])

    def full_detail(self, tid):
        self.full_calls.append(tid)
        return copy.deepcopy(self.full[tid])

    async def dispatch(self, command):
        self.commands.append(copy.deepcopy(command))
        outcome = self.outcomes.pop(0) if self.outcomes else {"sequence": len(self.commands)}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class Harness:
    def __init__(self, client, server=None, **kwargs):
        self.client = client
        self.results, self.snapshots, self.connections = [], [], []
        self.c = T3Connector(server or ServerConfig("t3", "http://127.0.0.1:13773", "secret", "t3"),
                             on_snapshot=lambda sid, states: self.snapshots.append((sid, states)),
                             on_event=lambda *a: None,
                             on_connection=lambda sid, up: self.connections.append((sid, up)),
                             on_result=lambda req, data: self.results.append(data),
                             client=client, **kwargs)
        self.c._clock = lambda: NOW
        self.c._retry_delay = 0
        self.c._interval = 0

    def state(self, tid):
        return self.c.states[tid]

    async def press(self, tid, action, payload=None, *, revision=None, text=None, req="r"):
        state = self.c.states[tid]
        if payload is None:
            payload = next((a["payload"] for a in state.backend_actions if a["id"] == action), {})
        msg = {"type": "backend_action", "pane_id": tid, "action": action, "payload": payload,
               "revision": state.backend_revision if revision is None else revision, "req": req}
        if text is not None:
            msg["text"] = text
        await self.c.send(msg)
        return msg

    async def run_rounds(self, rounds=1):
        """Run the poll loop for ``rounds`` iterations."""
        left = [rounds]
        original = self.c._on_connection

        def on_connection(sid, up):
            original(sid, up)
            left[0] -= 1
            if left[0] <= 0:
                self.c.stop()
        self.c._stop = False
        self.c._on_connection = on_connection
        try:
            await self.c.run()
        finally:
            self.c._on_connection = original


def tid_of(name):
    return fx(f"shell_{name}")["threads"][0]["id"]


# --- connection ------------------------------------------------------------


@pytest.mark.asyncio
async def test_incompatible_descriptor_is_offline_with_the_protocol_message():
    client = FakeClient(thread("completed"))
    client.desc = {"environmentId": "e", "serverVersion": "0.0.45"}
    h = Harness(client)
    await h.run_rounds()
    message = "T3 server speaks orchestration protocol 1; Herdeck needs 2 (T3 0.0.46+)"
    assert h.c.health()["last_error"] == message
    assert h.c.last_connect_error == message
    assert h.connections[0] == ("t3", False)
    assert h.c.health()["protocol"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("code,message", [
    (401, "T3 credential invalid or expired"),
    (403, "T3 credential lacks orchestration:read"),
    (400, "T3 orchestration protocol mismatch (Herdeck needs 2)"),
    (500, "T3 unavailable; check the connection"),
    (None, "T3 unavailable; check the connection"),
])
async def test_shell_errors_take_the_server_offline_with_plain_messages(code, message):
    client = FakeClient(thread("completed"))
    client.shell_error = T3Error("T3 HTTP x", code)
    h = Harness(client)
    await h.run_rounds()
    assert h.c.health()["last_error"] == message
    assert h.connections[0] == ("t3", False)


@pytest.mark.asyncio
async def test_descriptor_is_negotiated_once_per_connection_and_again_after_a_drop():
    client = FakeClient(thread("completed"))
    client.shell_script = [None, None, T3Error("down"), None]
    h = Harness(client)
    epoch = h.c._epoch
    await h.run_rounds(4)
    assert [up for _, up in h.connections][:4] == [True, True, False, True]
    assert client.descriptor_calls == 2
    assert h.c._epoch != epoch and h.c.health()["ever_connected"] is True


@pytest.mark.asyncio
async def test_incompatible_server_counts_as_answered_so_its_error_stays_visible():
    client = FakeClient(thread("completed"))
    client.desc = {"environmentId": "e", "serverVersion": "0.0.45"}
    h = Harness(client)
    await h.run_rounds()
    health = h.c.health()
    assert health["ever_connected"] is True and health["connected"] is False
    assert health["last_error"] == "T3 server speaks orchestration protocol 1; Herdeck needs 2 (T3 0.0.46+)"


@pytest.mark.asyncio
async def test_an_http_error_answer_counts_as_answered():
    client = FakeClient(thread("completed"))
    client.shell_error = T3Error("T3 HTTP 401", 401)
    h = Harness(client)
    await h.run_rounds()
    health = h.c.health()
    assert health["ever_connected"] is True and health["connected"] is False
    assert health["last_error"] == "T3 credential invalid or expired"


@pytest.mark.asyncio
async def test_a_refused_connection_before_any_answer_stays_never_connected():
    client = FakeClient(thread("completed"))

    def refused():
        raise T3Error("T3 transport or response error")
    client.descriptor = refused
    h = Harness(client)
    await h.run_rounds()
    health = h.c.health()
    assert health["ever_connected"] is False and health["connected"] is False
    assert health["last_error"] == "T3 unavailable; check the connection"


def test_health_before_any_connect_reports_never_connected():
    h = Harness(FakeClient(thread("completed")))
    health = h.c.health()
    assert health["connected"] is False and health["ever_connected"] is False
    assert health["self_update"] is False and health["managed"] is None
    assert health["bridge_version"] is None and health["protocol"] == 2
    assert T3Connector.protocol == 2 and T3Connector.capabilities == frozenset()


# --- reads -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_carries_approval_actions():
    h = Harness(FakeClient(thread("approval")))
    await h.c.refresh()
    sid, states = h.snapshots[-1]
    assert sid == "t3" and len(states) == 1
    s = states[0]
    assert s.backend == "t3" and s.status == Status.BLOCKED and s.attention == "approval"
    assert [a["id"] for a in s.backend_actions][:3] == ["approve", "approve_session", "deny"]
    assert {"read", "approve", "approve_session", "deny"} <= set(s.capabilities)
    assert s.project == "proj" and s.repo == "/Users/dev/proj"


@pytest.mark.asyncio
async def test_detail_is_cached_until_the_key_changes_or_forced():
    client = FakeClient(thread("completed"))
    tid = tid_of("completed")
    h = Harness(client)
    await h.c.refresh()
    await h.c.refresh()
    assert client.detail_calls == [tid]
    client.threads[0]["updatedAt"] = "2026-10-14T15:30:00.000Z"
    await h.c.refresh()
    assert client.detail_calls == [tid, tid]
    await h.c.refresh(force=tid)
    assert client.detail_calls == [tid, tid, tid]


@pytest.mark.asyncio
async def test_inactive_threads_stay_in_the_snapshot_without_a_detail_read():
    archived = thread("completed", tid="old-thread")
    client = FakeClient(thread("completed"), archived=[archived])
    h = Harness(client)
    await h.c.refresh()
    assert client.detail_calls == [tid_of("completed")]
    old = h.state("old-thread")
    assert old.lifecycle == "archived" and old.status == Status.IDLE
    assert old.capabilities == ("read",) and old.backend_actions == []


@pytest.mark.asyncio
async def test_detail_404_drops_the_thread_and_other_errors_mark_it_stale():
    client = FakeClient(thread("completed"), thread("approval", tid="t-approval"))
    h = Harness(client)
    await h.c.refresh()
    revision = h.state("t-approval").backend_revision
    client.detail_errors = {tid_of("completed"): T3Error("T3 HTTP 404", 404),
                            "t-approval": T3Error("T3 HTTP 500", 500)}
    client.threads[0]["updatedAt"] = client.threads[1]["updatedAt"] = "2026-10-14T15:30:00.000Z"
    await h.c.refresh()
    assert set(h.c.states) == {"t-approval"}
    s = h.state("t-approval")
    assert s.capabilities == ("read",) and s.backend_actions == []
    assert s.backend_revision != revision and s.backend_revision.endswith(":stale")
    assert s.state_labels == {s.status.value: "STALE"}
    assert s.preview.startswith("Stale detail")
    assert "approval-probe" in s.preview  # the cached detail is kept


@pytest.mark.asyncio
async def test_connector_notices_follow_the_deck_language():
    client = FakeClient(thread("completed"), thread("approval", tid="t-approval"))
    client.outcomes = [T3Rejected("credential lacks orchestration:operate")]
    h = Harness(client, language="cs")
    await h.c.refresh()
    assert h.state("t-approval").preview == "Run `touch approval-probe.txt`?"
    client.detail_errors = {"t-approval": T3Error("T3 HTTP 500", 500)}
    client.threads[1]["updatedAt"] = "2026-10-14T15:30:00.000Z"
    await h.c.refresh()
    s = h.state("t-approval")
    assert s.progress == "Zastaralý detail"
    assert s.preview.startswith("Zastaralý detail. Ovládání je vypnuté. Zkontroluj T3.\nRun `touch")
    await h.press(tid_of("completed"), "continue", text="go")
    assert h.state(tid_of("completed")).preview.startswith(
        "Token T3 nemá oprávnění orchestration:operate — akce jsou vypnuté.\n")


def test_the_connector_factory_passes_the_language_only_to_t3():
    from herdeck.config import DEFAULT_PROFILES
    from herdeck.connector import Connector, create_connector
    from herdeck.deckapp.live import build_live_source

    def cb(*a):
        return None
    t3 = create_connector(ServerConfig("t3", "http://127.0.0.1:13773", "x", "t3"), on_snapshot=cb,
                          on_event=cb, on_connection=cb, language="cs")
    assert isinstance(t3, T3Connector) and t3._lang == "cs"
    herdr = create_connector(ServerConfig("h", "ws://h", "x"), on_snapshot=cb, on_event=cb,
                             on_connection=cb, language="cs")
    assert isinstance(herdr, Connector)
    seen = []

    class Runner:
        def __init__(self, conn):
            pass

        def start(self):
            pass

    config = Config(servers=[ServerConfig("t3", "http://127.0.0.1:3773", "x", "t3")],
                    profiles=dict(DEFAULT_PROFILES), overview_order=["t3"], grid=(5, 3))
    config.view.language = "cs"
    build_live_source(config, connector_factory=lambda server, **kw: seen.append(kw.get("language")),
                      runner_factory=Runner)
    assert seen == ["cs"]


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [401, 403])
async def test_detail_auth_errors_take_the_connection_down(code):
    client = FakeClient(thread("completed"))
    client.detail_errors = {tid_of("completed"): T3Error("x", code)}
    h = Harness(client)
    with pytest.raises(T3Error):
        await h.c.refresh()


@pytest.mark.asyncio
async def test_a_plan_kept_only_in_a_turn_item_is_read_from_the_full_detail():
    shell, detail = thread("plan", status="completed")
    full = copy.deepcopy(detail)
    markdown = detail["plans"][-1]["markdown"]
    detail["plans"][-1].update(markdown="", detailInTurnItem=True)
    detail["turnItems"] = [i for i in detail["turnItems"] if i.get("type") != "proposed_plan"]
    detail["visibleTurnItems"] = []
    client = FakeClient((shell, detail))
    client.full[shell["id"]] = full
    h = Harness(client)
    await h.c.refresh()
    assert client.full_calls == [shell["id"]]
    assert markdown.strip()[:40] in h.state(shell["id"]).preview


@pytest.mark.asyncio
async def test_a_malformed_thread_does_not_break_the_refresh(caplog):
    good = thread("approval", tid="good")
    bad_options = thread("input", tid="bad-options")
    for item in bad_options[1]["turnItems"]:
        if item.get("type") == "user_input_request":
            del item["questions"][0]["options"][0]["label"]
    bad_shell = thread("completed", tid="bad-shell", pendingRuntimeRequest="garbage")
    client = FakeClient(good, bad_options, bad_shell)
    client.threads.append({"title": "no id"})
    h = Harness(client)
    with caplog.at_level(logging.WARNING, logger="herdeck.t3"):
        await h.c.refresh()
    assert "approve" in h.state("good").capabilities
    assert h.state("bad-options").capabilities == ("read",)
    assert h.state("bad-options").backend_actions == []
    assert "bad-shell" not in h.c.states or h.state("bad-shell").capabilities == ("read",)
    assert "malformed" in caplog.text


# --- subagents -------------------------------------------------------------


def family(name, details):
    """Every thread of a multi-thread shell fixture, each with its detail (or {})."""
    shells = fx(f"shell_{name}")["threads"]
    return [(s, fx(details[s["id"]])["projection"] if s["id"] in details else {}) for s in shells]


@pytest.mark.asyncio
async def test_subagent_children_are_not_tiles_and_their_detail_is_not_read():
    threads = family("subagent", {tid_of("subagent"): "detail_subagent"})
    parent, fork = threads[0][0]["id"], threads[-1][0]["id"]
    client = FakeClient(*threads)
    h = Harness(client)
    await h.c.refresh()
    assert set(h.c.states) == {parent, fork}  # a fork is a thread of its own
    assert [s.key.pane_id for s in h.snapshots[-1][1]] == [parent, fork]
    assert set(client.detail_calls) == {parent, fork}
    s = h.state(parent)
    assert (s.subagents_running, s.subagents_total) == (2, 4)
    assert s.subagents[1].type == "Read tsconfig"


@pytest.mark.asyncio
async def test_a_subagent_update_refreshes_the_list_but_keeps_the_revision():
    client = FakeClient(*family("subagent", {tid_of("subagent"): "detail_subagent"}))
    parent = tid_of("subagent")
    h = Harness(client)
    await h.c.refresh()
    revision = h.state(parent).backend_revision
    # subagent.updated bumps the parent projection's updatedAt (ProjectionStore
    # applyToProjection), so the parent's next detail read carries the change.
    for sub in client.details[parent]["subagents"]:
        sub["status"] = "completed"
    client.threads[0]["updatedAt"] = "2026-10-14T15:07:00.000Z"
    await h.c.refresh()
    s = h.state(parent)
    assert s.subagents_running == 0 and {x.status for x in s.subagents} == {"done"}
    assert s.backend_revision == revision


@pytest.mark.asyncio
async def test_a_native_subagent_approval_is_answered_on_the_parent():
    """Codex/OpenCode ask a subagent's approval on the parent thread and run;
    the child shell never holds the request (subagent_v2_approval fixture)."""
    parent_shell, parent_detail = thread("approval")
    child = copy.deepcopy(parent_shell)
    child.update(id="child", pendingRuntimeRequest=None, status="idle",
                 lineage={"parentThreadId": parent_shell["id"], "relationshipToParent": "subagent",
                          "rootThreadId": parent_shell["id"]})
    client = FakeClient((parent_shell, parent_detail), (child, {}))
    h = Harness(client)
    await h.c.refresh()
    assert set(h.c.states) == {parent_shell["id"]}
    await h.press(parent_shell["id"], "approve")
    assert client.commands[0]["threadId"] == parent_shell["id"]


@pytest.mark.asyncio
async def test_a_delegated_child_holding_its_own_request_stays_a_tile_until_answered():
    """A delegate_task child (creationSource "mcp") runs its own provider
    session; T3 does not roll its request up to the parent shell."""
    child = fx("shell_subagent_request")["threads"][1]["id"]
    parent = tid_of("subagent_request")
    client = FakeClient(*family("subagent_request", {parent: "detail_subagent_request_parent",
                                                     child: "detail_subagent_request"}))
    h = Harness(client)
    await h.c.refresh()
    assert set(h.c.states) == {parent, child}
    s = h.state(child)
    assert s.status == Status.BLOCKED and s.attention == "approval" and "approve" in s.capabilities
    p = h.state(parent)
    assert p.status == Status.WAITING and p.subagents_running == 1
    await h.press(child, "approve")
    assert client.commands[0]["threadId"] == child
    assert client.commands[0]["requestId"] == "req-approval-1"
    client.threads[1].update(pendingRuntimeRequest=None, status="running",
                             updatedAt="2026-10-14T15:20:00.000Z")
    await h.c.refresh()
    assert set(h.c.states) == {parent}
    assert child not in h.c._cache


# --- delivery --------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_dispatches_a_runtime_response_with_a_uuid_command_id():
    client = FakeClient(thread("approval"))
    tid = tid_of("approval")
    h = Harness(client)
    await h.c.refresh()
    await h.press(tid, "approve")
    assert len(client.commands) == 1
    command = client.commands[0]
    assert command["type"] == "runtime-request.respond" and command["threadId"] == tid
    assert command["requestId"] == "req-approval-1" and command["decision"] == "accept"
    assert uuid.UUID(command["commandId"]).version == 4 and "createdAt" not in command
    assert h.results == [{"ok": True, "sent": True, "accepted": True}]


@pytest.mark.asyncio
async def test_deny_matches_the_offered_option_by_payload():
    shell, detail = thread("approval")
    for item in detail["turnItems"]:
        if item.get("type") == "approval_request":
            item["options"].append({"decision": "cancel", "label": "Cancel run"})
    client = FakeClient((shell, detail))
    h = Harness(client)
    await h.c.refresh()
    cancel = {"requestId": "req-approval-1", "decision": "cancel"}
    await h.press(shell["id"], "deny", cancel)
    assert client.commands[0]["decision"] == "cancel"
    await h.c.refresh()
    await h.press(shell["id"], "deny", {"requestId": "req-approval-1", "decision": "bogus"})
    assert len(client.commands) == 1 and h.results[-1]["skipped"] is True


@pytest.mark.asyncio
async def test_stale_revision_is_skipped_without_dispatch():
    client = FakeClient(thread("approval"))
    h = Harness(client)
    await h.c.refresh()
    await h.press(tid_of("approval"), "approve", revision="old")
    assert client.commands == []
    assert h.results[-1]["ok"] is False and h.results[-1]["skipped"] is True


@pytest.mark.asyncio
async def test_resolved_request_rejects_the_stale_press():
    client = FakeClient(thread("approval"))
    tid = tid_of("approval")
    h = Harness(client)
    await h.c.refresh()
    revision = h.state(tid).backend_revision
    payload = h.state(tid).backend_actions[0]["payload"]
    client.threads[0].update(pendingRuntimeRequest=None, updatedAt="2026-10-14T15:30:00.000Z")
    await h.press(tid, "approve", payload, revision=revision)
    assert client.commands == [] and h.results[-1]["skipped"] is True


@pytest.mark.asyncio
async def test_transport_error_is_retried_once_with_the_same_command_id():
    client = FakeClient(thread("approval"))
    client.outcomes = [T3Error("T3 dispatch outcome unknown"), {"sequence": 7}]
    h = Harness(client)
    await h.c.refresh()
    await h.press(tid_of("approval"), "approve")
    assert len(client.commands) == 2
    assert client.commands[0] == client.commands[1]
    assert h.results == [{"ok": True, "sent": True, "accepted": True}]


@pytest.mark.asyncio
async def test_unknown_outcome_locks_the_thread_and_a_repress_reuses_the_command_id():
    client = FakeClient(thread("approval"))
    tid = tid_of("approval")
    client.outcomes = [T3Error("lost"), T3Error("lost")]
    h = Harness(client)
    await h.c.refresh()
    msg = await h.press(tid, "approve")
    assert len(client.commands) == 2
    assert h.results[-1] == {"ok": False, "uncertain": True,
                             "message": "T3 delivery uncertain; inspect the conversation before retrying"}
    s = h.state(tid)
    assert s.capabilities == ("read",) and s.backend_actions == []
    assert s.backend_revision.endswith(":uncertain")
    await h.c.refresh()
    assert h.state(tid).capabilities == ("read",)  # unchanged thread stays locked
    # The same press (same revision/action/payload) replays the remembered
    # command; the server dedupes by commandId, so it can never run twice.
    await h.c.send({**msg, "req": "again"})
    assert len(client.commands) == 3
    assert client.commands[2]["commandId"] == client.commands[0]["commandId"]
    assert h.results[-1] == {"ok": True, "sent": True, "accepted": True}


@pytest.mark.asyncio
async def test_uncertain_lock_clears_once_the_thread_changes():
    client = FakeClient(thread("approval"))
    tid = tid_of("approval")
    client.outcomes = [T3Error("lost"), T3Error("lost")]
    h = Harness(client)
    await h.c.refresh()
    await h.press(tid, "approve")
    client.threads[0].update(pendingRuntimeRequest=None, status="running",
                             updatedAt="2026-10-14T15:30:00.000Z")
    await h.c.refresh()
    s = h.state(tid)
    assert "stop" in s.capabilities and not s.backend_revision.endswith(":uncertain")


@pytest.mark.asyncio
async def test_a_repeat_press_before_t3_publishes_the_effect_replays_the_same_commands():
    # T3 accepted the reply but the shell has not changed yet: a double press
    # must replay the remembered command (server dedupe), never a new message.
    client = FakeClient(thread("completed"))
    tid = tid_of("completed")
    h = Harness(client)
    await h.c.refresh()
    msg = await h.press(tid, "continue", text="Next step")
    await h.c.send({**msg, "req": "again"})
    assert len(client.commands) == 2
    assert client.commands[0] == client.commands[1]  # same commandId and messageId
    assert [r["accepted"] for r in h.results] == [True, True]


@pytest.mark.asyncio
async def test_uncertain_lock_survives_a_stale_detail():
    client = FakeClient(thread("approval"))
    tid = tid_of("approval")
    client.outcomes = [T3Error("lost"), T3Error("lost")]
    h = Harness(client)
    await h.c.refresh()
    await h.press(tid, "approve")
    client.detail_errors = {tid: T3Error("T3 HTTP 500", 500)}
    client.threads[0].update(status="running", updatedAt="2026-10-14T15:30:00.000Z")
    await h.c.refresh()
    # The revision moved, but only on a stale detail: no evidence yet.
    assert h.state(tid).backend_revision.endswith(":stale")
    assert tid in h.c._uncertain
    del client.detail_errors[tid]
    await h.c.refresh()
    assert tid not in h.c._uncertain  # a fresh read shows the thread changed


@pytest.mark.asyncio
async def test_a_different_press_while_uncertain_is_skipped():
    client = FakeClient(thread("approval"))
    tid = tid_of("approval")
    client.outcomes = [T3Error("lost"), T3Error("lost")]
    h = Harness(client)
    await h.c.refresh()
    revision = h.state(tid).backend_revision
    deny = next(a["payload"] for a in h.state(tid).backend_actions if a["id"] == "deny")
    await h.press(tid, "approve")
    await h.press(tid, "deny", deny, revision=revision)
    assert len(client.commands) == 2 and h.results[-1]["skipped"] is True


@pytest.mark.asyncio
async def test_missing_operate_scope_disables_actions_but_keeps_reads():
    client = FakeClient(thread("approval"), thread("completed", tid="other"))
    tid = tid_of("approval")
    client.outcomes = [T3Rejected("credential lacks orchestration:operate")]
    h = Harness(client)
    await h.c.refresh()
    await h.press(tid, "approve")
    assert h.results[-1] == {"ok": False, "rejected": True,
                             "message": "credential lacks orchestration:operate"}
    assert len(client.commands) == 1  # a rejection is not retried
    await h.run_rounds()
    assert ("t3", True) in h.connections and h.c.last_connect_error is None
    for s in h.c.states.values():
        assert s.capabilities == ("read",) and s.backend_actions == []
        assert s.preview.startswith(OPERATE)


@pytest.mark.asyncio
async def test_a_rejected_command_mints_a_new_id_on_the_next_press():
    client = FakeClient(thread("approval"))
    tid = tid_of("approval")
    client.outcomes = [T3Rejected("request expired")]
    h = Harness(client)
    await h.c.refresh()
    await h.press(tid, "approve")
    assert h.results[-1]["rejected"] is True and h.results[-1]["message"] == "request expired"
    await h.press(tid, "approve")
    assert client.commands[0]["commandId"] != client.commands[1]["commandId"]


@pytest.mark.asyncio
async def test_a_refused_retry_handshake_after_an_unknown_attempt_is_uncertain():
    client = FakeClient(thread("approval"))
    tid = tid_of("approval")
    client.outcomes = [T3Error("lost"), T3Rejected("credential invalid or expired", handshake=True),
                       {"sequence": 3}]
    h = Harness(client)
    await h.c.refresh()
    msg = await h.press(tid, "approve")
    assert h.results[-1]["uncertain"] is True and "rejected" not in h.results[-1]
    assert tid in h.c._uncertain
    await h.c.send({**msg, "req": "again"})
    assert [c["commandId"] for c in client.commands] == [client.commands[0]["commandId"]] * 3


@pytest.mark.asyncio
async def test_a_handshake_rejection_on_the_first_attempt_is_still_a_rejection():
    client = FakeClient(thread("approval"))
    client.outcomes = [T3Rejected("credential lacks orchestration:operate", handshake=True)]
    h = Harness(client)
    await h.c.refresh()
    await h.press(tid_of("approval"), "approve")
    assert h.results[-1]["rejected"] is True and len(client.commands) == 1


@pytest.mark.asyncio
async def test_the_post_delivery_refresh_runs_under_the_lock():
    client = FakeClient(thread("approval"))
    h = Harness(client)
    await h.c.refresh()
    held = []
    original = h.c.refresh

    async def spy(force=None):
        held.append(h.c._lock.locked())
        return await original(force=force)
    h.c.refresh = spy
    await h.press(tid_of("approval"), "approve")
    assert held and all(held)  # the pre-press and post-delivery refreshes


@pytest.mark.asyncio
async def test_ticket_401_is_reported_as_an_invalid_credential_without_retry():
    client = FakeClient(thread("approval"))
    client.outcomes = [T3Error("T3 HTTP 401", 401)]
    h = Harness(client)
    await h.c.refresh()
    await h.press(tid_of("approval"), "approve")
    assert len(client.commands) == 1
    assert h.results[-1] == {"ok": False, "rejected": True, "message": "T3 credential invalid or expired"}


@pytest.mark.asyncio
async def test_acknowledge_visits_at_the_completion_time():
    client = FakeClient(thread("completed"))
    tid = tid_of("completed")
    h = Harness(client)
    await h.c.refresh()
    assert h.state(tid).status == Status.DONE
    await h.press(tid, "acknowledge")
    command = client.commands[0]
    assert command["type"] == "thread.visit"
    assert command["visitedAt"] == client.threads[0]["latestRunCompletedAt"]
    assert h.results[-1]["accepted"] is True


@pytest.mark.asyncio
async def test_implement_plan_sends_two_commands_in_order():
    client = FakeClient(thread("plan", status="completed"))
    tid = tid_of("plan")
    h = Harness(client)
    await h.c.refresh()
    await h.press(tid, "implement_plan")
    assert [c["type"] for c in client.commands] == ["thread.interaction-mode.set", "message.dispatch"]
    assert client.commands[0]["commandId"] != client.commands[1]["commandId"]
    assert h.results[-1]["accepted"] is True


@pytest.mark.asyncio
async def test_implement_plan_stops_after_a_rejected_first_command():
    client = FakeClient(thread("plan", status="completed"))
    client.outcomes = [T3Rejected("nope")]
    h = Harness(client)
    await h.c.refresh()
    await h.press(tid_of("plan"), "implement_plan")
    assert [c["type"] for c in client.commands] == ["thread.interaction-mode.set"]
    assert h.results[-1]["rejected"] is True


@pytest.mark.asyncio
async def test_a_reply_to_a_ready_plan_is_one_plan_mode_message():
    client = FakeClient(thread("plan", status="completed"))
    tid = tid_of("plan")
    h = Harness(client)
    await h.c.refresh()
    assert "continue" in h.state(tid).capabilities
    await h.press(tid, "continue", text="Drop step 3")
    [cmd] = client.commands
    assert cmd["type"] == "message.dispatch" and cmd["text"] == "Drop step 3"
    assert "sourcePlanRef" not in cmd
    assert h.results[-1]["accepted"] is True


@pytest.mark.asyncio
async def test_an_unavailable_action_is_reported_before_the_revision():
    client = FakeClient(thread("running"))
    h = Harness(client)
    await h.c.refresh()
    await h.press(tid_of("running"), "continue", text="hi", revision="old")
    assert client.commands == []
    assert h.results[-1] == {"ok": False, "skipped": True, "message": "T3 action is not available"}


@pytest.mark.asyncio
async def test_continue_with_overlong_text_is_skipped():
    client = FakeClient(thread("completed"))
    h = Harness(client)
    await h.c.refresh()
    await h.press(tid_of("completed"), "continue", text="x" * 5000)
    assert client.commands == []
    assert h.results[-1] == {"ok": False, "skipped": True,
                             "message": "T3 message must contain 1–4096 characters"}


@pytest.mark.asyncio
async def test_continue_and_stop_dispatch():
    client = FakeClient(thread("completed"), thread("running", tid="busy"))
    h = Harness(client)
    await h.c.refresh()
    await h.press(tid_of("completed"), "continue", text="Next step")
    await h.press("busy", "stop")
    assert [c["type"] for c in client.commands] == ["message.dispatch", "run.interrupt"]
    assert client.commands[0]["text"] == "Next step"
    assert client.commands[1]["runId"] == client.threads[1]["activeRunId"]


@pytest.mark.asyncio
async def test_read_returns_the_preview_and_list_only_refreshes():
    client = FakeClient(thread("approval"))
    tid = tid_of("approval")
    h = Harness(client)
    await h.c.send({"type": "list", "req": "l"})
    assert h.results == [] and tid in h.c.states
    await h.c.send({"type": "read", "req": "r", "pane_id": tid})
    assert h.results == [{"pane_id": tid, "text": h.state(tid).preview}]


@pytest.mark.asyncio
async def test_unknown_thread_is_skipped():
    h = Harness(FakeClient(thread("approval")))
    await h.c.send({"type": "backend_action", "req": "r", "pane_id": "nope", "action": "approve",
                    "revision": "x", "payload": {}})
    assert h.results[-1]["skipped"] is True


# --- configuration ---------------------------------------------------------


def test_desktop_read_state_is_obsolete_and_logs_once(caplog, monkeypatch):
    monkeypatch.setattr(t3mod, "_read_state_warned", False)
    server = ServerConfig("t3", "http://127.0.0.1:13773", "secret", "t3", True)
    with caplog.at_level(logging.WARNING, logger="herdeck.t3"):
        Harness(FakeClient(), server)
        Harness(FakeClient(), server)
    warning = ("[[servers]] desktop_read_state is obsolete: "
               "T3 0.0.46+ keeps read state on the server")
    assert caplog.text.count(warning) == 1


@pytest.mark.parametrize("url", ["http://example.com", "http://127.0.0.1/?token=secret",
                                 "http://u:p@127.0.0.1", "http://127.0.0.1/path"])
def test_endpoint_rejects_unsafe_or_ambiguous_urls(url):
    with pytest.raises(ValueError):
        T3Http(url, "secret")
    with pytest.raises(ValueError):
        T3Connector(ServerConfig("t3", url, "secret", "t3"), on_snapshot=None, on_event=None,
                    on_connection=None)


# --- deck integration ------------------------------------------------------


@pytest.mark.asyncio
async def test_mixed_deck_routes_t3_choice_without_terminal_keys():
    from herdeck.config import DEFAULT_PROFILES
    config = Config(servers=[ServerConfig("herdr", "ws://old", "x"),
                             ServerConfig("t3", "http://127.0.0.1:3773", "x", "t3")],
                    profiles=dict(DEFAULT_PROFILES), overview_order=["herdr", "t3"], grid=(5, 3))
    orch = Orchestrator(config)
    h = Harness(FakeClient(thread("approval")))
    await h.c.refresh()
    t3 = h.state(tid_of("approval"))
    orch.apply_snapshot("herdr", [AgentState(AgentKey("herdr", "p1"), "codex", "Old", Status.IDLE)])
    orch.apply_snapshot("t3", [t3])
    orch._drill = t3.key
    actions, _, _ = orch._drill_layout()
    cmd = actions[0]["make"](t3.key)
    assert cmd.kind == "backend_action" and cmd.server_id == "t3"
    assert cmd.payload == {"requestId": "req-approval-1", "decision": "accept"}
    assert cmd.keys == [] and cmd.terminal_id is None


@pytest.mark.asyncio
async def test_a_ready_plan_drill_offers_implement_and_the_reply_macros():
    from herdeck.config import DEFAULT_PROFILES
    config = Config(servers=[ServerConfig("t3", "http://127.0.0.1:3773", "x", "t3")],
                    profiles=dict(DEFAULT_PROFILES), overview_order=["t3"], grid=(5, 3))
    orch = Orchestrator(config)
    h = Harness(FakeClient(thread("plan", status="completed")))
    await h.c.refresh()
    t3 = h.state(tid_of("plan"))
    orch.apply_snapshot("t3", [t3])
    orch._drill = t3.key
    actions, _, _ = orch._drill_layout()
    assert actions[0]["id"] == "implement_plan"
    reply = next(a for a in actions if "id" not in a)
    cmd = reply["make"](t3.key)
    assert cmd.kind == "backend_action" and cmd.action == "continue" and cmd.text


# --- project favicons ------------------------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 32


class IconClient(FakeClient):
    def __init__(self, *threads, **kw):
        super().__init__(*threads, **kw)
        self.favicon_calls = []
        self.favicons = {}  # (root, path) -> (mime, bytes) | None | Exception

    async def project_favicon(self, root, path=None):
        self.favicon_calls.append((root, path))
        found = self.favicons.get((root, path))
        if isinstance(found, Exception):
            raise found
        return found


def _icon_harness(client):
    icons = []
    h = Harness(client, on_project_icon=lambda sid, icon: icons.append(icon))
    return h, icons


async def tick(h, rounds=1):
    """One poll-loop iteration as run() does it: refresh, then the favicon sync."""
    for _ in range(rounds):
        await h.c.refresh()
        await h.c._sync_icons()


async def test_a_project_favicon_from_t3_reaches_the_icon_store_then_the_tile():
    client = IconClient(thread("completed"))
    client.projects[0]["faviconPath"] = "app/icon.png"
    client.favicons[("/Users/dev/proj", "app/icon.png")] = ("image/png", PNG)
    h, icons = _icon_harness(client)
    await tick(h)
    tid = tid_of("completed")
    assert h.state(tid).project_icon == ""  # bytes arrive before any tile names the hash
    assert [(i.server_id, i.mime, i.data) for i in icons] == [("t3", "image/png", PNG)]
    await tick(h)
    assert h.state(tid).project_icon == icons[0].hash
    assert client.favicon_calls == [("/Users/dev/proj", "app/icon.png")]  # not re-fetched yet


async def test_a_project_without_a_favicon_keeps_the_monogram_and_is_retried_later():
    client = IconClient(thread("completed"))
    h, icons = _icon_harness(client)
    await tick(h, 2)
    assert icons == [] and h.state(tid_of("completed")).project_icon == ""
    assert client.favicon_calls == [("/Users/dev/proj", None)]
    client.favicons[("/Users/dev/proj", None)] = ("image/png", PNG)
    h.c._clock = lambda: NOW + t3mod._ICON_RETRY_S
    await tick(h, 2)
    assert len(icons) == 1 and h.state(tid_of("completed")).project_icon == icons[0].hash


async def test_a_failed_refetch_keeps_the_icon_already_shown():
    client = IconClient(thread("completed"))
    client.favicons[("/Users/dev/proj", None)] = ("image/png", PNG)
    h, icons = _icon_harness(client)
    await tick(h, 2)
    shown = h.state(tid_of("completed")).project_icon
    assert shown
    client.favicons[("/Users/dev/proj", None)] = T3Error("down")
    h.c._clock = lambda: NOW + t3mod._ICON_RECHECK_S
    await tick(h, 2)
    assert h.state(tid_of("completed")).project_icon == shown
    assert len(client.favicon_calls) == 2


async def test_favicons_are_not_fetched_without_an_icon_consumer():
    client = IconClient(thread("completed"))
    h = Harness(client)
    await tick(h, 2)
    assert client.favicon_calls == []


async def test_the_poll_loop_syncs_favicons_after_each_refresh():
    client = IconClient(thread("completed"))
    client.favicons[("/Users/dev/proj", None)] = ("image/png", PNG)
    h, icons = _icon_harness(client)
    await h.run_rounds(3)
    assert len(icons) == 1 and h.state(tid_of("completed")).project_icon == icons[0].hash


async def test_a_malformed_favicon_answer_never_takes_the_connection_down():
    client = IconClient(thread("completed"))
    client.favicons[("/Users/dev/proj", None)] = AttributeError("'list' object has no attribute 'get'")
    h, icons = _icon_harness(client)
    await h.run_rounds(3)
    assert h.connections[:3] == [("t3", True)] * 3  # then the shutdown's False
    assert icons == [] and client.favicon_calls == [("/Users/dev/proj", None)]  # retried later, not every poll
