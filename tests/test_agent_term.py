"""Live terminal for the desktop agent card: /agent/term/* over the bridge's observe.

The runtime relays the bridge's ``observe`` stream into a bounded, long-polled
buffer per card. Observation must stop when the card closes, when the window
stops polling (hidden / crashed), and must respect the bridge's limits.
"""

import base64
import json
import threading
import urllib.error
import urllib.request

import pytest
from test_agent_card import _serving_live, blocked, make
from test_deckapp_live import StubIcons

from herdeck.deckapp import DeckApp, MockSource
from herdeck.deckapp.agent_term import CardTerminals
from herdeck.model import AgentKey, AgentState, Status
from herdeck.protocol import TermClosed, TermFrame


def frame(req, seq, data="aGk=", full=False, cols=80, rows=24):
    return TermFrame(req, seq, full, cols, rows, data)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def terms(clock=None, **kw):
    sent: list[tuple[str, dict]] = []

    def send(server_id, msg):
        sent.append((server_id, msg))
        return True

    return CardTerminals(send, clock=clock or Clock(), reaper=False, **kw), sent


# --- CardTerminals ------------------------------------------------------------


def test_open_sends_a_clamped_observe_with_identity():
    t, sent = terms()
    sid = t.open("prod", "p0", "term-1", cols=999, rows=1)
    assert sid
    (server, msg), = sent
    assert server == "prod"
    assert msg["type"] == "observe"
    assert msg["pane_id"] == "p0" and msg["terminal_id"] == "term-1"
    assert (msg["cols"], msg["rows"]) == (240, 5)  # the bridge's own bounds


def test_frames_are_long_polled_in_order_with_a_cursor():
    t, sent = terms()
    sid = t.open("prod", "p0", "", cols=80, rows=24)
    req = sent[0][1]["req"]
    t.on_term("prod", frame(req, 1, full=True))
    t.on_term("prod", frame(req, 2))
    out = t.poll(sid, 0, 0)
    assert [f["seq"] for f in out["frames"]] == [1, 2]
    assert out["frames"][0]["full"] is True
    assert out["closed"] is None
    again = t.poll(sid, out["next"], 0)
    assert again["frames"] == []


def test_poll_wakes_when_a_frame_arrives():
    t, sent = terms()
    sid = t.open("prod", "p0", "", cols=80, rows=24)
    req = sent[0][1]["req"]
    threading.Timer(0.05, lambda: t.on_term("prod", frame(req, 1))).start()
    out = t.poll(sid, 0, 2.0)
    assert [f["seq"] for f in out["frames"]] == [1]


def test_a_full_frame_supersedes_the_backlog():
    t, sent = terms()
    sid = t.open("prod", "p0", "", cols=80, rows=24)
    req = sent[0][1]["req"]
    for seq in range(1, 5):
        t.on_term("prod", frame(req, seq))
    t.on_term("prod", frame(req, 5, full=True))
    out = t.poll(sid, 0, 0)
    assert [f["seq"] for f in out["frames"]] == [5]
    assert out["gap"] is False  # a full frame repaints everything


def test_buffer_is_bounded_and_reports_a_gap():
    t, sent = terms(max_frames=4)
    sid = t.open("prod", "p0", "", cols=80, rows=24)
    req = sent[0][1]["req"]
    for seq in range(1, 10):
        t.on_term("prod", frame(req, seq))
    out = t.poll(sid, 0, 0)
    assert len(out["frames"]) == 4
    assert out["gap"] is True


def test_an_oversized_frame_ends_the_session():
    t, sent = terms(max_bytes=16)
    sid = t.open("prod", "p0", "", cols=80, rows=24)
    req = sent[0][1]["req"]
    t.on_term("prod", frame(req, 1, data="x" * 64))
    out = t.poll(sid, 0, 0)
    assert out["closed"]
    assert sent[-1][1] == {"type": "observe_stop", "req": req}


def test_close_stops_the_remote_observe_and_forgets_the_session():
    t, sent = terms()
    sid = t.open("prod", "p0", "", cols=80, rows=24)
    req = sent[0][1]["req"]
    assert t.close(sid) is True
    assert sent[-1][1] == {"type": "observe_stop", "req": req}
    assert t.poll(sid, 0, 0) is None
    assert t.close(sid) is False  # idempotent


