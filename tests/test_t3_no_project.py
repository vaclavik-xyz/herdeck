"""A T3 thread created without a project.

T3 files such a thread under an ordinary project titled "No project" whose
workspace root is its scratch dir. The deck headlines the thread title, shows a
muted "No project" label and a neutral glyph instead of a monogram of the
scratch path.
"""

import io
import os

import pytest
from PIL import Image

from herdeck import layout
from herdeck.config import ViewConfig
from herdeck.driver.base import TileView
from herdeck.icons import IconProvider, _monogram_image, no_project_image
from herdeck.model import AgentKey, AgentState, Status
from herdeck.notify_icons import NotificationIconCache
from herdeck.project_icons import NO_PROJECT_ICON, ProjectIconStore, tile_icon_fields
from herdeck.t3_state import is_no_project, thread_state

SCRATCH = {
    "id": "35bd",
    "title": "No project",
    "workspaceRoot": "/Users/admin/.t3/scratch",
    "repositoryIdentity": None,
    "faviconPath": None,
    "projectIcon": {"kind": "lucide", "name": "message-square-dashed", "color": "gray"},
}
CRM = {
    "id": "crm",
    "title": "macDoktor CRM",
    "workspaceRoot": "/Users/admin/projects/crm",
    "repositoryIdentity": {"canonicalKey": "github.com/x/crm"},
    "faviconPath": "public/favicon.ico",
    "projectIcon": None,
}
TITLE = "Vyřešit SSH z herního PC na M4"


def _state(project_id, projects, lang="en"):
    shell = {"id": "t1", "projectId": project_id, "title": TITLE, "status": "idle",
             "updatedAt": "2026-10-22T10:00:00.000Z"}
    return thread_state("t3", shell, {}, projects, "e", features={}, now=1e9, lang=lang)


# --- detection ----------------------------------------------------------------


@pytest.mark.parametrize(
    "project",
    [
        SCRATCH,
        {**SCRATCH, "workspaceRoot": "/home/me/.t3/scratch/"},
        {**SCRATCH, "workspaceRoot": "C:\\Users\\me\\.t3\\scratch"},
        # The bucket's title is T3's (possibly localized) text: never read.
        {**SCRATCH, "title": "Žádný projekt"},
        {**SCRATCH, "title": ""},
        None,
        {},
    ],
)
def test_project_less_buckets_are_detected(project):
    assert is_no_project(project) is True


@pytest.mark.parametrize(
    "project",
    [
        CRM,
        # A real project that happens to be titled "No project".
        {**CRM, "title": "No project"},
        # A git repo that lives in the scratch dir is a project after all.
        {**SCRATCH, "repositoryIdentity": {"canonicalKey": "x"}},
        {**SCRATCH, "workspaceRoot": "/Users/admin/scratch"},
        {**SCRATCH, "workspaceRoot": "/Users/admin/.t3/scratch/sub"},
        {**SCRATCH, "workspaceRoot": None},
    ],
)
def test_regular_projects_are_not_project_less(project):
    assert is_no_project(project) is False


def test_thread_state_flags_scratch_missing_and_unknown_projects():
    projects = {"35bd": SCRATCH, "crm": CRM}
    scratch = _state("35bd", projects)
    assert scratch.no_project is True
    assert scratch.project == "No project"
    assert scratch.repo == "/Users/admin/.t3/scratch"
    assert _state(None, projects).no_project is True
    assert _state("gone", projects).no_project is True
    regular = _state("crm", projects)
    assert (regular.no_project, regular.project) == (False, "macDoktor CRM")


def test_project_label_follows_the_deck_language():
    assert _state("35bd", {"35bd": SCRATCH}, lang="cs").project == "Bez projektu"


# --- tile lines ---------------------------------------------------------------


def test_thread_title_headlines_and_label_takes_the_title_line():
    s = _state("35bd", {"35bd": SCRATCH})
    assert layout.compose_tile_lines(s, ["project"], ["tab"]) == (TITLE, "No project")
    assert layout.compose_tile_lines(s, ["repo"], ["title", "tab"]) == (TITLE, "No project")
    assert layout.compose_tile_lines(s, ["project"], []) == (TITLE, "")


