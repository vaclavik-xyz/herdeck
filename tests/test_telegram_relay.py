"""Editor API for a bridge's Telegram config: GET /config bridges[id].telegram
and POST /bridge-telegram/<id>[/token|/test]. Never a real token."""

import json
import logging
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest
from test_deckapp_live import StubIcons

from herdeck import secrets as herdeck_secrets
from herdeck.config import DEFAULT_PROFILES, Config, ServerConfig, TelegramConfig
from herdeck.deckapp import DeckApp
from herdeck.deckapp import settings_relay as sr
from herdeck.deckapp.config_service import ConfigService
from herdeck.deckapp.live import LiveSource
from herdeck.protocol import TelegramFrame

FAKE_TOKEN = "123456789:" + "A" * 35  # obviously fake, matches the token regex
LOCAL_TOKEN = "987654321:" + "B" * 35
PATH = "/bridge-telegram/prod"
PUT = {"base_revision": 2, "settings": {"enabled": True, "chat_id": "-100"}}
CAPS = ("settings", "telegram_config")
SENT = ("telegram_put", "telegram_token", "telegram_test")


class Runner:
    def __init__(self, reply=None, capabilities=CAPS):
        self.sent: list[dict] = []
        self.reply = reply
        self.src = None
        self.connector = SimpleNamespace(
            protocol=3, capabilities=frozenset(capabilities), health=lambda: {}
        )

    def send(self, msg):
        self.sent.append(msg)
        if self.reply is not None and msg.get("type") in SENT:
            self.reply(self, msg)

    def close(self):
        pass


def result(data):
    return lambda r, m: r.src._on_result("prod", m["req"], data)


def refuse(message):
    return lambda r, m: r.src._on_bridge_error("prod", m["req"], message)


def make(reply=None, *, connected=True, local_env=None, **kw):
    server = ServerConfig(id="prod", url="ws://bridge.local:8765", token="t")
    config = Config(
        servers=[server], profiles=dict(DEFAULT_PROFILES), overview_order=["prod"], grid=(5, 3)
    )
    if local_env is not None:
        config.notifications.telegram = TelegramConfig(token_env=local_env, chat_id="1")
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


def _get_config(app):
    url = f"http://{app.host}:{app.port}/config?token={app.token}"
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


def test_route_parsing():
    assert sr.telegram_route("/bridge-telegram/prod") == ("prod", "")
    assert sr.telegram_route("/bridge-telegram/a%20b/token") == ("a b", "token")
    assert sr.telegram_route("/bridge-telegram/prod/test") == ("prod", "test")
    assert sr.telegram_route("/bridge-telegram/") is None
    assert sr.telegram_route("/bridge-telegram/prod/other") is None
    assert sr.telegram_route("/bridge-settings/prod") is None


# --- /config ---------------------------------------------------------------


def test_get_config_exposes_telegram_without_a_token(tmp_path):
    app, src, _ = _serve(tmp_path=tmp_path)
    try:
        src._on_telegram(
            "prod",
            TelegramFrame(
                "prod", 5, 1000, "me", {"enabled": True, "chat_id": "-100"},
                {"token": "file", "active": True},
            ),
        )
        raw = json.dumps(_get_config(app))
        tg = json.loads(raw)["bridges"]["prod"]["telegram"]
        assert set(tg) == {"offered", "revision", "settings", "status"}
        assert tg["offered"] is True and tg["revision"] == 5
        assert tg["settings"] == {"enabled": True, "chat_id": "-100"}
        assert tg["status"] == {"token": "file", "active": True}
        assert FAKE_TOKEN not in raw
    finally:
        app.close()


def test_get_config_telegram_not_offered_by_an_old_bridge(tmp_path):
    app, src, _ = _serve(tmp_path=tmp_path, capabilities=("settings",))
    try:
        tg = _get_config(app)["bridges"]["prod"]["telegram"]
        assert tg == {"offered": False, "revision": None, "settings": None, "status": None}
    finally:
        app.close()


# --- put -------------------------------------------------------------------


def test_put_relays_the_frame_and_maps_ok():
    src, runner = make(result({"ok": True, "revision": 3}))
    assert src.bridge_telegram_put("prod", 2, PUT["settings"], wait_s=1) == (
        200,
        {"ok": True, "revision": 3},
    )
    assert runner.sent == [
        {"type": "telegram_put", "req": "sp1", "base_revision": 2, "settings": PUT["settings"]}
    ]


@pytest.mark.parametrize(
    ("error", "code"), [("stale_revision", 409), ("invalid", 422), ("too_large", 422)]
)
def test_put_refusals(error, code):
    src, _ = make(result({"ok": False, "error": error, "messages": ["m"], "revision": 7}))
    assert src.bridge_telegram_put("prod", 2, {}, wait_s=1) == (
        code,
        {"ok": False, "error": error, "messages": ["m"], "revision": 7},
    )


