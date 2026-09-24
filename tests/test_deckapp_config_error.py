"""A config file that exists but cannot be loaded must never start the demo.

Incident: the runtime ran as a launchd service without the shell env its
token_env names needed, one token was not in the keychain, and the deck
silently showed the MockSource demo fleet while looking healthy. Now it shows
an explicit error state, exposes the message on /health and /maintenance and
recovers without a restart once the config loads.
"""

import json
import os
import urllib.request

import pytest

from herdeck.deckapp import server as srv
from herdeck.deckapp.config_error import ConfigErrorSource
from herdeck.deckapp.mock import MockSource
from herdeck.deckapp.server import select_source_kind
from herdeck.orchestrator import Orchestrator
from herdeck.settings import TokenNotFoundError


class _Keyring:
    def __init__(self):
        self.store = {}

    def get_password(self, service, name):
        return self.store.get(name)


class _LiveStub(MockSource):
    """A non-networked stand-in for the LiveSource a fixed config builds."""

    source_name = "live"


@pytest.fixture
def broken(tmp_path, monkeypatch):
    """A config whose only server's token resolves from nowhere (the incident)."""
    keyring = _Keyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: keyring)
    for name in ("HERDECK_MOCK", "HERDECK_TOKEN", "HERDR_SOCKET_PATH", "HERDR_SESSION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERDR_SOCKET", str(tmp_path / "no-herdr.sock"))
    monkeypatch.setenv("HOME", str(tmp_path))
    token_file = tmp_path / "local-token"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[[servers]]\nid = "local"\nurl = "ws://127.0.0.1:9"\n'
        f'token_env = "HERDECK_TOKEN"\ntoken_file = "{token_file}"\n'
    )
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.setattr("herdeck.deckapp.live.build_live_source", lambda config, server: _LiveStub())
    return {"config": cfg, "token_file": token_file, "keyring": keyring}


def _get(app, path):
    url = f"http://{app.host}:{app.port}{path}{'&' if '?' in path else '?'}token={app.token}"
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode())


# --- pure precedence -------------------------------------------------------------

ERR = TokenNotFoundError("bridge token for server 'local' not found", "local")


def k(**kw):
    base = dict(mock_env=False, remote=None, choice=None, socket_path="/s", socket_exists=False)
    base.update(kw)
    return select_source_kind(**base)


def test_config_error_is_an_error_state_not_the_demo():
    assert k(config_error=ERR) == ("error", ERR)


def test_explicit_mock_choices_still_win_over_a_config_error():
    assert k(config_error=ERR, mock_env=True) == ("mock", "mock_env")
    assert k(config_error=ERR, choice="demo") == ("mock", "demo")
    assert k(config_error=ERR, choice="local", socket_exists=True) == ("local", "/s")


def test_no_config_error_keeps_first_run_onboarding():
    assert k() == ("mock", "first_run")


# --- source selection from disk -----------------------------------------------------


def test_broken_config_resolves_to_error(broken):
    kind = srv._resolve_source_kind()
    assert kind[0] == "error"
    assert isinstance(kind[1], TokenNotFoundError) and kind[1].server_id == "local"


