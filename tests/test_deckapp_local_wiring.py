import functools
import types

from herdeck.bridge import StubHerdr, start_local_bridge
from herdeck.deckapp import server as srv
from herdeck.deckapp.local_bridge import LocalBridgeRunner


def test_reloader_is_noop_in_local_mode():
    calls = []
    fake_app = types.SimpleNamespace(swap_source=lambda s: calls.append(s))
    reload = srv._reloader_for(fake_app, ("local", "/s.sock"), lambda: "NEWSRC")
    reload()
    assert calls == []  # local reload must NOT swap (would orphan the bridge)


def test_reloader_swaps_in_mock_or_remote_mode():
    calls = []
    fake_app = types.SimpleNamespace(swap_source=lambda s: calls.append(s))
    reload = srv._reloader_for(fake_app, ("mock", "first_run"), lambda: "NEWSRC")
    reload()
    assert calls == ["NEWSRC"]


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
