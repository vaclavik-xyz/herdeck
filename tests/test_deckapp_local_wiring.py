import functools
import types

from herdeck.bridge import StubHerdr, start_local_bridge
from herdeck.deckapp import server as srv
from herdeck.deckapp.local_bridge import LocalBridgeRunner


def test_local_reloader_rebuilds_source_against_running_bridge(monkeypatch):
    """An editor Apply / on-disk edit in LOCAL mode must reach the deck: the
    reloader rebuilds the live source against the RUNNING bridge's port/token
    (never restarting the bridge). The old no-op silently ignored every Apply
    (audit: local-apply-reload)."""
    captured = {}

    def fake_build(config, server):
        captured["server"] = server
        return "REBUILT"

    monkeypatch.setattr(srv, "build_live_source_for_connect", fake_build)
    swaps = []
    fake_runner = types.SimpleNamespace(bound=("127.0.0.1", 4242, "tok"))
    fake_app = types.SimpleNamespace(
        swap_source=lambda s: swaps.append(s), _local_bridge=fake_runner
    )
    reload = srv._reloader_for(fake_app, ("local", "/s.sock"), lambda: "NEWSRC")
    reload()
    assert swaps == ["REBUILT"]
    assert captured["server"].url == "ws://127.0.0.1:4242"
    assert captured["server"].token == "tok"


def test_local_reloader_is_noop_without_a_running_bridge():
    swaps = []
    fake_app = types.SimpleNamespace(swap_source=lambda s: swaps.append(s), _local_bridge=None)
    srv._reloader_for(fake_app, ("local", "/s.sock"), lambda: "NEWSRC")()
    assert swaps == []  # nothing to rebuild against; keep the current source


def test_reloader_swaps_in_mock_or_remote_mode():
    calls = []
    fake_app = types.SimpleNamespace(swap_source=lambda s: calls.append(s))
    reload = srv._reloader_for(fake_app, ("mock", "first_run"), lambda: "NEWSRC")
    reload()
    assert calls == ["NEWSRC"]


def test_mock_reloader_promotes_new_settings_selection_to_local(
    tmp_path, monkeypatch
):
    session = types.SimpleNamespace(
        name="review",
        server_id="local:review",
        socket_path="/review.sock",
        available=True,
        selected=True,
    )
    config = types.SimpleNamespace(servers=[types.SimpleNamespace(id="local:review")])
    runner = types.SimpleNamespace(close=lambda: None)
    calls = []
    local = tmp_path / "local.toml"
    local.write_text('[local]\nherdr_sessions = []\n')
    fake_app = types.SimpleNamespace(
        _config_service=types.SimpleNamespace(_local_path=local),
        swap_source=lambda source: calls.append(("swap", source)),
        _set_local_bridges=lambda runners: calls.append(("bridges", runners)),
    )
    selected = []
    monkeypatch.setattr(
        "herdeck.deckapp.sessions.discover_local_sessions",
        lambda path: list(selected),
    )
    monkeypatch.setattr(
        srv,
        "_explicit_selected_local_sessions",
        lambda service: list(selected),
    )
    monkeypatch.setattr(
        srv,
        "_start_local_session_bridges",
        lambda sessions, partial=None: (config, {"local:review": runner}),
    )
    monkeypatch.setattr(
        srv,
        "build_live_source_for_connect",
        lambda built_config, server: "LIVE",
    )
    monkeypatch.setattr(srv, "_load_partial_config", lambda: None)

    reload = srv._reloader_for(fake_app, ("mock", "demo"), lambda: "MOCK")
    selected.append(session)
    reload()

    assert calls == [
        ("swap", "LIVE"),
        ("bridges", {"local:review": runner}),
    ]


def test_mock_reloader_does_not_revive_unchanged_demo_selection(
    tmp_path, monkeypatch
):
    local = tmp_path / "local.toml"
    local.write_text('[local]\nherdr_sessions = ["review"]\n')
    session = types.SimpleNamespace(name="review", selected=True)
    monkeypatch.setattr(
        "herdeck.deckapp.sessions.discover_local_sessions",
        lambda path: [session],
    )
    calls = []
    fake_app = types.SimpleNamespace(
        _config_service=types.SimpleNamespace(_local_path=local),
        swap_source=lambda source: calls.append(source),
    )

    srv._reloader_for(fake_app, ("mock", "demo"), lambda: "MOCK")()

    assert calls == ["MOCK"]


def test_mock_env_blocks_new_local_selection(tmp_path, monkeypatch):
    local = tmp_path / "local.toml"
    local.write_text('[local]\nherdr_sessions = []\n')
    selected = []
    monkeypatch.setattr(
        "herdeck.deckapp.sessions.discover_local_sessions",
        lambda path: list(selected),
    )
    monkeypatch.setattr(
        srv,
        "_explicit_selected_local_sessions",
        lambda service: list(selected),
    )
    calls = []
    fake_app = types.SimpleNamespace(
        _config_service=types.SimpleNamespace(_local_path=local),
        swap_source=lambda source: calls.append(source),
    )
    reload = srv._reloader_for(fake_app, ("mock", "mock_env"), lambda: "MOCK")
    selected.append(types.SimpleNamespace(name="review", selected=True))

    reload()

    assert calls == ["MOCK"]