def test_put_503_504_and_unknown():
    src, runner = make(result({"ok": True}), connected=False)
    assert src.bridge_telegram_put("prod", 0, {}, wait_s=1)[0] == 503
    src, runner = make(result({"ok": True}), capabilities=("settings",))
    code, payload = src.bridge_telegram_put("prod", 0, {}, wait_s=1)
    assert code == 503 and payload["error"] == "unsupported" and runner.sent == []
    src, _ = make()
    assert src.bridge_telegram_put("prod", 0, {}, wait_s=0.05)[0] == 504
    assert src.bridge_telegram_put("ghost", 0, {}, wait_s=1) is None


def test_request_ids_are_unique_across_settings_and_telegram():
    src, runner = make(result({"ok": True, "revision": 1}))
    src.bridge_telegram_put("prod", 0, {}, wait_s=1)
    src.bridge_telegram_test("prod", wait_s=1)
    src.bridge_telegram_token("prod", "clear", wait_s=1)
    src.bridge_settings_put("prod", 0, {}, wait_s=1)
    reqs = [m["req"] for m in runner.sent]
    assert len(reqs) == 4 and len(set(reqs)) == 4


def test_late_telegram_reply_never_reaches_the_deck():
    src, _ = make()
    assert src.bridge_telegram_test("prod", wait_s=0.05)[0] == 504
    seen = []
    src.set_result_tap(lambda sid, req, data: seen.append(req))
    src._on_result("prod", "sp1", {"ok": True})
    assert seen == []


# --- token -----------------------------------------------------------------


def test_token_set_relays_and_returns_no_token():
    src, runner = make(result({"ok": True}))
    code, payload = src.bridge_telegram_token("prod", "set", FAKE_TOKEN, wait_s=1)
    assert (code, payload) == (200, {"ok": True})
    assert runner.sent == [
        {"type": "telegram_token", "req": "sp1", "action": "set", "token": FAKE_TOKEN}
    ]
    assert FAKE_TOKEN not in json.dumps(payload)


def test_token_clear_sends_no_token_field():
    src, runner = make(result({"ok": True}))
    assert src.bridge_telegram_token("prod", "clear", wait_s=1) == (200, {"ok": True})
    assert runner.sent == [{"type": "telegram_token", "req": "sp1", "action": "clear"}]


@pytest.mark.parametrize("error", ["invalid", "env_locked", "io_error"])
def test_token_refusals_are_422(error):
    src, _ = make(result({"ok": False, "error": error}))
    assert src.bridge_telegram_token("prod", "set", FAKE_TOKEN, wait_s=1) == (
        422,
        {"ok": False, "error": error},
    )


def test_token_error_frame_never_forwards_the_bridge_text():
    src, _ = make(refuse(f"boom {FAKE_TOKEN}"))
    code, payload = src.bridge_telegram_token("prod", "set", FAKE_TOKEN, wait_s=1)
    assert code == 502 and FAKE_TOKEN not in json.dumps(payload)


def test_token_503_504():
    src, _ = make(connected=False)
    assert src.bridge_telegram_token("prod", "set", FAKE_TOKEN, wait_s=1)[0] == 503
    src, _ = make()
    assert src.bridge_telegram_token("prod", "set", FAKE_TOKEN, wait_s=0.05)[0] == 504


def test_from_local_resolves_the_runtime_token_server_side(monkeypatch, caplog):
    monkeypatch.setenv("HERDECK_TEST_TG", LOCAL_TOKEN)
    src, runner = make(result({"ok": True}), local_env="HERDECK_TEST_TG")
    with caplog.at_level(logging.DEBUG):
        code, payload = src.bridge_telegram_token("prod", "set", from_local=True, wait_s=1)
    assert (code, payload) == (200, {"ok": True})
    assert runner.sent[0]["token"] == LOCAL_TOKEN and runner.sent[0]["action"] == "set"
    assert LOCAL_TOKEN not in json.dumps(payload) and LOCAL_TOKEN not in caplog.text


def test_from_local_uses_the_keychain_fallback(monkeypatch):
    monkeypatch.delenv("HERDECK_TEST_TG", raising=False)
    monkeypatch.setattr(herdeck_secrets, "_keyring", lambda: SimpleNamespace(
        get_password=lambda service, name: LOCAL_TOKEN if name == "HERDECK_TEST_TG" else None
    ))
    src, runner = make(result({"ok": True}), local_env="HERDECK_TEST_TG")
    assert src.bridge_telegram_token("prod", "set", from_local=True, wait_s=1)[0] == 200
    assert runner.sent[0]["token"] == LOCAL_TOKEN


def test_from_local_without_a_token_is_422_and_sends_nothing(monkeypatch):
    monkeypatch.delenv("HERDECK_TEST_TG", raising=False)
    monkeypatch.setattr(herdeck_secrets, "_keyring", lambda: SimpleNamespace(
        get_password=lambda service, name: None
    ))
    for env in ("HERDECK_TEST_TG", None):
        src, runner = make(result({"ok": True}), local_env=env)
        code, payload = src.bridge_telegram_token("prod", "set", from_local=True, wait_s=1)
        assert code == 422 and payload["error"] == "no_local_token" and runner.sent == []


# --- test ------------------------------------------------------------------


