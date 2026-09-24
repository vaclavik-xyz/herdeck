"""Runtime side of the bridge history: GET /stats (deckapp/stats.py).

Driven through fake runners (no bridge) that answer a ``stats`` message the
way the connector callbacks would."""

import json
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest
from test_deckapp_live import StubIcons

from herdeck.config import DEFAULT_PROFILES, Config, ServerConfig
from herdeck.deckapp import DeckApp
from herdeck.deckapp import stats as st
from herdeck.deckapp.live import LiveSource
from herdeck.history import Episode, aggregate

H = 3_600_000
M = 60_000
NOON = 1_790_251_200_000


def bridge_data(episodes, group_by="repo"):
    rows = [e.row() for e in episodes]
    return aggregate(rows, now_ms=NOON, range_days=1, group_by=group_by)


def ep(status, start, end, to="idle", repo="herdeck", pane="p1"):
    return Episode(pane, "t1", "claude", repo, repo, repo, status, to, start, end, False)


class StatsRunner:
    def __init__(self, server_id, reply, capabilities=("history",)):
        self.server_id = server_id
        self.sent: list[dict] = []
        self.reply = reply
        self.src = None
        self.connector = SimpleNamespace(capabilities=frozenset(capabilities))

    def send(self, msg):
        self.sent.append(msg)
        if self.reply is not None and msg.get("type") == "stats":
            self.reply(self, msg)

    def close(self):
        pass


def answer(data_by_group):
    def reply(runner, msg):
        runner.src._on_result(runner.server_id, msg["req"], data_by_group(msg["group_by"]))

    return reply


def make(replies, *, connected=None, capabilities=None):
    servers = [ServerConfig(id=sid, url=f"ws://{sid}:8765", token="t") for sid in replies]
    config = Config(
        servers=servers,
        profiles=dict(DEFAULT_PROFILES),
        overview_order=list(replies),
        grid=(5, 3),
    )
    src = LiveSource(config)
    runners = {}
    for sid, reply in replies.items():
        caps = (capabilities or {}).get(sid, ("history",))
        runner = StatsRunner(sid, reply, caps)
        runner.src = src
        src.attach_runner(runner, sid)
        runners[sid] = runner
        if connected is None or sid in connected:
            src._on_connection(sid, True)
    return src, runners


A = [
    ep("working", NOON - 2 * H, NOON - H, to="blocked"),
    ep("blocked", NOON - H, NOON - H + 10 * M, to="working"),
]
B = [
    ep("working", NOON - 3 * H, NOON - H, to="done", repo="api", pane="p9"),
    ep("blocked", NOON - H, NOON - 30 * M, to="working", repo="herdeck", pane="p8"),
    ep("blocked", NOON - 30 * M, NOON - 10 * M, to="working", repo="herdeck", pane="p8"),
]


def test_single_bridge_is_relayed_with_its_own_percentiles():
    src, runners = make({"m4": answer(lambda g: bridge_data(A, g))})
    out = src.stats(1, "repo", wait_s=1)
    sent = runners["m4"].sent
    assert len(sent) == 1 and sent[0]["type"] == "stats"
    assert sent[0]["range_days"] == 1 and sent[0]["group_by"] == "repo"
    assert isinstance(sent[0]["tz_offset_min"], int)
    assert out["ok"] is True and out["servers"] == ["m4"]
    assert out["total"]["working_ms"] == H
    assert out["total"]["answer_median_ms"] == 10 * M
    assert "answer_samples_ms" not in out["total"]
    assert all("answer_samples_ms" not in g for g in out["groups"])


def test_two_bridges_are_summed_and_percentiles_recomputed():
    src, runners = make(
        {
            "m4": answer(lambda g: bridge_data(A, g)),
            "mb": answer(lambda g: bridge_data(B, g)),
        }
    )
    out = src.stats(1, "repo", wait_s=1)
    assert out["ok"] is True and sorted(out["servers"]) == ["m4", "mb"]
    total = out["total"]
    assert total["working_ms"] == 3 * H
    assert total["blocked_ms"] == 10 * M + 30 * M + 20 * M
    assert total["blocked_count"] == 3 and total["done_count"] == 1
    # merged samples: 10, 20, 30 min
    assert total["answer_median_ms"] == 20 * M
    assert total["answer_p90_ms"] == 30 * M
    groups = {g["key"]: g for g in out["groups"]}
    assert groups["herdeck"]["blocked_ms"] == 60 * M  # same repo on both bridges
    assert groups["api"]["working_ms"] == 2 * H
    assert len(out["days"]) == 1 and out["days"][0]["working_ms"] == 3 * H
    # no deck resync `list` after a stats reply
    assert [m["type"] for m in runners["m4"].sent] == ["stats"]


