"""Tests for the herdeck.deckapp LiveSource (slice 4: live source + mock/live switch).

The live path is exercised with a FAKE connector/runner — no real bridge, no
network, no asyncio loop. We drive the connector callbacks directly (the seam the
real Connector uses) and capture sends through a fake runner. Covers:

* snapshot / event -> orchestrator (the deck reflects live state),
* press -> Command -> connector.send (orchestrator translates the press),
* offline handling (connected:false + reuse of the OFFLINE overview panel),
* the mock/live switch by config + token presence,
* the bridge token never leaking into /state, /health or the source surface.
"""

import io
import json

from PIL import Image

from herdeck.config import DEFAULT_PROFILES, Config, ServerConfig
from herdeck.deckapp import DeckApp
from herdeck.deckapp.live import LiveSource, build_live_source
from herdeck.deckapp.server import create_app, create_live_app, select_live
from herdeck.model import AgentKey, AgentState, Status
from herdeck.orchestrator import Orchestrator

SECRET = "super-secret-bridge-token-xyz"


class StubIcons:
    """Deterministic, content-keyed icon provider (mirrors the slice-1 stub); it
    never touches the network or Pillow's font stack beyond a 4px fill."""

    def render_tile_bytes(self, tile):
        sig = f"{tile.index}|{tile.label}|{tile.color}|{tile.status_text}"
        buf = io.BytesIO()
        c = sum(sig.encode()) % 256
        Image.new("RGB", (4, 4), (c, c, c)).save(buf, "PNG")
        return buf.getvalue()


class FakeRunner:
    """Captures the wire messages a press would send to the bridge, synchronously
    (no asyncio). Each send is recorded exactly once — there is no retry."""

    def __init__(self, connector=None):
        self.connector = connector
        self.sent: list[dict] = []
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def send(self, msg):
        self.sent.append(msg)

    def close(self):
        self.closed = True
        if self.connector is not None and hasattr(self.connector, "stop"):
            self.connector.stop()


class FakeConnector:
    """Records the callbacks wired by build_live_source so a test can deliver a
    snapshot/event/connection exactly as the real Connector would, with no WS."""

    instances: list["FakeConnector"] = []

    def __init__(self, server, on_snapshot, on_event, on_connection, **kw):
        self.server = server
        self.on_snapshot = on_snapshot
        self.on_event = on_event
        self.on_connection = on_connection
        self.stopped = False
        FakeConnector.instances.append(self)

    async def run(self):
        # Real ConnectorRunner awaits this on its loop; return at once so the
        # background thread exits immediately and never touches the network.
        return

    async def send(self, msg):
        return

    def stop(self):
        self.stopped = True


def live_config(token=SECRET):
    server = ServerConfig(id="prod", url="ws://bridge.local:8765", token=token)
    config = Config(
        servers=[server],
        profiles=dict(DEFAULT_PROFILES),
        overview_order=["prod"],
        grid=(5, 3),
    )
    return config, server


def agent(sid, pane, status, agent_type="claude", label=None):
    return AgentState(
        AgentKey(sid, pane), agent_type, label or pane, status, repo=pane, branch="main"
    )


def make_live(token=SECRET):
    config, server = live_config(token)
    src = LiveSource(config, server)
    runner = FakeRunner()
    src.attach_runner(runner)
    app = DeckApp(src, serve=False, icon_provider=StubIcons())
    return app, src, server, runner


# --- snapshot / event -> orchestrator ---------------------------------------


def test_snapshot_feeds_orchestrator_and_state():
    app, src, server, _ = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(
        server.id,
        [
            agent(server.id, "p0", Status.WORKING),
            agent(server.id, "p1", Status.BLOCKED),
            agent(server.id, "p2", Status.IDLE),
        ],
    )
    app.refresh()
    st = app._state()
    assert st["source"] == "live"
    assert st["connected"] is True
    assert st["summary"]["agents"] == 3
    assert st["summary"]["blocked"] == 1
    assert st["summary"]["working"] == 1
    assert st["summary"]["idle"] == 1


def test_event_updates_a_single_agent():
    app, src, server, _ = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(
        server.id, [agent(server.id, "p0", Status.WORKING), agent(server.id, "p1", Status.IDLE)]
    )
    src._on_event(server.id, agent(server.id, "p0", Status.BLOCKED))
    app.refresh()
    summ = app._state()["summary"]
    assert summ["agents"] == 2
    assert summ["blocked"] == 1
    assert summ["working"] == 0


def test_snapshot_replaces_previous_state():
    app, src, server, _ = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, f"p{i}", Status.WORKING) for i in range(4)])
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.IDLE)])
    app.refresh()
    assert app._state()["summary"]["agents"] == 1


# --- press -> Command -> connector.send -------------------------------------


def test_press_agent_sends_focus_then_read():
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    app.refresh()
    app.press(0)  # drill into the only agent tile
    assert [m["type"] for m in runner.sent] == ["focus", "read"]
    assert all(m["pane_id"] == "p0" for m in runner.sent)


