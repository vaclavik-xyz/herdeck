"""Runtime side of the usage agent installer: /maintenance/servers/{id}/usage-agent.

Driven through a fake runner (no bridge) that answers a ``usage_agent``
message the way the connector callbacks would — result, error frame, or silence."""

import json
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest
from test_deckapp_live import StubIcons

from herdeck.config import DEFAULT_PROFILES, Config, HardwareConfig, ServerConfig
from herdeck.deckapp import DeckApp
from herdeck.deckapp import usage_agent_relay as rel
from herdeck.deckapp.live import LiveSource
from herdeck.deckapp.maintenance import Maintenance


def _data(action="status", ok=True, code="ok", error=None, installed=True):
    return {
        "action": action,
        "ok": ok,
        "code": code,
        "error": error,
        "installed": installed,
        "running": installed,
        "managed": True,
        "gui_session": True,
        "bridge_usage": True,
        "file": "/h/.local/state/herdeck/bridge-usage.json",
        "file_age_s": 12.5 if installed else None,
        "fresh": installed,
        "providers": ["codex", "claude"] if installed else [],
    }


class Runner:
    def __init__(self, reply=None, capabilities=("usage", "usage_agent")):
        self.sent: list[dict] = []
        self.reply = reply
        self.src = None
        self.connector = SimpleNamespace(
            protocol=3, capabilities=frozenset(capabilities), health=lambda: {}
        )

    def send(self, msg):
        self.sent.append(msg)
        if self.reply is not None and msg.get("type") == "usage_agent":
            self.reply(self, msg)

    def close(self):
        pass


def answer(data=None):
    def reply(runner, msg):
        out = data or _data(msg["action"], installed=msg["action"] != "uninstall")
        runner.src._on_result("prod", msg["req"], out)

    return reply


def refuse(message):
    def reply(runner, msg):
        runner.src._on_bridge_error("prod", msg["req"], message)

    return reply


def make(reply=None, *, connected=True, **kw):
    server = ServerConfig(id="prod", url="ws://bridge.local:8765", token="t")
    config = Config(
        servers=[server], profiles=dict(DEFAULT_PROFILES), overview_order=["prod"], grid=(5, 3)
    )
    src = LiveSource(config, server)
    runner = Runner(reply, **kw)
    runner.src = src
    src.attach_runner(runner, "prod")
    if connected:
        src._on_connection("prod", True)
    return src, runner


def test_install_relays_and_refreshes_the_summary():
    src, runner = make(answer())
    out = src.bridge_usage_agent("prod", "install", wait_s=1)
    assert runner.sent == [{"type": "usage_agent", "req": "ua1", "action": "install"}]
    assert out["ok"] is True and out["code"] == "ok" and out["message"] == ""
    assert out["agent"]["installed"] is True and out["agent"]["file_age_s"] == 12.5
    assert src.usage_agent_summary("prod") == {
        "installed": True,
        "running": True,
        "fresh": True,
        "bridge_usage": True,
        "file_age_s": 12.5,
        "providers": ["codex", "claude"],
        "error": None,
    }


def test_a_refused_install_keeps_the_bridge_code_and_state():
    data = _data("install", ok=False, code="no_gui_session", error="log in", installed=False)
    src, runner = make(answer(data))
    out = src.bridge_usage_agent("prod", "install", wait_s=1)
    assert out["ok"] is False and out["code"] == "no_gui_session" and out["message"] == "log in"
    assert out["agent"]["installed"] is False
    assert src.usage_agent_summary("prod")["error"] == "log in"


def test_the_reply_never_reaches_the_deck_result_path():
    src, runner = make(answer())
    seen = []
    src.set_result_tap(lambda sid, req, data: seen.append(req))
    src.bridge_usage_agent("prod", "status", wait_s=1)
    assert seen == []


def test_status_is_asked_once_per_connection():
    src, runner = make(answer())
    src._on_snapshot("prod", [])
    src._on_snapshot("prod", [])
    assert [m["action"] for m in runner.sent if m["type"] == "usage_agent"] == ["status"]
    assert src.usage_agent_summary("prod")["installed"] is True
    src._on_connection("prod", False)
    assert src.usage_agent_summary("prod") is None
    src._on_connection("prod", True)
    src._on_snapshot("prod", [])
    assert [m["action"] for m in runner.sent if m["type"] == "usage_agent"] == ["status", "status"]


