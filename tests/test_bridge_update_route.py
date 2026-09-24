"""Runtime side of the bridge self-update: POST/GET /maintenance/servers/{id}/update.

Driven through a fake runner (no bridge): a runner may answer an ``update``
the way the connector callbacks would — result, error frame, progress lines,
or silence."""

import json
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest
from test_deckapp_live import StubIcons

from herdeck import __version__
from herdeck.config import DEFAULT_PROFILES, Config, ServerConfig
from herdeck.connector import Connector
from herdeck.deckapp import DeckApp
from herdeck.deckapp import bridge_update as bu
from herdeck.deckapp.live import LiveSource, build_live_source
from herdeck.protocol import Progress, decode_inbound


class UpdateRunner:
    def __init__(self, reply=None, capabilities=("self_update",), version="0.0.1"):
        self.sent: list[dict] = []
        self.reply = reply
        self.src = None
        self.connector = SimpleNamespace(
            protocol=3,
            capabilities=frozenset(capabilities),
            health=lambda: {"bridge_version": self.version},
        )
        self.version = version

    def send(self, msg):
        self.sent.append(msg)
        if self.reply is not None and msg.get("type") == "update":
            self.reply(self, msg)

    def close(self):
        pass

    # helpers a reply function uses
    def progress(self, msg, stage, message):
        self.src._on_progress("prod", msg["req"], stage, message)

    def result(self, msg, data):
        self.src._on_result("prod", msg["req"], data)

    def error(self, msg, message, req=True):
        self.src._on_bridge_error("prod", msg["req"] if req else None, message)


def make(reply=None, *, connected=True, **runner_kw):
    server = ServerConfig(id="prod", url="ws://bridge.local:8765", token="t")
    config = Config(
        servers=[server], profiles=dict(DEFAULT_PROFILES), overview_order=["prod"], grid=(5, 3)
    )
    src = LiveSource(config, server)
    runner = UpdateRunner(reply, **runner_kw)
    runner.src = src
    src.attach_runner(runner, "prod")
    if connected:
        src._on_connection("prod", True)
    return src, runner


def updated(runner, msg):
    runner.progress(msg, "download", "downloading")
    runner.result(msg, {"updated": msg["version"], "source": "wheel", "restarting": True})


def test_update_sends_the_runtime_version_and_reports_updated():
    src, runner = make(updated)
    out = src.bridge_update("prod", 1.0)
    assert runner.sent == [{"type": "update", "req": "u1", "version": __version__}]
    assert out["ok"] is True and out["code"] == "updated"
    assert out["server_id"] == "prod" and out["target"] == __version__
    assert out["progress"] == [{"seq": 1, "stage": "download", "message": "downloading"}]
    assert out["next"] == 1 and out["output"] == ""


def test_update_reply_never_reaches_the_deck_result_path():
    src, runner = make(updated)
    src.bridge_update("prod", 1.0)
    assert [m["type"] for m in runner.sent] == ["update"]  # no resync `list` after it


@pytest.mark.parametrize(
    "reply, code, message",
    [
        (
            lambda r, m: r.result(
                m, {"updated": None, "error": {"code": "not_managed", "message": "no marker"}}
            ),
            "not_managed",
            "no marker",
        ),
        (
            lambda r, m: r.error(m, "read-only token: 'update' is not allowed"),
            "readonly",
            "read-only token: 'update' is not allowed",
        ),
        (
            lambda r, m: r.result(
                m, {"updated": None, "error": {"code": "busy", "message": "already running"}}
            ),
            "busy",
            "already running",
        ),
        (
            lambda r, m: r.error(m, "unknown client message: update", req=False),
            "unsupported",
            "unknown client message: update",
        ),
    ],
)
def test_refusals_map_to_structured_outcomes(reply, code, message):
    src, _ = make(reply)
    out = src.bridge_update("prod", 1.0)
    assert (out["ok"], out["code"], out["message"]) == (False, code, message)


def test_failure_carries_the_installer_tail():
    def failed(runner, msg):
        runner.result(
            msg,
            {
                "updated": None,
                "error": {"code": "failed", "message": "installer exited with 1", "output": "E: x"},
            },
        )

    out = make(failed)[0].bridge_update("prod", 1.0)
    assert out["code"] == "failed" and out["ok"] is False
    assert out["message"] == "installer exited with 1" and out["output"] == "E: x"


def test_unchanged_bridge_is_updated():
    def same(runner, msg):
        runner.result(msg, {"updated": msg["version"], "unchanged": True, "restarting": False})

    out = make(same)[0].bridge_update("prod", 1.0)
    assert out["code"] == "updated" and "already at" in out["message"]


