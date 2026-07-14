# tests/test_deckapp_setup_routes.py
import json
import urllib.error
import urllib.request

from herdeck.deckapp import server as srv
from herdeck.deckapp.mock import MockSource
from herdeck.deckapp.probe import ProbeResult


class _DisconnectedSource(MockSource):
    """A MockSource reporting connected=False — proves /setup/connect returns the real
    source status (the connector dials asynchronously), not a hardcoded True."""

    @property
    def connected(self) -> bool:
        return False


def _post(app, path, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"http://{app.host}:{app.port}{path}", data=data, method="POST",
        headers={"X-Herdeck-Token": app.token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read().decode())


class _FakeKeyring:
    def __init__(self):
        self.store = {}

    def set_password(self, service, name, value):
        self.store[(service, name)] = value

    def get_password(self, service, name):
        return self.store.get((service, name))

    def delete_password(self, service, name):
        self.store.pop((service, name), None)


def _get(app, path):
    req = urllib.request.Request(f"http://{app.host}:{app.port}{path}")
    with urllib.request.urlopen(req, timeout=3) as r:
        return r.status, json.loads(r.read().decode())


def test_setup_status_first_run(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    monkeypatch.setenv("HERDECK_CONFIG", str(tmp_path / "config.toml"))  # absent -> first_run
    monkeypatch.setenv("HERDR_SOCKET", str(tmp_path / "nope.sock"))      # absent -> no local
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        status, body = _get(app, f"/setup?token={app.token}")
        assert status == 200
        assert body["mode"] == "mock"
        assert body["reason"] == "first_run"
        assert body["local_herdr_available"] is False
        assert body["choice"] is None
    finally:
        app.close()


def test_setup_status_demo_reason(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    cfg = str(tmp_path / "config.toml")
    monkeypatch.setenv("HERDECK_CONFIG", cfg)
    monkeypatch.setenv("HERDR_SOCKET", str(tmp_path / "nope.sock"))
    from herdeck.deckapp import onboarding

    onboarding.write_choice(cfg, "demo")
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        _, body = _get(app, f"/setup?token={app.token}")
        assert body["reason"] == "demo"
        assert body["choice"] == "demo"
    finally:
        app.close()


def test_setup_status_uses_configured_local_socket(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    socket_path = tmp_path / "custom.sock"
    socket_path.touch()
    (tmp_path / "local.toml").write_text(
        f'[local]\nherdr_socket = "{socket_path}"\n'
    )
    monkeypatch.setenv("HERDECK_CONFIG", str(config_path))
    monkeypatch.delenv("HERDR_SOCKET", raising=False)
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    monkeypatch.delenv("HERDR_SESSION", raising=False)
    app = srv.create_mock_app(serve=False, config_service=srv._default_config_service())
    try:
        status = app._setup_status()
        assert status["socket_path"] == str(socket_path)
        assert status["local_herdr_available"] is True
    finally:
        app.close()


def test_connect_local_uses_configured_local_socket(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    socket_path = tmp_path / "custom.sock"
    socket_path.touch()
    (tmp_path / "local.toml").write_text(
        f'[local]\nherdr_socket = "{socket_path}"\n'
    )
    monkeypatch.setenv("HERDECK_CONFIG", str(config_path))
    monkeypatch.delenv("HERDR_SOCKET", raising=False)
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    monkeypatch.delenv("HERDR_SESSION", raising=False)
    seen = []

    def fail_start(path):
        seen.append(path)
        raise RuntimeError("stop after socket resolution")

    monkeypatch.setattr(srv, "_start_local_bridge", fail_start)
    app = srv.create_mock_app(serve=False, config_service=srv._default_config_service())
    try:
        result = srv.connect(app, {"choice": "local"})
        assert result == {"ok": False, "error": "could not start local source"}
        assert seen == [str(socket_path)]
    finally:
        app.close()


def test_setup_status_requires_token(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDECK_CONFIG", str(tmp_path / "config.toml"))
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        try:
            _get(app, "/setup?token=wrong")
            raise AssertionError("expected 403")
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        app.close()


def test_connect_demo_persists(tmp_path, monkeypatch):
    cfg = str(tmp_path / "config.toml")
    local = tmp_path / "local.toml"
    local.write_text("[hardware]\nbrightness = 35\ntick_interval = 1.0\n")
    monkeypatch.setenv("HERDECK_CONFIG", cfg)
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        local.write_text("[hardware]\nbrightness = 45\ntick_interval = 2.0\n")
        status, body = _post(app, "/setup/connect", {"choice": "demo"})
        assert status == 200 and body["ok"] is True
        assert app.config.hardware.brightness == 45
        assert app._tick_interval == 2.0
        from herdeck.deckapp import onboarding

        assert onboarding.read_choice(cfg) == "demo"
    finally:
        app.close()


def test_connect_demo_marker_write_failure_rolls_back(tmp_path, monkeypatch):
    from herdeck.deckapp import onboarding

    cfg = str(tmp_path / "config.toml")
    monkeypatch.setenv("HERDECK_CONFIG", cfg)
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    monkeypatch.setattr(onboarding, "write_choice", lambda cp, choice: (_ for _ in ()).throw(OSError("disk full")))
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    prev = app._source
    try:
        _, body = _post(app, "/setup/connect", {"choice": "demo"})
        assert body["ok"] is False  # marker write failed before the commit
        assert app._source is prev  # swap not committed
        assert onboarding.read_choice(cfg) is None  # no marker persisted
    finally:
        app.close()


def test_config_write_does_not_touch_marker(tmp_path, monkeypatch):
    # The config editor edits content, not connection mode: a /config write must NOT change
    # the onboarding marker, so an explicit local/demo choice sticks across edits.
    from herdeck.deckapp import onboarding

    cfg = str(tmp_path / "config.toml")
    monkeypatch.setenv("HERDECK_CONFIG", cfg)
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    onboarding.write_choice(cfg, "demo")  # explicit demo choice
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        write_body = {
            "base": {"servers": [{"id": "herdr", "url": "ws://x:8788", "token_env": "HERDECK_HERDR_TOKEN"}]},
            "profiles": {},
            "local": {},
        }
        _, res = _post(app, "/config", write_body)
        assert res.get("errors") == []  # config written
        assert onboarding.read_choice(cfg) == "demo"  # marker untouched — demo choice sticks
    finally:
        app.close()


def test_connect_remote_writes_secret_then_config_and_verifies(tmp_path, monkeypatch):
    cfg = str(tmp_path / "config.toml")
    monkeypatch.setenv("HERDECK_CONFIG", cfg)
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    # Inject a probe that says ok + a remote source builder that does not touch the
    # network and reports connected=False (so we can assert the response is honest).
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    monkeypatch.setattr(srv, "build_live_source_for_connect", lambda config, server: _DisconnectedSource())
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        status, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://10.0.0.5:8788", "token": "secret-tok", "id": "herdr"},
        )
        assert status == 200 and body["ok"] is True
        assert body["connected"] is False  # honest: the just-built source isn't connected yet
        # secret stored under the derived env name, NOT written to TOML
        assert fake.store[("herdeck", "HERDECK_HERDR_TOKEN")] == "secret-tok"
        text = (tmp_path / "config.toml").read_text()
        assert "HERDECK_HERDR_TOKEN" in text and "secret-tok" not in text
    finally:
        app.close()


def test_connect_remote_build_failure_leaves_previous_source_and_bridge(tmp_path, monkeypatch):
    import functools

    from herdeck.bridge import StubHerdr, start_local_bridge
    from herdeck.deckapp.local_bridge import LocalBridgeRunner

    cfg = str(tmp_path / "config.toml")
    monkeypatch.setenv("HERDECK_CONFIG", cfg)
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))

    def _boom(config, server):
        raise RuntimeError("connector blew up")

    monkeypatch.setattr(srv, "build_live_source_for_connect", _boom)
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    # Simulate already being in local mode: a live bridge runner is adopted pre-connect.
    runner = LocalBridgeRunner(
        "unused.sock", start_bridge=functools.partial(start_local_bridge, herdr=StubHerdr(panes=[]))
    )
    runner.start()
    app._set_local_bridge(runner)
    prev_source = app._source
    try:
        status, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://10.0.0.5:8788", "token": "secret-tok", "id": "herdr"},
        )
        # Build runs BEFORE persist, so a build failure: honest {ok:false}, NOTHING
        # persisted, and the previous source AND local bridge are left fully intact.
        assert status == 200 and body["ok"] is False and "could not build" in body["error"]
        assert not (tmp_path / "config.toml").exists()  # config never written
        assert ("herdeck", "HERDECK_HERDR_TOKEN") not in fake.store  # secret never stored
        assert app._source is prev_source
        assert app._local_bridge is runner and not runner._loop.is_closed()
    finally:
        app._set_local_bridge(None)
        app.close()


def test_connect_remote_probe_fail_persists_nothing(tmp_path, monkeypatch):
    cfg = str(tmp_path / "config.toml")
    monkeypatch.setenv("HERDECK_CONFIG", cfg)
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(False, "bad_token"))
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        status, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://10.0.0.5:8788", "token": "x", "id": "herdr"},
        )
        assert status == 200 and body["ok"] is False and body["error"] == "bad_token"
        assert not (tmp_path / "config.toml").exists()
        assert fake.store == {}
    finally:
        app.close()


