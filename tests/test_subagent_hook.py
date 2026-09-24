"""herdeck-subagent-hook: payload handling, spool rules, token reporting.

Payload fixtures follow the shapes observed on Claude Code 2.1.281 (hook
input + ``<session>/subagents/agent-<id>.meta.json`` + the Agent tool's
``tool_response``) and the JSON schemas embedded in codex-cli 0.156.1
(``subagent-start.command.input`` / ``subagent-stop.command.input``).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time

import pytest

from herdeck import subagent_hook as hook
from herdeck.model import parse_subagents_token

PANE = "w1-p3"
MIN = 60 * 1000


# --- fixtures ----------------------------------------------------------------


def claude_start(agent_id="a1", session="sess-1", transcript="/nowhere/sess-1.jsonl", **kw):
    payload = {
        "session_id": session,
        "transcript_path": transcript,
        "cwd": "/repo",
        "permission_mode": "default",
        "hook_event_name": "SubagentStart",
        "agent_id": agent_id,
        "agent_type": "general-purpose",
    }
    payload.update(kw)
    return payload


def claude_stop(agent_id="a1", session="sess-1", transcript="/nowhere/sess-1.jsonl"):
    return {
        "session_id": session,
        "transcript_path": transcript,
        "cwd": "/repo",
        "permission_mode": "default",
        "hook_event_name": "SubagentStop",
        "stop_hook_active": False,
        "agent_id": agent_id,
        "agent_type": "general-purpose",
        "agent_transcript_path": f"/nowhere/sess-1/subagents/agent-{agent_id}.jsonl",
        "last_assistant_message": "done",
    }


def claude_pre_tool(agent_id="a1", session="sess-1"):
    payload = {
        "session_id": session,
        "transcript_path": "/nowhere/sess-1.jsonl",
        "cwd": "/repo",
        "permission_mode": "default",
        "hook_event_name": "PreToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": "/repo/x"},
        "tool_use_id": "toolu_1",
    }
    if agent_id:
        payload.update(agent_id=agent_id, agent_type="Explore")
    return payload


def claude_post_agent(response, session="sess-1"):
    return {
        "session_id": session,
        "transcript_path": "/nowhere/sess-1.jsonl",
        "cwd": "/repo",
        "permission_mode": "default",
        "hook_event_name": "PostToolUse",
        "tool_name": "Agent",
        "tool_input": {
            "description": "Review recent changes",
            "prompt": "…",
            "subagent_type": "code-reviewer",
        },
        "tool_response": response,
        "tool_use_id": "toolu_2",
    }


def claude_session_start(source="startup", session="sess-1"):
    return {
        "session_id": session,
        "transcript_path": f"/nowhere/{session}.jsonl",
        "cwd": "/repo",
        "hook_event_name": "SessionStart",
        "source": source,
    }


def codex_start(agent_id="019a-child", session="019a-parent"):
    return {
        "agent_id": agent_id,
        "agent_type": "worker",
        "cwd": "/repo",
        "hook_event_name": "SubagentStart",
        "model": "gpt-5.5-codex",
        "permission_mode": "default",
        "session_id": session,
        "transcript_path": None,
        "turn_id": "turn-1",
    }


def codex_stop(agent_id="019a-child", session="019a-parent"):
    payload = codex_start(agent_id, session)
    payload.update(
        hook_event_name="SubagentStop",
        agent_transcript_path=None,
        last_assistant_message="ok",
        stop_hook_active=False,
    )
    return payload


class Clock:
    def __init__(self, ms=1_000_000_000_000):
        self.ms = ms

    def __call__(self):
        return self.ms


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, pane, token, timeout_s):
        self.calls.append((pane, token))


@pytest.fixture
def env(tmp_path):
    return {"HERDR_PANE_ID": PANE, "HERDECK_SUBAGENT_SPOOL_DIR": str(tmp_path / "spool")}


def fire(payload, env, clock, rec, argv=None):
    hook.run(json.dumps(payload).encode(), argv or [], env, reporter=rec, now_ms=clock)


def read_spool(env):
    with open(hook.spool_path(PANE, env), encoding="utf-8") as fh:
        return json.load(fh)


# --- provider + event parsing -----------------------------------------------


def test_provider_detection_by_payload_shape_and_override():
    assert hook.detect_provider(claude_start()) == "claude"
    assert hook.detect_provider(codex_start()) == "codex"
    assert hook.detect_provider(claude_start(), "codex") == "codex"
    assert hook.detect_provider(codex_start(), "bogus") == "codex"
    # Codex's SessionStart has no turn_id; Codex exports CODEX_THREAD_ID
    assert hook.detect_provider(claude_session_start(), None, {"CODEX_THREAD_ID": "t"}) == "codex"


@pytest.mark.parametrize(
    "payload",
    [
        {"hook_event_name": "Stop", "session_id": "s"},
        {"hook_event_name": "UserPromptSubmit", "prompt": "hi"},
        {"hook_event_name": "SubagentStart"},  # no agent_id
        claude_pre_tool(agent_id=None),  # main agent's own tool call
        {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_response": {}},
        claude_post_agent("plain text result"),
        claude_post_agent({"status": "completed"}),  # no agentId
        {},
    ],
)
def test_unknown_or_irrelevant_events_are_ignored(payload, env):
    rec = Recorder()
    fire(payload, env, Clock(), rec)
    assert rec.calls == []
    assert not os.path.exists(hook.spool_path(PANE, env))


def test_codex_child_thread_session_start_does_not_reset(env):
    clock, rec = Clock(), Recorder()
    codex_env = dict(env, CODEX_THREAD_ID="019a-parent")
    fire(codex_start(), codex_env, clock, rec)
    child_start = {
        "hook_event_name": "SessionStart",
        "session_id": "019a-child",
        "source": "startup",
        "cwd": "/repo",
        "model": "m",
        "permission_mode": "default",
        "transcript_path": None,
    }
    fire(child_start, dict(codex_env), clock, rec)
    assert [e["status"] for e in read_spool(env)["entries"]] == ["running"]


# --- lifecycle -----------------------------------------------------------------


def test_claude_start_stop_reports_running_over_total(env):
    clock, rec = Clock(), Recorder()
    fire(claude_start("a1"), env, clock, rec)
    fire(claude_start("a2"), env, clock, rec)
    fire(claude_stop("a1"), env, clock, rec)
    assert rec.calls == [(PANE, "1/1"), (PANE, "2/2"), (PANE, "1/2")]
    spool = read_spool(env)
    assert spool["session_id"] == "sess-1"
    a1 = next(e for e in spool["entries"] if e["id"] == "a1")
    assert a1["status"] == "done" and a1["ended_ms"] == clock.ms and a1["provider"] == "claude"


def test_codex_start_stop(env):
    clock, rec = Clock(), Recorder()
    fire(codex_start(), env, clock, rec)
    clock.ms += 5000
    fire(codex_stop(), env, clock, rec)
    assert rec.calls == [(PANE, "1/1"), (PANE, "0/1")]
    (entry,) = read_spool(env)["entries"]
    assert entry["provider"] == "codex"
    assert entry["type"] == "worker"
    assert entry["model"] == "gpt-5.5-codex"
    assert entry["ended_ms"] - entry["started_ms"] == 5000


def test_claude_meta_json_enriches_description_model_depth(tmp_path, env):
    transcript = tmp_path / "proj" / "sess-1.jsonl"
    meta_dir = tmp_path / "proj" / "sess-1" / "subagents"
    meta_dir.mkdir(parents=True)
    (meta_dir / "agent-a8b1.meta.json").write_text(
        json.dumps(
            {
                "agentType": "general-purpose",
                "description": "B1: usage polled by bridge",
                "toolUseId": "toolu_01",
                "spawnDepth": 1,
                "requestShape": "background",
                "model": "opus",
            }
        )
    )
    fire(claude_start("a8b1", transcript=str(transcript)), env, Clock(), Recorder())
    (entry,) = read_spool(env)["entries"]
    assert entry["description"] == "B1: usage polled by bridge"
    assert entry["model"] == "opus"
    assert entry["depth"] == 1
    assert entry["type"] == "general-purpose"


def test_meta_json_path_is_not_built_from_a_hostile_agent_id(tmp_path):
    payload = claude_start("../../etc/passwd", transcript=str(tmp_path / "s.jsonl"))
    assert hook.claude_meta(payload, "../../etc/passwd") == {}


def test_post_tool_use_agent_background_launch_and_completion(env):
    clock, rec = Clock(), Recorder()
    fire(
        claude_post_agent(
            {"isAsync": True, "status": "async_launched", "agentId": "bg1",
             "description": "Review recent changes"}
        ),
        env, clock, rec,
    )
    (entry,) = read_spool(env)["entries"]
    assert entry["status"] == "running"
    assert entry["description"] == "Review recent changes"
    assert entry["type"] == "code-reviewer"
    # a missed SubagentStop: the foreground result finishes a different one
    fire(claude_post_agent({"status": "completed", "agentId": "fg1",
                            "agentType": "feature-dev:code-reviewer"}), env, clock, rec)
    fire(claude_post_agent({"status": "failed", "agentId": "bg1"}), env, clock, rec)
    by_id = {e["id"]: e for e in read_spool(env)["entries"]}
    assert by_id["fg1"]["status"] == "done"
    assert by_id["bg1"]["status"] == "failed"
    assert rec.calls[-1] == (PANE, "0/2")


def test_stop_after_post_tool_completion_is_idempotent(env):
    clock, rec = Clock(), Recorder()
    fire(claude_start("a1"), env, clock, rec)
    fire(claude_post_agent({"status": "completed", "agentId": "a1"}), env, clock, rec)
    fire(claude_stop("a1"), env, clock, rec)
    (entry,) = read_spool(env)["entries"]
    assert entry["status"] == "done"
    assert rec.calls == [(PANE, "1/1"), (PANE, "0/1")]


# --- heartbeat + stale rules -------------------------------------------------


def test_heartbeat_keeps_an_entry_alive_and_is_throttled(env):
    clock, rec = Clock(), Recorder()
    fire(claude_start("a1"), env, clock, rec)
    path = hook.spool_path(PANE, env)
    before = os.stat(path).st_mtime_ns
    clock.ms += 1000
    fire(claude_pre_tool("a1"), env, clock, rec)
    assert os.stat(path).st_mtime_ns == before  # within HEARTBEAT_WRITE_MS: no rewrite
    clock.ms += 9 * MIN
    fire(claude_pre_tool("a1"), env, clock, rec)
    assert read_spool(env)["entries"][0]["last_seen_ms"] == clock.ms
    clock.ms += 9 * MIN  # 18 min after start but only 9 since the heartbeat
    fire(claude_start("a2"), env, clock, rec)
    assert {e["id"]: e["status"] for e in read_spool(env)["entries"]} == {
        "a1": "running",
        "a2": "running",
    }


def test_heartbeat_for_unknown_subagent_adds_it(env):
    fire(claude_pre_tool("late"), env, Clock(), rec := Recorder())
    (entry,) = read_spool(env)["entries"]
    assert entry["id"] == "late" and entry["status"] == "running" and entry["type"] == "Explore"
    assert rec.calls == [(PANE, "1/1")]


def test_heartbeat_does_not_revive_a_finished_subagent(env):
    clock, rec = Clock(), Recorder()
    fire(claude_start("a1"), env, clock, rec)
    fire(claude_stop("a1"), env, clock, rec)
    clock.ms += MIN
    fire(claude_pre_tool("a1"), env, clock, rec)
    assert read_spool(env)["entries"][0]["status"] == "done"


def test_silent_running_entry_goes_stale_then_is_dropped(env):
    clock, rec = Clock(), Recorder()
    fire(claude_start("a1"), env, clock, rec)
    clock.ms += 11 * MIN
    fire(claude_start("a2"), env, clock, rec)
    by_id = {e["id"]: e["status"] for e in read_spool(env)["entries"]}
    assert by_id == {"a1": "stale", "a2": "running"}
    assert rec.calls[-1] == (PANE, "1/2")
    clock.ms += 20 * MIN  # a1 last seen 31 min ago; a2 20 min ago -> stale too
    fire(claude_start("a3"), env, clock, rec)
    by_id = {e["id"]: e["status"] for e in read_spool(env)["entries"]}
    assert by_id == {"a2": "stale", "a3": "running"}


def test_heartbeat_revives_a_stale_entry(env):
    clock, rec = Clock(), Recorder()
    fire(claude_start("a1"), env, clock, rec)
    clock.ms += 11 * MIN
    fire(claude_start("a2"), env, clock, rec)
    fire(claude_pre_tool("a1"), env, clock, rec)
    assert {e["id"]: e["status"] for e in read_spool(env)["entries"]}["a1"] == "running"
    assert rec.calls[-1] == (PANE, "2/2")


def test_apply_stale_rules_boundaries():
    now = 100 * MIN
    spool = {"entries": [
        _entry("fresh", "running", now - 10 * MIN),  # exactly 10 min: still running
        _entry("old", "running", now - 10 * MIN - 1),
        _entry("keep", "stale", now - 30 * MIN),
        _entry("drop", "stale", now - 30 * MIN - 1),
        _entry("done", "done", now - 60 * MIN),  # finished entries never expire here
    ]}
    assert hook.apply_stale_rules(spool, now)
    assert {e["id"]: e["status"] for e in spool["entries"]} == {
        "fresh": "running", "old": "stale", "keep": "stale", "done": "done"
    }


def _entry(entry_id, status, last_seen):
    return {"id": entry_id, "provider": "claude", "type": "", "description": "", "model": "",
            "depth": None, "started_ms": last_seen, "ended_ms": None, "status": status,
            "last_seen_ms": last_seen}


# --- reset rules -------------------------------------------------------------


@pytest.mark.parametrize("source", ["startup", "clear"])
def test_session_start_startup_or_clear_resets(env, source):
    clock, rec = Clock(), Recorder()
    fire(claude_start("a1"), env, clock, rec)
    fire(claude_session_start(source), env, clock, rec)
    assert read_spool(env)["entries"] == []
    assert rec.calls[-1] == (PANE, "0/0")


@pytest.mark.parametrize("source", ["resume", "compact"])
def test_session_start_resume_or_compact_same_session_keeps(env, source):
    clock, rec = Clock(), Recorder()
    fire(claude_start("a1"), env, clock, rec)
    fire(claude_session_start(source), env, clock, rec)
    assert len(read_spool(env)["entries"]) == 1
    assert rec.calls == [(PANE, "1/1")]


def test_a_new_agent_session_resets_the_spool(env):
    clock, rec = Clock(), Recorder()
    fire(claude_start("a1", session="old"), env, clock, rec)
    fire(claude_start("b1", session="new"), env, clock, rec)
    spool = read_spool(env)
    assert spool["session_id"] == "new"
    assert [e["id"] for e in spool["entries"]] == ["b1"]
    assert rec.calls[-1] == (PANE, "1/1")


def test_heartbeat_session_id_never_resets(env):
    clock, rec = Clock(), Recorder()
    fire(claude_start("a1", session="parent"), env, clock, rec)
    fire(claude_pre_tool("a1", session="child-thread"), env, clock, rec)
    assert read_spool(env)["session_id"] == "parent"
    assert len(read_spool(env)["entries"]) == 1


def test_session_start_on_a_pane_without_subagents_reports_nothing(env):
    rec = Recorder()
    fire(claude_session_start("startup"), env, Clock(), rec)
    assert rec.calls == []


# --- spool bounds + robustness -----------------------------------------------


def test_spool_keeps_at_most_20_entries_dropping_finished_first(env):
    clock, rec = Clock(), Recorder()
    for i in range(15):
        fire(claude_start(f"d{i}"), env, clock, rec)
        fire(claude_stop(f"d{i}"), env, clock, rec)
        clock.ms += 1
    for i in range(10):
        fire(claude_start(f"r{i}"), env, clock, rec)
        clock.ms += 1
    entries = read_spool(env)["entries"]
    assert len(entries) == hook.MAX_ENTRIES
    assert sum(e["status"] == "running" for e in entries) == 10
    assert {e["id"] for e in entries if e["status"] == "done"} == {f"d{i}" for i in range(5, 15)}
    assert rec.calls[-1] == (PANE, "10/20")


def test_spool_bound_drops_oldest_running_when_all_run(env):
    clock, rec = Clock(), Recorder()
    for i in range(22):
        fire(claude_start(f"r{i}"), env, clock, rec)
        clock.ms += 1
    ids = [e["id"] for e in read_spool(env)["entries"]]
    assert len(ids) == 20 and "r0" not in ids and "r1" not in ids


def test_spool_file_is_0600_and_long_fields_are_capped(env):
    long = "x" * 1000
    fire(claude_start("a1", agent_type=long), env, Clock(), Recorder())
    path = hook.spool_path(PANE, env)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert len(read_spool(env)["entries"][0]["type"]) == 64
    assert not [n for n in os.listdir(os.path.dirname(path)) if n.endswith(".tmp")]


@pytest.mark.parametrize("junk", ["", "not json", "[]", '{"version": 99, "entries": [1]}',
                                  '{"version": 1, "entries": [{"id": "x"}, "y", null]}'])
def test_corrupt_spool_is_replaced(env, junk):
    path = hook.spool_path(PANE, env)
    os.makedirs(os.path.dirname(path))
    with open(path, "w") as fh:
        fh.write(junk)
    fire(claude_start("a1"), env, Clock(), rec := Recorder())
    assert [e["id"] for e in read_spool(env)["entries"]] == ["a1"]
    assert rec.calls == [(PANE, "1/1")]


def test_spool_path_sanitises_pane_id(tmp_path):
    env = {"HERDECK_SUBAGENT_SPOOL_DIR": str(tmp_path)}
    assert hook.spool_path("../../x", env) == str(tmp_path / "_._.._x.json")
    assert os.path.dirname(hook.spool_path("a/b", env)) == str(tmp_path)


def test_default_spool_dir_is_under_cache_herdeck():
    assert hook.spool_dir({}).endswith(os.path.join(".cache", "herdeck", "subagents"))


def test_malformed_stdin_is_a_noop(env):
    rec = Recorder()
    for raw in (b"", b"{", b"[1,2]", b"\xff\xfe", b"null"):
        hook.run(raw, [], env, reporter=rec, now_ms=Clock())
    assert rec.calls == []


# --- reporting ---------------------------------------------------------------


def test_unchanged_token_is_refreshed_inside_its_ttl(env):
    clock, rec = Clock(), Recorder()
    fire(claude_start("a1"), env, clock, rec)
    clock.ms += 2 * MIN
    fire(claude_pre_tool("a1"), env, clock, rec)
    assert rec.calls == [(PANE, "1/1")]
    clock.ms += 4 * MIN  # 6 min since the report
    fire(claude_pre_tool("a1"), env, clock, rec)
    assert rec.calls == [(PANE, "1/1"), (PANE, "1/1")]
    assert hook.REPORT_REFRESH_MS < hook.TOKEN_TTL_MS


def test_format_token_round_trips_through_the_bridge_parser():
    assert hook.format_token(2, 5) == "2/5"
    assert parse_subagents_token(hook.format_token(2, 5)) == (2, 5)
    assert parse_subagents_token(hook.format_token(0, 0)) == (0, 0)


def test_no_herdr_pane_id_is_a_noop(tmp_path):
    rec = Recorder()
    env = {"HERDECK_SUBAGENT_SPOOL_DIR": str(tmp_path / "spool")}
    hook.run(json.dumps(claude_start()).encode(), [], env, reporter=rec, now_ms=Clock())
    assert rec.calls == []
    assert not (tmp_path / "spool").exists()


def _stub_herdr(tmp_path, body):
    script = tmp_path / "herdr"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    return str(script)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell stub")
def test_cli_reporter_invokes_herdr_report_metadata(tmp_path, env):
    log = tmp_path / "args"
    env = dict(env, HERDECK_HERDR_BIN=_stub_herdr(tmp_path, f'printf "%s\\n" "$@" > {log}'))
    hook.run(json.dumps(claude_start()).encode(), [], env, now_ms=Clock())
    args = log.read_text().split("\n")
    # herdr 0.9.1 rejects a trailing pane id ("unknown option: <source>"):
    # it must follow the subcommand.
    assert args[:10] == [
        "pane", "report-metadata", PANE, "--source", "herdeck:subagents",
        "--token", "subagents=1/1", "--ttl-ms", "900000", "--seq",
    ]
    assert args[10].isdigit()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell stub")
def test_a_hanging_herdr_is_cut_off_by_the_budget(tmp_path, env):
    env = dict(env, HERDECK_HERDR_BIN=_stub_herdr(tmp_path, "sleep 30"))
    start = time.monotonic()
    hook.run(json.dumps(claude_start()).encode(), [], env, now_ms=Clock(), budget_s=0.5)
    assert time.monotonic() - start < 1.5
    # the spool was still written before reporting
    assert read_spool(env)["entries"][0]["id"] == "a1"


def test_a_held_lock_gives_up_at_the_deadline(env):
    path = hook.spool_path(PANE, env)
    os.makedirs(os.path.dirname(path))
    rec = Recorder()
    with hook._SpoolLock(path, time.monotonic() + 5) as locked:
        assert locked
        start = time.monotonic()
        hook.run(json.dumps(claude_start()).encode(), [], env, reporter=rec,
                 now_ms=Clock(), budget_s=0.3)
        assert time.monotonic() - start < 1.0
    assert rec.calls == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell stub")
def test_entry_point_exits_0_silently_within_the_hard_budget(tmp_path, env):
    """The real process: no stdout (would be read as hook output), exit 0,
    done within the 2 s budget even with a herdr that never returns."""
    env = dict(os.environ, **env, HERDECK_HERDR_BIN=_stub_herdr(tmp_path, "sleep 30"))
    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "herdeck.subagent_hook", "--provider", "claude"],
        input=json.dumps(claude_start()).encode(),
        capture_output=True,
        env=env,
        timeout=20,
    )
    assert proc.returncode == 0
    assert proc.stdout == b""
    assert time.monotonic() - start < 4.0
    assert read_spool(env)["entries"][0]["id"] == "a1"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell stub")
def test_entry_point_without_pane_exits_immediately(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "HERDR_PANE_ID"}
    env["HERDECK_SUBAGENT_SPOOL_DIR"] = str(tmp_path / "spool")
    proc = subprocess.run(
        [sys.executable, "-m", "herdeck.subagent_hook"],
        input=b"not even json",
        capture_output=True,
        env=env,
        timeout=20,
    )
    assert (proc.returncode, proc.stdout) == (0, b"")
    assert not (tmp_path / "spool").exists()


def test_subagent_hook_uses_only_the_standard_library():
    import ast

    tree = ast.parse(open(hook.__file__, encoding="utf-8").read())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative (herdeck) imports in the hook"
            modules.add((node.module or "").split(".")[0])
    assert modules <= set(sys.stdlib_module_names) | {"__future__"}


def test_readme_hook_snippets_are_valid_json_for_the_entry_point():
    import re
    from pathlib import Path

    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    section = readme.split("### Subagent tracking", 1)[1].split("\n## ", 1)[0]
    blocks = [json.loads(b) for b in re.findall(r"```json\n(.*?)```", section, re.S)]
    assert len(blocks) == 2
    claude, codex = (b["hooks"] for b in blocks)
    assert set(claude) == {
        "SubagentStart", "SubagentStop", "PostToolUse", "PreToolUse", "SessionStart"
    }
    assert set(codex) == {"SubagentStart", "SubagentStop", "SessionStart"}
    for hooks, provider in ((claude, "claude"), (codex, "codex")):
        for groups in hooks.values():
            for group in groups:
                for handler in group["hooks"]:
                    assert handler["command"] == f"herdeck-subagent-hook --provider {provider}"


def test_codex_parent_session_start_resets(env):
    clock, rec = Clock(), Recorder()
    codex_env = dict(env, CODEX_THREAD_ID="019a-parent")
    fire(codex_start(), codex_env, clock, rec)
    parent_start = {
        "hook_event_name": "SessionStart",
        "session_id": "019a-parent",
        "source": "clear",
        "cwd": "/repo",
        "model": "m",
        "permission_mode": "default",
        "transcript_path": None,
    }
    fire(parent_start, codex_env, clock, rec)
    assert read_spool(env)["entries"] == []
    assert rec.calls[-1] == (PANE, "0/0")


# --- transcript hints for the bridge's reconciler ----------------------------------


def test_claude_entries_remember_their_transcripts(env):
    clock, rec = Clock(), Recorder()
    fire(claude_start("a1", transcript="/p/sess-1.jsonl"), env, clock, rec)
    (entry,) = read_spool(env)["entries"]
    assert entry["transcript"] == "/p/sess-1.jsonl" and entry["agent_transcript"] == ""
    fire(claude_stop("a1"), env, clock, rec)
    (entry,) = read_spool(env)["entries"]
    assert entry["transcript"] == "/p/sess-1.jsonl"  # the first one stays
    assert entry["agent_transcript"] == "/nowhere/sess-1/subagents/agent-a1.jsonl"


def test_codex_entries_remember_the_sessions_tree(env):
    fire(codex_start(), dict(env, CODEX_HOME="/srv/codex"), Clock(), Recorder())
    (entry,) = read_spool(env)["entries"]
    assert entry["sessions_dir"] == "/srv/codex/sessions" and entry["transcript"] == ""


def test_codex_sessions_tree_comes_from_the_transcript_first():
    payload = dict(codex_start(), transcript_path="/h/.codex/sessions/2026/09/24/rollout-x.jsonl")
    assert hook.codex_sessions_dir(payload, {"CODEX_HOME": "/elsewhere"}) == "/h/.codex/sessions"


@pytest.mark.parametrize("bad", ["relative/x.jsonl", "/a\nb.jsonl", "/" + "x" * 2000, 7, None])
def test_unusable_transcript_paths_are_dropped(env, bad):
    fire(claude_start("a1", transcript=bad), env, Clock(), Recorder())
    assert read_spool(env)["entries"][0]["transcript"] == ""


def test_transcript_hints_never_reach_the_wire(env):
    from herdeck.subagent_spool import SubagentSpoolReader

    fire(claude_start("a1", transcript="/p/sess-1.jsonl"), env, Clock(), Recorder())
    reader = SubagentSpoolReader(env["HERDECK_SUBAGENT_SPOOL_DIR"], clock=lambda: 1_000_000_000)
    (wire,) = reader.entries_for(PANE)
    assert not set(hook.PATH_FIELDS) & set(wire)
