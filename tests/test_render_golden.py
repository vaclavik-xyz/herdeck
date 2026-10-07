"""Rendered pixels are the same on every OS.

Tiles and panels are drawn with the vendored Inter font (src/herdeck/assets/
fonts), never a system font, and bundled agent marks come from the committed
pre-baked PNGs, so the goldens below hold on macOS and on the Linux CI runner
alike. A golden is a SHA-256 of the raw RGB pixels (not of the PNG bytes, which
zlib may encode differently).

Regenerate after an intended change (see the failure message):
    HERDECK_UPDATE_GOLDENS=1 python -m pytest tests/test_render_golden.py
"""

from __future__ import annotations

import hashlib
import io
import os
import sys

from golden_support import UPDATE_ENV, check_goldens
from PIL import Image, ImageDraw, ImageFont

from herdeck import icons, layout
from herdeck.driver.base import PanelGauge, PanelView, TileView
from herdeck.frozen import make_png_rasterizer
from herdeck.model import Status
from herdeck.project_icons import ProjectIconStore

GOLDEN_FAVICON = "golden-favicon"

GOLDEN_FILE = os.path.join(os.path.dirname(__file__), "goldens", "render.json")


# --- the vendored font -------------------------------------------------------


def test_font_prefers_the_vendored_inter():
    bold = icons._font(24)
    regular = icons._font(24, bold=False)
    assert bold.getname() == ("Inter", "Bold")
    assert regular.getname() == ("Inter", "Regular")
    # Basic layout: raqm (present on some Pillow builds, not on others) would
    # otherwise shape text differently per OS.
    assert bold.layout_engine == ImageFont.Layout.BASIC


def test_vendored_font_ships_with_its_licence():
    font_dir = os.path.join(os.path.dirname(icons.__file__), "assets", "fonts")
    names = set(os.listdir(font_dir))
    assert {"Inter-Bold.ttf", "Inter-Regular.ttf", "LICENSE.txt", "VENDORED.md"} <= names
    with open(os.path.join(font_dir, "LICENSE.txt"), encoding="utf-8") as fh:
        assert "SIL Open Font License" in fh.read()


def test_bundled_font_path_finds_a_frozen_bundle(tmp_path, monkeypatch):
    """Inside PyInstaller the package dir has no assets/: the fonts live under
    sys._MEIPASS/herdeck_assets/fonts (the spec bundles the assets dir there)."""
    fonts = tmp_path / "herdeck_assets" / "fonts"
    fonts.mkdir(parents=True)
    (fonts / "Inter-Bold.ttf").write_bytes(b"x")
    monkeypatch.setattr(icons, "_ASSETS_DIR", str(tmp_path / "missing"))
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert icons.bundled_font_path(bold=True) == str(fonts / "Inter-Bold.ttf")
    assert icons.bundled_font_path(bold=False) is None


# --- pixel goldens -----------------------------------------------------------


def _agent(status: Status, *, agent_type="claude", fill="none", **kw) -> TileView:
    word = {
        Status.BLOCKED: "BLOCKED",
        Status.WORKING: "WORKING",
        Status.IDLE: "IDLE",
        Status.DONE: "DONE",
        Status.WAITING: "CI",
    }[status]
    base = dict(
        agent_type=agent_type,
        tile_fill=fill,
        repo="herdeck",
        branch="feat/render-goldens",
        status_text=word,
        time_text="12m",
    )
    base.update(kw)
    return TileView(0, "herdeck", layout.status_color(status), **base)


