from herdeck.config import AnswerProfile, Config, ConfigMeta, ServerConfig
from herdeck.model import AgentKey, AgentState, Status
from herdeck.orchestrator import Orchestrator


def _config(management="launcher_menu", profile_names=("default",)):
    cfg = Config(
        servers=[ServerConfig("dev", "wss://x", "t")],
        profiles={"default": AnswerProfile(["enter"], ["esc"], ["ctrl+c"], ["enter"])},
        overview_order=["dev"],
        grid=(5, 3),
        meta=ConfigMeta(profile_names=list(profile_names)),
    )
    cfg.view.management = management
    return cfg


def _agent(pane, status=Status.IDLE):
    return AgentState(AgentKey("dev", pane), "claude", "api", status)


def test_tileview_section_defaults_none():
    from herdeck.driver.base import TileView
    assert TileView(0, "x", "grey").section is None


def test_overview_agent_tile_section_is_view():
    o = Orchestrator(_config(), slots=13)
    o.apply_snapshot("dev", [_agent("p1")])
    assert o.render().tiles[0].section == "view"


def test_overview_launcher_tile_section_is_start_profiles():
    o = Orchestrator(_config(), slots=13)  # launcher_menu → tile slots-1 is "+ New"
    assert o.render().tiles[12].section == "start_profiles"


def test_overview_empty_tile_has_no_section():
    o = Orchestrator(_config(), slots=13)
    assert o.render().tiles[0].section is None


def test_launcher_menu_type_tiles_section_is_start_profiles():
    o = Orchestrator(_config(), slots=13)
    o.on_press(12)  # enter launcher via "+ New"
    assert o.render().tiles[0].section == "start_profiles"


def test_profile_menu_tiles_section_is_profiles():
    o = Orchestrator(_config(profile_names=("default", "work")), slots=13)
    o.on_press(12)  # enter launcher; entries = start types + "Profiles" at index 5
    o.on_press(5)   # press "Profiles" → profile menu
    assert o.render().tiles[0].section == "profiles"


def test_drill_tiles_section_is_answer_profiles():
    o = Orchestrator(_config(), slots=13)
    o.apply_snapshot("dev", [_agent("p1", Status.BLOCKED)])
    o.on_press(0)  # press the agent tile → drill
    sections = {t.section for t in o.render().tiles if t.section}
    assert sections == {"answer_profiles"}


def test_management_bottom_row_tiles_tagged():
    o = Orchestrator(_config(management="bottom_row"), slots=13)
    o.apply_snapshot("dev", [_agent("p1")])
    tagged = {t.section for t in o.render().tiles if t.section in ("profiles", "start_profiles")}
    assert tagged  # management controls carry profiles/start_profiles