def test_absent_config_still_first_run(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    monkeypatch.setenv("HERDECK_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("HERDR_SOCKET", str(tmp_path / "no.sock"))
    assert srv._resolve_source_kind() == ("mock", "first_run")


def test_mock_env_still_picks_the_demo_for_a_broken_config(broken, monkeypatch):
    monkeypatch.setenv("HERDECK_MOCK", "1")
    assert srv._resolve_source_kind() == ("mock", "mock_env")


def test_demo_choice_still_picks_the_demo_for_a_broken_config(broken):
    from herdeck.deckapp import onboarding

    onboarding.write_choice(str(broken["config"]), "demo")
    assert srv._resolve_source_kind() == ("mock", "demo")


def test_malformed_config_is_an_error_too(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    monkeypatch.setenv("HERDR_SOCKET", str(tmp_path / "no.sock"))
    cfg = tmp_path / "config.toml"
    cfg.write_text('[deck]\ngrid = "wide"\n')
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    kind = srv._resolve_source_kind()
    assert kind[0] == "error" and "grid" in str(kind[1])


# --- the running app ---------------------------------------------------------------


def test_runtime_with_broken_config_shows_error_not_demo(broken, caplog):
    import herdeck.deckapp.config_error as config_error

    config_error._last_logged = None
    with caplog.at_level("ERROR", logger="herdeck.deckapp.config_error"):
        app = srv.create_app(serve=True)
    try:
        assert app.source_name == "config_error"
        assert not isinstance(app._source, MockSource)
        rs = app._orch.render()
        assert rs.panel.title == "Config error"
        assert rs.panel.headline == "No token for local"
        assert rs.panel.lines == ["Fix it in the app → Maintenance"]
        assert rs.panel.solid
        assert all(tile.label == "" for tile in rs.tiles)  # no (fake) agents
        health = _get(app, "/health")
        assert health["source"] == "config_error"
        assert "bridge token for server 'local' not found" in health["config_error"]
        assert _get(app, "/maintenance")["config_error"] == health["config_error"]
        setup = _get(app, "/setup")
        assert (setup["mode"], setup["reason"]) == ("error", "config_error")
        assert any("bridge token for server 'local'" in r.getMessage() for r in caplog.records)
    finally:
        app.close()


def test_error_panel_speaks_the_configured_language(broken):
    text = broken["config"].read_text() + '\n[view]\nlanguage = "cs"\n'
    broken["config"].write_text(text)
    app = srv.create_app(serve=False)
    try:
        rs = app._orch.render()
        assert rs.panel.title == "Chyba configu"
        assert rs.panel.headline == "Chybí token pro local"
        assert rs.panel.lines == ["Oprav v aplikaci → Údržba"]
    finally:
        app.close()


def test_creating_the_token_file_recovers_without_restart(broken):
    app = srv.create_app(serve=False)
    try:
        assert app.source_name == "config_error"
        broken["token_file"].write_text("the-token\n")
        os.chmod(broken["token_file"], 0o600)
        assert app._watcher.dirty()  # the token file is watched like the config
        app._watcher_reload()
        assert app.source_name == "live"
        assert app.config_error is None
    finally:
        app.close()


def test_chmod_of_a_refused_token_file_recovers(broken):
    broken["token_file"].write_text("the-token\n")
    os.chmod(broken["token_file"], 0o644)
    app = srv.create_app(serve=False)
    try:
        assert "chmod 600" in app.config_error
        os.chmod(broken["token_file"], 0o600)  # changes no mtime, only the mode
        assert app._watcher.dirty()
        app._watcher_reload()
        assert app.source_name == "live"
    finally:
        app.close()


def test_keychain_fix_recovers_via_the_periodic_probe(broken):
    now = [0.0]
    app = srv.create_app(serve=False)
    try:
        probe = srv._config_error_probe(app, interval=10.0, clock=lambda: now[0])
        first = probe()
        assert first == app.config_error
        broken["keyring"].store["HERDECK_TOKEN"] = "from-keychain"
        now[0] = 5.0
        assert probe() == first  # throttled: no config reload every poll
        now[0] = 10.0
        assert probe() is None  # the error went away -> the watcher fires a reload
    finally:
        app.close()


def test_a_live_deck_whose_config_breaks_shows_the_error(broken):
    broken["token_file"].write_text("the-token\n")
    os.chmod(broken["token_file"], 0o600)
    app = srv.create_app(serve=False)
    try:
        assert app.source_name == "live"
        broken["token_file"].unlink()
        app.reload()
        assert app.source_name == "config_error"
        assert "not found" in app.config_error
    finally:
        app.close()


def test_orchestrator_generic_config_error_panel():
    source = ConfigErrorSource("invalid grid 'wide'")
    orch = Orchestrator(source.config)
    source.apply_to(orch)
    rs = orch.render()
    assert rs.panel.headline == "Config can't be loaded"
    assert rs.panel.lines == ["Fix it in the app → Maintenance"]
    assert rs.panel.hint == "the deck recovers once it's fixed"
    assert source.connected is False and source.summary()["agents"] == 0


def test_headless_runtime_publishes_the_error_source(broken, monkeypatch, tmp_path):
    from herdeck import runtime

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    def no_device(config):
        raise OSError("no D200 attached")

    app, sink, info, _path = runtime.build_runtime(
        driver_factory=no_device, write_discovery=False
    )
    try:
        assert info["source"] == "config_error"  # runtime.json never says "mock" here
        assert app.config_error and "local" in app.config_error
    finally:
        sink.close()
        app.close()