def _tiles() -> dict[str, TileView]:
    return {
        "agent_blocked": _agent(Status.BLOCKED, pinned=True, server_tag="HERDR"),
        "agent_working_spin": _agent(Status.WORKING, spinner=3),
        "agent_working_subagents": _agent(Status.WORKING, spinner=3, subagents=3),
        "agent_subagents_pinned_tag": _agent(
            Status.WORKING, fill="solid", pinned=True, server_tag="HERDR", subagents=12
        ),
        "agent_idle": _agent(Status.IDLE, agent_type="codex"),
        "agent_done_wrapped_repo": _agent(Status.DONE, repo="macdoktor-crm-production"),
        "agent_waiting": _agent(Status.WAITING),
        "agent_letter_glyph": _agent(Status.IDLE, agent_type="zzagent"),
        "fill_solid_blocked": _agent(Status.BLOCKED, fill="solid"),
        "fill_solid_working_sweep": _agent(
            Status.WORKING, fill="solid", spinner=2, working_animation="sweep"
        ),
        "fill_tint_idle": _agent(Status.IDLE, fill="tint"),
        "project_monogram": _agent(
            Status.WORKING, tile_icon="project", project_name="herdeck", spinner=1
        ),
        "project_badge": _agent(Status.IDLE, tile_icon="both", project_name="t3code"),
        "label_back": TileView(0, "Back", "grey"),
        "label_launcher": TileView(0, "+ New", "launcher"),
        "label_drill_option": TileView(
            0,
            "1",
            "amber",
            subtext="Yes, and don't ask again for rm commands in /home/user/projects",
        ),
        "label_empty": TileView(0, "", "empty"),
        "agent_working_comet": _agent(Status.WORKING, spinner=5, working_animation="comet"),
        "agent_working_pulse": _agent(Status.WORKING, spinner=7, working_animation="pulse"),
        "project_favicon_working": _agent(
            Status.WORKING,
            tile_icon="project",
            project_icon=GOLDEN_FAVICON,
            project_name="herdeck",
            spinner=2,
        ),
    }


def _panels() -> dict[str, PanelView]:
    overview = layout.panel_overview(layout.Counts(1, 3, 2, 1, waiting=1), 0, 2, set(), 8, None)
    spotlight = layout.panel_overview(
        layout.Counts(2, 1, 0, 0), 0, 1, set(), 3, ("herdeck · claude", "4m")
    )
    overview_usage = layout.panel_overview(
        layout.Counts(0, 2, 1, 0),
        0,
        1,
        set(),
        3,
        None,
        usage_gauges=[
            PanelGauge("Claude", "5h", 42, hint="2h 10m", color="green"),
            PanelGauge("Codex", "7d", 91, hint="Mon", color="red"),
        ],
    )
    usage_detail = PanelView(
        "Usage limits",
        gauges=[
            PanelGauge(
                "Session", "5h", 42, hint="resets 2h 10m", color="green", pace="full ~40m early"
            ),
            PanelGauge("Weekly", "7d", 87, hint="resets Mon", color="amber"),
        ],
        meta="used / reset",
        page=(0, 2),
        hint="press · more",
    )
    offline = layout.panel_overview(
        layout.Counts(1, 0, 0, 0), 0, 1, {"macbench"}, 1, None, down_for="2m"
    )
    return {
        "panel_overview": overview,
        "panel_spotlight": spotlight,
        "panel_overview_usage": overview_usage,
        "panel_usage_detail": usage_detail,
        "panel_offline": offline,
    }


def _panels_two_cell() -> dict[str, PanelView]:
    """Panels composed at the 392px two-cell width (web, desktop, Elgato)."""
    panels = _panels()
    detail = PanelView(
        "BLOCKED · 4m",
        ["Allow rm -rf build? This deletes the build directory and every cached artefact in it."],
        "amber",
        meta="claude · herdeck",
        hint="answer on the keys",
    )
    sent = layout.panel_overview(
        layout.Counts(0, 2, 1, 0), 0, 1, set(), 3, None, sent="Sent to herdeck"
    )
    menu = PanelView("New agent", ["Starts on m4"], headline="Pick a type")
    return {
        "panel_overview_392": panels["panel_overview"],
        "panel_spotlight_392": panels["panel_spotlight"],
        "panel_offline_392": panels["panel_offline"],
        "panel_detail_392": detail,
        "panel_sent_392": sent,
        "panel_menu_392": menu,
    }