def test_nothing_is_sent_to_a_disconnected_or_old_bridge():
    src, runner = make(connected=False)
    assert src.bridge_update("prod", 0)["code"] == "disconnected"
    src, runner = make(capabilities=())
    assert src.bridge_update("prod", 0)["code"] == "unsupported"
    assert runner.sent == []
    assert src.bridge_update("nope", 0) is None
    assert src.bridge_update_status("prod", 0, 0) is None  # no job yet


def test_pending_then_long_poll_progress_and_outcome():
    src, runner = make()  # silent: the bridge is installing
    out = src.bridge_update("prod", 0)
    assert (out["ok"], out["code"]) == (True, "pending") and out["next"] == 0
    req = runner.sent[0]["req"]
    # a second POST joins the running update instead of sending another
    assert src.bridge_update("prod", 0)["code"] == "pending"
    assert len(runner.sent) == 1

    def later():
        time.sleep(0.1)
        src._on_progress("prod", req, "install", "Collecting herdeck")

    threading.Thread(target=later).start()
    polled = src.bridge_update_status("prod", 0, 3.0)
    assert polled["code"] == "pending"
    assert polled["progress"] == [{"seq": 1, "stage": "install", "message": "Collecting herdeck"}]
    src._on_result("prod", req, {"updated": __version__, "source": "wheel", "restarting": True})
    done = src.bridge_update_status("prod", polled["next"], 3.0)
    assert done["code"] == "updated" and done["progress"] == []


def test_a_lost_reply_is_settled_only_by_the_target_version():
    src, runner = make()
    src.bridge_update("prod", 0)
    src._on_connection("prod", False)
    assert "disconnected" in src.bridge_update_status("prod", 0, 0)["message"]
    src._on_connection("prod", True)
    # back at the old version: it may still be installing -> stays pending
    src._on_snapshot("prod", [])
    assert src.bridge_update_status("prod", 0, 0)["code"] == "pending"
    # a retry meanwhile joins the running update instead of sending again
    assert src.bridge_update("prod", 0)["code"] == "pending"
    assert len([m for m in runner.sent if m["type"] == "update"]) == 1
    runner.version = __version__
    src._on_snapshot("prod", [])
    assert src.bridge_update_status("prod", 0, 0)["code"] == "updated"


@pytest.mark.parametrize("bridge, code", [(__version__, "current"), ("99.0.0", "newer")])
def test_a_bridge_at_or_above_the_runtime_version_is_never_sent_an_update(bridge, code):
    src, runner = make(updated, version=bridge)
    out = src.bridge_update("prod", 0)
    assert (out["ok"], out["code"]) == (True, code)
    assert runner.sent == []


def test_downgrade_refusal_maps_through():
    def refuse(runner, msg):
        runner.result(msg, {"updated": None, "error": {"code": "downgrade", "message": "no"}})

    out = make(refuse)[0].bridge_update("prod", 1.0)
    assert (out["ok"], out["code"]) == (False, "downgrade")


def test_a_snapshot_during_the_install_does_not_settle_it():
    src, runner = make()
    src.bridge_update("prod", 0)
    src._on_snapshot("prod", [])
    assert src.bridge_update_status("prod", 0, 0)["code"] == "pending"


def test_an_unanswered_update_fails_after_the_deadline():
    clock = [0.0]
    jobs = bu.BridgeUpdates(clock=lambda: clock[0])
    job, created = jobs.begin("prod", "u1", "9.9.9")
    assert created
    clock[0] += bu.JOB_DEADLINE_S + 1
    out = jobs.wait(job, 0, 0)
    assert out["code"] == "failed" and "no reply" in out["message"]
    # a new update may start after the old one expired
    assert jobs.begin("prod", "u2", "9.9.9")[1] is True


def test_progress_keeps_a_bounded_tail():
    jobs = bu.BridgeUpdates()
    job, _ = jobs.begin("prod", "u1", "9.9.9")
    for i in range(bu.PROGRESS_KEEP + 10):
        jobs.on_progress("u1", "install", str(i))
    view = jobs.wait(job, 0, 0)
    assert len(view["progress"]) == bu.PROGRESS_KEEP and view["next"] == bu.PROGRESS_KEEP + 10


def test_route_server_id():
    assert bu.route_server_id("/maintenance/servers/prod/update") == "prod"
    assert bu.route_server_id("/maintenance/servers/a%20b/update") == "a b"
    assert bu.route_server_id("/maintenance/servers//update") is None
    assert bu.route_server_id("/maintenance/servers/a/b/update") is None
    assert bu.route_server_id("/maintenance/deck/restart") is None


