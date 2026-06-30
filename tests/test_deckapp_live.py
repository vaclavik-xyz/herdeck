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
from herdeck.deckapp.sinks import RenderFrame  # noqa: F401
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


class RecordingSink:
    def __init__(self):
        self.frames = []
        self.closed = False

    def deliver(self, frame):
        self.frames.append(frame)

    def close(self):
        self.closed = True


class SpinIcons:
    """Like StubIcons but its bytes depend on the spinner phase, so a tick that
    advances a working tile's phase changes that tile's PNG (and only that one)."""

    def render_tile_bytes(self, tile):
        sig = f"{tile.index}|{tile.status_text}|{tile.spinner}"
        buf = io.BytesIO()
        c = sum(sig.encode()) % 256
        Image.new("RGB", (4, 4), (c, c, c)).save(buf, "PNG")
        return buf.getvalue()


def make_live_icons(icons, *, serve=False, tick_interval=0.0):
    config, server = live_config()
    src = LiveSource(config, server)
    src.attach_runner(FakeRunner())
    app = DeckApp(src, serve=serve, icon_provider=icons, tick_interval=tick_interval)
    return app, src, server


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


# --- result-driven relist + stale-prompt invalidation (mirrors App) ---------


def test_non_read_result_triggers_relist():
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])
    runner.sent.clear()
    src._on_result("r5", {"ok": True})  # no "text" -> an act/send ack
    assert runner.sent == [{"type": "list"}]  # resync this server, like App


def _drill_with_detection(app, src, server, runner):
    app.press(0)  # drill into p0 -> focus + read
    read = [m for m in runner.sent if m["type"] == "read"][-1]
    src._on_result(read["req"], {"text": "1. Approve\n2. Deny", "pane_id": "p0"})
    runner.sent.clear()


def test_event_on_drilled_pane_invalidates_detection():
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])
    _drill_with_detection(app, src, server, runner)
    # the pane re-blocks (possibly a different prompt) -> stale options must clear
    src._on_event(server.id, agent(server.id, "p0", Status.BLOCKED, label="changed"))
    app.press(0)  # no actionable option remains
    assert runner.sent == []


def test_snapshot_changing_drilled_pane_invalidates_detection():
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])
    _drill_with_detection(app, src, server, runner)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED, label="changed")])
    app.press(0)
    assert runner.sent == []


def test_snapshot_not_changing_drilled_pane_keeps_detection():
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])
    _drill_with_detection(app, src, server, runner)
    # an unrelated agent appears; the drilled pane is unchanged -> options stay live
    src._on_snapshot(
        server.id,
        [agent(server.id, "p0", Status.BLOCKED), agent(server.id, "p1", Status.WORKING)],
    )
    app.press(0)
    assert len(runner.sent) == 1
    assert runner.sent[0]["type"] == "act"
    assert runner.sent[0]["keys"] == ["1"]


def test_snapshot_transition_is_atomic_under_deck_lock():
    # A live update must apply its buffer swap, invalidation and render as ONE
    # transition under the DeckApp lock, so a press (which holds the same lock)
    # cannot observe a half-applied update (new state buffered, prompt not yet
    # invalidated) and act on a stale prompt.
    import threading

    app, src, server, _ = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])

    started, done = threading.Event(), threading.Event()

    def deliver():
        started.set()
        src._on_snapshot(server.id, [agent(server.id, "p1", Status.WORKING)])
        done.set()

    with app._lock:  # stand in for a press/state holding the deck lock
        t = threading.Thread(target=deliver)
        t.start()
        started.wait(1)
        assert not done.wait(0.3)  # the whole transition blocks, not just the render
        with src._lock:
            buffered = set(src._agents)
        assert buffered == {AgentKey(server.id, "p0")}  # buffer swap is gated too
    t.join(2)
    assert done.is_set()
    with src._lock:
        assert set(src._agents) == {AgentKey(server.id, "p1")}


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

    monkeypatch.setattr(deckapp_main, "create_app", lambda *, host, port: _MainStub("mock"))
    monkeypatch.setattr(deckapp_main.threading.Event, "wait", lambda self: None)
    rc = deckapp_main.main()
    assert rc == 0
    data = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert data["source"] == "mock"
    assert data["host"] == "127.0.0.1"
    assert data["token"] == "loopback-token"


def test_main_uses_live_when_select_live_returns_target(monkeypatch, capsys):
    from herdeck.deckapp import __main__ as deckapp_main

    monkeypatch.setattr(deckapp_main, "create_app", lambda *, host, port: _MainStub("live"))
    monkeypatch.setattr(deckapp_main.threading.Event, "wait", lambda self: None)
    rc = deckapp_main.main()
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out.strip().splitlines()[-1])
    assert data["source"] == "live"
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


# --- background ticker (Task 1: herdeck-runtime-slice-a) --------------------