def _favicon_store() -> ProjectIconStore:
    """A store holding one deterministic PNG favicon (a red rounded app tile
    with a white square), drawn without anti-aliasing so it is the same
    bytes on every OS."""
    img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle((2, 2, 29, 29), fill=(200, 40, 60, 255))
    draw.rectangle((10, 10, 21, 21), fill=(255, 255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    store = ProjectIconStore()
    store.put(GOLDEN_FAVICON, "image/png", buf.getvalue())
    return store


def _render_all(tmp_path) -> dict[str, Image.Image]:
    # The frozen path's rasterizer: bundled marks come from the committed
    # pre-baked PNGs (no resvg float maths), unknown agents get the letter glyph.
    provider = icons.IconProvider(
        cache_dir=str(tmp_path / "cache"),
        slug_map={},
        fetch=lambda slug: None,
        rasterize=make_png_rasterizer(icons._ASSETS_DIR),
        project_icons=_favicon_store(),
    )
    out = {n: Image.open(io.BytesIO(provider.render_tile_bytes(t))) for n, t in _tiles().items()}
    out.update({n: icons.compose_panel(p) for n, p in _panels().items()})
    out.update(
        {
            n: icons.compose_panel(p, width=icons.PANEL_W_TWO_CELL)
            for n, p in _panels_two_cell().items()
        }
    )
    return out


def test_rendered_tiles_and_panels_match_goldens(tmp_path):
    check_goldens(
        _render_all(tmp_path),
        GOLDEN_FILE,
        tmp_path,
        title="Rendered pixels changed",
        note="If the change is intended (a composition change also bumps"
        " icons.TILE_VERSION), look at the PNGs above, then regenerate with",
        after="and commit tests/goldens/render.json. The goldens are OS-independent"
        " (vendored Inter font, pre-baked marks): a mismatch on only one OS is a"
        " determinism bug, not a golden to regenerate.",
        regenerate=f"{UPDATE_ENV}=1 python -m pytest tests/test_render_golden.py",
        show_new_digests=True,
    )


# --- classic cache names -----------------------------------------------------
# The D200 reads tiles and panels by file name; a theme platform must not
# rename a single classic file (a rename is a full re-render + re-upload of
# every key). Pinned with assets_dir=None so the bundled-asset fingerprint
# ("0") cannot move them.

CLASSIC_TILE_NAMES = {
    "agent_idle": "tile_587ad3e12b02974b.png",
    "agent_spin": "tile_5ee478c92eafe439.png",
    "agent_pulse": "tile_410679658949bb5d.png",
    "label_back": "tile_385dda9a5f3cdbe7.png",
    "empty": "tile_087110854e8265e7.png",
}


def _name_tiles() -> dict[str, TileView]:
    def agent(**kw) -> TileView:
        base = dict(
            agent_type="claude", repo="herdeck", branch="main", status_text="WORKING", time_text="3m"
        )
        base.update(kw)
        return TileView(0, "herdeck", "green", **base)

    return {
        "agent_idle": agent(status_text="IDLE"),
        "agent_spin": agent(spinner=3),
        "agent_pulse": agent(spinner=7, working_animation="pulse"),
        "label_back": TileView(0, "Back", "grey"),
        "empty": TileView(0, "", "empty"),
    }


def test_classic_tile_cache_names_are_stable(tmp_path):
    provider = icons.IconProvider(
        cache_dir=str(tmp_path / "cache"),
        slug_map={},
        fetch=lambda slug: None,
        assets_dir=None,
        project_icons=ProjectIconStore(),
    )
    names = {n: provider._tile_name(t)[0] for n, t in _name_tiles().items()}
    assert names == CLASSIC_TILE_NAMES


def test_classic_panel_cache_key_is_stable():
    """d200.py names the panel file sha1(TILE_VERSION|repr(cache_key))."""
    panel = layout.panel_overview(layout.Counts(1, 3, 2, 1, waiting=1), 0, 2, set(), 8, None)
    digest = hashlib.sha1(repr(panel.cache_key()).encode()).hexdigest()[:12]
    assert digest == "d498fae3290e"
    assert icons.TILE_VERSION == 20