def test_mock_reloader_detects_implicit_to_explicit_same_name(
    tmp_path, monkeypatch
):
    local = tmp_path / "local.toml"
    explicit = {"value": False}
    session = types.SimpleNamespace(name="default", selected=True)
    monkeypatch.setattr(
        "herdeck.deckapp.sessions.discover_local_sessions",
        lambda path: [session],
    )
    monkeypatch.setattr(
        "herdeck.deckapp.sessions.has_explicit_local_session_selection",
        lambda path: explicit["value"],
    )
    monkeypatch.setattr(
        srv,
        "_explicit_selected_local_sessions",
        lambda service: [session],
    )
    config = types.SimpleNamespace(servers=[types.SimpleNamespace(id="local")])
    monkeypatch.setattr(
        srv,
        "_start_local_session_bridges",
        lambda sessions, partial=None: (config, {}),
    )
    monkeypatch.setattr(
        srv,
        "build_live_source_for_connect",
        lambda built_config, server: "LIVE",
    )
    monkeypatch.setattr(srv, "_load_partial_config", lambda: None)
    calls = []
    fake_app = types.SimpleNamespace(
        _config_service=types.SimpleNamespace(_local_path=local),
        swap_source=lambda source: calls.append(source),
        _set_local_bridges=lambda runners: None,
    )
    reload = srv._reloader_for(fake_app, ("mock", "demo"), lambda: "MOCK")
    explicit["value"] = True

    reload()

    assert calls == ["LIVE"]


def test_mock_reloader_connects_pending_selection_when_socket_appears(
    tmp_path, monkeypatch
):
    local = tmp_path / "local.toml"
    local.write_text('[local]\nherdr_sessions = []\n')
    selected = []
    available = []
    monkeypatch.setattr(
        "herdeck.deckapp.sessions.discover_local_sessions",
        lambda path: list(selected),
    )
    monkeypatch.setattr(
        srv,
        "_explicit_selected_local_sessions",
        lambda service: list(available),
    )
    config = types.SimpleNamespace(servers=[types.SimpleNamespace(id="local:review")])
    monkeypatch.setattr(
        srv,
        "_start_local_session_bridges",
        lambda sessions, partial=None: (config, {}),
    )
    monkeypatch.setattr(
        srv,
        "build_live_source_for_connect",
        lambda built_config, server: "LIVE",
    )
    monkeypatch.setattr(srv, "_load_partial_config", lambda: None)
    calls = []
    fake_app = types.SimpleNamespace(
        _config_service=types.SimpleNamespace(_local_path=local),
        swap_source=lambda source: calls.append(source),
        _set_local_bridges=lambda runners: None,
    )
    reload = srv._reloader_for(fake_app, ("mock", "demo"), lambda: "MOCK")
    session = types.SimpleNamespace(name="review", selected=True)
    selected.append(session)

    reload()
    available.append(session)
    reload()

    assert calls == ["MOCK", "LIVE"]


def test_mock_reloader_retries_pending_selection_after_build_failure(
    tmp_path, monkeypatch
):
    local = tmp_path / "local.toml"
    local.write_text('[local]\nherdr_sessions = []\n')
    selected = []
    session = types.SimpleNamespace(name="review", selected=True)
    monkeypatch.setattr(
        "herdeck.deckapp.sessions.discover_local_sessions",
        lambda path: list(selected),
    )
    monkeypatch.setattr(
        srv,
        "_explicit_selected_local_sessions",
        lambda service: [session],
    )
    config = types.SimpleNamespace(servers=[types.SimpleNamespace(id="local:review")])
    monkeypatch.setattr(
        srv,
        "_start_local_session_bridges",
        lambda sessions, partial=None: (config, {}),
    )
    attempts = []

    def build(built_config, server):
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise RuntimeError("temporary build failure")
        return "LIVE"

    monkeypatch.setattr(srv, "build_live_source_for_connect", build)
    monkeypatch.setattr(srv, "_load_partial_config", lambda: None)
    calls = []
    fake_app = types.SimpleNamespace(
        _config_service=types.SimpleNamespace(_local_path=local),
        swap_source=lambda source: calls.append(source),
        _set_local_bridges=lambda runners: None,
    )
    reload = srv._reloader_for(fake_app, ("mock", "demo"), lambda: "MOCK")
    selected.append(session)

    import pytest

    with pytest.raises(RuntimeError, match="temporary build failure"):
        reload()
    reload()

    assert attempts == [1, 2]
    assert calls == ["LIVE"]


def _stub_runner_factory(socket_path):
    return LocalBridgeRunner(
        socket_path, start_bridge=functools.partial(start_local_bridge, herdr=StubHerdr(panes=[]))
    )


def test_start_local_bridge_yields_loopback_config_and_runner():
    config, server, runner = srv._start_local_bridge("unused.sock", runner_factory=_stub_runner_factory)
    try:
        assert server.url.startswith("ws://127.0.0.1:")
        assert server.token  # the in-memory bridge token
        assert config.servers == [server]
    finally:
        runner.close()


def test_start_local_bridge_preserves_serverless_config_settings(tmp_path, monkeypatch):
    """A serverless config.toml (no [[servers]]) provides its grid/view/etc. as the
    partial overlay so local mode does NOT fall back to defaults."""
    cfg = tmp_path / "config.toml"
    cfg.write_text("[deck]\ngrid = \"4x2\"\n")
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))

    config, _server, runner = srv._start_local_bridge("unused.sock", runner_factory=_stub_runner_factory)
    try:
        assert config.grid == (4, 2)
    finally:
        runner.close()


def test_set_local_bridge_closes_previous():
    app = srv.create_mock_app(serve=False)
    try:
        r1 = _stub_runner_factory("unused.sock")
        r1.start()
        app._set_local_bridge(r1)
        r2 = _stub_runner_factory("unused.sock")
        r2.start()
        app._set_local_bridge(r2)  # must close r1
        assert r1._loop.is_closed()
        assert not r2._loop.is_closed()
    finally:
        app._set_local_bridge(None)
        app.close()