def test_bridge_close_is_reported_then_the_session_is_dropped():
    t, sent = terms()
    sid = t.open("prod", "p0", "", cols=80, rows=24)
    req = sent[0][1]["req"]
    t.on_term("prod", frame(req, 1))
    t.on_term("prod", TermClosed(req, "too many live previews"))
    out = t.poll(sid, 0, 0)
    assert out["closed"] == "too many live previews"
    assert [f["seq"] for f in out["frames"]] == [1]  # the tail is still delivered
    assert t.poll(sid, out["next"], 0) is None
    # no observe_stop for a stream the bridge already ended
    assert [m["type"] for _, m in sent] == ["observe"]


def test_frames_for_another_server_or_request_are_ignored():
    t, sent = terms()
    sid = t.open("prod", "p0", "", cols=80, rows=24)
    req = sent[0][1]["req"]
    t.on_term("other", frame(req, 1))
    t.on_term("prod", frame("t-unknown", 1))
    assert t.poll(sid, 0, 0)["frames"] == []


def test_session_cap_evicts_the_least_recently_polled():
    clock = Clock()
    t, sent = terms(clock=clock, max_sessions=2)
    a = t.open("prod", "p0", "", cols=80, rows=24)
    clock.now += 1
    b = t.open("prod", "p1", "", cols=80, rows=24)
    clock.now += 1
    t.poll(a, 0, 0)  # a is fresher than b now
    c = t.open("prod", "p2", "", cols=80, rows=24)
    assert t.poll(b, 0, 0) is None
    assert t.poll(a, 0, 0) is not None and t.poll(c, 0, 0) is not None
    stops = [m for _, m in sent if m["type"] == "observe_stop"]
    assert len(stops) == 1


def test_idle_sessions_are_reaped_when_the_window_stops_polling():
    clock = Clock()
    t, sent = terms(clock=clock, idle_s=15.0)
    sid = t.open("prod", "p0", "", cols=80, rows=24)
    clock.now += 10
    t.reap()
    assert t.poll(sid, 0, 0) is not None  # polled: fresh again
    clock.now += 16
    t.reap()
    assert t.poll(sid, 0, 0) is None
    assert sent[-1][1]["type"] == "observe_stop"


def test_reaper_thread_stops_idle_sessions_on_its_own():
    t = CardTerminals(lambda sid, msg: True, idle_s=0.05, reap_interval_s=0.02)
    sid = t.open("prod", "p0", "", cols=80, rows=24)
    deadline = threading.Event()
    for _ in range(100):
        if t.poll_exists(sid) is False:
            break
        deadline.wait(0.02)
    assert t.poll_exists(sid) is False


def test_connection_drop_closes_that_servers_sessions():
    t, sent = terms()
    sid = t.open("prod", "p0", "", cols=80, rows=24)
    t.close_server("prod", "disconnected")
    assert t.poll(sid, 0, 0)["closed"] == "disconnected"


def test_failed_send_closes_at_once():
    t = CardTerminals(lambda sid, msg: False, clock=Clock(), reaper=False)
    sid = t.open("prod", "p0", "", cols=80, rows=24)
    assert t.poll(sid, 0, 0)["closed"] == "disconnected"


# --- LiveSource wiring ----------------------------------------------------------


def test_live_source_opens_polls_and_closes_through_the_runner():
    app, src, runner, _ = make()
    src._on_snapshot("prod", [blocked()])
    runner.sent.clear()
    out = src.card_term_open("prod", "p0", 100, 30)
    assert out["ok"] is True
    observe = [m for m in runner.sent if m["type"] == "observe"][0]
    assert observe["terminal_id"] == "term-1"
    src._on_term("prod", frame(observe["req"], 1, full=True))
    polled = src.card_term_poll(out["id"], 0, 0)
    assert [f["seq"] for f in polled["frames"]] == [1]
    assert src.card_term_close(out["id"]) is True
    assert runner.sent[-1] == {"type": "observe_stop", "req": observe["req"]}


def test_live_source_refuses_unknown_or_disconnected_agents():
    app, src, runner, _ = make()
    src._on_snapshot("prod", [blocked()])
    assert src.card_term_open("prod", "ghost", 80, 24) is None
    src._on_connection("prod", False)
    assert src.card_term_open("prod", "p0", 80, 24)["code"] == "disconnected"


def test_live_source_disconnect_ends_the_preview():
    app, src, runner, _ = make()
    src._on_snapshot("prod", [blocked()])
    out = src.card_term_open("prod", "p0", 80, 24)
    src._on_connection("prod", False)
    assert src.card_term_poll(out["id"], 0, 0)["closed"]


