# tests/test_deckapp_setup_routes.py
import json
import urllib.request

from herdeck.deckapp import server as srv


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


def test_setup_status_requires_token(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDECK_CONFIG", str(tmp_path / "config.toml"))
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        import urllib.error

        try:
            _get(app, "/setup?token=wrong")
            raise AssertionError("expected 403")
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        app.close()