def test_tick_once_advances_spinner_phase():
    app, src, server = make_live_icons(StubIcons())
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    app.refresh()
    before = app._orch._phase
    app._tick_once()
    assert app._orch._phase == before + 1


def test_tick_once_animates_working_tile():
    app, src, server = make_live_icons(SpinIcons())
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    app.refresh()
    v = app._state()["version"]
    app._tick_once()
    assert app._state()["version"] > v  # the working tile re-keyed (spinner advanced)


def test_tick_once_quiet_when_all_idle():
    app, src, server = make_live_icons(SpinIcons())
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.IDLE)])
    app.refresh()
    v = app._state()["version"]
    app._tick_once()
    assert app._state()["version"] == v  # idle deck does not churn /state


def test_ticker_thread_not_started_without_serve_or_interval():
    app_a, _, _ = make_live_icons(StubIcons(), serve=False, tick_interval=0.0)
    app_b, _, _ = make_live_icons(StubIcons(), serve=False, tick_interval=0.4)
    assert app_a._ticker_thread is None  # tick_interval 0 -> no ticker
    assert app_b._ticker_thread is None  # serve=False -> no ticker (not actually serving)


def test_ticker_thread_runs_and_stops_when_serving():
    app, src, server = make_live_icons(SpinIcons(), serve=True, tick_interval=0.02)
    try:
        assert app._ticker_thread is not None and app._ticker_thread.is_alive()
    finally:
        app.close()
    assert not app._ticker_thread or not app._ticker_thread.is_alive()


def test_create_live_app_enables_ticker_from_config():
    config, server = live_config()
    app = create_live_app(config, server, serve=False, connector_factory=FakeConnector)
    assert app._tick_interval == config.hardware.tick_interval
    assert config.hardware.tick_interval == 0.4  # the live default that drives animation


# --- render-sink fan-out (Task 1: herdeck-runtime-slice-b) -------------------


def test_add_sink_delivers_initial_full_frame():
    app, src, server = make_live_icons(StubIcons())
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    app.refresh()
    sink = RecordingSink()
    app.add_sink(sink)
    assert len(sink.frames) == 1
    assert sink.frames[0].full is True
    assert sink.frames[0].render is not None  # the RenderState was handed over


def test_press_delivers_full_frame_to_sink():
    app, src, server = make_live_icons(StubIcons())
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.IDLE)])
    sink = RecordingSink()
    app.add_sink(sink)
    sink.frames.clear()
    app.press(0)
    assert sink.frames and sink.frames[-1].full is True


def test_tick_delivers_working_frame_with_indices():
    app, src, server = make_live_icons(StubIcons())
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    sink = RecordingSink()
    app.add_sink(sink)
    sink.frames.clear()
    app._tick_once()
    frame = sink.frames[-1]
    assert frame.full is False
    assert frame.working == [0]  # the single working agent is tile 0


def test_full_refresh_tick_delivers_exactly_one_full_frame():
    app, src, server = make_live_icons(StubIcons())
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    sink = RecordingSink()
    app.add_sink(sink)
    sink.frames.clear()
    for _ in range(app.FULL_REFRESH_TICKS):
        app._tick_once()
    fulls = [f for f in sink.frames if f.full]
    assert len(fulls) == 1  # one full frame at the Nth tick, working frames otherwise


def test_failing_sink_does_not_break_http_or_other_sinks():
    app, src, server = make_live_icons(StubIcons())
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])

    class BoomSink:
        def deliver(self, frame):
            raise RuntimeError("boom")

        def close(self):
            pass

    good = RecordingSink()
    app.add_sink(BoomSink())
    app.add_sink(good)
    good.frames.clear()
    v = app._state()["version"]
    app._tick_once()  # must NOT raise despite BoomSink
    assert app._state()["version"] >= v  # HTTP buffer still advanced
    assert good.frames  # the healthy sink still got its frame


def test_close_closes_sinks():
    app, src, server = make_live_icons(StubIcons())
    sink = RecordingSink()
    app.add_sink(sink)
    app.close()
    assert sink.closed is True


def test_swap_source_fans_full_frame_to_sinks():
    """swap_source must immediately repaint every registered sink (Finding 1)."""
    app, src, server = make_live_icons(StubIcons())
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    sink = RecordingSink()
    app.add_sink(sink)
    sink.frames.clear()  # discard the initial paint from add_sink

    # Build a second live source the same way make_live_icons does.
    config2, server2 = live_config()
    new_src = LiveSource(config2, server2)
    new_src.attach_runner(FakeRunner())

    app.swap_source(new_src)
    assert sink.frames, "swap_source must fan out at least one frame"
    assert sink.frames[-1].full is True


def test_close_detaches_sinks_before_closing():
    """close() must detach the sink list under the lock before calling close() on each
    sink (Finding 2: race-free teardown)."""
    app, src, server = make_live_icons(StubIcons())
    sink = RecordingSink()
    app.add_sink(sink)
    app.close()
    assert sink.closed is True          # sink was closed
    assert app._sinks == []             # detached
