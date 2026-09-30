"""Editor API for bridge shared settings: GET /config `bridges` + POST /bridge-settings/{id}."""

import json
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest
from test_deckapp_live import StubIcons

from herdeck.config import DEFAULT_PROFILES, Config, ServerConfig
from herdeck.deckapp import DeckApp
from herdeck.deckapp import settings_relay as sr
from herdeck.deckapp.config_service import ConfigService
from herdeck.deckapp.live import LiveSource

PATH = "/bridge-settings/prod"
BODY = {"base_revision": 3, "settings": {"notifications": {"on": ["done"]}}}


class Runner:
    def __init__(self, reply=None, capabilities=("settings",)):
        self.sent: list[dict] = []
        self.reply = reply
        self.src = None
        self.connector = SimpleNamespace(
            protocol=3, capabilities=frozenset(capabilities), health=lambda: {}
        )

    def send(self, msg):
        self.sent.append(msg)
        if self.reply is not None and msg.get("type") == "settings_put":
            self.reply(self, msg)

    def close(self):
        pass


def result(data):
    return lambda r, m: r.src._on_result("prod", m["req"], data)


def refuse(message):
    return lambda r, m: r.src._on_bridge_error("prod", m["req"], message)


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


def _serve(reply=None, tmp_path=None, **kw):
    src, runner = make(reply, **kw)
    service = ConfigService(tmp_path / "config.toml", tmp_path / "local.toml") if tmp_path else None
    app = DeckApp(
        src, host="127.0.0.1", port=0, serve=True, icon_provider=StubIcons(),
        config_service=service,
    )
    return app, src, runner


def _post(app, path, body, token=None):
    req = urllib.request.Request(
        f"http://{app.host}:{app.port}{path}", data=json.dumps(body).encode(), method="POST"
    )
    req.add_header("X-Herdeck-Token", token if token is not None else app.token)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, None


def test_route_server_id():
    assert sr.route_server_id("/bridge-settings/prod") == "prod"
    assert sr.route_server_id("/bridge-settings/a%20b") == "a b"
    assert sr.route_server_id("/bridge-settings/") is None
    assert sr.route_server_id("/bridge-settings/a/b") is None


def test_ok_relays_the_put_frame():
    src, runner = make(result({"ok": True, "revision": 4}))
    assert src.bridge_settings_put("prod", 3, BODY["settings"], wait_s=1) == (
        200,
        {"ok": True, "revision": 4},
    )
    assert runner.sent == [
        {"type": "settings_put", "req": "sp1", "base_revision": 3, "settings": BODY["settings"]}
    ]


@pytest.mark.parametrize(
    ("error", "code"), [("stale_revision", 409), ("invalid", 422), ("too_large", 422)]
)
def test_bridge_refusals_map_to_codes(error, code):
    reply = result({"ok": False, "error": error, "messages": ["m"], "revision": 7})
    src, _ = make(reply)
    assert src.bridge_settings_put("prod", 3, {}, wait_s=1) == (
        code,
        {"ok": False, "error": error, "messages": ["m"], "revision": 7},
    )


def test_error_frame_is_502_with_its_message():
    src, _ = make(refuse("read-only token"))
    code, payload = src.bridge_settings_put("prod", 3, {}, wait_s=1)
    assert code == 502 and payload["messages"] == ["read-only token"] and payload["ok"] is False


def test_not_connected_or_no_capability_is_503_and_nothing_is_sent():
    src, runner = make(result({"ok": True, "revision": 1}), connected=False)
    assert src.bridge_settings_put("prod", 0, {}, wait_s=1)[0] == 503
    src, runner = make(result({"ok": True, "revision": 1}), capabilities=("hooks",))
    code, payload = src.bridge_settings_put("prod", 0, {}, wait_s=1)
    assert code == 503 and payload["error"] == "unsupported" and runner.sent == []


def test_silence_times_out_with_504_and_a_late_reply_is_dropped():
    src, runner = make()
    assert src.bridge_settings_put("prod", 0, {}, wait_s=0.05)[0] == 504
    seen = []
    src.set_result_tap(lambda sid, req, data: seen.append(req))
    src._on_result("prod", "sp1", {"ok": True, "revision": 1})
    assert seen == []
    assert src._settings_waits == {}


