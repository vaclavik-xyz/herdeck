"""Editable workspace names remain display-only across both physical render paths."""

from dataclasses import replace

import pytest

from herdeck.config import DEFAULT_PROFILES, Config
from herdeck.elgato.session import ElgatoSession
from herdeck.layout import compose_line, compose_tile_lines
from herdeck.model import AgentKey, AgentState, Status
from herdeck.orchestrator import Orchestrator
from herdeck.settings import _view_config


def agent(**kwargs):
    return replace(
        AgentState(
            AgentKey("dev", "w1:p1"), "codex", "crm", Status.IDLE,
            repo="macdoktor-crm", workspace="macDoktor CRM", title="Opravit přihlášení",
            terminal_id="stable-terminal",
        ),
        **kwargs,
    )


@pytest.mark.parametrize("workspace,repo,label,expected", [
    ("  macDoktor CRM  ", "repo", "pane", "macDoktor CRM"),
    ("", "repo", "pane", "repo"),
    (" \t ", "repo", "pane", "repo"),
    ("", "", "pane", "pane"),
    ("", "", "", ""),
    ("Příliš žluťoučký kůň – zákaznická podpora", "repo", "pane",
     "Příliš žluťoučký kůň – zákaznická podpora"),
])
def test_herdr_project_name_fallback(workspace, repo, label, expected):
    state = agent(workspace=workspace, repo=repo, label=label)
    assert compose_line(state, ["project"]) == expected


@pytest.mark.parametrize("project,repo,expected", [
    ("Přejmenovaný projekt", "/Users/me/original/", "Přejmenovaný projekt"),
    (" \t", "/Users/me/original/", "original"),
    ("", "", "crm"),
])
def test_t3_project_name_uses_backend_title_then_path_basename(project, repo, expected):
    state = agent(backend="t3", project=project, repo=repo, workspace="")
    assert compose_line(state, ["project"]) == expected


def test_explicit_repo_token_retains_its_existing_meaning():
    state = agent()
    assert compose_line(state, ["repo"]) == "macdoktor-crm"
    assert compose_line(state, ["project", "repo"]) == "macDoktor CRM · macdoktor-crm"
    # Existing T3 custom layouts keep their historical display-name behavior.
    assert compose_line(agent(backend="t3", project="T3 project"), ["repo"]) == "T3 project"


def test_unnamed_thread_falls_back_to_workspace_without_duplicate_line():
    assert compose_tile_lines(agent(tab=""), ["tab"], ["project"]) == ("macDoktor CRM", "")


def test_project_token_is_accepted_by_settings_validation():
    view = _view_config({"tile_primary": ["project"], "tile_secondary": ["title"]})
    assert view.tile_primary == ["project"]
    assert view.tile_secondary == ["title"]


class TextIcons:
    def render_tile_bytes(self, tile):
        return f"{tile.repo}|{tile.branch}".encode()


@pytest.fixture(params=["d200", "elgato"])
def deck(request):
    config = Config(servers=[], profiles=dict(DEFAULT_PROFILES), overview_order=["dev"], grid=(5, 3))
    if request.param == "d200":
        instance = Orchestrator(config, slots=13)
        return instance, lambda: instance.render().tiles
    instance = ElgatoSession(config, TextIcons())
    instance.set_slots([("s0", (0, 0)), ("s1", (1, 0))])
    return instance, lambda: [instance._slot_tile(i) for i in range(2)]


def test_workspace_rename_changes_heading_preserving_session_title_and_identity(deck):
    instance, tiles = deck
    original = agent()
    instance.apply_snapshot("dev", [original])
    assert (tiles()[0].repo, tiles()[0].branch) == ("macDoktor CRM", original.title)
    renamed = replace(original, workspace="Servis – zákazníci")
    instance.apply_event("dev", renamed)
    assert (tiles()[0].repo, tiles()[0].branch) == ("Servis – zákazníci", original.title)
    assert (renamed.key, renamed.terminal_id, renamed.repo) == (
        original.key, original.terminal_id, "macdoktor-crm",
    )
    # A reconnect snapshot carries the current name, without a rename event replay.
    instance.apply_snapshot("dev", [renamed])
    assert tiles()[0].repo == "Servis – zákazníci"


def test_duplicate_project_names_keep_distinct_herdr_and_t3_tiles(deck):
    instance, tiles = deck
    herdr = agent()
    t3 = agent(key=AgentKey("dev", "thread-2"), backend="t3", workspace="",
               project="macDoktor CRM", repo="/projects/another-repo", title="T3 task")
    instance.apply_snapshot("dev", [herdr, t3])
    shown = tiles()[:2]
    assert [tile.repo for tile in shown] == ["macDoktor CRM", "macDoktor CRM"]
    assert {tile.branch for tile in shown} == {herdr.title, t3.title}


def test_explicit_primary_layout_still_wins(deck):
    instance, tiles = deck
    instance.config.view.tile_primary = ["repo"]
    instance.apply_snapshot("dev", [agent()])
    assert tiles()[0].repo == "macdoktor-crm"
    instance.config.view.tile_primary = []
    assert tiles()[0].repo == ""
