"""Bridge episode history (history.py): store, recorder, aggregation, stats."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat

import pytest

from herdeck import history as hist
from herdeck.bridge import StubHerdr, _serve_connection, handle_client_message
from herdeck.history import (
    BridgeHistory,
    Episode,
    HistoryRecorder,
    HistoryStore,
    aggregate,
    parse_stats_request,
    percentile,
    range_start_ms,
    thin,
)
from herdeck.status_since import StatusSinceTracker

H = 3_600_000
M = 60_000
DAY = 86_400_000
# 2026-09-24 12:00:00 UTC
NOON = 1_790_251_200_000
MIDNIGHT = NOON - 12 * H


def ep(
    status,
    start,
    end,
    *,
    to="idle",
    pane="p1",
    term="t1",
    agent="claude",
    repo="herdeck",
    label="herdeck",
    answered=False,
):
    return Episode(pane, term, agent, repo, repo, label, status, to, start, end, answered)


def rows(*episodes):
    return [e.row() for e in episodes]


# --- store ----------------------------------------------------------------------


def _count(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT count(*) FROM episodes").fetchone()[0]
    finally:
        conn.close()


def test_store_appends_in_batches_and_is_private(tmp_path):
    path = str(tmp_path / "state" / "herdeck" / "history.sqlite")
    store = HistoryStore(path, clock=lambda: NOON / 1000)
    for i in range(3):
        store.append(ep("working", NOON - (i + 1) * H, NOON - i * H))
    assert store.flush()
    assert _count(path) == 3
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700
    mode = store.query(lambda c: c.execute("PRAGMA journal_mode").fetchone()[0]).result(5)
    assert mode == "wal"
    store.close()
    store.append(ep("working", 1, 2))  # after close: silently ignored
    assert _count(path) == 3


def test_store_prunes_by_retention_and_row_cap(tmp_path):
    path = str(tmp_path / "h.sqlite")
    store = HistoryStore(path, clock=lambda: NOON / 1000, retention_days=30, max_rows=5)
    store.append(ep("idle", NOON - 40 * DAY, NOON - 31 * DAY))  # past retention
    for i in range(8):
        store.append(ep("working", NOON - (i + 2) * H, NOON - (i + 1) * H))
    assert store.flush()
    store.query(lambda c: store.prune(c)).result(5)
    assert _count(path) == 5
    kept = store.query(
        lambda c: [r[0] for r in c.execute("SELECT ended_ms FROM episodes ORDER BY id")]
    ).result(5)
    # the newest five inserts survive (ids 5..9); the stale one is gone
    assert kept == [NOON - (i + 1) * H for i in range(3, 8)]
    store.close()


def test_store_recovers_from_a_corrupt_file(tmp_path, caplog):
    path = tmp_path / "h.sqlite"
    path.write_bytes(b"this is not a sqlite database at all" * 200)
    store = HistoryStore(str(path), clock=lambda: NOON / 1000)
    store.append(ep("working", NOON - H, NOON))
    assert store.flush()
    assert _count(str(path)) == 1
    assert any(p.name.startswith("h.sqlite.corrupt-") for p in tmp_path.iterdir())
    store.close()


def test_store_unavailable_never_raises(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    store = HistoryStore(str(blocker / "sub" / "h.sqlite"))  # parent is a file
    store.append(ep("working", 1, 2))
    with pytest.raises(RuntimeError):
        store.query(lambda c: 1).result(5)
    store.close()


def test_full_queue_drops_instead_of_blocking(tmp_path):
    store = HistoryStore(str(tmp_path / "h.sqlite"), queue_max=1)
    for _ in range(50):
        store.append(ep("working", 1, 2))  # never blocks, never raises
    store.close()


# --- recorder --------------------------------------------------------------------


def pane(status, *, pane_id="p1", term="t1", since=None, waiting_on="", **extra):
    p = {
        "pane_id": pane_id,
        "terminal_id": term,
        "agent_type": "claude",
        "repo": "herdeck",
        "project": "herdeck",
        "label": "herdeck",
        "status": status,
        "waiting_on": waiting_on,
        **extra,
    }
    if since is not None:
        p["status_since_ms"] = since
    return p


def test_recorder_emits_episode_on_status_change():
    out: list[Episode] = []
    rec = HistoryRecorder(out.append)
    rec.observe([pane("working", since=NOON - H)], NOON)
    rec.observe([pane("working", since=NOON - H)], NOON + M)  # same episode
    assert out == []
    rec.observe([pane("blocked")], NOON + 2 * M)
    assert out == [ep("working", NOON - H, NOON + 2 * M, to="blocked")]


def test_recorder_uses_effective_status_and_marks_answers():
    out: list[Episode] = []
    rec = HistoryRecorder(out.append)
    rec.observe([pane("idle", waiting_on="CI")], NOON)
    rec.observe([pane("working", waiting_on="CI")], NOON + M)  # still WAITING
    assert out == []
    rec.observe([pane("blocked")], NOON + 2 * M)
    rec.note_answer("p1")
    rec.note_answer("nope")
    rec.observe([pane("working")], NOON + 5 * M)
    assert out[0].from_status == "waiting"
    assert out[1] == ep("blocked", NOON + 2 * M, NOON + 5 * M, to="working", answered=True)


def test_answer_outside_a_block_is_ignored():
    out: list[Episode] = []
    rec = HistoryRecorder(out.append)
    rec.observe([pane("working")], NOON)
    rec.note_answer("p1")
    rec.observe([pane("blocked")], NOON + M)
    rec.observe([pane("idle")], NOON + 2 * M)
    assert [e.answered for e in out] == [False, False]


def test_recorder_closed_and_recycled_panes_end_as_gone():
    out: list[Episode] = []
    rec = HistoryRecorder(out.append)
    rec.observe([pane("working"), pane("idle", pane_id="p2")], NOON)
    rec.observe([pane("working", term="t2")], NOON + M)  # p1 recycled, p2 closed
    assert sorted((e.pane_id, e.terminal_id, e.to_status) for e in out) == [
        ("p1", "t1", "gone"),
        ("p2", "t1", "gone"),
    ]
    assert rec.open_episodes(NOON + 2 * M)[0][1] == "t2"


def test_recorder_sink_failure_is_swallowed():
    def boom(_):
        raise RuntimeError("disk full")

    rec = HistoryRecorder(boom)
    rec.observe([pane("working")], NOON)
    rec.observe([pane("idle")], NOON + M)  # no exception


def test_status_since_tracker_feeds_observers():
    seen = []

    class Obs:
        def observe(self, panes, now):
            seen.append((panes[0]["status_since_ms"], now))

        def note_answer(self, pane_id):
            seen.append(pane_id)

    tracker = StatusSinceTracker(None, clock=lambda: NOON / 1000)
    tracker.add_observer(Obs())
    tracker.stamp([pane("working")])
    tracker.note_answer("p1")
    tracker.note_answer(None)
    assert seen == [(NOON, NOON), "p1"]


# --- aggregation --------------------------------------------------------------------


def test_percentile_and_thin():
    assert percentile([], 0.5) is None
    assert percentile([10, 20, 30, 40], 0.5) == 20
    assert percentile([10, 20, 30, 40], 0.9) == 40
    assert percentile(list(range(1, 101)), 0.9) == 90
    assert thin([1, 2, 3], 5) == [1, 2, 3]
    t = thin(list(range(1000)), 10)
    assert len(t) == 10 and t[0] == 0 and t[-1] == 999


def test_range_start_is_local_midnight():
    assert range_start_ms(NOON, 1) == MIDNIGHT
    assert range_start_ms(NOON, 7) == MIDNIGHT - 6 * DAY
    # UTC+2: local time 14:00, local midnight = 22:00 UTC the day before
    assert range_start_ms(NOON, 1, 120) == MIDNIGHT - 2 * H


def test_aggregate_known_durations():
    data = rows(
        ep("working", NOON - 3 * H, NOON - 2 * H, to="blocked"),
        ep("blocked", NOON - 2 * H, NOON - 2 * H + 10 * M, to="working", answered=True),
        ep("working", NOON - 2 * H + 10 * M, NOON - H, to="done"),
        ep("done", NOON - H, NOON - 30 * M, to="idle"),
        ep("idle", NOON - 30 * M, NOON, to=""),  # still open
        ep("blocked", NOON - 5 * H, NOON - 4 * H, to="working", pane="p2", repo="api", label="api"),
        ep("blocked", NOON - 4 * H, NOON - 3 * H + 30 * M, to="gone", pane="p2", repo="api"),
    )
    out = aggregate(data, now_ms=NOON, range_days=1, group_by="repo")
    total = out["total"]
    assert total["working_ms"] == H + 50 * M
    assert total["blocked_ms"] == 10 * M + H + 90 * M
    assert total["idle_ms"] == 30 * M
    assert total["done_ms"] == 30 * M
    assert total["blocked_count"] == 3
    assert total["answered_count"] == 1
    assert total["done_count"] == 1
    # time to answer excludes the block that ended with the pane closing
    assert total["answer_samples_ms"] == [10 * M, H]
    assert total["answer_median_ms"] == 10 * M
    assert total["answer_p90_ms"] == H
    by_key = {g["key"]: g for g in out["groups"]}
    assert set(by_key) == {"herdeck", "api"}
    assert by_key["api"]["blocked_ms"] == H + 90 * M
    assert by_key["herdeck"]["done_count"] == 1
    assert out["groups"][0]["key"] == "herdeck"  # most time first
    assert len(out["days"]) == 1 and out["days"][0]["day"] == "2026-09-24"
    assert out["days"][0]["working_ms"] == total["working_ms"]


def test_aggregate_clips_to_range_and_splits_days():
    data = rows(
        # 22:00 two days ago → 02:00 yesterday: 2 h on day 0 of a 2-day range? no:
        ep("working", MIDNIGHT - DAY - 2 * H, MIDNIGHT - DAY + 2 * H, to="idle"),
        # 20:00 yesterday → 04:00 today
        ep("blocked", MIDNIGHT - 4 * H, MIDNIGHT + 4 * H, to="working"),
    )
    out = aggregate(data, now_ms=NOON, range_days=1, group_by="agent")
    assert out["total"]["working_ms"] == 0
    assert out["total"]["blocked_ms"] == 4 * H  # clipped at today's midnight
    assert out["total"]["answer_samples_ms"] == [8 * H]  # the full block
    out7 = aggregate(data, now_ms=NOON, range_days=7, group_by="agent")
    days = out7["days"]
    assert [d["day"] for d in days][-2:] == ["2026-09-23", "2026-09-24"]
    assert days[-1]["blocked_ms"] == 4 * H
    assert days[-2]["blocked_ms"] == 4 * H
    assert days[-2]["working_ms"] == 2 * H
    assert days[-3]["working_ms"] == 2 * H
    assert days[-1]["blocked_count"] == 1
    assert out7["total"]["working_ms"] == 4 * H


def test_aggregate_group_keys():
    data = rows(
        ep("working", NOON - H, NOON, agent="claude", pane="p1", repo="a"),
        ep("working", NOON - H, NOON, agent="codex", pane="p2", repo="a", label="a-wt"),
    )
    by_type = aggregate(data, now_ms=NOON, range_days=1, group_by="agent_type")
    assert {g["key"] for g in by_type["groups"]} == {"claude", "codex"}
    by_agent = aggregate(data, now_ms=NOON, range_days=1, group_by="agent")
    assert {g["label"] for g in by_agent["groups"]} == {"herdeck · claude", "a-wt · codex"}
    by_repo = aggregate(data, now_ms=NOON, range_days=1, group_by="repo")
    assert [(g["key"], g["working_ms"]) for g in by_repo["groups"]] == [("a", 2 * H)]


def test_aggregate_respects_the_deadline():
    data = rows(*[ep("working", NOON - H, NOON) for _ in range(10_000)])
    ticks = iter(range(100))
    out = aggregate(
        data,
        now_ms=NOON,
        range_days=1,
        group_by="agent",
        deadline=0.5,
        clock=lambda: next(ticks),
    )
    assert out["truncated"] is True
    assert out["total"]["working_ms"] < 10_000 * H


def test_parse_stats_request():
    assert parse_stats_request({}) == (7, "agent", 0)
    assert parse_stats_request({"range_days": 30, "group_by": "repo", "tz_offset_min": -300}) == (
        30,
        "repo",
        -300,
    )
    assert isinstance(parse_stats_request({"range_days": 3}), str)
    assert isinstance(parse_stats_request({"range_days": True}), str)
    assert isinstance(parse_stats_request({"group_by": "x"}), str)
    assert isinstance(parse_stats_request({"tz_offset_min": 5000}), str)


# --- bridge facade + wire -------------------------------------------------------------


async def test_stats_reply_combines_store_and_open_episodes(tmp_path):
    now = {"t": NOON / 1000}
    history = BridgeHistory(str(tmp_path / "h.sqlite"), clock=lambda: now["t"])
    try:
        history.observe([pane("working", since=NOON - 2 * H)], NOON)
        now["t"] = (NOON + H) / 1000
        history.observe([pane("blocked")], NOON + H)
        history.note_answer("p1")
        now["t"] = (NOON + H + 5 * M) / 1000
        history.observe([pane("idle")], NOON + H + 5 * M)
        now["t"] = (NOON + 2 * H) / 1000
        reply = await history.stats_reply(
            {"type": "stats", "req": "s1", "range_days": 1, "group_by": "agent_type"}
        )
        assert reply["type"] == "result" and reply["req"] == "s1"
        total = reply["data"]["total"]
        assert total["working_ms"] == 3 * H
        assert total["blocked_ms"] == 5 * M
        assert total["idle_ms"] == 55 * M  # the open episode, up to now
        assert total["answered_count"] == 1
        bad = await history.stats_reply({"type": "stats", "req": "s2", "range_days": 2})
        assert bad["type"] == "error" and bad["req"] == "s2"
    finally:
        history.close()


class _FakeWs:
    def __init__(self, token, messages):
        self.request = type("R", (), {"headers": {"Authorization": f"Bearer {token}"}})()
        self._messages = list(messages)
        self.sent: list[dict] = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        await asyncio.sleep(0)
        return self._messages.pop(0)

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self, code=1000, reason=""):
        pass


async def _run_connection(token, messages, history):
    herdr = StubHerdr([])
    ws = _FakeWs(token, messages)
    await _serve_connection(
        ws, herdr, "srv", "full", {}, "/nonexistent", readonly_token="ro", history=history
    )
    return ws.sent


async def test_stats_message_over_the_wire_readonly_ok(tmp_path):
    history = BridgeHistory(str(tmp_path / "h.sqlite"))
    try:
        sent = await _run_connection(
            "ro",
            [json.dumps({"type": "stats", "req": "q", "range_days": 7, "group_by": "repo"})],
            history,
        )
    finally:
        history.close()
    assert "history" in sent[0]["capabilities"]
    reply = sent[-1]
    assert reply["type"] == "result" and reply["req"] == "q"
    assert reply["data"]["range_days"] == 7 and len(reply["data"]["days"]) == 7


async def test_stats_without_history_is_an_error():
    sent = await _run_connection("full", [json.dumps({"type": "stats", "req": "q"})], None)
    assert sent[-1]["type"] == "error" and sent[-1]["req"] == "q"


async def test_choose_if_blocked_marks_the_episode_answered(tmp_path):
    from herdeck.decisions import decision_revision

    prompt = "Allow?\n❯ 1. Yes\n  2. No\n"
    herdr = StubHerdr(
        [{"pane_id": "p1", "agent": "claude", "agent_status": "blocked", "terminal_id": "t1"}]
    )
    herdr.detection["p1"] = prompt
    tracker = StatusSinceTracker(None)
    seen = []

    class Obs:
        def observe(self, panes, now):
            pass

        def note_answer(self, pane_id):
            seen.append(pane_id)

    tracker.add_observer(Obs())
    rev = decision_revision("srv", "p1", "t1", prompt)
    await handle_client_message(
        herdr,
        "srv",
        json.dumps(
            {
                "type": "choose_if_blocked",
                "req": "c",
                "pane_id": "p1",
                "terminal_id": "t1",
                "decision_revision": rev,
                "choice": "1",
            }
        ),
        status_since=tracker,
    )
    await handle_client_message(
        herdr,
        "srv",
        json.dumps({"type": "act", "req": "a", "pane_id": "p1", "keys": ["enter"]}),
        status_since=tracker,
    )
    assert seen == ["p1", "p1"]


def test_default_path_honours_xdg(monkeypatch):
    monkeypatch.undo()  # drop the conftest isolation for this one check
    monkeypatch.setenv("XDG_STATE_HOME", "/xdg")
    assert hist.default_path() == "/xdg/herdeck/history.sqlite"