def test_live_source_close_stops_every_preview():
    app, src, runner, _ = make()
    src._on_snapshot("prod", [blocked()])
    src.card_term_open("prod", "p0", 80, 24)
    runner.sent.clear()
    src.close()
    assert [m["type"] for m in runner.sent] == ["observe_stop"]


# --- HTTP routes --------------------------------------------------------------


def _get(app, path, token=None):
    url = f"http://{app.host}:{app.port}{path}&token={token if token is not None else app.token}"
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, json.loads(r.read())


def _post(app, path, body, token=None):
    req = urllib.request.Request(
        f"http://{app.host}:{app.port}{path}", data=json.dumps(body).encode(), method="POST"
    )
    req.add_header("X-Herdeck-Token", token if token is not None else app.token)
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read() or b"null")


def test_term_routes_roundtrip_and_require_the_token():
    app, src, runner = _serving_live()
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(app, "/agent/term/open", {"server_id": "prod", "pane_id": "p0"}, token="no")
        assert e.value.code == 403
        code, opened = _post(
            app, "/agent/term/open", {"server_id": "prod", "pane_id": "p0", "cols": 90, "rows": 20}
        )
        assert code == 200 and opened["ok"] is True
        observe = [m for m in runner.sent if m["type"] == "observe"][-1]
        assert (observe["cols"], observe["rows"]) == (90, 20)
        data = base64.b64encode(b"\x1b[1mhello").decode()
        src._on_term("prod", frame(observe["req"], 7, data=data, full=True))
        code, polled = _get(app, f"/agent/term/poll?id={opened['id']}&after=0&wait_ms=0")
        assert polled["frames"][0]["data"] == data
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(app, f"/agent/term/poll?id={opened['id']}&after=0", token="bad")
        assert e.value.code == 403
        code, closed = _post(app, "/agent/term/close", {"id": opened["id"]})
        assert closed["ok"] is True
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(app, f"/agent/term/poll?id={opened['id']}&after=0&wait_ms=0")
        assert e.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(app, "/agent/term/poll?id=x&after=nope")
        assert e.value.code == 400
    finally:
        app.close()


def test_term_routes_404_on_the_mock():
    app = DeckApp(MockSource(), host="127.0.0.1", port=0, serve=True, icon_provider=StubIcons())
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(app, "/agent/term/open", {"server_id": "demo", "pane_id": "p0"})
        assert e.value.code == 404
    finally:
        app.close()


def test_open_is_refused_when_the_bridge_cannot_observe():
    app, src, runner, _ = make()
    src._on_snapshot("prod", [blocked()])
    runner.connector.capabilities = frozenset()
    runner.sent.clear()
    assert src.card_term_open("prod", "p0", 80, 24)["code"] == "unsupported"
    assert [m for m in runner.sent if m["type"] == "observe"] == []


def test_anonymous_error_right_after_open_ends_the_preview():
    app, src, runner, _ = make()
    src._on_snapshot("prod", [blocked()])
    out = src.card_term_open("prod", "p0", 80, 24)
    src._on_request_error("prod", None, "unknown message type: observe")
    assert src.card_term_poll(out["id"], 0, 0)["closed"] == "unknown message type: observe"


def test_anonymous_error_leaves_a_streaming_preview_alone():
    app, src, runner, _ = make()
    src._on_snapshot("prod", [blocked()])
    out = src.card_term_open("prod", "p0", 80, 24)
    observe = [m for m in runner.sent if m["type"] == "observe"][-1]
    src._on_term("prod", frame(observe["req"], 1, full=True))
    src._on_request_error("prod", None, "something else failed")
    assert src.card_term_poll(out["id"], 0, 0)["closed"] is None


def test_error_naming_the_observe_ends_it():
    app, src, runner, _ = make()
    src._on_snapshot("prod", [blocked()])
    out = src.card_term_open("prod", "p0", 80, 24)
    observe = [m for m in runner.sent if m["type"] == "observe"][-1]
    src._on_request_error("prod", observe["req"], "nope")
    assert src.card_term_poll(out["id"], 0, 0)["closed"] == "nope"


def test_t3_agents_have_no_terminal():
    app, src, runner, _ = make()
    src._on_snapshot(
        "prod",
        [AgentState(AgentKey("prod", "t1"), "codex", "t1", Status.WORKING, backend="t3")],
    )
    assert src.card_term_open("prod", "t1", 80, 24)["code"] == "invalid"
