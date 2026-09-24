import asyncio

import pytest

from herdeck.bootstrap import _discover_config_path, local_config, resolve_mode
from herdeck.bridge import StubHerdr, start_local_bridge
from herdeck.config import AnswerProfile, Config, HardwareConfig, ServerConfig
from herdeck.connector import Connector
from herdeck.driver.fake import FakeRenderer
from herdeck.host import build_web_deck, open_front

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


class _Lock:
    def __init__(self, free=True):
        self.free = free
        self.held = False

    def acquire(self):
        self.held = self.free
        return self.free

    def release(self):
        self.held = False

    def owner_pid(self):
        return None if self.free else 4242


def _boom():
    raise RuntimeError("no device")


def _front(kind, **kwargs):
    kwargs.setdefault("lock_factory", _Lock)
    return open_front(kind, 13, **kwargs)


def test_auto_falls_back_to_web_when_d200_unavailable():
    lock = _Lock()
    front = _front(
        None, d200_factory=_boom, elgato_factory=_boom, web_factory=_Web, lock_factory=lambda: lock
    )
    assert front.kind == "web" and isinstance(front.deck, _Web)
    assert lock.held is False  # a failed probe releases the D200 lock


def test_auto_keeps_a_probed_d200_and_its_lock():
    lock = _Lock()
    driver = object()
    front = _front(None, d200_factory=lambda: driver, web_factory=_Web, lock_factory=lambda: lock)
    assert front.kind == "d200"
    assert front.d200_driver is driver and front.d200_lock is lock and lock.held


def test_auto_skips_a_d200_owned_by_another_runtime(capsys):
    opened = []
    front = _front(
        None,
        d200_factory=lambda: opened.append(1),
        elgato_factory=_boom,
        web_factory=_Web,
        lock_factory=lambda: _Lock(free=False),
    )
    assert front.kind == "web" and opened == []
    assert "owned by another herdeck runtime, pid 4242" in capsys.readouterr().out


def test_explicit_d200_is_opened_by_the_runtime_sink_under_the_lock():
    # The reconnecting sink opens (and re-opens) the device while holding
    # d200.lock, so an explicit D200 is never opened here.
    front = _front("d200", d200_factory=_boom, web_factory=_Web)
    assert front.kind == "d200" and front.d200_driver is None and front.d200_lock is None


def test_explicit_elgato_kind_uses_factory():
    front = _front("elgato", d200_factory=_boom, elgato_factory=_Elgato, web_factory=_Web)
    assert isinstance(front.deck, _Elgato)


def test_auto_tries_elgato_after_d200_and_before_web():
    front = _front(None, d200_factory=_boom, elgato_factory=_Elgato, web_factory=_Web)
    assert isinstance(front.deck, _Elgato)


def test_explicit_elgato_failure_propagates():
    with pytest.raises(RuntimeError):
        _front("elgato", elgato_factory=_boom, web_factory=_Web)


def test_fake_kind_returns_fake_renderer():
    front = _front("fake", d200_factory=_boom, web_factory=_Web)
    assert isinstance(front.deck, FakeRenderer)


def test_fake_deck_ignores_invalid_web_port(monkeypatch):
    monkeypatch.setenv("HERDECK_WEB_PORT", "not-a-port")

    front = _front("fake", d200_factory=_boom, web_factory=_Web)

    assert isinstance(front.deck, FakeRenderer)


class _RecordingWebDeck:
    seen: dict = {}

    def __init__(self, slots, **kwargs):
        type(self).seen = {"slots": slots, **kwargs}
        self.host = kwargs["host"]
        self.port = kwargs["port"]
        self.press_token = "token"
        self._allow_query_token = False
        self._base_path = kwargs.get("base_path", "")
        self._public_origin = kwargs.get("public_origin", "")