def test_drill_macro_press_sends_text_command():
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    app.refresh()
    app.press(0)  # overview -> drill (focus + read)
    runner.sent.clear()
    app.press(0)  # drill -> first quick-send macro
    assert len(runner.sent) == 1
    msg = runner.sent[0]
    assert msg["type"] == "send_text"
    assert msg["pane_id"] == "p0"
    assert msg["text"]  # the macro body


def test_press_translates_via_orchestrator_on_press():
    # No agents -> pressing the launcher tile opens the launcher; no bridge send.
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    app.refresh()
    app.press(12)  # the reserved launcher tile (slots-1)
    assert runner.sent == []


def test_press_is_noop_without_a_runner():
    # A press before the runner is attached must never raise (defensive wiring).
    config, server = live_config()
    src = LiveSource(config, server)
    orch = Orchestrator(src.config, slots=13)
    src.attach(orch)
    src.press(0)  # no runner -> silently ignored, no crash


# --- live updates re-render the deck (no manual poll) -----------------------


def test_snapshot_callback_refreshes_deck_without_manual_refresh():
    app, src, server, _ = make_live()
    v0 = app._state()["version"]
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    # No manual app.refresh(): the live callback must re-render the DeckApp itself,
    # otherwise /state keeps serving stale tile versions.
    st = app._state()
    assert st["version"] > v0
    assert st["summary"]["agents"] == 1
    assert st["connected"] is True


def test_event_callback_refreshes_deck_without_manual_refresh():
    app, src, server, _ = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    v1 = app._state()["version"]
    src._on_event(server.id, agent(server.id, "p0", Status.BLOCKED))
    st = app._state()
    assert st["version"] > v1
    assert st["summary"]["blocked"] == 1


def test_connection_drop_callback_refreshes_deck():
    app, src, server, _ = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    assert app._state()["connected"] is True
    src._on_connection(server.id, False)  # no manual refresh
    assert app._state()["connected"] is False


# --- read result -> detection enables the blocked-approve drill --------------


def test_read_result_enables_approve_on_blocked_agent():
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])
    app.press(0)  # drill into the blocked agent -> focus + read
    read = [m for m in runner.sent if m["type"] == "read"][-1]
    runner.sent.clear()
    # the bridge answers the read with the prompt text
    src._on_result(read["req"], {"text": "1. Approve\n2. Deny", "pane_id": "p0"})
    app.press(0)  # the first option is now actionable
    assert len(runner.sent) == 1
    msg = runner.sent[0]
    assert msg["type"] == "act"
    assert msg["pane_id"] == "p0"
    assert msg["keys"] == ["1"]
    assert msg["guard"] is True  # act_if_blocked -> guarded send


def test_stale_read_result_is_ignored():
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])
    app.press(0)  # drill -> focus + read
    runner.sent.clear()
    # a result for a request we never issued must not populate detection
    src._on_result("r999", {"text": "1. Approve", "pane_id": "p0"})
    app.press(0)  # no options -> blank tile -> nothing sent
    assert runner.sent == []


# --- non-bridge commands (switch_profile) must not crash the press ------------


def test_press_skips_local_only_commands(monkeypatch):
    from herdeck.commands import Command

    app, src, server, runner = make_live()
    monkeypatch.setattr(
        src._orch,
        "on_press",
        lambda i: [Command("focus", server.id, "p0"), Command("switch_profile", "x", text="x")],
    )
    src.press(0)  # switch_profile is local-only -> skipped, not sent, no crash
    assert [m["type"] for m in runner.sent] == ["focus"]


# --- offline handling -------------------------------------------------------


def test_offline_sets_connected_false_in_state():
    app, src, server, _ = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    app.refresh()
    assert app._state()["connected"] is True
    src._on_connection(server.id, False)  # bridge drops
    app.refresh()
    assert app._state()["connected"] is False


def test_offline_renders_offline_overview_panel():
    config, server = live_config()
    src = LiveSource(config, server)
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    src._on_connection(server.id, False)
    orch = Orchestrator(src.config, slots=13)
    src.apply_to(orch)
    panel = orch.render().panel
    assert panel.title == "OFFLINE"  # reuses layout.panel_overview offline path
    assert src.connected is False


def test_live_source_starts_disconnected():
    config, server = live_config()
    src = LiveSource(config, server)
    assert src.connected is False  # nothing live until the connector says so


# --- secret hygiene: the bridge token never leaks ---------------------------


def test_token_absent_from_state_and_health():
    app, src, server, _ = make_live(token=SECRET)
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    app.refresh()
    blob = json.dumps(app._state()) + json.dumps(app._health())
    assert SECRET not in blob
    assert app._health()["server_id"] == "prod"  # only the non-secret id
    assert app.source_name == "live"


