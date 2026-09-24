"""S2: the bridge reads herdeck-subagent-hook's per-pane spools and adds
``subagents`` to the wire pane; the runtime carries it into AgentState, the
desktop agent card and the semantic inventory."""

import json
import os

import pytest

from herdeck import subagent_hook
from herdeck.bridge import StubHerdr, _snapshot_message, _wired_snapshot, handle_client_message
from herdeck.model import SUBAGENTS_MAX, AgentKey, AgentState, Status, Subagent, parse_subagents
from herdeck.protocol import _pane_to_state, decode_inbound
from herdeck.subagent_spool import MAX_FILE_BYTES, SubagentSpoolReader

NOW_MS = 1_800_000_000_000


def entry(entry_id, *, status="running", started=NOW_MS - 60_000, ended=None, last_seen=None, **kw):
    return {
        "id": entry_id,
        "provider": kw.get("provider", "claude"),
        "type": kw.get("type", "Explore"),
        "description": kw.get("description", f"task {entry_id}"),
        "model": kw.get("model", ""),
        "depth": kw.get("depth", 1),
        "started_ms": started,
        "ended_ms": ended,
        "status": status,
        "last_seen_ms": last_seen if last_seen is not None else (ended or NOW_MS - 1000),
    }


def write(directory, pane_id, entries):
    path = subagent_hook.spool_path(pane_id, {"HERDECK_SUBAGENT_SPOOL_DIR": str(directory)})
    subagent_hook.write_spool(
        path, {"version": subagent_hook.SPOOL_VERSION, "pane": pane_id, "session_id": "s1", "entries": entries}
    )
    return path


def reader(tmp_path, now_ms=NOW_MS):
    return SubagentSpoolReader(str(tmp_path), clock=lambda: now_ms / 1000)


def test_reads_spool_most_recent_first_without_internal_fields(tmp_path):
    write(
        tmp_path,
        "w1:p1",
        [
            entry("a", status="done", started=NOW_MS - 300_000, ended=NOW_MS - 200_000),
            entry("b", started=NOW_MS - 10_000, depth=2, model="haiku"),
        ],
    )
    out = reader(tmp_path).entries_for("w1:p1")
    assert [e["id"] for e in out] == ["b", "a"]
    assert out[0] == {
        "id": "b",
        "provider": "claude",
        "type": "Explore",
        "description": "task b",
        "model": "haiku",
        "depth": 2,
        "status": "running",
        "started_ms": NOW_MS - 10_000,
        "ended_ms": None,
    }
    assert out[1]["ended_ms"] == NOW_MS - 200_000
    assert "last_seen_ms" not in out[0]


@pytest.mark.parametrize(
    "content",
    [b"", b"{", b"not json", b"[]", b'{"version": 99, "entries": []}', b'{"version": 1, "entries": "x"}'],
)
def test_corrupt_or_foreign_spool_reads_as_empty(tmp_path, content):
    path = write(tmp_path, "p1", [entry("a")])
    with open(path, "wb") as fh:
        fh.write(content)
    assert reader(tmp_path).entries_for("p1") == []


def test_malformed_entries_are_dropped_not_fatal(tmp_path):
    path = write(tmp_path, "p1", [])
    with open(path, "w") as fh:
        json.dump(
            {
                "version": 1,
                "entries": [entry("ok"), {"id": "x", "status": "weird"}, "junk", {"status": "running"}],
            },
            fh,
        )
    assert [e["id"] for e in reader(tmp_path).entries_for("p1")] == ["ok"]


def test_oversized_spool_is_ignored(tmp_path):
    path = write(tmp_path, "p1", [entry("a")])
    with open(path, "a") as fh:
        fh.write(" " * (MAX_FILE_BYTES + 1))
    assert reader(tmp_path).entries_for("p1") == []


def test_missing_spool_is_empty(tmp_path):
    assert reader(tmp_path).entries_for("nope") == []


def test_stale_and_drop_rules_apply_at_read_time(tmp_path):
    silent = NOW_MS - subagent_hook.STALE_AFTER_MS - 1000
    gone = NOW_MS - subagent_hook.DROP_STALE_AFTER_MS - 1000
    write(
        tmp_path,
        "p1",
        [
            entry("silent", started=silent - 5000, last_seen=silent),
            entry("long-stale", status="stale", started=gone - 5000, last_seen=gone),
            entry("fresh", started=NOW_MS - 5000, last_seen=NOW_MS - 1000),
        ],
    )
    rd = reader(tmp_path)
    out = {e["id"]: e["status"] for e in rd.entries_for("p1")}
    assert out == {"fresh": "running", "silent": "stale"}
    # The cached raw entries are not mutated: a later (earlier-clock) read
    # still sees the original status.
    rd._clock = lambda: (silent + 1000) / 1000
    assert {e["id"]: e["status"] for e in rd.entries_for("p1")}["silent"] == "running"


def test_caps_at_twenty_and_sanitizes_text(tmp_path):
    entries = [entry(f"e{i}", status="done", started=NOW_MS - 100_000 + i, ended=NOW_MS) for i in range(25)]
    entries[-1]["description"] = "evil\x1b[31m‮line\nbreak"
    path = write(tmp_path, "p1", [])
    with open(path, "w") as fh:  # the hook itself bounds to 20: bypass it
        json.dump({"version": 1, "entries": entries}, fh)
    out = reader(tmp_path).entries_for("p1")
    assert len(out) == SUBAGENTS_MAX
    assert out[0]["id"] == "e24"  # newest first survives the cap
    assert out[0]["description"] == "evil[31mlinebreak"