def test_agent_groups_stay_per_server():
    src, _ = make(
        {
            "m4": answer(lambda g: bridge_data(A, g)),
            "mb": answer(lambda g: bridge_data(A, g)),
        }
    )
    out = src.stats(1, "agent", wait_s=1)
    keys = sorted(g["key"] for g in out["groups"])
    assert keys == ["m4:p1/t1", "mb:p1/t1"]
    assert any(g["label"].endswith("(mb)") for g in out["groups"])


def test_missing_bridges_are_reported():
    src, runners = make(
        {
            "m4": answer(lambda g: bridge_data(A, g)),
            "old": None,
            "down": None,
            "silent": None,
        },
        connected={"m4", "old", "silent"},
        capabilities={"old": ("status_since",)},
    )
    out = src.stats(7, "agent", wait_s=0.2)
    assert out["ok"] is True
    reasons = {m["server_id"]: m["reason"] for m in out["missing"]}
    assert reasons == {"old": "unsupported", "down": "disconnected", "silent": "timeout"}
    assert runners["old"].sent == [] and runners["down"].sent == []


def test_errors_and_no_answers():
    def refuse(runner, msg):
        runner.src._on_bridge_error(runner.server_id, msg["req"], "stats unavailable: x")

    src, _ = make({"m4": refuse})
    out = src.stats(1, "repo", wait_s=1)
    assert out == {
        "ok": False,
        "code": "failed",
        "message": "no bridge answered",
        "missing": [{"server_id": "m4", "reason": "stats unavailable: x"}],
    }
    src, _ = make({"m4": None}, capabilities={"m4": ()})
    assert src.stats(1, "repo", wait_s=0.1)["code"] == "unsupported"
    src, _ = make({"m4": None}, connected=set())
    assert src.stats(1, "repo", wait_s=0.1)["code"] == "disconnected"


def test_disconnect_fails_the_waiting_request():
    def drop(runner, msg):
        runner.src._on_connection(runner.server_id, False)

    src, _ = make({"m4": drop})
    out = src.stats(1, "repo", wait_s=5)
    assert out["ok"] is False
    assert out["missing"] == [{"server_id": "m4", "reason": "disconnected"}]


def test_local_tz_offset_is_minutes():
    assert isinstance(st.local_tz_offset_min(), int)


# --- HTTP ---------------------------------------------------------------------------


def _get(app, path, token=None):
    sep = "&" if "?" in path else "?"
    url = (
        f"http://{app.host}:{app.port}{path}{sep}token={token if token is not None else app.token}"
    )
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, json.loads(r.read())


def test_http_stats_route():
    src, runners = make({"m4": answer(lambda g: bridge_data(A, g))})
    app = DeckApp(src, host="127.0.0.1", port=0, serve=True, icon_provider=StubIcons())
    try:
        status, body = _get(app, "/stats?range=1&group=repo")
        assert status == 200 and body["ok"] is True and body["group_by"] == "repo"
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(app, "/stats?range=1", token="wrong")
        assert e.value.code == 403
        for bad in ("/stats?range=2", "/stats?range=x", "/stats?group=nope"):
            with pytest.raises(urllib.error.HTTPError) as e:
                _get(app, bad)
            assert e.value.code == 400
        assert len(runners["m4"].sent) == 1
    finally:
        app.close()


def test_mock_source_has_no_stats_route():
    from herdeck.deckapp import MockSource

    app = DeckApp(MockSource(), host="127.0.0.1", port=0, serve=True, icon_provider=StubIcons())
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(app, "/stats")
        assert e.value.code == 404
    finally:
        app.close()