def test_regular_t3_lines_are_unchanged():
    s = _state("crm", {"crm": CRM})
    assert layout.compose_tile_lines(s, ["project"], ["tab"]) == ("macDoktor CRM", TITLE)


# --- icon -------------------------------------------------------------------


def test_tile_icon_fields_name_the_glyph_for_a_project_less_thread():
    s = _state("35bd", {"35bd": SCRATCH})
    store = ProjectIconStore()
    assert tile_icon_fields(ViewConfig(tile_icon="project"), s, store)["project_icon"] == NO_PROJECT_ICON
    assert tile_icon_fields(ViewConfig(tile_icon="both"), s, store)["project_icon"] == NO_PROJECT_ICON
    # agent mode keeps the agent mark only
    assert tile_icon_fields(ViewConfig(), s, store)["project_icon"] is None
    regular = _state("crm", {"crm": CRM})
    assert tile_icon_fields(ViewConfig(tile_icon="project"), regular, store)["project_icon"] is None


def test_an_override_for_the_scratch_dir_still_wins(tmp_path):
    icon = tmp_path / "scratch.png"
    buf = io.BytesIO()
    Image.new("RGBA", (8, 8), (1, 2, 3, 255)).save(buf, format="PNG")
    icon.write_bytes(buf.getvalue())
    view = ViewConfig(tile_icon="project", project_icons={"/Users/admin/.t3/scratch": str(icon)})
    fields = tile_icon_fields(view, _state("35bd", {"35bd": SCRATCH}), ProjectIconStore())
    assert fields["project_icon"] not in (None, NO_PROJECT_ICON)


def test_glyph_is_a_grey_transparent_mark():
    img = no_project_image()
    assert img.mode == "RGBA" and img.size == _monogram_image("x").size
    assert img.getpixel((img.width // 2, img.height // 2))[3] == 0  # hollow bubble
    alpha = img.getchannel("A")
    assert alpha.getbbox() is not None
    r, g, b, _ = img.getpixel((round(img.width * 15 / 24), round(img.height * 3 / 24)))
    assert max(r, g, b) - min(r, g, b) < 16  # neutral grey, not a status colour


def _provider(tmp_path):
    return IconProvider(
        cache_dir=str(tmp_path), slug_map={}, fetch=lambda slug: None,
        rasterize=lambda svg, size: Image.new("RGBA", (size, size), (0, 0, 0, 0)),
        project_icons=ProjectIconStore(),
    )


def _render(provider, view, state):
    primary, secondary = layout.compose_tile_lines(state, ["project"], ["tab"])
    tile = layout.agent_tile_view(0, state, view, color="blue", repo=primary, branch=secondary,
                                  status_text="IDLE", project_icons=provider._project_icons)
    return tile, Image.open(io.BytesIO(provider.render_tile_bytes(tile))).convert("RGB")


def test_tile_draws_the_glyph_not_the_monogram(tmp_path):
    p = _provider(tmp_path)
    view = ViewConfig(tile_icon="project")
    tile, img = _render(p, view, _state("35bd", {"35bd": SCRATCH}))
    assert (tile.repo, tile.branch, tile.project_icon) == (TITLE, "No project", NO_PROJECT_ICON)
    box = img.crop((12, 12, 58, 58))
    # The monogram is an opaque coloured square; the glyph leaves the box's
    # centre on the dark tile background.
    assert box.getpixel((23, 23)) == (26, 26, 30)
    monogram_tile = TileView(**{**tile.__dict__, "project_icon": None})
    mono = Image.open(io.BytesIO(p.render_tile_bytes(monogram_tile))).convert("RGB")
    assert mono.crop((12, 12, 58, 58)).getpixel((23, 23)) != (26, 26, 30)
    # Distinct render-cache names: the glyph is part of the tile signature.
    assert p._tile_name(tile)[0] != p._tile_name(monogram_tile)[0]


def test_notification_icon_uses_the_glyph(tmp_path):
    cache = NotificationIconCache(str(tmp_path), store=ProjectIconStore())
    path = cache.path_for(_state("35bd", {"35bd": SCRATCH}))
    assert path is not None and os.path.basename(path).endswith("g-no-project.png")
    regular = AgentState(AgentKey("s", "p"), "codex", "x", Status.IDLE, repo="crm")
    assert "g-no-project" not in (cache.path_for(regular) or "")