def test_cache_reparses_only_when_the_file_changes(tmp_path, monkeypatch):
    path = write(tmp_path, "p1", [entry("a")])
    calls = []
    real = subagent_hook.load_spool
    monkeypatch.setattr(subagent_hook, "load_spool", lambda *a: calls.append(a) or real(*a))
    rd = reader(tmp_path)
    rd.entries_for("p1")
    rd.entries_for("p1")
    assert len(calls) == 1
    write(tmp_path, "p1", [entry("a"), entry("b", started=NOW_MS - 1)])
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    assert [e["id"] for e in rd.entries_for("p1")] == ["b", "a"]
    assert len(calls) == 2


def test_attach_only_when_non_empty_and_prunes_cache(tmp_path):
    write(tmp_path, "p1", [entry("a")])
    rd = reader(tmp_path)
    panes = [{"pane_id": "p1"}, {"pane_id": "p2", "subagents": ["left over"]}]
    rd.attach(panes)
    assert [e["id"] for e in panes[0]["subagents"]] == ["a"]
    assert "subagents" not in panes[1]
    rd.attach([{"pane_id": "p2"}])
    assert "p1" not in rd._cache


async def test_wired_snapshot_carries_subagents_and_roundtrips(tmp_path):
    write(tmp_path, "w1:p1", [entry("a", depth=1), entry("b", status="failed", ended=NOW_MS, depth=2)])
    herdr = StubHerdr(
        panes=[
            {"pane_id": "w1:p1", "workspace_id": "w1", "cwd": "/x/api", "agent": "claude", "agent_status": "working"},
            {"pane_id": "w1:p2", "workspace_id": "w1", "cwd": "/x/web", "agent": "codex", "agent_status": "idle"},
        ]
    )
    rd = reader(tmp_path)
    panes = await _wired_snapshot(herdr, subagents=rd)
    by_id = {p["pane_id"]: p for p in panes}
    assert [e["id"] for e in by_id["w1:p1"]["subagents"]] == ["b", "a"]
    assert "subagents" not in by_id["w1:p2"]
    snap = decode_inbound(json.dumps(_snapshot_message("dev", panes)))
    assert "subagents" in snap.capabilities
    states = {s.key.pane_id: s for s in snap.states}
    assert states["w1:p1"].subagents == (
        Subagent("b", "claude", "Explore", "task b", "", 2, "failed", NOW_MS - 60_000, NOW_MS),
        Subagent("a", "claude", "Explore", "task a", "", 1, "running", NOW_MS - 60_000, None),
    )
    assert states["w1:p2"].subagents == ()
    # the list request goes through the same path
    out = json.loads(await handle_client_message(herdr, "dev", json.dumps({"type": "list"}), subagents=rd))
    assert "subagents" in {p["pane_id"]: p for p in out["panes"]}["w1:p1"]


async def test_herdr_events_stream_attaches_subagents(tmp_path):
    from herdeck.bridge import HerdrEvents

    write(tmp_path, "w1:p1", [entry("a")])
    herdr = StubHerdr(panes=[{"pane_id": "w1:p1", "workspace_id": "w1", "cwd": "/x", "agent": "claude", "agent_status": "working"}])
    gen = HerdrEvents(herdr, poll_interval=0, subagents=reader(tmp_path)).stream()
    first = await gen.__anext__()
    assert first[0]["subagents"][0]["id"] == "a"
    await gen.aclose()


@pytest.mark.parametrize(
    "value",
    [None, "x", 3, {"id": "a"}, [None, "x", {"id": "", "status": "running", "started_ms": 1}]],
)
def test_protocol_tolerates_malformed_subagents(value):
    pane = {"pane_id": "p1", "status": "working"}
    if value is not None:
        pane["subagents"] = value
    assert _pane_to_state("dev", pane).subagents == ()


def test_parse_subagents_validates_fields():
    subs = parse_subagents(
        [
            {"id": "a", "status": "done", "started_ms": 10, "ended_ms": 5, "depth": -1, "type": 7},
            {"id": "b", "status": "running", "started_ms": True},
            {"id": "c", "status": "stale", "started_ms": 20, "depth": 3, "provider": "codex"},
        ]
    )
    assert [s.id for s in subs] == ["c", "a"]
    assert subs[1].ended_ms is None and subs[1].depth is None and subs[1].type == ""
    assert subs[0].depth == 3 and subs[0].provider == "codex"


def test_agent_card_rows_and_semantic_inventory():
    from herdeck.deckapp.agent_card import subagent_rows
    from herdeck.semantic_api import _agent_record

    subs = (
        Subagent("r", "claude", "Plan", "plan it", "", 2, "running", NOW_MS - 95_000, None),
        Subagent("d", "codex", "worker", "do it", "gpt", 1, "done", NOW_MS - 300_000, NOW_MS - 180_000),
        Subagent("skew", status="running", started_ms=NOW_MS + 5000),
    )
    rows = subagent_rows(subs, NOW_MS)
    assert [(r["id"], r["status"], r["duration_s"], r["depth"]) for r in rows] == [
        ("r", "running", 95, 2),
        ("d", "done", 120, 1),
        ("skew", "running", 0, None),
    ]
    state = AgentState(AgentKey("dev", "p1"), "claude", "api", Status.WORKING, subagents=subs[:1])
    record = _agent_record(state, available=True)
    assert record["subagents"] == [subs[0].to_wire()]
    empty = AgentState(AgentKey("dev", "p2"), "claude", "api", Status.WORKING)
    assert _agent_record(empty, available=True)["subagents"] == []