def test_connect_remote_selection_mismatch_persists_nothing(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    # Pre-existing config whose default-selected server is "other" (servers[0]); the
    # upserted "herdr" is appended, so the resolved selection stays "other" -> mismatch.
    cfg.write_text('[[servers]]\nid = "other"\nurl = "ws://1.2.3.4:8788"\ntoken_env = "OTHER_TOK"\n')
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    fake.store[("herdeck", "OTHER_TOK")] = "other-secret"  # other server resolves -> genuine mismatch
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        status, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://10.0.0.5:8788", "token": "secret-tok", "id": "herdr"},
        )
        assert status == 200 and body["ok"] is False and "resolve to this server" in body["error"]
        # nothing about herdr persisted: no new server, no keychain entry
        assert "herdr" not in cfg.read_text().lower()
        assert ("herdeck", "HERDECK_HERDR_TOKEN") not in fake.store
    finally:
        app.close()


def test_connect_remote_rejects_unloadable_other_token_missing(tmp_path, monkeypatch):
    # herdr is servers[0] (selected), but a second server's token is missing — so the
    # config would NOT load on restart. The preflight placeholders only the new token,
    # so the missing OTHER token fails resolution and onboarding rejects, persisting nothing.
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[[servers]]\nid = "herdr"\nurl = "ws://old:8788"\ntoken_env = "HERDECK_HERDR_TOKEN"\n'
        '[[servers]]\nid = "extra"\nurl = "ws://x:8788"\ntoken_env = "EXTRA_TOK"\n'
    )
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()  # EXTRA_TOK is not stored anywhere
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        _, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://new:8788", "token": "t", "id": "herdr"},
        )
        assert body["ok"] is False  # EXTRA_TOK won't resolve -> config unloadable -> rejected
        assert ("herdeck", "HERDECK_HERDR_TOKEN") not in fake.store  # nothing persisted
        assert "ws://new" not in cfg.read_text()  # config not rewritten with the new url
    finally:
        app.close()