def test_server_id_is_id_not_token():
    config, server = live_config(token=SECRET)
    src = LiveSource(config, server)
    assert src.server_id == "prod"
    assert SECRET not in str(src.server_id)


# --- mock/live switch by config + token presence ----------------------------


def _write_config(tmp_path, *, token_env="TEST_BRIDGE_TOKEN"):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[[servers]]\n"
        'id = "prod"\n'
        'url = "ws://bridge.local:8765"\n'
        f'token_env = "{token_env}"\n\n'
        "[profiles.default]\n"
        'servers = ["prod"]\n'
    )
    return cfg


def test_select_live_returns_config_when_server_and_token_present(tmp_path, monkeypatch):
    cfg = _write_config(tmp_path)
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.setenv("TEST_BRIDGE_TOKEN", SECRET)
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    selected = select_live()
    assert selected is not None
    config, server = selected
    assert server.id == "prod"
    assert server.token == SECRET


def test_select_live_falls_back_to_mock_without_token(tmp_path, monkeypatch):
    cfg = _write_config(tmp_path)
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("TEST_BRIDGE_TOKEN", raising=False)  # token env unset
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    assert select_live() is None


def test_select_live_falls_back_to_mock_without_config(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDECK_CONFIG", raising=False)
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    monkeypatch.chdir(tmp_path)  # no config.toml here
    assert select_live() is None


def test_select_live_honours_mock_env(tmp_path, monkeypatch):
    cfg = _write_config(tmp_path)
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.setenv("TEST_BRIDGE_TOKEN", SECRET)
    monkeypatch.setenv("HERDECK_MOCK", "1")  # explicit mock wins
    assert select_live() is None


def test_create_app_falls_back_to_mock(monkeypatch):
    monkeypatch.setenv("HERDECK_MOCK", "1")
    app = create_app(serve=False, icon_provider=StubIcons())
    try:
        assert app.source_name == "mock"
        assert app._state()["source"] == "mock"
    finally:
        app.close()


# --- build_live_source wiring (fake connector, no network) ------------------


def test_build_live_source_wires_callbacks_and_starts_runner():
    FakeConnector.instances.clear()
    config, server = live_config()
    captured = {}

    def runner_factory(conn):
        runner = FakeRunner(conn)
        captured["runner"] = runner
        return runner

    src = build_live_source(
        config, server, connector_factory=FakeConnector, runner_factory=runner_factory
    )
    conn = FakeConnector.instances[-1]
    assert conn.server is server
    assert captured["runner"].started is True
    assert src.source_name == "live"
    assert src.server_id == "prod"

    # delivering a snapshot through the wired callback updates the source
    conn.on_connection(server.id, True)
    conn.on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])
    assert src.connected is True
    assert src.summary()["blocked"] == 1

    src.close()
    assert conn.stopped is True


# --- __main__ entry wiring (mock/live switch) -------------------------------


class _MainStub:
    def __init__(self, name):
        self.host = "127.0.0.1"
        self.port = 4321
        self.token = "loopback-token"
        self.source_name = name

    def close(self):
        pass


def test_main_uses_mock_when_select_live_returns_none(monkeypatch, capsys):
    from herdeck.deckapp import __main__ as deckapp_main

    monkeypatch.setattr(deckapp_main, "select_live", lambda: None)
    monkeypatch.setattr(deckapp_main, "create_mock_app", lambda *, host, port: _MainStub("mock"))
    monkeypatch.setattr(deckapp_main.threading.Event, "wait", lambda self: None)
    rc = deckapp_main.main()
    assert rc == 0
    data = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert data["source"] == "mock"
    assert data["host"] == "127.0.0.1"
    assert data["token"] == "loopback-token"


def test_main_uses_live_when_select_live_returns_target(monkeypatch, capsys):
    from herdeck.deckapp import __main__ as deckapp_main

    config, server = live_config(token=SECRET)
    monkeypatch.setattr(deckapp_main, "select_live", lambda: (config, server))
    captured = {}

    def fake_live(cfg, srv, *, host, port):
        captured["cfg"], captured["srv"] = cfg, srv
        return _MainStub("live")

    monkeypatch.setattr(deckapp_main, "create_live_app", fake_live)
    monkeypatch.setattr(deckapp_main.threading.Event, "wait", lambda self: None)
    rc = deckapp_main.main()
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out.strip().splitlines()[-1])
    assert data["source"] == "live"
    assert captured["srv"] is server
    assert SECRET not in out  # the discovery line never carries the bridge token


def test_create_live_app_reports_live_without_network():
    FakeConnector.instances.clear()
    config, server = live_config()
    app = create_live_app(
        config,
        server,
        serve=False,
        icon_provider=StubIcons(),
        connector_factory=FakeConnector,
    )
    try:
        assert app.source_name == "live"
        assert app._health()["server_id"] == "prod"
        assert SECRET not in json.dumps(app._health())
    finally:
        app.close()
