"""Tests for the herdeck.deckapp sidecar (slice 1: mock core).

Covers the deterministic MockSource, the DeckApp render/version pipeline (which
reuses the core Orchestrator + icons), and the token-authed loopback HTTP API.
Network is never touched: tests inject a StubIcons provider.
"""

import io
import json
import urllib.error
import urllib.request

import pytest
from PIL import Image

from herdeck.deckapp import DeckApp, MockSource, create_mock_app, demo_agents, mock_config
from herdeck.model import Status


class StubIcons:
    """Deterministic icon provider whose bytes depend on the tile's content, so
    identical tiles render identical bytes and changed tiles render different
    bytes (mirrors the web-driver test stub). Never hits the network."""

    def render_tile_bytes(self, tile):
        sig = (
            f"{tile.index}|{tile.label}|{tile.color}|{tile.spinner}"
            f"|{tile.status_text}|{tile.repo}|{tile.branch}|{tile.server_tag}"
        )
        buf = io.BytesIO()
        # encode a 1px image keyed by a stable colour derived from the signature
        c = sum(sig.encode()) % 256
        Image.new("RGB", (4, 4), (c, c, c)).save(buf, "PNG")
        return buf.getvalue()


def make_app(**kw):
    return DeckApp(MockSource(), serve=False, icon_provider=StubIcons(), **kw)


# --- MockSource determinism -------------------------------------------------


def test_demo_agents_is_deterministic():
    a = demo_agents()
    b = demo_agents()
    assert a == b  # no randomness, no time-based seeds
    assert 5 <= len(a) <= 8


def test_demo_agents_span_statuses_and_two_servers():
    agents = demo_agents()
    statuses = {s.status for s in agents}
    assert {Status.WORKING, Status.IDLE, Status.BLOCKED, Status.DONE} <= statuses
    servers = {s.key.server_id for s in agents}
    assert len(servers) == 2


def test_demo_agent_keys_are_unique():
    agents = demo_agents()
    keys = [s.key for s in agents]
    assert len(keys) == len(set(keys))


def test_mock_config_overview_order_matches_servers():
    cfg = mock_config()
    server_ids = {s.id for s in cfg.servers}
    assert set(cfg.overview_order) == server_ids
    assert len(server_ids) == 2


def test_mock_config_carries_no_real_secrets():
    cfg = mock_config()
    for s in cfg.servers:
        assert s.token == ""  # no real bridge token in mock


def test_mock_source_summary_counts_match_agents():
    src = MockSource()
    summary = src.summary()
    agents = src._agents
    assert summary["agents"] == len(agents)
    assert summary["blocked"] == sum(1 for s in agents if s.status is Status.BLOCKED)
    assert summary["working"] == sum(1 for s in agents if s.status is Status.WORKING)
    assert summary["idle"] == sum(1 for s in agents if s.status is Status.IDLE)
    assert summary["done"] == sum(1 for s in agents if s.status is Status.DONE)


def test_mock_source_is_connected_and_named_mock():
    src = MockSource()
    assert src.connected is True
    assert src.source_name == "mock"
    assert src.server_id is None  # never leak a bridge server id in mock


def test_mock_press_cycles_status_deterministically():
    src = MockSource()
    from herdeck.layout import order_agents

    ordered = order_agents(src._agents, mock_config().overview_order)
    before = ordered[0].status
    src.press(0)
    after = order_agents(src._agents, mock_config().overview_order)
    # the agent object pressed changed status (visual feedback), no randomness
    pressed = next(a for a in src._agents if a is ordered[0])
    assert pressed.status is not before
    assert after  # still renderable


def test_mock_press_out_of_range_is_ignored():
    src = MockSource()
    snapshot = [(a.key, a.status) for a in src._agents]
    src.press(-1)
    src.press(999)
    assert [(a.key, a.status) for a in src._agents] == snapshot


# --- DeckApp render / state pipeline ----------------------------------------


def test_state_has_required_shape():
    app = make_app()
    st = app._state()
    assert st["version"] >= 1
    assert st["slots"] == 13
    assert st["has_panel"] is True
    assert isinstance(st["panel"], int)
    assert isinstance(st["tiles"], dict) and st["tiles"]
    assert st["source"] == "mock"
    assert st["connected"] is True
    summ = st["summary"]
    assert set(summ) == {"agents", "blocked", "working", "idle", "done"}


def test_tiles_render_as_png_bytes():
    app = make_app()
    png = app._tile_png(0)
    assert png is not None and png[:4] == b"\x89PNG"
    assert app._panel_png()[:4] == b"\x89PNG"


def test_press_changing_state_bumps_version():
    app = make_app()
    v0 = app._state()["version"]
    app.press(0)  # cycles an agent's status -> tiles/summary change
    assert app._state()["version"] > v0


def test_press_updates_summary():
    app = make_app()
    s0 = app._state()["summary"]
    # press every agent tile once; the running totals must stay consistent
    for i in range(8):
        app.press(i)
    s1 = app._state()["summary"]
    assert s1["agents"] == s0["agents"]  # mock never adds/removes agents
    assert sum(s1[k] for k in ("blocked", "working", "idle", "done")) <= s1["agents"]