def test_connect_remote_write_raises_rolls_back_secret(tmp_path, monkeypatch):
    cfg = str(tmp_path / "config.toml")
    monkeypatch.setenv("HERDECK_CONFIG", cfg)
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    # Build succeeds (no real network); the failure is injected at the config write.
    monkeypatch.setattr(srv, "build_live_source_for_connect", lambda config, server: _DisconnectedSource())
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())

    def _boom_write(payload):  # atomic write faults AFTER the secret was stored
        raise OSError("disk full")

    monkeypatch.setattr(app._config_service, "write", _boom_write)
    try:
        status, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://10.0.0.5:8788", "token": "t", "id": "herdr"},
        )
        assert status == 200 and body["ok"] is False
        # the orphaned keychain secret was rolled back (no half-commit on a disk fault)
        assert ("herdeck", "HERDECK_HERDR_TOKEN") not in fake.store
    finally:
        app.close()


def test_connect_remote_partial_write_restores_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    monkeypatch.setattr(srv, "build_live_source_for_connect", lambda config, server: _DisconnectedSource())
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    svc = app._config_service
    real_atomic = svc._atomic_write
    calls = {"n": 0}

    def _flaky_atomic(path, text):  # config.toml write succeeds, local.toml write faults
        calls["n"] += 1
        if calls["n"] == 1:
            return real_atomic(path, text)
        raise OSError("local write failed")

    monkeypatch.setattr(svc, "_atomic_write", _flaky_atomic)
    try:
        status, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://10.0.0.5:8788", "token": "t", "id": "herdr"},
        )
        assert status == 200 and body["ok"] is False
        # the partially-written config.toml is restored away (prior was absent), secret rolled back
        assert not cfg.exists()
        assert ("herdeck", "HERDECK_HERDR_TOKEN") not in fake.store
    finally:
        app.close()


def test_connect_remote_prepare_failure_closes_source(tmp_path, monkeypatch):
    # build_live_source starts a connector immediately; if _prepare_swap then raises, the
    # built source MUST be closed so the connector doesn't leak.
    monkeypatch.setenv("HERDECK_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    closed = {"v": False}

    class _BadPrepare(MockSource):
        def close(self):
            closed["v"] = True

        @property
        def config(self):  # makes _prepare_swap raise (it reads config.grid)
            raise ValueError("boom")

    monkeypatch.setattr(srv, "build_live_source_for_connect", lambda c, s: _BadPrepare())
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        _, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://x:8788", "token": "t", "id": "herdr"},
        )
        assert body["ok"] is False and "could not build" in body["error"]
        assert closed["v"]  # built source closed on prepare failure (no connector leak)
        assert ("herdeck", "HERDECK_HERDR_TOKEN") not in fake.store
    finally:
        app.close()


def test_connect_remote_config_snapshot_failure_persists_nothing(tmp_path, monkeypatch):
    # A config read fault during the pre-mutation snapshot must abort BEFORE set_secret,
    # leaving no orphaned keychain entry AND closing the just-built source.
    monkeypatch.setenv("HERDECK_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    closed = {"v": False}

    class _RecSrc(_DisconnectedSource):
        def close(self):
            closed["v"] = True

    monkeypatch.setattr(srv, "build_live_source_for_connect", lambda c, s: _RecSrc())

    def _boom_snapshot(svc):
        raise OSError("read fault")

    monkeypatch.setattr(srv, "_snapshot_config", _boom_snapshot)
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        _, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://x:8788", "token": "t", "id": "herdr"},
        )
        assert body["ok"] is False
        assert ("herdeck", "HERDECK_HERDR_TOKEN") not in fake.store  # snapshot failed before set_secret
        assert closed["v"]  # built source closed (no connector leak)
    finally:
        app.close()


def test_connect_remote_write_failure_restores_prior_secret(tmp_path, monkeypatch):
    # Reconnecting an EXISTING "herdr" server whose token was already stored: a write
    # failure must RESTORE the prior secret (and prior config), not destroy them.
    cfg = tmp_path / "config.toml"
    cfg.write_text('[[servers]]\nid = "herdr"\nurl = "ws://old:8788"\ntoken_env = "HERDECK_HERDR_TOKEN"\n')
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    fake.store[("herdeck", "HERDECK_HERDR_TOKEN")] = "old-tok"  # pre-existing keychain token
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    monkeypatch.setattr(srv, "build_live_source_for_connect", lambda c, s: _DisconnectedSource())
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())

    def _boom_write(payload):
        raise OSError("disk full")

    monkeypatch.setattr(app._config_service, "write", _boom_write)
    try:
        status, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://new:8788", "token": "new-tok", "id": "herdr"},
        )
        assert status == 200 and body["ok"] is False
        # the PRIOR secret + config are restored, not destroyed/overwritten
        assert fake.store[("herdeck", "HERDECK_HERDR_TOKEN")] == "old-tok"
        assert "ws://old" in cfg.read_text()
    finally:
        app.close()


