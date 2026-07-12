import asyncio

import pytest

from herdeck.app import _discover_config_path, local_config, make_deck, resolve_mode
from herdeck.bridge import StubHerdr, start_local_bridge
from herdeck.config import AnswerProfile, Config, ServerConfig
from herdeck.connector import Connector
from herdeck.driver.fake import FakeRenderer

SOCK = "/Users/x/.config/herdr/herdr.sock"


def test_mock_wins():
    assert resolve_mode(
        mock=True, config_path="/c", config_has_servers=True, socket_path=SOCK, socket_exists=True
    ) == ("mock",)


def test_config_with_servers_is_remote():
    assert resolve_mode(
        mock=False, config_path="/c", config_has_servers=True, socket_path=SOCK, socket_exists=True
    ) == ("remote", "/c")


def test_socket_without_servers_is_local():
    assert resolve_mode(
        mock=False, config_path=None, config_has_servers=False, socket_path=SOCK, socket_exists=True
    ) == ("local", SOCK)


def test_serverless_config_plus_socket_is_local():
    assert resolve_mode(
        mock=False, config_path="/c", config_has_servers=False, socket_path=SOCK, socket_exists=True
    ) == ("local", SOCK)


def test_no_socket_no_servers_is_error():
    mode = resolve_mode(
        mock=False,
        config_path=None,
        config_has_servers=False,
        socket_path=SOCK,
        socket_exists=False,
    )
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
    deck = make_deck(None, 13, d200_factory=_boom, elgato_factory=_boom, web_factory=_Web)
    assert isinstance(deck, _Web)


def test_explicit_d200_failure_propagates():
    with pytest.raises(RuntimeError):
        make_deck("d200", 13, d200_factory=_boom, web_factory=_Web)


def test_explicit_elgato_kind_uses_factory():
    deck = make_deck("elgato", 13, d200_factory=_boom, elgato_factory=_Elgato, web_factory=_Web)
    assert isinstance(deck, _Elgato)


def test_auto_tries_elgato_after_d200_and_before_web():
    deck = make_deck(None, 13, d200_factory=_boom, elgato_factory=_Elgato, web_factory=_Web)
    assert isinstance(deck, _Elgato)


def test_explicit_elgato_failure_propagates():
    with pytest.raises(RuntimeError):
        make_deck("elgato", 13, elgato_factory=_boom, web_factory=_Web)


def test_fake_kind_returns_fake_renderer():
    deck = make_deck("fake", 13, d200_factory=_boom, web_factory=_Web)
    assert isinstance(deck, FakeRenderer)


def test_fake_deck_ignores_invalid_web_port(monkeypatch):
    monkeypatch.setenv("HERDECK_WEB_PORT", "not-a-port")

    deck = make_deck("fake", 13, d200_factory=_boom, web_factory=_Web)

    assert isinstance(deck, FakeRenderer)


def test_make_deck_uses_hardware_web_bind_and_port(monkeypatch):
    from herdeck.config import HardwareConfig

    seen = {}
    monkeypatch.delenv("HERDECK_WEB_BIND", raising=False)
    monkeypatch.delenv("HERDECK_WEB_PORT", raising=False)

    def web_factory(host=None, port=None):
        seen["host"] = host
        seen["port"] = port
        return _Web()

    hw = HardwareConfig(web_bind="100.65.2.3", web_port=1234)
    make_deck("web", 13, web_factory=web_factory, hardware=hw)

    assert seen == {"host": "100.65.2.3", "port": 1234}


def test_web_bind_rejects_wildcard_public_and_lan_without_explicit_override(monkeypatch):
    from herdeck.app import validate_web_bind

    for host in ("0.0.0.0", "::", "8.8.8.8", "192.168.1.10"):
        with pytest.raises(ValueError, match="loopback or a Tailscale"):
            validate_web_bind(host)

    assert validate_web_bind("127.0.0.1") == "127.0.0.1"
    assert validate_web_bind("100.86.178.12") == "100.86.178.12"
    assert validate_web_bind("mac-mini.tail123.ts.net") == "mac-mini.tail123.ts.net"

    monkeypatch.setenv("HERDECK_ALLOW_UNSAFE_BIND", "1")
    assert validate_web_bind("0.0.0.0") == "0.0.0.0"


