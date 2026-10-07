"""The built-in themes' shared drawing helpers (themes/_kit.py)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw
from theme_support import agent, make_ctx, make_provider

from herdeck.themes import _kit as k


@pytest.fixture
def ctx(tmp_path):
    return make_ctx(make_provider(tmp_path))


@pytest.fixture
def draw():
    return ImageDraw.Draw(Image.new("RGB", (k.S, k.S)))


def test_status_of_maps_everything_else_to_unknown():
    assert k.status_of(agent("blocked")) == "blocked"
    assert k.status_of(agent("offline")) == "offline"
    assert k.status_of(agent(None)) == "unknown"
    assert k.status_of(SimpleNamespace()) == "unknown"
    assert k.status_of(SimpleNamespace(status="weird")) == "unknown"


def test_text_line_falls_back_title_workspace_repo_label():
    assert k.text_line(agent("idle", title="T", workspace="W")) == "T"
    assert k.text_line(agent("idle", title=" ", workspace="W")) == "W"
    assert k.text_line(agent("idle", title="", workspace="")) == "herdeck"
    assert k.text_line(SimpleNamespace(label="L")) == "L"
    assert k.name_line(agent("idle", title="T")) == "herdeck"


def test_fit_never_goes_below_the_floor(ctx, draw):
    font, text = k.fit(ctx, draw, "x" * 200, 100, 40, 10)
    assert font.size == k.MIN_TEXT
    assert text.endswith("…") and draw.textlength(text, font=font) <= 100
    font, text = k.fit(ctx, draw, "ok", 100, 40)
    assert (font.size, text) == (40, "ok")


def test_block_wraps_then_cuts_at_the_floor(ctx, draw):
    font, lines = k.block(ctx, draw, "short", 150, largest=30)
    assert (font.size, lines) == (30, ["short"])
    font, lines = k.block(ctx, draw, "a fairly long task title here", 150, largest=30)
    assert font.size in (24, k.MIN_TEXT) and len(lines) == 2
    font, lines = k.block(ctx, draw, "word " * 40, 150, largest=30)
    assert font.size == k.MIN_TEXT and lines[-1].endswith("…")
    assert all(draw.textlength(line, font=font) <= 150 for line in lines)
    assert k.block(ctx, draw, "", 150, largest=30)[1] == []


def test_t_formats_and_survives_missing_keys(ctx):
    assert k.t(ctx, "signal.waiting_for", t="4m") == "4m waiting"
    assert k.t(ctx, "no.such.key") == "no.such.key"


def test_identity_mark_is_project_first(ctx):
    with_project = agent("idle", project_name="x")
    no_project = agent("idle", project_name="", repo="")
    bare = agent(None, agent_type=None, project_name="", repo="", title="", workspace="")
    proj = k.identity_mark(ctx, with_project, 40)
    logo = k.identity_mark(ctx, no_project, 40)
    mono = k.identity_mark(ctx, bare, 40)
    for img in (proj, logo, mono):
        assert img.size == (40, 40) and img.mode == "RGBA"
    assert proj.tobytes() == ctx.project_mark(with_project, 40).tobytes()
    assert logo.tobytes() == ctx.agent_mark("claude", 40).tobytes()
    assert proj.tobytes() != logo.tobytes()
    # tile_icon is not consulted: an "agent" tile with project data still shows the project
    as_agent = k.identity_mark(ctx, agent("idle", tile_icon="agent", project_name="x"), 40)
    assert as_agent.tobytes() == proj.tobytes()


def test_marks_never_mutate_the_shared_cache(ctx):
    before = ctx.agent_mark("claude", 40, dark=True).tobytes()
    k.identity_mark(ctx, agent("idle", project_name="", repo=""), 40, on_light=True).paste(
        (255, 0, 0, 255), (0, 0, 40, 40)
    )
    k.queue_mark(ctx, SimpleNamespace(agent_type="claude", label="x"), 40).paste(
        (255, 0, 0, 255), (0, 0, 40, 40)
    )
    assert ctx.agent_mark("claude", 40, dark=True).tobytes() == before


def test_greyed_leaves_its_input_alone():
    img = Image.new("RGBA", (10, 10), (200, 30, 30, 255))
    k.greyed(img)
    assert img.getpixel((5, 5)) == (200, 30, 30, 255)


def test_greyed_keeps_size_and_alpha_shape():
    img = Image.new("RGBA", (10, 10), (200, 30, 30, 255))
    out = k.greyed(img)
    r, g, b, a = out.getpixel((5, 5))
    assert out.size == (10, 10) and max(r, g, b) - min(r, g, b) <= 4 and a > 0


def _fallback_ctx(tmp_path):
    from herdeck.driver.base import ThemeEnv
    from herdeck.themes.api import ThemeContext

    return ThemeContext(ThemeEnv("en", (), "agent"), make_provider(tmp_path))


def test_agent_fallback_project_mark_is_a_copy_and_honours_on_light(tmp_path):
    ctx = _fallback_ctx(tmp_path)
    tile = agent("idle", project_name="x", project_icon=None)
    before = ctx.agent_mark("claude", 40).tobytes()
    before_dark = ctx.agent_mark("claude", 40, dark=True).tobytes()
    light = k.identity_mark(ctx, tile, 40)
    dark = k.identity_mark(ctx, tile, 40, on_light=True)
    assert light.tobytes() == before and dark.tobytes() == before_dark
    for img in (
        light,
        dark,
        k.queue_mark(
            ctx,
            SimpleNamespace(agent_type="claude", label="x", project_name="x", project_icon=None),
            40,
        ),
    ):
        img.paste((255, 0, 0, 255), (0, 0, 40, 40))
    assert ctx.agent_mark("claude", 40).tobytes() == before
    assert ctx.agent_mark("claude", 40, dark=True).tobytes() == before_dark


def test_project_branch_mark_is_a_copy(ctx):
    tile = agent("idle", project_name="x")
    before = ctx.project_mark(tile, 40).tobytes()
    k.identity_mark(ctx, tile, 40).paste((255, 0, 0, 255), (0, 0, 40, 40))
    assert ctx.project_mark(tile, 40).tobytes() == before


def test_repo_only_tile_gets_a_repo_monogram_not_question_mark(ctx):
    tile = agent("idle", project_name="", repo="herdeck", project_icon=None)
    got = k.identity_mark(ctx, tile, 40)
    assert got.tobytes() == ctx.project_mark(k.monogram_source("herdeck"), 40).tobytes()
    assert got.tobytes() != ctx.project_mark(k.monogram_source(""), 40).tobytes()
