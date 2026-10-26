"""Drawing through a deck theme (themes/dispatch.py + IconProvider): base
fallback, size enforcement, static-theme cache names and classic staying
byte-identical."""

from __future__ import annotations

import io
import logging
import os
import textwrap
from pathlib import Path

import pytest
from PIL import Image

from herdeck import icons
from herdeck.driver.base import PanelView, TileView
from herdeck.project_icons import ProjectIconStore
from herdeck.themes import registry as reg_mod
from herdeck.themes.registry import ThemeRegistry

MAGENTA = (255, 0, 255)
CYAN = (0, 255, 255)


def _theme_file(directory: Path, theme_id: str, body: str, *, base="classic", animated=False):
    src = textwrap.dedent(
        f"""
        from PIL import Image

        from herdeck.themes.api import Theme


        class T(Theme):
            api_version = 1
            id = {theme_id!r}
            name = {{"en": {theme_id!r}, "cs": {theme_id!r}}}
            base = {base!r}
            animated = {animated!r}
        """
    ) + textwrap.indent(textwrap.dedent(body), "    ") + "\n\nTHEME = T()\n"
    path = directory / f"{theme_id}.py"
    path.write_text(src, encoding="utf-8")
    return path


SOLID_AGENT = """
def agent_tile(self, tile, ctx, frame):
    if tile.kind != "agent":
        return None
    return Image.new("RGB", (ctx.TILE, ctx.TILE), (255, 0, 255))
"""
SOLID_PANEL = """
def panel(self, panel, ctx, width):
    return Image.new("RGB", (width, ctx.PANEL_H), (0, 255, 255))
"""


@pytest.fixture
def themes(tmp_path):
    """A registry over tmp_path/themes, installed as the process default."""
    directory = tmp_path / "themes"
    directory.mkdir()
    clock = [0.0]
    reg = ThemeRegistry(str(directory), clock=lambda: clock[0])
    reg_mod.set_default_registry(reg)
    yield directory, clock
    reg_mod.set_default_registry(None)


@pytest.fixture
def provider(tmp_path):
    return icons.IconProvider(
        cache_dir=str(tmp_path / "cache"),
        slug_map={},
        fetch=lambda slug: None,
        project_icons=ProjectIconStore(),
    )


def _agent(**kw) -> TileView:
    base = dict(
        agent_type="claude",
        repo="herdeck",
        branch="main",
        status_text="WORKING",
        time_text="3m",
        status="working",
    )
    base.update(kw)
    return TileView(0, "herdeck", "green", **base)


def _img(provider, tile) -> Image.Image:
    return Image.open(io.BytesIO(provider.render_tile_bytes(tile))).convert("RGB")


def test_an_unknown_theme_draws_and_names_tiles_exactly_like_classic(themes, provider):
    for tile_kw in ({}, {"spinner": 3}):
        classic = _agent(**tile_kw)
        ghost = _agent(deck_theme="ghost", **tile_kw)
        assert provider._tile_name(ghost) == provider._tile_name(classic)
        assert provider.render_tile_bytes(ghost) == provider.render_tile_bytes(classic)
    panel = PanelView("All clear", meta="3 agents")
    assert icons.compose_panel(PanelView("All clear", meta="3 agents", deck_theme="ghost")).tobytes() == (
        icons.compose_panel(panel).tobytes()
    )


def test_a_theme_draws_its_parts_and_its_base_draws_the_rest(themes, provider):
    directory, _ = themes
    _theme_file(directory, "pink", SOLID_AGENT + SOLID_PANEL)
    assert _img(provider, _agent(deck_theme="pink")).getpixel((98, 98)) == MAGENTA
    back = TileView(0, "Back", "grey")
    assert provider.render_tile_bytes(TileView(0, "Back", "grey", deck_theme="pink")) == (
        provider.render_tile_bytes(back)
    )
    panel = icons.compose_panel(PanelView("All clear", deck_theme="pink"), width=392)
    assert panel.size == (392, 196) and panel.mode == "RGB"
    assert panel.getpixel((10, 10)) == CYAN


def test_an_elgato_action_key_reaches_agent_tile_as_a_control(themes, provider):
    directory, _ = themes
    _theme_file(directory, "pink", SOLID_AGENT)
    action = TileView(0, "Approve", "green", repo="herdeck", status_text="APPROVE", kind="control")
    themed = TileView(
        0, "Approve", "green", repo="herdeck", status_text="APPROVE", kind="control", deck_theme="pink"
    )
    # the theme declines non-agent kinds, so classic draws the action key
    assert _img(provider, themed).tobytes() == _img(provider, action).tobytes()


def test_a_static_theme_keeps_one_cache_entry_per_working_tile(themes, provider):
    directory, _ = themes
    _theme_file(directory, "still", SOLID_AGENT)
    names = {provider._tile_name(_agent(deck_theme="still", spinner=p))[0] for p in range(8)}
    assert len(names) == 1
    assert provider._tile_name(_agent(deck_theme="still", spinner=2))[1] is None


