import asyncio

import pytest

from herdeck.app import _discover_config_path, local_config, make_deck, resolve_mode
from herdeck.bridge import StubHerdr, start_local_bridge
from herdeck.config import AnswerProfile, Config, ServerConfig
from herdeck.connector import Connector
from herdeck.driver.fake import FakeRenderer

SOCK = "/Users/x/.config/herdr/herdr.sock"


def test_mock_wins():
    assert resolve_mode(mock=True, config_path="/c", config_has_servers=True,
                        socket_path=SOCK, socket_exists=True) == ("mock",)


def test_config_with_servers_is_remote():
    assert resolve_mode(mock=False, config_path="/c", config_has_servers=True,
                        socket_path=SOCK, socket_exists=True) == ("remote", "/c")


def test_socket_without_servers_is_local():
    assert resolve_mode(mock=False, config_path=None, config_has_servers=False,
                        socket_path=SOCK, socket_exists=True) == ("local", SOCK)


def test_serverless_config_plus_socket_is_local():
    assert resolve_mode(mock=False, config_path="/c", config_has_servers=False,
                        socket_path=SOCK, socket_exists=True) == ("local", SOCK)


def test_no_socket_no_servers_is_error():
    mode = resolve_mode(mock=False, config_path=None, config_has_servers=False,
                        socket_path=SOCK, socket_exists=False)
    assert mode[0] == "error" and SOCK in mode[1]


class _Web:
    def __init__(self):
        self.kind = "web"


class _Elgato:
    def __init__(self):
        self.kind = "elgato"


def _boom():
    raise RuntimeError("no device")


def test_auto_falls_back_to_web_when_d200_unavailable():
    deck = make_deck(None, 13, d200_factory=_boom, elgato_factory=_boom,
                     web_factory=_Web)
    assert isinstance(deck, _Web)


def test_explicit_d200_failure_propagates():
    with pytest.raises(RuntimeError):
        make_deck("d200", 13, d200_factory=_boom, web_factory=_Web)


def test_explicit_elgato_kind_uses_factory():
    deck = make_deck("elgato", 13, d200_factory=_boom, elgato_factory=_Elgato,
                     web_factory=_Web)
    assert isinstance(deck, _Elgato)


def test_auto_tries_elgato_after_d200_and_before_web():
    deck = make_deck(None, 13, d200_factory=_boom, elgato_factory=_Elgato,
                     web_factory=_Web)
    assert isinstance(deck, _Elgato)


def test_explicit_elgato_failure_propagates():
    with pytest.raises(RuntimeError):
        make_deck("elgato", 13, elgato_factory=_boom, web_factory=_Web)


def test_fake_kind_returns_fake_renderer():
    deck = make_deck("fake", 13, d200_factory=_boom, web_factory=_Web)
    assert isinstance(deck, FakeRenderer)


def test_unknown_explicit_deck_kind_raises():
    with pytest.raises(ValueError, match="unsupported deck kind"):
        make_deck("dw00", 13, d200_factory=_boom, web_factory=_Web)


def test_default_web_deck_prints_tokenized_url(monkeypatch, capsys):
    monkeypatch.setenv("HERDECK_WEB_PORT", "0")
    deck = make_deck("web", 4)
    try:
        out = capsys.readouterr().out
        assert "/?token=" in out
        assert deck.press_token in out
    finally:
        deck.close()


async def test_start_local_bridge_serves_snapshot_to_connector():
    herdr = StubHerdr([
        {"pane_id": "p1", "agent": "claude", "agent_status": "working",
         "foreground_cwd": "/proj/api", "workspace_id": "w1"},
    ], worktrees=[
        {"open_workspace_id": "w1", "label": "herdeck", "branch": "feat/clawpatch"},
    ])
    host, port, token, (server, btask) = await start_local_bridge(
        "/nonexistent.sock", herdr=herdr)
    got = asyncio.Event()
    seen = []
    conn = Connector(
        ServerConfig("local", f"ws://{host}:{port}", token),
        on_snapshot=lambda sid, st: (seen.extend(st), got.set()),
        on_event=lambda sid, s: None,
        on_connection=lambda sid, up: None,
    )
    run = asyncio.create_task(conn.run())
    try:
        await asyncio.wait_for(got.wait(), timeout=5)
        assert seen[0].agent_type == "claude"
        assert seen[0].label == "api"
        assert seen[0].repo == "herdeck"
        assert seen[0].branch == "feat/clawpatch"
    finally:
        conn.stop()
        btask.cancel()
        server.close()
        await server.wait_closed()
        run.cancel()


def test_local_config_defaults():
    cfg = local_config(9999, "tok")
    assert cfg.servers[0].id == "local"
    assert cfg.servers[0].url == "ws://127.0.0.1:9999"
    assert cfg.servers[0].token == "tok"
    assert cfg.overview_order == ["local"]
    assert cfg.profiles["default"].approve == ["enter"]


def test_local_config_merges_partial_profiles():
    partial = Config(
        servers=[],
        profiles={"claude": AnswerProfile(["x"], ["y"], ["z"], ["x"])},
        overview_order=[],
        grid=(5, 3),
    )
    cfg = local_config(1, "t", partial)
    assert cfg.profiles["claude"].approve == ["x"]
    assert cfg.profiles["default"].approve == ["enter"]


def test_local_config_preserves_notifications():
    partial = Config(
        servers=[],
        profiles={},
        overview_order=[],
        grid=(5, 3),
    )
    partial.notifications.enabled = True
    partial.notifications.sound = False
    cfg = local_config(1, "t", partial)
    assert cfg.notifications.enabled is True
    assert cfg.notifications.sound is False


def test_discover_prefers_env(monkeypatch, tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("")
    monkeypatch.setenv("HERDECK_CONFIG", str(p))
    assert _discover_config_path() == str(p)


def test_discover_none_when_nothing(monkeypatch, tmp_path):
    monkeypatch.delenv("HERDECK_CONFIG", raising=False)
    monkeypatch.setattr("os.path.exists", lambda p: False)
    assert _discover_config_path() is None