def test_test_relays_ok_and_error_with_200():
    src, runner = make(result({"ok": True}))
    assert src.bridge_telegram_test("prod", wait_s=1) == (200, {"ok": True})
    assert runner.sent == [{"type": "telegram_test", "req": "sp1"}]
    src, _ = make(result({"ok": False, "error": "chat not found"}))
    assert src.bridge_telegram_test("prod", wait_s=1) == (
        200,
        {"ok": False, "error": "chat not found"},
    )
    src, _ = make(connected=False)
    assert src.bridge_telegram_test("prod", wait_s=1)[0] == 503
    src, _ = make()
    assert src.bridge_telegram_test("prod", wait_s=0.05)[0] == 504


# --- HTTP ------------------------------------------------------------------


def test_http_routes_round_trip_and_stale():
    replies = iter(
        [
            {"ok": True, "revision": 3},
            {"ok": False, "error": "stale_revision", "messages": [], "revision": 3},
            {"ok": True},
            {"ok": False, "error": "env_locked"},
            {"ok": True},
        ]
    )
    app, src, runner = _serve(lambda r, m: r.src._on_result("prod", m["req"], next(replies)))
    try:
        assert _post(app, PATH, PUT) == (200, {"ok": True, "revision": 3})
        status, body = _post(app, PATH, PUT)
        assert status == 409 and body["error"] == "stale_revision"
        assert _post(app, PATH + "/token", {"action": "set", "token": FAKE_TOKEN}) == (
            200,
            {"ok": True},
        )
        assert _post(app, PATH + "/token", {"action": "clear"}) == (
            422,
            {"ok": False, "error": "env_locked"},
        )
        assert _post(app, PATH + "/test", {}) == (200, {"ok": True})
    finally:
        app.close()


def test_http_from_local_never_returns_the_token(monkeypatch):
    monkeypatch.setenv("HERDECK_TEST_TG", LOCAL_TOKEN)
    app, src, runner = _serve(result({"ok": True}), local_env="HERDECK_TEST_TG")
    try:
        status, body = _post(app, PATH + "/token", {"action": "set", "from_local": True})
        assert (status, body) == (200, {"ok": True})
        assert runner.sent[0]["token"] == LOCAL_TOKEN
    finally:
        app.close()
    monkeypatch.delenv("HERDECK_TEST_TG")
    monkeypatch.setattr(herdeck_secrets, "_keyring", lambda: SimpleNamespace(
        get_password=lambda service, name: None
    ))
    app, src, runner = _serve(result({"ok": True}), local_env="HERDECK_TEST_TG")
    try:
        status, body = _post(app, PATH + "/token", {"action": "set", "from_local": True})
        assert status == 422 and body["error"] == "no_local_token"
    finally:
        app.close()


def test_http_auth_validation_and_unknown_server(caplog):
    app, src, runner = _serve(result({"ok": True, "revision": 1}))
    try:
        for path, body in (
            (PATH, PUT),
            (PATH + "/token", {"action": "set", "token": FAKE_TOKEN}),
            (PATH + "/test", {}),
        ):
            assert _post(app, path, body, token="wrong")[0] == 403
        bad_put = [{}, {"base_revision": True, "settings": {}}, {"base_revision": 1}]
        for bad in bad_put:
            assert _post(app, PATH, bad)[0] == 400
        bad_tokens = [
            {},
            {"action": "wipe"},
            {"action": "set"},
            {"action": "set", "token": 5},
            {"action": "set", "token": FAKE_TOKEN, "from_local": True},
            {"action": "set", "from_local": False},
            {"action": "clear", "token": FAKE_TOKEN},
        ]
        with caplog.at_level(logging.DEBUG):
            for bad in bad_tokens:
                status, body = _post(app, PATH + "/token", bad)
                assert status == 400 and FAKE_TOKEN not in json.dumps(body)
        assert FAKE_TOKEN not in caplog.text
        assert runner.sent == []
        assert _post(app, "/bridge-telegram/ghost", PUT)[0] == 404
        assert _post(app, "/bridge-telegram/ghost/token", {"action": "clear"})[0] == 404
        assert _post(app, "/bridge-telegram/ghost/test", {})[0] == 404
    finally:
        app.close()


def test_mock_source_has_no_route():
    from herdeck.deckapp import MockSource

    app = DeckApp(MockSource(), host="127.0.0.1", port=0, serve=True, icon_provider=StubIcons())
    try:
        for suffix in ("", "/token", "/test"):
            assert _post(app, PATH + suffix, {"action": "clear", **PUT})[0] == 404
    finally:
        app.close()


def test_test_waits_longer_than_the_bots_api_call():
    """A slow sendMessage (up to the Bot API timeout) must not read as a relay
    timeout while the message still arrives."""
    import inspect

    from herdeck import telegram

    assert sr.TELEGRAM_TEST_WAIT_S > telegram.BOT_API_TIMEOUT_S
    default = inspect.signature(LiveSource.bridge_telegram_test).parameters["wait_s"].default
    assert default == sr.TELEGRAM_TEST_WAIT_S