# --- wire: protocol + connector ---------------------------------------------------


def test_progress_frames_decode_and_reach_the_consumer():
    msg = decode_inbound(json.dumps({"type": "progress", "req": "u1", "stage": "install", "message": "x"}))
    assert msg == Progress("u1", "install", "x")
    with pytest.raises(ValueError):
        decode_inbound(json.dumps({"type": "progress", "stage": "x"}))
    seen = []
    conn = Connector(
        ServerConfig("prod", "ws://x", "t"),
        on_snapshot=lambda *a: None,
        on_event=lambda *a: None,
        on_connection=lambda *a: None,
        on_progress=lambda *a: seen.append(a),
    )
    conn._dispatch(json.dumps({"type": "progress", "req": "u1", "stage": "download", "message": "m"}))
    assert seen == [("u1", "download", "m")]


def test_build_live_source_routes_update_frames():
    captured = {}

    class Conn:
        def __init__(self, server, **kw):
            captured.update(kw)
            self.capabilities = frozenset({"self_update"})

        def health(self):
            return {"bridge_version": None}

    class Runner:
        def __init__(self, connector):
            self.connector = connector
            self.sent = []

        def start(self):
            pass

        def send(self, msg):
            self.sent.append(msg)

        def close(self):
            pass

    server = ServerConfig(id="prod", url="ws://b", token="t")
    config = Config(
        servers=[server], profiles=dict(DEFAULT_PROFILES), overview_order=["prod"], grid=(5, 3)
    )
    src = build_live_source(config, server, connector_factory=Conn, runner_factory=Runner)
    captured["on_connection"]("prod", True)
    src.bridge_update("prod", 0)
    req = src._runners["prod"].sent[-1]["req"]
    captured["on_progress"](req, "install", "hello")
    captured["on_request_error"](req, "read-only token: 'update' is not allowed")
    out = src.bridge_update_status("prod", 0, 0)
    assert out["code"] == "readonly"
    assert out["progress"][0]["message"] == "hello"


# --- HTTP routes -------------------------------------------------------------------


def _serve(reply=None, **kw):
    src, runner = make(reply, **kw)
    app = DeckApp(src, host="127.0.0.1", port=0, serve=True, icon_provider=StubIcons())
    return app, src, runner


def _post(app, path, body, token=None):
    req = urllib.request.Request(
        f"http://{app.host}:{app.port}{path}", data=json.dumps(body).encode(), method="POST"
    )
    req.add_header("X-Herdeck-Token", token if token is not None else app.token)
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, json.loads(r.read() or b"null")


def _get(app, path, token=None):
    sep = "&" if "?" in path else "?"
    url = f"http://{app.host}:{app.port}{path}{sep}token={token if token is not None else app.token}"
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, json.loads(r.read())


PATH = "/maintenance/servers/prod/update"


def test_http_update_round_trip():
    app, src, runner = _serve(updated)
    try:
        status, body = _post(app, PATH, {"wait_ms": 1000})
        assert status == 200 and body["code"] == "updated"
        status, body = _get(app, PATH + "?after=0&wait_ms=0")
        assert status == 200 and body["code"] == "updated"
    finally:
        app.close()


def test_http_routes_require_the_token_and_a_known_server():
    app, src, runner = _serve(updated)
    try:
        for call in (
            lambda: _post(app, PATH, {}, token="wrong"),
            lambda: _get(app, PATH, token="wrong"),
        ):
            with pytest.raises(urllib.error.HTTPError) as e:
                call()
            assert e.value.code == 403
        assert runner.sent == []
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(app, "/maintenance/servers/ghost/update", {})
        assert e.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(app, PATH)  # no update has run yet
        assert e.value.code == 404
        for bad in ({"wait_ms": "soon"}, {"wait_ms": True}):
            with pytest.raises(urllib.error.HTTPError) as e:
                _post(app, PATH, bad)
            assert e.value.code == 400
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(app, PATH + "?after=x")
        assert e.value.code == 400
    finally:
        app.close()


def test_http_pending_answer_is_bounded():
    app, src, runner = _serve()  # silent bridge
    try:
        started = time.monotonic()
        status, body = _post(app, PATH, {"wait_ms": 200})
        assert status == 200 and body["code"] == "pending"
        assert time.monotonic() - started < 3
    finally:
        app.close()


def test_mock_source_has_no_update_route():
    from herdeck.deckapp import MockSource

    app = DeckApp(MockSource(), host="127.0.0.1", port=0, serve=True, icon_provider=StubIcons())
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(app, PATH, {})
        assert e.value.code == 404
    finally:
        app.close()