def test_press_out_of_range_does_not_raise():
    app = make_app()
    v0 = app._state()["version"]
    app.press(-1)
    app.press(9999)
    # crafted indices are ignored; nothing changes, nothing raises
    assert app._state()["version"] == v0


def test_create_mock_app_factory_builds_serving_app():
    app = create_mock_app(host="127.0.0.1", port=0, icon_provider=StubIcons())
    try:
        assert app.host == "127.0.0.1"
        assert app.port > 0
        assert isinstance(app.token, str) and app.token
    finally:
        app.close()


# --- token-authed HTTP API --------------------------------------------------


def _serving_app():
    return DeckApp(
        MockSource(), host="127.0.0.1", port=0, serve=True, icon_provider=StubIcons()
    )


def _get(app, path):
    return urllib.request.urlopen(f"http://{app.host}:{app.port}{path}", timeout=2)


def test_http_endpoints_require_token():
    app = _serving_app()
    try:
        for path in ("/state", "/panel", "/tile/0", "/health"):
            with pytest.raises(urllib.error.HTTPError) as exc:
                _get(app, path)
            assert exc.value.code == 403
    finally:
        app.close()


def test_http_state_returns_json_with_token():
    app = _serving_app()
    try:
        with _get(app, f"/state?token={app.token}") as r:
            assert r.status == 200
            assert r.headers.get("Content-Type", "").startswith("application/json")
            body = r.read().decode()
        data = json.loads(body)
        assert data["source"] == "mock"
        assert data["connected"] is True
        assert data["slots"] == 13
        assert app.token not in body  # never echo the token
    finally:
        app.close()


def test_http_tile_and_panel_serve_png():
    app = _serving_app()
    try:
        with _get(app, f"/tile/0?token={app.token}") as r:
            assert r.status == 200
            assert r.headers.get("Content-Type") == "image/png"
            assert r.read()[:4] == b"\x89PNG"
        with _get(app, f"/panel?token={app.token}") as r:
            assert r.status == 200
            assert r.headers.get("Content-Type") == "image/png"
            assert r.read()[:4] == b"\x89PNG"
    finally:
        app.close()


def test_http_health_reports_mock_without_token_in_body():
    app = _serving_app()
    try:
        with _get(app, f"/health?token={app.token}") as r:
            assert r.status == 200
            body = r.read().decode()
        data = json.loads(body)
        assert data["ok"] is True
        assert data["source"] == "mock"
        assert data["connected"] is True
        assert data["server_id"] is None
        assert app.token not in body
    finally:
        app.close()


def test_http_press_requires_token_then_returns_204():
    app = _serving_app()
    try:
        url = f"http://{app.host}:{app.port}/press/0"
        # no token -> 403
        req = urllib.request.Request(url, method="POST")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=2)
        assert exc.value.code == 403
        # bad token -> 403
        req = urllib.request.Request(url, method="POST", headers={"X-Herdeck-Token": "nope"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=2)
        assert exc.value.code == 403
        # valid token -> 204
        req = urllib.request.Request(url, method="POST", headers={"X-Herdeck-Token": app.token})
        with urllib.request.urlopen(req, timeout=2) as r:
            assert r.status == 204
    finally:
        app.close()


def test_http_press_out_of_range_index_is_safely_ignored():
    app = _serving_app()
    try:
        # a crafted out-of-range index must not 500; state must be unchanged
        with _get(app, f"/state?token={app.token}") as r:
            v0 = json.loads(r.read().decode())["version"]
        url = f"http://{app.host}:{app.port}/press/9999"
        req = urllib.request.Request(url, method="POST", headers={"X-Herdeck-Token": app.token})
        with urllib.request.urlopen(req, timeout=2) as r:
            assert r.status == 204
        with _get(app, f"/state?token={app.token}") as r:
            assert json.loads(r.read().decode())["version"] == v0
    finally:
        app.close()


def test_http_press_non_integer_index_returns_400():
    app = _serving_app()
    try:
        url = f"http://{app.host}:{app.port}/press/abc"
        req = urllib.request.Request(url, method="POST", headers={"X-Herdeck-Token": app.token})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=2)
        assert exc.value.code == 400
    finally:
        app.close()


def test_http_binds_loopback_only():
    app = _serving_app()
    try:
        assert app.host == "127.0.0.1"
    finally:
        app.close()