def test_make_deck_preserves_hardware_web_port_zero(monkeypatch):
    from herdeck.config import HardwareConfig

    seen = {}
    monkeypatch.delenv("HERDECK_WEB_PORT", raising=False)

    def web_factory(host=None, port=None):
        seen["port"] = port
        return _Web()

    make_deck("web", 13, web_factory=web_factory, hardware=HardwareConfig(web_port=0))

    assert seen == {"port": 0}


def test_make_deck_wires_icons_dir_to_web_driver(monkeypatch):
    from herdeck.config import HardwareConfig

    seen = {}

    class WebDeck:
        def __init__(self, slots, host=None, port=None, icons_dir=None):
            seen["slots"] = slots
            seen["host"] = host
            seen["port"] = port
            seen["icons_dir"] = icons_dir
            self.host = host
            self.port = port
            self.press_token = "token"

    monkeypatch.setattr("herdeck.driver.web.WebDeck", WebDeck)

    make_deck("web", 13, hardware=HardwareConfig(icons_dir="~/herdeck-icons"))

    assert seen == {
        "slots": 13,
        "host": "127.0.0.1",
        "port": 8800,
        "icons_dir": "~/herdeck-icons",
    }


def test_make_deck_wires_icons_dir_to_hardware_drivers(monkeypatch):
    from herdeck.config import HardwareConfig

    seen = {}

    class D200Driver:
        def __init__(self, *, icons_dir=None, **kwargs):
            seen["d200"] = icons_dir

    class ElgatoDriver:
        def __init__(self, *, icons_dir=None, **kwargs):
            seen["elgato"] = icons_dir

    monkeypatch.setattr("herdeck.driver.d200.D200Driver", D200Driver)
    monkeypatch.setattr("herdeck.driver.elgato.ElgatoDriver", ElgatoDriver)
    hw = HardwareConfig(icons_dir="~/herdeck-icons")

    make_deck("d200", 13, hardware=hw)
    make_deck("elgato", 13, hardware=hw)

    assert seen == {"d200": "~/herdeck-icons", "elgato": "~/herdeck-icons"}


def test_make_deck_prefers_env_web_bind_and_port(monkeypatch):
    from herdeck.config import HardwareConfig

    seen = {}

    def web_factory(host=None, port=None):
        seen["host"] = host
        seen["port"] = port
        return _Web()

    monkeypatch.setenv("HERDECK_WEB_BIND", "127.9.9.9")
    monkeypatch.setenv("HERDECK_WEB_PORT", "9911")

    hw = HardwareConfig(web_bind="100.1.2.3", web_port=1234)
    make_deck("web", 13, web_factory=web_factory, hardware=hw)

    assert seen == {"host": "127.9.9.9", "port": 9911}


def test_make_deck_wires_reverse_proxy_environment(monkeypatch):
    seen = {}

    def web_factory(**kwargs):
        seen.update(kwargs)
        return _Web()

    monkeypatch.setenv("HERDECK_WEB_BASE_PATH", "/cockpit/herdeck")
    monkeypatch.setenv("HERDECK_WEB_PUBLIC_ORIGIN", "https://cockpit.example")
    monkeypatch.setenv(
        "HERDECK_WEB_FRAME_ANCESTORS",
        "https://cockpit.example, https://admin.example",
    )

    make_deck("web", 13, web_factory=web_factory)

    assert seen["base_path"] == "/cockpit/herdeck"
    assert seen["public_origin"] == "https://cockpit.example"
    assert seen["frame_ancestors"] == (
        "https://cockpit.example",
        "https://admin.example",
    )


