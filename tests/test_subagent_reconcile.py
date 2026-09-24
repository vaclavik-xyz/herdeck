"""Bridge transcript fallback for subagents (subagent_reconcile.py).

Fixture transcripts follow the shapes seen on Claude Code 2.1.28x (parent
session ``.jsonl`` with ``<task-notification>`` user messages and
``toolUseResult`` records) and codex-cli 0.12x-0.15x (child rollouts whose
``session_meta`` carries ``source.subagent.thread_spawn.parent_thread_id`` and
whose turns end with an ``event_msg`` ``task_complete``)."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import threading

import pytest

from herdeck import subagent_hook as hook
from herdeck import subagent_reconcile as rc
from herdeck.subagent_spool import SubagentSpoolReader

PANE = "w2-p1"
NOW = 1_790_000_000_000  # 2026-09-21
MIN = 60_000


@pytest.fixture
def env(tmp_path):
    return {"HERDECK_SUBAGENT_SPOOL_DIR": str(tmp_path / "spool")}


def iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).isoformat().replace("+00:00", "Z")


def make_spool(env, *entries, session="sess-1", reported=None):
    spool = {"version": hook.SPOOL_VERSION, "pane": PANE, "session_id": session, "entries": []}
    for raw in entries:
        entry = {
            "id": "a1",
            "provider": "claude",
            "type": "",
            "description": "",
            "model": "",
            "depth": None,
            "started_ms": NOW - 5 * MIN,
            "ended_ms": None,
            "status": "running",
            "last_seen_ms": NOW - 5 * MIN,
            **dict.fromkeys(hook.PATH_FIELDS, ""),
        }
        entry.update(raw)
        spool["entries"].append(entry)
    if reported:
        spool["reported"] = {"token": reported, "at_ms": NOW - 5 * MIN}
    hook.write_spool(hook.spool_path(PANE, env), spool)


def read_spool(env):
    with open(hook.spool_path(PANE, env), encoding="utf-8") as fh:
        return json.load(fh)


def by_id(env):
    return {e["id"]: e for e in read_spool(env)["entries"]}


def jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def notification(task_id, status, at_ms, summary="Agent finished"):
    content = (
        "<task-notification>\n"
        f"<task-id>{task_id}</task-id>\n"
        "<tool-use-id>toolu_01</tool-use-id>\n"
        f"<output-file>/tmp/tasks/{task_id}.output</output-file>\n"
        f"<status>{status}</status>\n"
        f"<summary>{summary}</summary>\n"
        "</task-notification>"
    )
    return {
        "type": "user",
        "message": {"role": "user", "content": content},
        "timestamp": iso(at_ms),
        "sessionId": "sess-1",
    }


def assistant(text="working", at_ms=NOW - 4 * MIN):
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        "timestamp": iso(at_ms),
    }


def tool_result(agent_id, status, at_ms):
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "content": "..."}]},
        "toolUseResult": {"status": status, "prompt": "do it", "agentId": agent_id, "content": []},
        "timestamp": iso(at_ms),
    }


# --- Claude ---------------------------------------------------------------------


def test_claude_background_completion_from_task_notification(tmp_path, env):
    parent = jsonl(tmp_path / "proj" / "sess-1.jsonl", [assistant(), notification("a1", "completed", NOW - MIN)])
    make_spool(env, {"transcript": str(parent)}, reported="1/1")
    assert rc.reconcile_spool(PANE, NOW, env=env) == "0/1"
    entry = by_id(env)["a1"]
    assert entry["status"] == "done" and entry["ended_ms"] == NOW - MIN


def test_claude_failed_notification_marks_failed(tmp_path, env):
    parent = jsonl(tmp_path / "s.jsonl", [notification("a1", "failed", NOW - MIN)])
    make_spool(env, {"transcript": str(parent)})
    rc.reconcile_spool(PANE, NOW, env=env)
    assert by_id(env)["a1"]["status"] == "failed"


def test_claude_foreground_completion_from_tool_result(tmp_path, env):
    parent = jsonl(tmp_path / "s.jsonl", [tool_result("a1", "completed", NOW - 2 * MIN)])
    make_spool(env, {"transcript": str(parent)})
    rc.reconcile_spool(PANE, NOW, env=env)
    assert by_id(env)["a1"]["status"] == "done"


def test_the_latest_record_wins_and_other_ids_do_not_match(tmp_path, env):
    both = notification("a2", "completed", NOW - 3 * MIN)
    both["message"]["content"] = (
        "<task-notification><task-id>a1</task-id><summary>x</summary></task-notification>"
        + both["message"]["content"]
    )
    parent = jsonl(
        tmp_path / "s.jsonl",
        [
            notification("a1", "completed", NOW - 4 * MIN),
            tool_result("a1", "async_launched", NOW - 3 * MIN),  # resumed: running again
            both,  # a1 has no status of its own here; a2's must not count for it
        ],
    )
    make_spool(env, {"id": "a1", "transcript": str(parent)}, {"id": "a2", "transcript": str(parent)})
    rc.reconcile_spool(PANE, NOW, env=env)
    entries = by_id(env)
    assert entries["a1"]["status"] == "running"
    assert entries["a2"]["status"] == "done"


@pytest.mark.parametrize(
    "records",
    [
        [notification("a1", "teleported", NOW - MIN)],  # unknown status: format drift
        [{"type": "user", "message": {"content": "<task-id>a1</task-id> no status"}}],
        [tool_result("a1", "teammate_spawned", NOW - MIN)],
        [],
    ],
)
def test_unrecognised_records_leave_the_entry_running(tmp_path, env, records):
    parent = tmp_path / "s.jsonl"
    parent.write_text("".join(json.dumps(r) + "\n" for r in records) + "{not json a1\n")
    make_spool(env, {"transcript": str(parent)}, reported="1/1")
    assert rc.reconcile_spool(PANE, NOW, env=env) is None
    assert by_id(env)["a1"]["status"] == "running"


def test_missing_transcripts_change_nothing(tmp_path, env):
    make_spool(env, {"transcript": str(tmp_path / "gone.jsonl")}, {"id": "a2"}, reported="2/2")
    before = read_spool(env)
    assert rc.reconcile_spool(PANE, NOW, env=env) is None
    assert read_spool(env) == before


def test_only_the_tail_is_read(tmp_path, env, monkeypatch):
    monkeypatch.setattr(rc, "TAIL_BYTES", 4096)
    parent = tmp_path / "s.jsonl"
    filler = [assistant("x" * 200) for _ in range(40)]  # ~12 KiB after the record
    jsonl(parent, [notification("a1", "completed", NOW - MIN), *filler])
    make_spool(env, {"transcript": str(parent)})
    rc.reconcile_spool(PANE, NOW, env=env)
    assert by_id(env)["a1"]["status"] == "running"


def test_a_written_subagent_transcript_revives_a_stale_entry(tmp_path, env):
    parent = jsonl(tmp_path / "proj" / "sess-1.jsonl", [assistant()])
    agent = jsonl(tmp_path / "proj" / "sess-1" / "subagents" / "agent-a1.jsonl", [assistant()])
    os.utime(agent, (NOW / 1000 - 30, NOW / 1000 - 30))
    make_spool(env, {"transcript": str(parent), "status": "stale", "last_seen_ms": NOW - 12 * MIN})
    rc.reconcile_spool(PANE, NOW, env=env)
    entry = by_id(env)["a1"]
    assert entry["status"] == "running" and entry["last_seen_ms"] == NOW - 30_000


def test_a_hostile_id_never_builds_a_path(tmp_path, env):
    make_spool(env, {"id": "../../x", "transcript": str(tmp_path / "s.jsonl")})
    assert rc.claude_finding(by_id(env)["../../x"]) == rc.Finding()


# --- Codex -----------------------------------------------------------------------

CHILD = "019dcbda-a44c-7d00-b29c-b591f5946406"
PARENT = "019dcbd8-4c67-7ee2-ad24-b622508c21a8"


def rollout(sessions, *events, child=CHILD, parent=PARENT, at_ms=NOW - 5 * MIN):
    day = dt.datetime.fromtimestamp(at_ms / 1000)
    path = sessions / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}" / f"rollout-{day:%Y-%m-%dT%H-%M-%S}-{child}.jsonl"
    meta = {
        "timestamp": iso(at_ms),
        "type": "session_meta",
        "payload": {
            "id": child,
            "source": {"subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": 1}}},
            "agent_nickname": "Peirce",
            "agent_role": "worker",
        },
    }
    return jsonl(path, [meta, *events])


def event(kind, at_ms, **payload):
    return {"timestamp": iso(at_ms), "type": "event_msg", "payload": {"type": kind, **payload}}


def codex_entry(sessions, **kw):
    return {"id": CHILD, "provider": "codex", "sessions_dir": str(sessions), **kw}


def test_codex_child_task_complete_marks_done(tmp_path, env):
    sessions = tmp_path / "codex" / "sessions"
    path = rollout(
        sessions,
        event("task_started", NOW - 4 * MIN),
        {"timestamp": iso(NOW - 2 * MIN), "type": "response_item", "payload": {"type": "message"}},
        event("task_complete", NOW - 2 * MIN, last_agent_message="DONE"),
    )
    make_spool(env, codex_entry(sessions), session=PARENT)
    assert rc.reconcile_spool(PANE, NOW, env=env) == "0/1"
    entry = by_id(env)[CHILD]
    assert entry["status"] == "done" and entry["ended_ms"] == NOW - 2 * MIN
    assert entry["agent_transcript"] == str(path)  # found once, kept


def test_codex_aborted_turn_marks_failed(tmp_path, env):
    sessions = tmp_path / "sessions"
    rollout(sessions, event("turn_aborted", NOW - MIN, reason="interrupted"))
    make_spool(env, codex_entry(sessions), session=PARENT)
    rc.reconcile_spool(PANE, NOW, env=env)
    assert by_id(env)[CHILD]["status"] == "failed"


def test_codex_child_that_started_a_new_turn_stays_running(tmp_path, env):
    sessions = tmp_path / "sessions"
    rollout(sessions, event("task_complete", NOW - 3 * MIN), event("task_started", NOW - 2 * MIN))
    make_spool(env, codex_entry(sessions), session=PARENT)
    rc.reconcile_spool(PANE, NOW, env=env)
    assert by_id(env)[CHILD]["status"] == "running"


def test_codex_rollout_of_another_parent_is_ignored(tmp_path, env):
    sessions = tmp_path / "sessions"
    rollout(sessions, event("task_complete", NOW - MIN), parent="someone-else")
    make_spool(env, codex_entry(sessions), session=PARENT)
    rc.reconcile_spool(PANE, NOW, env=env)
    assert by_id(env)[CHILD]["status"] == "running"


def test_codex_without_a_rollout_or_with_drifted_events_is_left_alone(tmp_path, env):
    sessions = tmp_path / "sessions"
    make_spool(env, codex_entry(sessions), session=PARENT, reported="1/1")
    assert rc.reconcile_spool(PANE, NOW, env=env) is None
    rollout(sessions, event("task_finished_v2", NOW - MIN), {"type": "event_msg", "payload": "odd"})
    rc.reconcile_spool(PANE, NOW, env=env)
    assert by_id(env)[CHILD]["status"] == "running"


def test_codex_falls_back_to_the_default_sessions_tree(tmp_path, env):
    sessions = tmp_path / "home" / ".codex" / "sessions"
    rollout(sessions, event("task_complete", NOW - MIN))
    make_spool(env, {"id": CHILD, "provider": "codex"}, session=PARENT)
    rc.reconcile_spool(PANE, NOW, env=env, sessions_default=str(sessions))
    assert by_id(env)[CHILD]["status"] == "done"
    assert rc.default_codex_sessions({"CODEX_HOME": "/c"}) == "/c/sessions"


# --- spool rules -----------------------------------------------------------------------


def test_finished_and_opencode_entries_are_not_touched(tmp_path, env):
    parent = jsonl(tmp_path / "s.jsonl", [notification("a1", "failed", NOW - MIN)])
    make_spool(
        env,
        {"transcript": str(parent), "status": "done", "ended_ms": NOW - 4 * MIN},
        {"id": "oc1", "provider": "opencode"},
    )
    rc.reconcile_spool(PANE, NOW, env=env)
    entries = by_id(env)
    assert entries["a1"]["status"] == "done" and entries["a1"]["ended_ms"] == NOW - 4 * MIN
    assert entries["oc1"]["status"] == "running"


def test_mark_reported_only_records_a_current_token(tmp_path, env):
    parent = jsonl(tmp_path / "s.jsonl", [notification("a1", "completed", NOW - MIN)])
    make_spool(env, {"transcript": str(parent)}, reported="1/1")
    token = rc.reconcile_spool(PANE, NOW, env=env)
    rc.mark_reported(PANE, "1/1", NOW, env)  # outdated: ignored
    assert read_spool(env)["reported"]["token"] == "1/1"
    rc.mark_reported(PANE, token, NOW, env)
    assert read_spool(env)["reported"] == {"token": "0/1", "at_ms": NOW}
    # now the hook's own next event sees nothing new to report
    assert rc.reconcile_spool(PANE, NOW, env=env) is None


def test_a_held_spool_lock_is_skipped_not_waited_for(tmp_path, env, monkeypatch):
    monkeypatch.setattr(rc, "LOCK_BUDGET_S", 0.05)
    make_spool(env, {})
    path = hook.spool_path(PANE, env)
    import time

    with hook._SpoolLock(path, time.monotonic() + 1):
        assert rc.reconcile_spool(PANE, NOW, env=env) is None


# --- reader + loop ---------------------------------------------------------------------


def test_reader_lists_panes_with_open_entries(tmp_path, env):
    make_spool(env, {"status": "done", "ended_ms": NOW})
    reader = SubagentSpoolReader(env["HERDECK_SUBAGENT_SPOOL_DIR"], clock=lambda: NOW / 1000)
    reader.attach([{"pane_id": PANE}, {"pane_id": "other"}])
    assert reader.open_panes() == []
    make_spool(env, {"status": "stale"})
    reader.attach([{"pane_id": PANE}])
    assert reader.open_panes() == [PANE]


class FakeReader:
    def __init__(self, panes):
        self.panes = panes

    def open_panes(self):
        return list(self.panes)


class FakeHerdr:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def report_metadata(self, pane_id, source, tokens, ttl_ms):
        self.calls.append((pane_id, source, tokens, ttl_ms))
        if self.fail:
            raise ConnectionError("herdr down")


async def test_tick_reports_the_new_token_and_retries_after_a_failure(tmp_path, env, monkeypatch):
    parent = jsonl(tmp_path / "s.jsonl", [notification("a1", "completed", NOW - MIN)])
    make_spool(env, {"transcript": str(parent)}, reported="1/1")
    threads = []
    real = rc.reconcile_spool

    def spy(*a, **k):
        threads.append(threading.get_ident())
        return real(*a, **k)

    monkeypatch.setattr(rc, "reconcile_spool", spy)
    down = FakeHerdr(fail=True)
    rec = rc.SubagentReconciler(FakeReader([PANE]), down, clock=lambda: NOW / 1000, env=env)
    assert await rec.tick() == {}
    assert read_spool(env)["reported"]["token"] == "1/1"  # not recorded: herdr refused
    assert threads and threading.get_ident() not in threads  # file work off the loop
    up = FakeHerdr()
    rec = rc.SubagentReconciler(FakeReader([PANE]), up, clock=lambda: NOW / 1000, env=env)
    assert await rec.tick() == {PANE: "0/1"}
    assert up.calls == [(PANE, "herdeck:subagents", {"subagents": "0/1"}, hook.TOKEN_TTL_MS)]
    assert read_spool(env)["reported"]["token"] == "0/1"
    assert await rec.tick() == {}


async def test_run_survives_a_failing_pass(env):
    class Boom:
        def open_panes(self):
            raise RuntimeError("boom")

    rec = rc.SubagentReconciler(Boom(), FakeHerdr(), interval=0.01, env=env)
    task = asyncio.create_task(rec.run())
    await asyncio.sleep(0.05)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_socket_herdr_report_metadata_sends_the_token(monkeypatch):
    from herdeck.bridge import SocketHerdr

    sent = []

    async def fake_rpc(self, method, params, *, retry=True):
        sent.append((method, params, retry))
        return {"result": {}}

    monkeypatch.setattr(SocketHerdr, "_rpc", fake_rpc)
    await SocketHerdr("/nowhere").report_metadata("p1", "herdeck:subagents", {"subagents": "0/2"}, 900000)
    ((method, params, retry),) = sent
    assert method == "pane.report_metadata" and retry is False
    assert params["pane_id"] == "p1" and params["tokens"] == {"subagents": "0/2"}
    assert params["ttl_ms"] == 900000 and params["source"] == "herdeck:subagents"
    assert isinstance(params["seq"], int)