def test_package_imports_without_pillow_at_import_time():
    # The rendering stack (herdeck.icons -> Pillow) must be imported lazily, so a
    # base install can import the package and its mock surface without Pillow.
    import os
    import subprocess
    import sys

    src = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
    env = dict(os.environ, PYTHONPATH=src + os.pathsep + os.environ.get("PYTHONPATH", ""))
    code = (
        "import sys; import herdeck.deckapp;"
        "from herdeck.deckapp import demo_agents, MockSource;"
        "assert 'PIL' not in sys.modules, 'Pillow imported at package import time';"
        "print('ok')"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30, env=env
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


def test_main_binds_ipv4_loopback_only(monkeypatch):
    # Even if a bind env var is set to a non-IPv4-loopback value, the sidecar must
    # bind 127.0.0.1 (the server is IPv4 and the discovery URL is bracket-less).
    from herdeck.deckapp import __main__ as deckapp_main

    monkeypatch.setenv("HERDECK_DECKAPP_BIND", "::1")
    monkeypatch.setenv("HERDECK_DECKAPP_PORT", "0")
    captured = {}

    def fake_create(*, host, port):
        captured["host"] = host

        class _Stub:
            def __init__(self):
                self.host, self.port, self.token = host, 5555, "tok"

            def close(self):
                pass

        return _Stub()

    monkeypatch.setattr(deckapp_main, "create_mock_app", fake_create)
    # stop the blocking wait immediately
    monkeypatch.setattr(deckapp_main.threading.Event, "wait", lambda self: None)
    rc = deckapp_main.main()
    assert rc == 0
    assert captured["host"] == "127.0.0.1"


def test_error_body_is_browser_friendly_text():
    app = _serving_app()
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(app, "/state")
        assert exc.value.headers.get("Content-Type", "").startswith("text/plain")
        assert app.token not in exc.value.read().decode()
    finally:
        app.close()


# --- config HTTP routes (Task 6) --------------------------------------------


import json as _json


def _post(app, path, body, token=None):
    req = urllib.request.Request(
        f"http://{app.host}:{app.port}{path}",
        data=_json.dumps(body).encode(),
        method="POST",
    )
    req.add_header("X-Herdeck-Token", token if token is not None else app.token)
    return urllib.request.urlopen(req)


class _FakeSource:
    """A StateSource with a chosen grid, for testing swap_source mechanics."""

    source_name = "mock"
    connected = True
    server_id = None

    def __init__(self, grid):
        from herdeck.config import DEFAULT_PROFILES, Config, ServerConfig

        self._cfg = Config(
            servers=[ServerConfig("m", "ws://m", "x")],
            profiles=dict(DEFAULT_PROFILES),
            overview_order=["m"],
            grid=grid,
        )

    @property
    def config(self):
        return self._cfg

    def attach(self, orch, **kw):
        pass

    def apply_to(self, orch):
        pass

    def press(self, index):
        pass

    def summary(self):
        return {"agents": 0, "blocked": 0, "working": 0, "idle": 0, "done": 0}

    def close(self):
        pass


def test_swap_source_rebuilds_orchestrator_on_grid_change():
    from herdeck.deckapp.server import DeckApp

    app = DeckApp(_FakeSource((5, 3)), serve=False)
    assert app._slots == 5 * 3 - 2
    app.swap_source(_FakeSource((4, 3)))
    assert app._slots == 4 * 3 - 2  # orchestrator + slots rebuilt from the new config


def test_config_get_requires_token_and_returns_redacted(tmp_path, monkeypatch):
    from herdeck.deckapp.config_service import ConfigService

    monkeypatch.setenv("TOK", "real")
    (tmp_path / "config.toml").write_text(
        '[[servers]]\nid="local"\nurl="ws://x"\ntoken_env="TOK"\n[deck]\ngrid="5x3"\n'
    )
    svc = ConfigService(tmp_path / "config.toml", tmp_path / "local.toml")
    app = create_mock_app(port=0, config_service=svc)
    try:
        # Wrong token -> 403
        bad = urllib.request.Request(f"http://{app.host}:{app.port}/config?token=nope")
        try:
            urllib.request.urlopen(bad)
            assert False, "expected 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403
        # Right token -> redacted config
        ok = urllib.request.urlopen(f"http://{app.host}:{app.port}/config?token={app.token}")
        data = _json.loads(ok.read())
        assert data["base"]["deck"] == {"grid": "5x3"}
        assert data["secrets"]["TOK"]["set"] is True
        assert "real" not in ok.headers.get("X-Debug", "")  # value never leaks
    finally:
        app.close()


def test_config_post_writes_and_triggers_reload(tmp_path, monkeypatch):
    from herdeck.deckapp.config_service import ConfigService

    monkeypatch.setenv("TOK", "real")
    cfg = tmp_path / "config.toml"
    cfg.write_text('[[servers]]\nid="local"\nurl="ws://x"\ntoken_env="TOK"\n[deck]\ngrid="5x3"\n')
    svc = ConfigService(cfg, tmp_path / "local.toml")
    reloaded = []
    app = create_mock_app(port=0, config_service=svc, reloader=lambda: reloaded.append(1))
    try:
        body = {"base": {"servers": [{"id": "local", "url": "ws://x", "token_env": "TOK"}],
                         "deck": {"grid": "4x3"}}, "profiles": {}, "local": {}}
        resp = _post(app, "/config", body, token=app.token)
        assert _json.loads(resp.read())["errors"] == []
        assert reloaded == [1]
        assert 'grid = "4x3"' in cfg.read_text()
    finally:
        app.close()