def test_connect_remote_set_secret_failure_restores_prior(tmp_path, monkeypatch):
    # set_secret can partially overwrite then raise (flaky backend); restore the prior.
    cfg = tmp_path / "config.toml"
    cfg.write_text('[[servers]]\nid = "herdr"\nurl = "ws://old:8788"\ntoken_env = "HERDECK_HERDR_TOKEN"\n')
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    fake.store[("herdeck", "HERDECK_HERDR_TOKEN")] = "old-tok"

    def _bad_set(service, name, value):  # partial-overwrites, then raises only for the new value
        fake.store[(service, name)] = value
        if value == "new-tok":
            raise RuntimeError("keychain backend error")

    monkeypatch.setattr(fake, "set_password", _bad_set)
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    monkeypatch.setattr(srv, "build_live_source_for_connect", lambda c, s: _DisconnectedSource())
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        _, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://new:8788", "token": "new-tok", "id": "herdr"},
        )
        assert body["ok"] is False and "store token" in body["error"]
        assert fake.store[("herdeck", "HERDECK_HERDR_TOKEN")] == "old-tok"  # restored after partial set
    finally:
        app.close()


def test_connect_remote_malformed_existing_config(tmp_path, monkeypatch):
    # An unparseable existing config.toml must surface as {ok:false}, not a 500.
    cfg = tmp_path / "config.toml"
    cfg.write_text("this is { not valid toml")
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        status, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://x:8788", "token": "t", "id": "herdr"},
        )
        assert status == 200 and body["ok"] is False and "unreadable" in body["error"]
        assert ("herdeck", "HERDECK_HERDR_TOKEN") not in fake.store  # nothing persisted
    finally:
        app.close()


def test_connect_remote_structurally_invalid_servers(tmp_path, monkeypatch):
    # Parseable TOML with a wrong-shaped servers value must be rejected, not 500.
    cfg = tmp_path / "config.toml"
    cfg.write_text('servers = ["bad"]\n')
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        status, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://x:8788", "token": "t", "id": "herdr"},
        )
        assert status == 200 and body["ok"] is False and "malformed" in body["error"]
        assert ("herdeck", "HERDECK_HERDR_TOKEN") not in fake.store
    finally:
        app.close()


def test_connect_remote_invalid_id_type_is_400(tmp_path, monkeypatch):
    # A non-string id (e.g. {"id": 123}) must be a 400, not a 500 from _token_env_for.
    monkeypatch.setenv("HERDECK_CONFIG", str(tmp_path / "config.toml"))
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        try:
            _post(app, "/setup/connect", {"choice": "remote", "url": "ws://x:8788", "token": "t", "id": 123})
            raise AssertionError("expected 400")
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        app.close()


def test_connect_remote_existing_server_missing_token_env(tmp_path, monkeypatch):
    # An existing server dict missing token_env is parseable + dict-shaped, but resolution
    # raises KeyError in _server_config — must surface as {ok:false}, not a 500.
    cfg = tmp_path / "config.toml"
    cfg.write_text('[deck]\ngrid = "5x3"\n[[servers]]\nid = "other"\nurl = "ws://o:8788"\n')
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        status, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://x:8788", "token": "t", "id": "herdr"},
        )
        assert status == 200 and body["ok"] is False  # resolve failed -> doesn't select -> {ok:false}
        assert ("herdeck", "HERDECK_HERDR_TOKEN") not in fake.store
    finally:
        app.close()


def test_connect_remote_rejects_env_token_collision(tmp_path, monkeypatch):
    # token_env already exported with a different value would shadow the keychain
    # (env-first resolution), so the persisted config wouldn't use the typed token.
    monkeypatch.setenv("HERDECK_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    monkeypatch.setenv("HERDECK_HERDR_TOKEN", "env-stale-token")
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        status, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://x:8788", "token": "typed-token", "id": "herdr"},
        )
        assert status == 200 and body["ok"] is False and "environment" in body["error"]
        assert not (tmp_path / "config.toml").exists()  # rejected before any persist
        assert ("herdeck", "HERDECK_HERDR_TOKEN") not in fake.store
    finally:
        app.close()


def test_connect_remote_rejects_token_env_collision_with_other_server(tmp_path, monkeypatch):
    # `foo-bar` and `foo_bar` derive the SAME HERDECK_FOO_BAR_TOKEN; connecting one must not
    # be allowed to overwrite the other server's keychain token.
    cfg = tmp_path / "config.toml"
    cfg.write_text('[[servers]]\nid = "foo_bar"\nurl = "ws://o:8788"\ntoken_env = "HERDECK_FOO_BAR_TOKEN"\n')
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        _, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://x:8788", "token": "t", "id": "foo-bar"},
        )
        assert body["ok"] is False and "already used elsewhere" in body["error"]
        assert ("herdeck", "HERDECK_FOO_BAR_TOKEN") not in fake.store  # nothing persisted
    finally:
        app.close()