def test_disconnect_fails_a_waiting_put():
    def drop(r, m):
        r.src._on_connection("prod", False)

    src, _ = make(drop)
    assert src.bridge_settings_put("prod", 0, {}, wait_s=1)[0] == 503


def test_unknown_server_is_none():
    src, _ = make()
    assert src.bridge_settings_put("ghost", 0, {}, wait_s=1) is None


def test_reply_never_reaches_the_deck_result_path():
    src, _ = make(result({"ok": True, "revision": 1}))
    seen = []
    src.set_result_tap(lambda sid, req, data: seen.append(req))
    src.bridge_settings_put("prod", 0, {}, wait_s=1)
    assert seen == []


def test_http_round_trip_and_stale_second_editor(tmp_path):
    replies = iter(
        [
            {"ok": True, "revision": 4},
            {"ok": False, "error": "stale_revision", "messages": [], "revision": 4},
        ]
    )
    app, src, runner = _serve(lambda r, m: r.src._on_result("prod", m["req"], next(replies)))
    try:
        assert _post(app, PATH, BODY) == (200, {"ok": True, "revision": 4})
        status, body = _post(app, PATH, BODY)  # second editor, same stale base
        assert status == 409 and body["error"] == "stale_revision" and body["revision"] == 4
    finally:
        app.close()


def test_http_auth_body_validation_and_unknown_server():
    app, src, runner = _serve(result({"ok": True, "revision": 1}))
    try:
        assert _post(app, PATH, BODY, token="wrong")[0] == 403
        for bad in (
            {},
            {"base_revision": True, "settings": {}},
            {"base_revision": "1", "settings": {}},
            {"base_revision": 1, "settings": []},
            {"base_revision": 1},
        ):
            status, body = _post(app, PATH, bad)
            assert status == 400 and body["ok"] is False
        assert runner.sent == []
        assert _post(app, "/bridge-settings/ghost", BODY)[0] == 404
    finally:
        app.close()


def test_mock_source_has_no_route():
    from herdeck.deckapp import MockSource

    app = DeckApp(MockSource(), host="127.0.0.1", port=0, serve=True, icon_provider=StubIcons())
    try:
        assert _post(app, PATH, BODY)[0] == 404
    finally:
        app.close()


def _get_config(app):
    url = f"http://{app.host}:{app.port}/config?token={app.token}"
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


def test_get_config_carries_bridges_per_configured_server(tmp_path):
    app, src, _ = _serve(tmp_path=tmp_path)
    try:
        bridges = _get_config(app)["bridges"]
        assert set(bridges) == {"prod"}
        assert bridges["prod"]["offered"] is True and bridges["prod"]["connected"] is True
        assert {"revision", "updated_at_ms", "updated_by", "set", "source", "settings"} <= set(
            bridges["prod"]
        )
    finally:
        app.close()


def test_get_config_bridges_empty_without_a_live_source(tmp_path):
    from herdeck.deckapp import MockSource

    app = DeckApp(
        MockSource(), host="127.0.0.1", port=0, serve=True, icon_provider=StubIcons(),
        config_service=ConfigService(tmp_path / "config.toml", tmp_path / "local.toml"),
    )
    try:
        assert _get_config(app)["bridges"] == {}
    finally:
        app.close()


def test_concurrent_stats_and_settings_replies_reach_their_own_relay():
    import threading
    import time

    src, runner = make(capabilities=("settings", "history"))
    out: dict = {}
    t_stats = threading.Thread(
        target=lambda: out.__setitem__("stats", src.stats(7, "day", wait_s=3))
    )
    t_stats.start()
    while not any(m["type"] == "stats" for m in runner.sent):
        time.sleep(0.005)
    stats_req = next(m["req"] for m in runner.sent if m["type"] == "stats")
    t_put = threading.Thread(
        target=lambda: out.__setitem__("put", src.bridge_settings_put("prod", 0, {}, wait_s=3))
    )
    t_put.start()
    while not any(m["type"] == "settings_put" for m in runner.sent):
        time.sleep(0.005)
    put_req = next(m["req"] for m in runner.sent if m["type"] == "settings_put")
    assert put_req != stats_req
    src._on_result("prod", put_req, {"ok": True, "revision": 9})
    t_put.join(3)
    assert out["put"] == (200, {"ok": True, "revision": 9})
    src._on_bridge_error("prod", stats_req, "boom")
    t_stats.join(3)
    assert out["stats"]["ok"] is False