@pytest.fixture
def web_deck(monkeypatch):
    monkeypatch.setattr("herdeck.driver.web.WebDeck", _RecordingWebDeck)
    for name in (
        "HERDECK_WEB_BIND",
        "HERDECK_WEB_PORT",
        "HERDECK_WEB_BASE_PATH",
        "HERDECK_WEB_PUBLIC_ORIGIN",
        "HERDECK_WEB_FRAME_ANCESTORS",
    ):
        monkeypatch.delenv(name, raising=False)
    return _RecordingWebDeck


def test_web_deck_uses_hardware_web_bind_and_port(web_deck):
    hw = HardwareConfig(web_bind="100.65.2.3", web_port=1234)
    build_web_deck(13, hardware=hw, cols=5, language="en")
    assert (web_deck.seen["host"], web_deck.seen["port"]) == ("100.65.2.3", 1234)


def test_web_bind_rejects_wildcard_public_and_lan_without_explicit_override(monkeypatch):
    from herdeck.host import validate_web_bind

    for host in ("0.0.0.0", "::", "8.8.8.8", "192.168.1.10"):
        with pytest.raises(ValueError, match="loopback or a Tailscale"):
            validate_web_bind(host)

    assert validate_web_bind("127.0.0.1") == "127.0.0.1"
    assert validate_web_bind("100.86.178.12") == "100.86.178.12"
    assert validate_web_bind("mac-mini.tail123.ts.net") == "mac-mini.tail123.ts.net"

    monkeypatch.setenv("HERDECK_ALLOW_UNSAFE_BIND", "1")
    assert validate_web_bind("0.0.0.0") == "0.0.0.0"


def test_web_deck_preserves_hardware_web_port_zero(web_deck):
    build_web_deck(13, hardware=HardwareConfig(web_port=0), cols=5, language="en")
    assert web_deck.seen["port"] == 0


def test_web_deck_gets_icons_dir_grid_and_language(web_deck):
    build_web_deck(
        13, hardware=HardwareConfig(icons_dir="~/herdeck-icons"), cols=4, language="cs"
    )
    seen = web_deck.seen
    assert (seen["slots"], seen["host"], seen["port"]) == (13, "127.0.0.1", 8800)
    assert seen["icons_dir"] == "~/herdeck-icons"
    assert (seen["cols"], seen["language"]) == (4, "cs")


def test_hardware_drivers_get_icons_dir(monkeypatch):
    from herdeck.host import _d200_driver, _elgato_driver

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

    _d200_driver(hw)
    _elgato_driver(hw)

    assert seen == {"d200": "~/herdeck-icons", "elgato": "~/herdeck-icons"}


def test_web_deck_prefers_env_web_bind_and_port(web_deck, monkeypatch):
    monkeypatch.setenv("HERDECK_WEB_BIND", "127.9.9.9")
    monkeypatch.setenv("HERDECK_WEB_PORT", "9911")

    hw = HardwareConfig(web_bind="100.1.2.3", web_port=1234)
    build_web_deck(13, hardware=hw, cols=5, language="en")

    assert (web_deck.seen["host"], web_deck.seen["port"]) == ("127.9.9.9", 9911)


def test_web_deck_wires_reverse_proxy_environment(web_deck, monkeypatch):
    monkeypatch.setenv("HERDECK_WEB_BASE_PATH", "/cockpit/herdeck")
    monkeypatch.setenv("HERDECK_WEB_PUBLIC_ORIGIN", "https://cockpit.example")
    monkeypatch.setenv(
        "HERDECK_WEB_FRAME_ANCESTORS",
        "https://cockpit.example, https://admin.example",
    )

    build_web_deck(13, hardware=HardwareConfig(), cols=5, language="en")

    seen = web_deck.seen
    assert seen["base_path"] == "/cockpit/herdeck"
    assert seen["public_origin"] == "https://cockpit.example"
    assert seen["frame_ancestors"] == (
        "https://cockpit.example",
        "https://admin.example",
    )


