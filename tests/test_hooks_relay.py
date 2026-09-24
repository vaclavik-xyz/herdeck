"""Runtime side of the subagent-hook installer: /maintenance/servers/{id}/hooks.

Driven through a fake runner (no bridge) that answers a ``hooks`` message the
way the connector callbacks would — result, error frame, or silence."""

import json
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest
from test_deckapp_live import StubIcons

from herdeck.config import DEFAULT_PROFILES, Config, HardwareConfig, ServerConfig
from herdeck.deckapp import DeckApp
from herdeck.deckapp import hooks_relay as hr
from herdeck.deckapp.live import LiveSource
from herdeck.deckapp.maintenance import Maintenance


def _agents(claude=True, codex=False, error=None):
    return {
        "claude": {
            "agent": "claude",
            "installed": claude,
            "file": "/h/.claude/settings.json",
            "events": [],
            "error": error,
        },
        "codex": {
            "agent": "codex",
            "installed": codex,
            "file": "/h/.codex/hooks.json",
            "events": [],
            "error": None,
            "needs_trust": codex,
            "features_hooks_enabled": False,
        },
    }


class HooksRunner:
    def __init__(self, reply=None, capabilities=("hooks",)):
        self.sent: list[dict] = []
        self.reply = reply
        self.src = None
        self.connector = SimpleNamespace(
            protocol=3, capabilities=frozenset(capabilities), health=lambda: {}
        )

    def send(self, msg):
        self.sent.append(msg)
        if self.reply is not None and msg.get("type") == "hooks":
            self.reply(self, msg)

    def close(self):
        pass


def answer(agents=None):
    def reply(runner, msg):
        installed = msg.get("action") == "install"
        data = {"action": msg["action"], "ok": True, "agents": agents or _agents(claude=installed)}
        runner.src._on_result("prod", msg["req"], data)

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
    runner = HooksRunner(reply, **kw)
    runner.src = src
    src.attach_runner(runner, "prod")
    if connected:
        src._on_connection("prod", True)
    return src, runner


def test_install_relays_to_the_bridge_and_refreshes_the_summary():
    src, runner = make(answer())
    out = src.bridge_hooks("prod", "install", ["claude"], wait_s=1)
    assert runner.sent == [{"type": "hooks", "req": "hk1", "action": "install", "agents": ["claude"]}]
    assert out["ok"] is True and out["code"] == "ok"
    assert out["agents"]["claude"]["installed"] is True
    assert src.hooks_summary("prod")["claude"] == {
        "installed": True,
        "file": "/h/.claude/settings.json",
        "error": None,
    }
    assert src.hooks_summary("prod")["codex"]["features_hooks_enabled"] is False


def test_the_reply_never_reaches_the_deck_result_path():
    src, runner = make(answer())
    seen = []
    src.set_result_tap(lambda sid, req, data: seen.append(req))
    src.bridge_hooks("prod", "status", None, wait_s=1)
    assert seen == []


def test_status_is_asked_once_per_connection_after_the_first_snapshot():
    src, runner = make(answer())
    src._on_snapshot("prod", [])
    src._on_snapshot("prod", [])
    assert [m["action"] for m in runner.sent] == ["status"]
    assert "agents" not in runner.sent[0]
    assert src.hooks_summary("prod")["claude"]["installed"] is False
    src._on_connection("prod", False)
    assert src.hooks_summary("prod") is None
    src._on_connection("prod", True)
    src._on_snapshot("prod", [])
    assert [m["action"] for m in runner.sent] == ["status", "status"]


def test_an_old_bridge_is_never_asked():
    src, runner = make(answer(), capabilities=("self_update",))
    src._on_snapshot("prod", [])
    assert runner.sent == []
    out = src.bridge_hooks("prod", "install", None, wait_s=1)
    assert out["code"] == "unsupported" and runner.sent == []
    assert src.hooks_summary("prod") is None


def test_disconnected_and_unknown_servers():
    src, runner = make(answer(), connected=False)
    assert src.bridge_hooks("prod", "status", None, wait_s=1)["code"] == "disconnected"
    assert runner.sent == []
    assert src.bridge_hooks("ghost", "status", None, wait_s=1) is None


@pytest.mark.parametrize(
    "message, code",
    [
        ("read-only token: 'hooks' is not allowed", "readonly"),
        ("hooks: timed out", "failed"),
    ],
)
def test_bridge_errors_map_to_codes(message, code):
    src, runner = make(refuse(message))
    out = src.bridge_hooks("prod", "install", None, wait_s=1)
    assert out["ok"] is False and out["code"] == code and out["message"] == message


def test_an_agent_file_error_is_reported_as_failed_with_the_statuses():
    src, runner = make(answer(_agents(claude=False, error="settings.json is not valid JSON")))
    out = src.bridge_hooks("prod", "install", None, wait_s=1)
    assert out["code"] == "failed" and "not valid JSON" in out["message"]
    assert out["agents"]["claude"]["error"]
    assert src.hooks_summary("prod")["claude"]["error"]


def test_a_silent_bridge_times_out_and_a_late_reply_is_dropped():
    src, runner = make()
    out = src.bridge_hooks("prod", "status", None, wait_s=0.05)
    assert out["code"] == "timeout"
    req = runner.sent[0]["req"]
    assert src._hooks_on_result(req, {"agents": {}}) is False


def test_a_disconnect_fails_a_waiting_request():
    def drop(runner, msg):
        runner.src._on_connection("prod", False)

    src, runner = make(drop)
    out = src.bridge_hooks("prod", "status", None, wait_s=1)
    assert out["code"] == "disconnected"


def test_route_server_id():
    assert hr.route_server_id("/maintenance/servers/prod/hooks") == "prod"
    assert hr.route_server_id("/maintenance/servers/local%3Ab/hooks") == "local:b"
    assert hr.route_server_id("/maintenance/servers/prod/update") is None
    assert hr.route_server_id("/maintenance/servers//hooks") is None


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
    assert status["servers"]["prod"]["hooks"]["claude"]["installed"] is False
    assert status["servers"]["prod"]["hooks"]["codex"]["needs_trust"] is False


# --- HTTP routes -------------------------------------------------------------------

PATH = "/maintenance/servers/prod/hooks"


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
        status, body = _post(app, PATH, {"action": "install", "agents": ["claude", "codex"]})
        assert status == 200 and body["code"] == "ok"
        assert runner.sent[-1]["agents"] == ["claude", "codex"]
        status, body = _get(app, PATH)
        assert status == 200 and body["agents"]["claude"]["installed"] is False
        assert runner.sent[-1]["action"] == "status"
    finally:
        app.close()


def test_http_routes_require_the_token_valid_bodies_and_a_known_server():
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
        for bad in (
            {},
            {"action": "wipe"},
            {"action": "install", "agents": ["pi"]},
            {"action": "install", "agents": []},
            {"action": "install", "agents": "claude"},
        ):
            with pytest.raises(urllib.error.HTTPError) as e:
                _post(app, PATH, bad)
            assert e.value.code == 400
        assert runner.sent == []
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(app, "/maintenance/servers/ghost/hooks", {"action": "status"})
        assert e.value.code == 404
    finally:
        app.close()


def test_mock_source_has_no_hooks_route():
    from herdeck.deckapp import MockSource

    app = DeckApp(MockSource(), host="127.0.0.1", port=0, serve=True, icon_provider=StubIcons())
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(app, PATH, {"action": "status"})
        assert e.value.code == 404
    finally:
        app.close()
