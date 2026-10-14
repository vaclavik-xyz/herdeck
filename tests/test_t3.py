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
from herdeck.t3 import T3Connector, T3Error, T3Http, validate_shell
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
    def __init__(self, client, server=None):
        self.client = client
        self.results, self.snapshots, self.connections = [], [], []
        self.c = T3Connector(server or ServerConfig("t3", "http://127.0.0.1:13773", "secret", "t3"),
                             on_snapshot=lambda sid, states: self.snapshots.append((sid, states)),
                             on_event=lambda *a: None,
                             on_connection=lambda sid, up: self.connections.append((sid, up)),
                             on_result=lambda req, data: self.results.append(data),
                             client=client)
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


def test_validate_shell_requires_v2_lists():
    validate_shell({"threads": [], "archivedThreads": [], "projects": []})
    for bad in ({"threads": [], "projects": []}, {"threads": {}, "archivedThreads": [], "projects": []}, []):
        with pytest.raises(T3Error, match="Unsupported T3 shell contract"):
            validate_shell(bad)


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