def test_connect_remote_rejects_token_env_collision_with_notifications(tmp_path, monkeypatch):
    # token_env is a flat namespace: a derived HERDECK_HERDR_TOKEN must not overwrite a
    # secret already referenced by a non-server section (here a Telegram notification).
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[deck]\ngrid = "5x3"\n'
        '[notifications.telegram]\ntoken_env = "HERDECK_HERDR_TOKEN"\nchat_id = "1"\n'
    )
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        _, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://x:8788", "token": "t", "id": "herdr"},
        )
        assert body["ok"] is False and "already used elsewhere" in body["error"]
    finally:
        app.close()


def test_connect_remote_keychain_read_failure_aborts(tmp_path, monkeypatch):
    # If snapshotting the prior token fails (keychain backend error), abort BEFORE set_secret
    # rather than risk erasing an existing token on a later rollback.
    cfg = tmp_path / "config.toml"
    cfg.write_text('[[servers]]\nid = "herdr"\nurl = "ws://old:8788"\ntoken_env = "HERDECK_HERDR_TOKEN"\n')
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    fake.store[("herdeck", "HERDECK_HERDR_TOKEN")] = "old-tok"

    def _bad_get(service, name):  # keychain backend read error (NOT "missing")
        raise RuntimeError("keychain read error")

    monkeypatch.setattr(fake, "get_password", _bad_get)
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    monkeypatch.setattr(srv, "build_live_source_for_connect", lambda c, s: _DisconnectedSource())
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        _, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://new:8788", "token": "new-tok", "id": "herdr"},
        )
        assert body["ok"] is False and "keychain" in body["error"]
        assert fake.store[("herdeck", "HERDECK_HERDR_TOKEN")] == "old-tok"  # not overwritten/erased
    finally:
        app.close()


def test_connect_remote_marker_clear_failure_rolls_back(tmp_path, monkeypatch):
    # clear_choice is part of the commit: if it faults, everything rolls back so a
    # later-removed remote config falls to first_run, never a stale masking marker.
    from herdeck.deckapp import onboarding

    cfg = tmp_path / "config.toml"
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    closed = {"v": False}

    class _RecSrc(_DisconnectedSource):
        def close(self):
            closed["v"] = True

    monkeypatch.setattr(srv, "build_live_source_for_connect", lambda c, s: _RecSrc())
    monkeypatch.setattr(onboarding, "clear_choice", lambda cp: (_ for _ in ()).throw(OSError("nope")))
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    prev = app._source
    try:
        status, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://10.0.0.5:8788", "token": "t", "id": "herdr"},
        )
        assert status == 200 and body["ok"] is False
        # full rollback: config + secret gone, source unchanged, AND the built source closed
        assert not cfg.exists()
        assert ("herdeck", "HERDECK_HERDR_TOKEN") not in fake.store
        assert app._source is prev
        assert closed["v"]  # the just-built source was closed (no connector leak)
    finally:
        app.close()