def test_runtime_startup_settings_prefer_env_over_local(monkeypatch):
    from herdeck.bootstrap import resolve_socket_path
    from herdeck.host import resolve_deck_kind

    cfg = Config(servers=[], profiles={}, overview_order=[], grid=(5, 3))
    cfg.hardware = HardwareConfig(deck="web", herdr_socket="/local.sock", tick_interval=1.25)

    monkeypatch.setenv("HERDECK_DECK", "fake")
    monkeypatch.setenv("HERDR_SOCKET", "/env.sock")

    assert resolve_deck_kind(cfg) == "fake"
    assert resolve_socket_path(cfg) == "/env.sock"


def test_runtime_startup_settings_use_local_when_env_absent(monkeypatch):
    from herdeck.bootstrap import resolve_socket_path
    from herdeck.host import resolve_deck_kind

    cfg = Config(servers=[], profiles={}, overview_order=[], grid=(5, 3))
    cfg.hardware = HardwareConfig(deck="web", herdr_socket="/local.sock", tick_interval=1.25)

    monkeypatch.delenv("HERDECK_DECK", raising=False)
    monkeypatch.delenv("HERDECK_FAKE_DECK", raising=False)
    monkeypatch.delenv("HERDR_SOCKET", raising=False)
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    monkeypatch.delenv("HERDR_SESSION", raising=False)

    assert resolve_deck_kind(cfg) == "web"
    assert resolve_socket_path(cfg) == "/local.sock"


def test_unknown_explicit_deck_kind_raises():
    with pytest.raises(ValueError, match="unsupported deck kind"):
        _front("dw00", d200_factory=_boom, web_factory=_Web)


def test_default_web_deck_redacts_capability_url_from_logs(monkeypatch, capsys):
    monkeypatch.setenv("HERDECK_WEB_PORT", "0")
    monkeypatch.delenv("HERDECK_SHOW_URL_TOKEN", raising=False)
    deck = build_web_deck(4, hardware=HardwareConfig(), cols=5, language="en")
    try:
        out = capsys.readouterr().out
        assert "authenticated browser session required" in out
        assert "/?token=" not in out
        assert deck.press_token not in out
    finally:
        deck.close()


def test_web_deck_can_explicitly_print_capability_url(monkeypatch, capsys):
    monkeypatch.setenv("HERDECK_WEB_PORT", "0")
    monkeypatch.setenv("HERDECK_SHOW_URL_TOKEN", "1")
    monkeypatch.setenv("HERDECK_WEB_ALLOW_QUERY_TOKEN", "1")
    deck = build_web_deck(4, hardware=HardwareConfig(), cols=5, language="en")
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


def test_local_config_preserves_usage():
    partial = Config(
        servers=[],
        profiles={},
        overview_order=[],
        grid=(5, 3),
    )
    partial.usage.providers = ["codex", "claude"]
    partial.usage.paid_only = True
    partial.usage.refresh_secs = 45
    partial.usage.codex_path = "/opt/codex"
    partial.usage.claude_cache_path = "/tmp/claude-usage.json"
    partial.usage.codexbar_path = "/opt/codexbar"

    cfg = local_config(1, "t", partial)

    assert cfg.usage == partial.usage


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
    from herdeck import host as host_mod

    def fake_iface(probe):
        return {"100.100.100.100": "100.64.1.2", "1.1.1.1": "192.168.1.5"}[probe]

    monkeypatch.setattr(host_mod, "_iface_addr", fake_iface)
    urls = host_mod.simulator_urls("0.0.0.0", 8800, "tok")
    assert urls[0] == "http://100.64.1.2:8800/?token=tok"  # Tailscale first
    assert "http://192.168.1.5:8800/?token=tok" in urls
    assert urls[-1] == "http://127.0.0.1:8800/?token=tok"
    # explicit binds announce exactly what was bound
    assert host_mod.simulator_urls("100.99.1.4", 8800, "t") == [
        "http://100.99.1.4:8800/?token=t"
    ]