def test_an_animated_theme_gets_the_frame(themes, provider):
    directory, _ = themes
    _theme_file(
        directory,
        "blink",
        """
        def agent_tile(self, tile, ctx, frame):
            shade = 0 if frame is None else 40 * frame
            return Image.new("RGB", (ctx.TILE, ctx.TILE), (shade, 0, 0))
        """,
        animated=True,
    )
    one, two = _agent(deck_theme="blink", spinner=1), _agent(deck_theme="blink", spinner=2)
    assert provider._tile_name(one)[0] != provider._tile_name(two)[0]
    assert _img(provider, two).getpixel((5, 5)) == (80, 0, 0)


def test_a_raising_part_is_drawn_by_the_base_and_logged_once(themes, provider, caplog):
    directory, _ = themes
    _theme_file(
        directory,
        "boom",
        """
        def agent_tile(self, tile, ctx, frame):
            raise ValueError("theme bug")
        """,
    )
    with caplog.at_level(logging.WARNING, logger="herdeck.themes.dispatch"):
        a = _img(provider, _agent(deck_theme="boom", repo="one"))
        _img(provider, _agent(deck_theme="boom", repo="two"))
    assert a.tobytes() == _img(provider, _agent(repo="one")).tobytes()
    assert sum("boom" in r.getMessage() for r in caplog.records) == 1


def test_a_truncated_wrong_sized_image_falls_back_to_the_base(themes, provider, caplog):
    directory, _ = themes
    _theme_file(
        directory,
        "trunc",
        """
        import io

        def agent_tile(self, tile, ctx, frame):
            buf = io.BytesIO()
            Image.new("RGB", (300, 300), (255, 0, 255)).save(buf, "PNG")
            return Image.open(io.BytesIO(buf.getvalue()[:100]))
        """,
    )
    with caplog.at_level(logging.WARNING, logger="herdeck.themes.dispatch"):
        a = _img(provider, _agent(deck_theme="trunc", repo="one"))
        _img(provider, _agent(deck_theme="trunc", repo="two"))
    assert a.tobytes() == _img(provider, _agent(repo="one")).tobytes()
    assert sum("trunc" in r.getMessage() for r in caplog.records) == 1


def test_a_wrong_size_is_resized(themes, provider, caplog):
    directory, _ = themes
    _theme_file(
        directory,
        "big",
        """
        def agent_tile(self, tile, ctx, frame):
            return Image.new("RGB", (300, 300), (255, 0, 255))

        def panel(self, panel, ctx, width):
            return Image.new("RGBA", (100, 50), (0, 255, 255, 255))
        """,
    )
    with caplog.at_level(logging.WARNING, logger="herdeck.themes.dispatch"):
        tile = _img(provider, _agent(deck_theme="big"))
        panel = icons.compose_panel(PanelView("x", deck_theme="big"))
    assert tile.size == (196, 196) and tile.getpixel((98, 98)) == MAGENTA
    assert panel.size == (458, 196) and panel.mode == "RGB"
    assert any("resizing" in r.getMessage() for r in caplog.records)


def test_switching_or_editing_a_theme_renames_the_tile(themes, provider):
    directory, clock = themes
    path = _theme_file(directory, "pink", SOLID_AGENT)
    _theme_file(directory, "blue", SOLID_AGENT)
    classic = provider._tile_name(_agent())[0]
    pink = provider._tile_name(_agent(deck_theme="pink"))[0]
    assert len({classic, pink, provider._tile_name(_agent(deck_theme="blue"))[0]}) == 3
    path.write_text(path.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
    os.utime(path, ns=(1, 2_000_000_000))
    clock[0] += reg_mod.RESCAN_S
    assert provider._tile_name(_agent(deck_theme="pink"))[0] != pink


def test_theme_tile_names_cover_the_theme_data(themes, provider):
    directory, _ = themes
    _theme_file(directory, "pink", SOLID_AGENT)

    def name(**kw):
        return provider._tile_name(_agent(deck_theme="pink", **kw))[0]

    base = name()
    assert name(status="blocked") != base
    assert name(title="fix the golden") != base
    assert name(workspace="herdeck-ws") != base
    assert name(project_icon="abc", project_name="herdeck") != base


def test_a_missing_favicon_is_filed_under_the_monogram_name(themes, provider):
    directory, _ = themes
    _theme_file(
        directory,
        "fav",
        """
        def agent_tile(self, tile, ctx, frame):
            img = Image.new("RGBA", (ctx.TILE, ctx.TILE), (0, 0, 0, 255))
            img.alpha_composite(ctx.project_mark(tile, 80), (58, 58))
            return img
        """,
    )
    tile = _agent(deck_theme="fav", project_icon="evicted", project_name="herdeck")
    name, spinner = provider._tile_name(tile)
    _, filed = provider._compose_checked(tile, spinner, name)
    assert filed == provider._tile_name(tile, drop_icon=True)[0] != name
