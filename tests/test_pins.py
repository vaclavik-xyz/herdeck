from dataclasses import replace

from test_orchestrator import make_config

from herdeck.model import AgentKey, AgentState, Status
from herdeck.orchestrator import Orchestrator
from herdeck.pins import PinStore


def test_pins_survive_rank_changes_missing_agents_and_restart(tmp_path):
    store = PinStore(tmp_path / "pins.json")
    key = AgentKey("dev", "b")
    store.save("default", {1: key})
    store.save("other", {0: AgentKey("dev", "c")})
    o = Orchestrator(make_config(), slots=5, clock=lambda: 100)
    o.pins = store.load("default")
    a = AgentState(AgentKey("dev", "a"), "codex", "A", Status.IDLE)
    b = AgentState(key, "codex", "B", Status.IDLE)
    o.apply_snapshot("dev", [a, b])
    assert o.render().tiles[1].repo == "B"
    o.apply_snapshot("dev", [replace(a, status=Status.BLOCKED), replace(b, status=Status.DONE)])
    assert o.render().tiles[1].repo == "B"
    assert o.render().tiles[1].pinned
    o.apply_snapshot("dev", [a])
    assert "missing" in o.render().tiles[1].label
    assert o.tick() == []
    o.confirm_rendered_preview()
    assert o.agent_for_preview(1) is None
    o.apply_snapshot("dev", [a, b])
    assert o.render().tiles[1].repo == "B"
    assert store.load("other") == {0: AgentKey("dev", "c")}


def test_pin_detail_command_and_page_position():
    o = Orchestrator(make_config(), slots=5, clock=lambda: 100)
    agents = [AgentState(AgentKey("dev", str(i)), "codex", str(i), Status.IDLE) for i in range(7)]
    o.apply_snapshot("dev", agents)
    o._page = 1
    o.render()
    o.on_press(2)
    assert o.drill_key() == agents[6].key
    cmd = o.on_press(2)[0]  # fixed pin button: slots - 3
    assert cmd.kind == "toggle_pin"
    assert cmd.payload["position"] == 6
    o.toggle_pin(agents[6].key, 6)
    assert o.render().tiles[2].label == "Unpin"
    o.on_press(4)
    assert o.render().tiles[2].repo == "6"


def test_app_persists_pin_without_sending_backend_commands(tmp_path):
    from herdeck.app import App
    from herdeck.driver.fake import FakeRenderer

    sent = []
    store = PinStore(tmp_path / "pins.json")
    app = App(make_config(), FakeRenderer(13), sent.append, pin_store=store)
    key = AgentKey("dev", "a")
    app.orch.apply_snapshot("dev", [AgentState(key, "codex", "A", Status.IDLE)])
    app.orch.render()
    app._handle_press(0)
    sent.clear()
    app._handle_press(10)
    assert sent == []
    assert store.load("default") == {0: key}
    restored = App(make_config(), FakeRenderer(13), sent.append, pin_store=store)
    assert restored.orch.pins == {0: key}
    app._handle_press(10)
    assert store.load("default") == {}


def test_thread_title_preset_keeps_t3_project_as_secondary():
    cfg = make_config()
    cfg.view.tile_primary = ["tab"]
    cfg.view.tile_secondary = ["repo"]
    o = Orchestrator(cfg, slots=13)
    a = AgentState(
        AgentKey("dev", "a"),
        "codex",
        "/Users/admin/projects/app",
        Status.IDLE,
        backend="t3",
        title="Fix sign in",
        project="My App",
    )
    o.apply_snapshot("dev", [a])
    tile = o.render().tiles[0]
    assert (tile.repo, tile.branch) == ("Fix sign in", "My App")
    o.apply_snapshot("dev", [replace(a, title="Renamed thread")])
    assert o.render().tiles[0].repo == "Renamed thread"


def test_unnamed_thread_preset_shows_project_only_once():
    from herdeck import layout
    agent = AgentState(AgentKey("dev", "a"), "codex", "/projects/app", Status.IDLE,
                       backend="t3", project="My App", title="")
    assert layout.compose_tile_lines(agent, ["tab"], ["repo"]) == ("My App", "")
    # Explicitly hidden titles and secondary-only tab fields stay hidden.
    assert layout.compose_tile_lines(agent, [], ["tab"]) == ("", "")