def test_runtime_startup_settings_prefer_env_over_local(monkeypatch):
    from herdeck.app import _resolve_deck_kind, _resolve_socket_path, _resolve_tick_interval
    from herdeck.config import Config, HardwareConfig

    cfg = Config(servers=[], profiles={}, overview_order=[], grid=(5, 3))
    cfg.hardware = HardwareConfig(deck="web", herdr_socket="/local.sock", tick_interval=1.25)

    monkeypatch.setenv("HERDECK_DECK", "fake")
    monkeypatch.setenv("HERDR_SOCKET", "/env.sock")

    assert _resolve_deck_kind(cfg) == "fake"
    assert _resolve_socket_path(cfg) == "/env.sock"
    assert _resolve_tick_interval(cfg) == 1.25


def test_runtime_startup_settings_use_local_when_env_absent(monkeypatch):
    from herdeck.app import _resolve_deck_kind, _resolve_socket_path, _resolve_tick_interval
    from herdeck.config import Config, HardwareConfig

    cfg = Config(servers=[], profiles={}, overview_order=[], grid=(5, 3))
    cfg.hardware = HardwareConfig(deck="web", herdr_socket="/local.sock", tick_interval=1.25)

    monkeypatch.delenv("HERDECK_DECK", raising=False)
    monkeypatch.delenv("HERDECK_FAKE_DECK", raising=False)
    monkeypatch.delenv("HERDR_SOCKET", raising=False)
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    monkeypatch.delenv("HERDR_SESSION", raising=False)

    assert _resolve_deck_kind(cfg) == "web"
    assert _resolve_socket_path(cfg) == "/local.sock"
    assert _resolve_tick_interval(cfg) == 1.25


def test_unknown_explicit_deck_kind_raises():
    with pytest.raises(ValueError, match="unsupported deck kind"):
        make_deck("dw00", 13, d200_factory=_boom, web_factory=_Web)


def test_default_web_deck_redacts_capability_url_from_logs(monkeypatch, capsys):
    monkeypatch.setenv("HERDECK_WEB_PORT", "0")
    monkeypatch.delenv("HERDECK_SHOW_URL_TOKEN", raising=False)
    deck = make_deck("web", 4)
    try:
        out = capsys.readouterr().out
        assert "herdeck-web url" in out
        assert "/?token=" not in out
        assert deck.press_token not in out
    finally:
        deck.close()


def test_web_deck_can_explicitly_print_capability_url(monkeypatch, capsys):
    monkeypatch.setenv("HERDECK_WEB_PORT", "0")
    monkeypatch.setenv("HERDECK_SHOW_URL_TOKEN", "1")
    deck = make_deck("web", 4)
    try:
        out = capsys.readouterr().out
        assert "/?token=" in out
        assert deck.press_token in out
    finally:
        deck.close()


async def test_start_local_bridge_serves_snapshot_to_connector():
    herdr = StubHerdr(
        [
            {
                "pane_id": "p1",
                "agent": "claude",
                "agent_status": "working",
                "foreground_cwd": "/proj/api",
                "workspace_id": "w1",
            },
        ],
        worktrees=[
            {"open_workspace_id": "w1", "label": "herdeck", "branch": "feat/clawpatch"},
        ],
    )
    host, port, token, (server, btask) = await start_local_bridge("/nonexistent.sock", herdr=herdr)
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


def test_simulator_urls_expand_wildcard_binds(monkeypatch):
    """http://0.0.0.0:8800 is literally unroutable; a wildcard bind announces
    the Tailscale + default-route addresses instead (audit: websim-url-announce)."""
    from herdeck import app as app_mod

    def fake_iface(probe):
        return {"100.100.100.100": "100.64.1.2", "1.1.1.1": "192.168.1.5"}[probe]

    monkeypatch.setattr(app_mod, "_iface_addr", fake_iface)
    urls = app_mod._simulator_urls("0.0.0.0", 8800, "tok")
    assert urls[0] == "http://100.64.1.2:8800/?token=tok"  # Tailscale first
    assert "http://192.168.1.5:8800/?token=tok" in urls
    assert urls[-1] == "http://127.0.0.1:8800/?token=tok"
    # explicit binds announce exactly what was bound
    assert app_mod._simulator_urls("100.99.1.4", 8800, "t") == [
        "http://100.99.1.4:8800/?token=t"
    ]
