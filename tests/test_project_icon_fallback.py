from types import SimpleNamespace

import pytest

from herdeck.config import PROJECT_ICON_FALLBACKS, ConfigError, ViewConfig
from herdeck.driver.base import PanelAgent, ThemeEnv
from herdeck.project_icons import ProjectIconStore, tile_icon_fields
from herdeck.settings import _view_config
from herdeck.themes.api import ThemeContext


def _state(repo="ledger", icon=""):
    return SimpleNamespace(repo=repo, project="", label=repo, project_icon=icon)


def test_default_is_monogram():
    assert PROJECT_ICON_FALLBACKS == ("monogram", "agent")
    assert ViewConfig().project_icon_fallback == "monogram"


def test_settings_accepts_and_rejects():
    assert _view_config({"project_icon_fallback": "agent"}).project_icon_fallback == "agent"
    with pytest.raises(ConfigError):
        _view_config({"project_icon_fallback": "emoji"})


@pytest.mark.parametrize("mode", ["project", "both"])
def test_no_favicon_with_agent_fallback_renders_as_agent_tile(mode):
    view = ViewConfig(tile_icon=mode, project_icon_fallback="agent")
    fields = tile_icon_fields(view, _state(), ProjectIconStore())
    assert fields == {"tile_icon": "agent", "project_icon": None, "project_name": ""}


@pytest.mark.parametrize("mode", ["project", "both"])
def test_no_favicon_with_monogram_fallback_is_unchanged(mode):
    view = ViewConfig(tile_icon=mode)
    fields = tile_icon_fields(view, _state(), ProjectIconStore())
    assert fields == {"tile_icon": mode, "project_icon": None, "project_name": "ledger"}


def test_theme_context_agent_fallback_for_panel_queue():
    ctx = ThemeContext(ThemeEnv(project_icon_fallback="agent"))
    item = PanelAgent(label="ledger", agent_type="codex", project_icon=None, project_name="ledger")
    got = ctx.project_mark(item, 40)
    want = ctx.agent_mark("codex", 40)
    assert got.size == (40, 40)
    assert got.tobytes() == want.tobytes()


def test_theme_context_monogram_fallback_by_default():
    ctx = ThemeContext(ThemeEnv())
    item = PanelAgent(label="ledger", agent_type="codex", project_icon=None, project_name="ledger")
    assert ctx.project_mark(item, 40).tobytes() != ctx.agent_mark("codex", 40).tobytes()