def test_connect_local_write_choice_failure_closes_source_and_runner(tmp_path, monkeypatch):
    from herdeck.deckapp import onboarding

    sock = tmp_path / "herdr.sock"
    sock.touch()  # exists, so the socket check passes
    monkeypatch.setenv("HERDR_SOCKET", str(sock))
    monkeypatch.setenv("HERDECK_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    closed = {"source": False, "runner": False}

    class _RecSource(MockSource):
        def close(self):
            closed["source"] = True

    class _RecRunner:
        def close(self):
            closed["runner"] = True

    monkeypatch.setattr(srv, "_start_local_bridge", lambda sp: ("CFG", "SRV", _RecRunner()))
    monkeypatch.setattr(srv, "build_live_source_for_connect", lambda c, s: _RecSource())
    monkeypatch.setattr(onboarding, "write_choice", lambda cp, choice: (_ for _ in ()).throw(OSError("disk full")))
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    prev = app._source
    try:
        status, body = _post(app, "/setup/connect", {"choice": "local"})
        assert status == 200 and body["ok"] is False
        assert closed["source"] and closed["runner"]  # neither the built source nor bridge leaks
        assert app._source is prev and app._local_bridge is None  # previous state untouched
    finally:
        app.close()


def test_reload_is_suppressed_while_flag_set(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDECK_CONFIG", str(tmp_path / "config.toml"))
    app = srv.create_mock_app(serve=False)
    calls = []
    app._reloader = lambda: calls.append(1)
    try:
        app._suppress_reload = True
        app.reload()
        assert calls == []  # the watcher-driven reload is muted during the commit
        app._suppress_reload = False
        app.reload()
        assert calls == [1]  # and fires normally once the commit clears the flag
    finally:
        app.close()


def test_watcher_resync_adopts_current_mtimes(tmp_path):
    from herdeck.deckapp.watcher import ConfigWatcher

    p = tmp_path / "config.toml"
    p.write_text("a = 1\n")
    w = ConfigWatcher([p], lambda: None, interval=999)  # not started: no polling in the test
    p.write_text("a = 2\n")  # change after the constructor snapshotted
    w.resync()  # adopt the new mtime as baseline
    assert w._snapshot() == w._last  # a subsequent poll would see no change


def test_swap_source_bad_config_does_not_half_swap(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDECK_CONFIG", str(tmp_path / "config.toml"))
    app = srv.create_mock_app(serve=False)
    prev = app._source

    class _BadSource(MockSource):
        @property
        def config(self):
            raise ValueError("malformed config")

    try:
        try:
            app.swap_source(_BadSource())
            raise AssertionError("expected swap_source to raise")
        except ValueError:
            pass
        assert app._source is prev  # build-then-assign: a bad config never half-swaps
    finally:
        app.close()


def test_prepare_commit_swap_adopts_given_clock(tmp_path, monkeypatch):
    import time

    monkeypatch.setenv("HERDECK_CONFIG", str(tmp_path / "config.toml"))
    app = srv.create_mock_app(serve=False)  # mock clock is a fixed lambda: 0.0
    try:
        src = MockSource()
        app._commit_swap(src, app._prepare_swap(src, clock=time.monotonic))
        assert app._clock is time.monotonic  # the live clock is adopted, not the mock's frozen one
        assert app._source is src
    finally:
        app.close()


def test_concurrent_remote_connects_are_serialized(tmp_path, monkeypatch):
    import threading
    import tomllib

    cfg = tmp_path / "config.toml"
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    monkeypatch.setattr(srv, "build_live_source_for_connect", lambda c, s: _DisconnectedSource())
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    results = []

    def _go():
        try:
            _, body = _post(
                app, "/setup/connect",
                {"choice": "remote", "url": "ws://x:8788", "token": "t", "id": "herdr"},
            )
            results.append(body)
        except Exception as exc:  # noqa: BLE001
            results.append(exc)

    threads = [threading.Thread(target=_go) for _ in range(2)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        # Both complete without error; the _setup_lock serialized them, so the config is
        # consistent: exactly one herdr server, not a half-interleaved write.
        assert len(results) == 2 and all(isinstance(r, dict) for r in results)
        data = tomllib.loads(cfg.read_text())
        assert sum(1 for s in data.get("servers", []) if s.get("id") == "herdr") == 1
    finally:
        app.close()


def test_config_write_serialized_with_connect_via_setup_lock(tmp_path, monkeypatch):
    # The shared _setup_lock must serialize a config-editor write against a /setup/connect:
    # while a connect holds the lock (blocked mid-transaction) a /config write cannot proceed,
    # and the final state stays coherent (no interleaved/half-written config).
    import threading
    import time as _t
    import tomllib

    cfg = tmp_path / "config.toml"
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "build_live_source_for_connect", lambda c, s: _DisconnectedSource())
    in_connect = threading.Event()
    release = threading.Event()

    def _blocking_probe(url, token):  # connect blocks here (holding _setup_lock), then fails
        in_connect.set()
        release.wait(5)
        return ProbeResult(False, "unreachable")

    monkeypatch.setattr(srv, "_probe_sync", _blocking_probe)
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    out = {}

    def _connect():
        _, out["connect"] = _post(
            app, "/setup/connect", {"choice": "remote", "url": "ws://x:8788", "token": "t", "id": "herdr"}
        )

    def _cfg():
        out["cfg"] = _post(
            app, "/config",
            {"base": {"servers": [{"id": "editor", "url": "ws://e:8788", "token_env": "HERDECK_EDITOR_TOKEN"}]},
             "profiles": {}, "local": {}},
        )

    ct, wt = threading.Thread(target=_connect), threading.Thread(target=_cfg)
    try:
        ct.start()
        assert in_connect.wait(5)  # connect now holds _setup_lock (blocked in the probe)
        wt.start()
        _t.sleep(0.2)  # give the config write time to reach + block on the lock
        assert "cfg" not in out  # it CANNOT complete while the connect holds the lock
        release.set()
        ct.join(10)
        wt.join(10)
        assert out["connect"]["ok"] is False  # connect failed (probe), nothing persisted
        _, cfg_body = out["cfg"]
        assert cfg_body.get("errors") == []  # config write succeeded once serialized
        ids = [s.get("id") for s in tomllib.loads(cfg.read_text()).get("servers", [])]
        assert ids == ["editor"]  # coherent: only the editor's server, no failed-connect leftover
    finally:
        release.set()
        app.close()


def test_connect_local_swap_failure_rolls_back(tmp_path, monkeypatch):
    from herdeck.deckapp import onboarding

    sock = tmp_path / "herdr.sock"
    sock.touch()
    monkeypatch.setenv("HERDR_SOCKET", str(sock))
    cfg = str(tmp_path / "config.toml")
    monkeypatch.setenv("HERDECK_CONFIG", cfg)
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    closed = {"source": False, "runner": False}

    class _Runner:
        def close(self):
            closed["runner"] = True

    class _BadSource(MockSource):
        def close(self):
            closed["source"] = True

        @property
        def config(self):  # makes _prepare_swap raise (it reads config.grid first)
            raise ValueError("malformed")

    monkeypatch.setattr(srv, "_start_local_bridge", lambda sp: ("CFG", "SRV", _Runner()))
    monkeypatch.setattr(srv, "build_live_source_for_connect", lambda c, s: _BadSource())
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    prev = app._source
    try:
        _, body = _post(app, "/setup/connect", {"choice": "local"})
        assert body["ok"] is False
        assert app._source is prev  # prepare failed before commit, previous source intact
        assert closed["source"] and closed["runner"]  # neither leaks
        assert onboarding.read_choice(cfg) is None  # marker never persisted (prepare ran before it)
    finally:
        app._set_local_bridge(None)
        app.close()


def test_local_connect_reloader_rebuilds_live_source_keeping_bridge(tmp_path, monkeypatch):
    """A reload in local mode rebuilds the live source from disk against the
    RUNNING bridge — it must neither fall back to mock nor touch the bridge
    (audit: local-apply-reload)."""
    sock = tmp_path / "herdr.sock"
    sock.touch()
    monkeypatch.setenv("HERDR_SOCKET", str(sock))
    monkeypatch.setenv("HERDECK_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)

    class _Runner:
        bound = ("127.0.0.1", 4242, "tok")
        closed = False

        def close(self):
            self.closed = True

    runner = _Runner()
    built = []

    def _build(config, server):
        src = _DisconnectedSource()
        built.append(src)
        return src

    monkeypatch.setattr(srv, "_start_local_bridge", lambda sp: ("CFG", "SRV", runner))
    monkeypatch.setattr(srv, "build_live_source_for_connect", _build)
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        _, body = _post(app, "/setup/connect", {"choice": "local"})
        assert body["ok"] is True
        first = app._source
        app.reload()  # rebuild from disk against the running bridge
        assert app._source is not first  # a FRESH live source was adopted
        assert app._source is built[-1]  # ... built via the live-source builder
        assert runner.closed is False  # ... and the bridge was left running
    finally:
        app._set_local_bridge(None)
        app.close()


def test_demo_reload_respects_marker_over_remote_config(tmp_path, monkeypatch):
    # In demo mode a config-watch reload must NOT swap to a resolvable remote config:
    # _select_source goes through the (marker-aware) precedence, not bare select_live().
    from herdeck.deckapp import onboarding

    cfg = tmp_path / "config.toml"
    cfg.write_text('[[servers]]\nid = "herdr"\nurl = "ws://x:8788"\ntoken_env = "HERDECK_HERDR_TOKEN"\n')
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    fake.store[("herdeck", "HERDECK_HERDR_TOKEN")] = "tok"  # remote WOULD be usable
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    onboarding.write_choice(str(cfg), "demo")  # explicit demo choice
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    app._reloader = srv._reloader_for(app, ("mock",), srv._select_source)  # as a demo connect installs
    try:
        app.reload()  # a config-watch reload
        assert app._source.source_name == "mock"  # demo marker honored, NOT swapped to live remote
    finally:
        app.close()


def test_connect_local_bridge_start_failure(tmp_path, monkeypatch):
    sock = tmp_path / "herdr.sock"
    sock.touch()
    monkeypatch.setenv("HERDR_SOCKET", str(sock))
    monkeypatch.setenv("HERDECK_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)

    def _boom(socket_path):  # bridge fails to bind
        raise RuntimeError("bridge bind failed")

    monkeypatch.setattr(srv, "_start_local_bridge", _boom)
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    prev = app._source
    try:
        _, body = _post(app, "/setup/connect", {"choice": "local"})
        assert body["ok"] is False  # the bridge-start failure is caught, not propagated
        assert app._source is prev and app._local_bridge is None
    finally:
        app.close()


def test_connect_local_bridge_surfaces_snapshot_unsupported(tmp_path, monkeypatch):
    """A pre-0.7.2 herdr makes the embedded bridge raise RuntimeError(_SNAPSHOT_UNSUPPORTED)
    (bridge.py's hard floor). The route must pass that exact message through as the error,
    not mask it behind the generic 'could not start local source' (audit: version guidance
    must reach the desktop user)."""
    from herdeck.bridge import _SNAPSHOT_UNSUPPORTED

    sock = tmp_path / "herdr.sock"
    sock.touch()
    monkeypatch.setenv("HERDR_SOCKET", str(sock))
    monkeypatch.setenv("HERDECK_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)

    def _boom(socket_path):  # herdr too old: session.snapshot missing
        raise RuntimeError(_SNAPSHOT_UNSUPPORTED)

    monkeypatch.setattr(srv, "_start_local_bridge", _boom)
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    prev = app._source
    try:
        _, body = _post(app, "/setup/connect", {"choice": "local"})
        assert body["ok"] is False
        assert body["error"] == _SNAPSHOT_UNSUPPORTED
        assert app._source is prev and app._local_bridge is None
    finally:
        app.close()


def test_connect_remote_deduplicates_server_ids(tmp_path, monkeypatch):
    # A malformed config with TWO [[servers]] both id="herdr" must be collapsed to
    # exactly ONE entry (the new url) after connect — no leftover duplicate survives.
    import tomllib

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[[servers]]\nid = "herdr"\nurl = "ws://a:8788"\ntoken_env = "HERDECK_HERDR_TOKEN"\n'
        '[[servers]]\nid = "herdr"\nurl = "ws://b:8788"\ntoken_env = "HERDECK_HERDR_TOKEN"\n'
    )
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    monkeypatch.setattr(srv, "build_live_source_for_connect", lambda config, server: _DisconnectedSource())
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        status, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://new:8788", "token": "t", "id": "herdr"},
        )
        assert status == 200 and body["ok"] is True
        data = tomllib.loads(cfg.read_text())
        herdr_servers = [s for s in data.get("servers", []) if s.get("id") == "herdr"]
        assert len(herdr_servers) == 1, f"expected 1 herdr server, got {herdr_servers}"
        assert herdr_servers[0]["url"] == "ws://new:8788"
        # neither of the old duplicate urls must remain
        text = cfg.read_text()
        assert "ws://a" not in text and "ws://b" not in text
    finally:
        app.close()


def test_connect_bad_body_is_400(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDECK_CONFIG", str(tmp_path / "config.toml"))
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        try:
            _post(app, "/setup/connect", {"choice": "remote", "url": "", "token": ""})
            raise AssertionError("expected 400")
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        app.close()


def test_has_saved_remote_true_with_servers(tmp_path):
    import types

    from herdeck.deckapp import server as s

    cfg = tmp_path / "config.toml"
    cfg.write_text('[[servers]]\nid = "herdr"\nurl = "ws://1.2.3.4:8788"\ntoken_env = "HERDECK_HERDR_TOKEN"\n')
    svc = types.SimpleNamespace(_config_path=cfg)
    assert s._has_saved_remote(svc) is True


def test_has_saved_remote_false_when_mock_env(tmp_path, monkeypatch):
    import types

    from herdeck.deckapp import server as s

    cfg = tmp_path / "config.toml"
    cfg.write_text('[[servers]]\nid = "herdr"\nurl = "ws://1.2.3.4:8788"\ntoken_env = "HERDECK_HERDR_TOKEN"\n')
    monkeypatch.setenv("HERDECK_MOCK", "1")  # masked even with a real saved config
    svc = types.SimpleNamespace(_config_path=cfg)
    assert s._has_saved_remote(svc) is False


def test_has_saved_remote_false_without_servers(tmp_path, monkeypatch):
    import types

    from herdeck.deckapp import server as s

    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    missing = tmp_path / "config.toml"
    assert s._has_saved_remote(types.SimpleNamespace(_config_path=missing)) is False  # no file
    empty = tmp_path / "empty.toml"
    empty.write_text("[base]\n")  # parses, but no [[servers]]
    assert s._has_saved_remote(types.SimpleNamespace(_config_path=empty)) is False
    assert s._has_saved_remote(None) is False  # no config service


def test_setup_status_exposes_saved_remote_available(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    cfg = tmp_path / "config.toml"
    cfg.write_text('[[servers]]\nid = "herdr"\nurl = "ws://1.2.3.4:8788"\ntoken_env = "HERDECK_HERDR_TOKEN"\n')
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.setenv("HERDR_SOCKET", str(tmp_path / "nope.sock"))
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        _, body = _get(app, f"/setup?token={app.token}")
        assert body["saved_remote_available"] is True
    finally:
        app.close()


def _write_saved_config(tmp_path, monkeypatch):
    """A resolvable saved remote: config.toml with one server + its keychain token +
    a pre-existing demo marker (the trap we are escaping). Returns the config path."""
    cfg = tmp_path / "config.toml"
    cfg.write_text('[[servers]]\nid = "herdr"\nurl = "ws://10.0.0.5:8788"\ntoken_env = "HERDECK_HERDR_TOKEN"\n')
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    fake.store[("herdeck", "HERDECK_HERDR_TOKEN")] = "saved-tok"  # so select_live() resolves
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    from herdeck.deckapp import onboarding

    onboarding.write_choice(str(cfg), "demo")  # currently trapped in demo
    return str(cfg)


def test_connect_saved_swaps_live_and_clears_marker(tmp_path, monkeypatch):
    from herdeck.deckapp import onboarding

    cfg = _write_saved_config(tmp_path, monkeypatch)
    # Build a non-networked live source that reports connected=False (honest async dial).
    monkeypatch.setattr(srv, "build_live_source_for_connect", lambda config, server: _DisconnectedSource())
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    prev = app._source
    try:
        status, body = _post(app, "/setup/connect", {"choice": "saved"})
        assert status == 200 and body["ok"] is True
        assert body["connected"] is False  # honest: the just-built source isn't connected yet
        assert app._source is not prev  # swapped to the saved remote
        assert onboarding.read_choice(cfg) is None  # demo marker cleared
    finally:
        app.close()


def test_connect_saved_no_config_is_soft_error_keeps_marker(tmp_path, monkeypatch):
    from herdeck.deckapp import onboarding

    monkeypatch.setenv("HERDECK_CONFIG", str(tmp_path / "config.toml"))  # absent
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    onboarding.write_choice(str(tmp_path / "config.toml"), "demo")
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    prev = app._source
    try:
        status, body = _post(app, "/setup/connect", {"choice": "saved"})
        assert status == 200 and body["ok"] is False and body["error"] == "no saved connection"
        assert app._source is prev  # no swap
        assert onboarding.read_choice(str(tmp_path / "config.toml")) == "demo"  # marker untouched
    finally:
        app.close()


def test_connect_saved_build_failure_restores_marker(tmp_path, monkeypatch):
    from herdeck.deckapp import onboarding

    cfg = _write_saved_config(tmp_path, monkeypatch)

    def _boom(config, server):
        raise RuntimeError("connector blew up")

    monkeypatch.setattr(srv, "build_live_source_for_connect", _boom)
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    prev = app._source
    try:
        status, body = _post(app, "/setup/connect", {"choice": "saved"})
        assert status == 200 and body["ok"] is False
        assert "could not restore saved connection" in body["error"]
        assert app._source is prev  # previous source intact
        assert onboarding.read_choice(cfg) == "demo"  # marker restored (build failed before clear)
    finally:
        app.close()