def test_an_old_bridge_is_never_asked():
    src, runner = make(answer(), capabilities=("usage", "hooks"))
    src._on_snapshot("prod", [])
    assert [m for m in runner.sent if m["type"] == "usage_agent"] == []
    out = src.bridge_usage_agent("prod", "install", wait_s=1)
    assert out["code"] == "unsupported"
    assert src.usage_agent_summary("prod") is None


def test_disconnected_and_unknown_servers():
    src, runner = make(answer(), connected=False)
    assert src.bridge_usage_agent("prod", "status", wait_s=1)["code"] == "disconnected"
    assert runner.sent == []
    assert src.bridge_usage_agent("ghost", "status", wait_s=1) is None


@pytest.mark.parametrize(
    "message, code",
    [
        ("read-only token: 'usage_agent' is not allowed", "readonly"),
        ("usage_agent: timed out", "failed"),
    ],
)
def test_bridge_errors_map_to_codes(message, code):
    src, runner = make(refuse(message))
    out = src.bridge_usage_agent("prod", "install", wait_s=1)
    assert out["ok"] is False and out["code"] == code and out["message"] == message
    assert out["agent"] is None


def test_a_malformed_reply_fails():
    src, runner = make(answer({"nope": 1}))
    out = src.bridge_usage_agent("prod", "status", wait_s=1)
    assert out["code"] == "failed" and "malformed" in out["message"]


def test_a_silent_bridge_times_out_and_a_late_reply_is_dropped():
    src, runner = make()
    out = src.bridge_usage_agent("prod", "status", wait_s=0.05)
    assert out["code"] == "timeout"
    assert src._usage_agent_on_result(runner.sent[0]["req"], _data()) is False


def test_a_disconnect_fails_a_waiting_request():
    def drop(runner, msg):
        runner.src._on_connection("prod", False)

    src, runner = make(drop)
    assert src.bridge_usage_agent("prod", "status", wait_s=1)["code"] == "disconnected"


def test_route_server_id():
    assert rel.route_server_id("/maintenance/servers/prod/usage-agent") == "prod"
    assert rel.route_server_id("/maintenance/servers/local%3Ab/usage-agent") == "local:b"
    assert rel.route_server_id("/maintenance/servers/prod/hooks") is None
    assert rel.route_server_id("/maintenance/servers//usage-agent") is None


def test_maintenance_status_carries_the_summary(tmp_path):
    src, runner = make(answer())
    src._on_snapshot("prod", [])
    app = SimpleNamespace(config=SimpleNamespace(hardware=HardwareConfig()), _sinks=[], _source=src)
    status = Maintenance(
        app,
        home=tmp_path,
        enumerate_usb=lambda: [],
        which=lambda name: None,
        sysfs_root=str(tmp_path / "no-sysfs"),
        state_path=tmp_path / "d200-usb.json",
    ).status()
    assert status["servers"]["prod"]["usage_agent"]["running"] is True


# --- HTTP routes -------------------------------------------------------------------

PATH = "/maintenance/servers/prod/usage-agent"


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
    url = f"http://{app.host}:{app.port}{path}?token={token if token is not None else app.token}"
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, json.loads(r.read())


def test_http_round_trip():
    app, src, runner = _serve(answer())
    try:
        status, body = _post(app, PATH, {"action": "uninstall"})
        assert status == 200 and body["code"] == "ok" and body["agent"]["installed"] is False
        assert runner.sent[-1] == {"type": "usage_agent", "req": "ua1", "action": "uninstall"}
        status, body = _get(app, PATH)
        assert status == 200 and body["agent"]["installed"] is True
        assert runner.sent[-1]["action"] == "status"
    finally:
        app.close()


def test_http_routes_require_the_token_a_valid_action_and_a_known_server():
    app, src, runner = _serve(answer())
    try:
        for call in (
            lambda: _post(app, PATH, {"action": "status"}, token="wrong"),
            lambda: _get(app, PATH, token="wrong"),
        ):
            with pytest.raises(urllib.error.HTTPError) as e:
                call()
            assert e.value.code == 403
        assert runner.sent == []
        for bad in ({}, {"action": "wipe"}, {"action": 1}):
            with pytest.raises(urllib.error.HTTPError) as e:
                _post(app, PATH, bad)
            assert e.value.code == 400
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(app, "/maintenance/servers/ghost/usage-agent", {"action": "status"})
        assert e.value.code == 404
        assert runner.sent == []
    finally:
        app.close()
