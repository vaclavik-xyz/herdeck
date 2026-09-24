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
import json
import os
import sys

import pytest
from PIL import Image, ImageFont

from herdeck import icons, layout
from herdeck.driver.base import PanelGauge, PanelView, TileView
from herdeck.frozen import make_png_rasterizer
from herdeck.model import Status
from herdeck.project_icons import ProjectIconStore

GOLDEN_FILE = os.path.join(os.path.dirname(__file__), "goldens", "render.json")
UPDATE_ENV = "HERDECK_UPDATE_GOLDENS"


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
        usage_lines=["Claude 5h 42%", "Codex 7d 91%"],
        usage_gauges=[
            PanelGauge("Claude", "5h", 42, hint="2h 10m", color="green"),
            PanelGauge("Codex", "7d", 91, hint="Mon", color="red"),
        ],
    )
    usage_detail = PanelView(
        "Claude usage",
        gauges=[
            PanelGauge(
                "Session", "5h", 42, hint="resets 2h 10m", color="green", pace="full ~40m early"
            ),
            PanelGauge("Weekly", "7d", 87, hint="resets Mon", color="amber"),
        ],
        gauge_meta="1/2",
    )
    return {
        "panel_overview": overview,
        "panel_spotlight": spotlight,
        "panel_overview_usage": overview_usage,
        "panel_usage_detail": usage_detail,
    }


def _digest(img: Image.Image) -> str:
    rgb = img.convert("RGB")
    h = hashlib.sha256(f"{rgb.width}x{rgb.height}\0".encode())
    h.update(rgb.tobytes())
    return h.hexdigest()


def _render_all(tmp_path) -> dict[str, Image.Image]:
    # The frozen path's rasterizer: bundled marks come from the committed
    # pre-baked PNGs (no resvg float maths), unknown agents get the letter glyph.
    provider = icons.IconProvider(
        cache_dir=str(tmp_path / "cache"),
        slug_map={},
        fetch=lambda slug: None,
        rasterize=make_png_rasterizer(icons._ASSETS_DIR),
        project_icons=ProjectIconStore(),
    )
    out = {n: Image.open(io.BytesIO(provider.render_tile_bytes(t))) for n, t in _tiles().items()}
    out.update({n: icons.compose_panel(p) for n, p in _panels().items()})
    return out


def _load_goldens() -> dict[str, str]:
    try:
        with open(GOLDEN_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}


def test_rendered_tiles_and_panels_match_goldens(tmp_path):
    rendered = _render_all(tmp_path)
    actual = {name: _digest(img) for name, img in sorted(rendered.items())}
    if os.environ.get(UPDATE_ENV):
        os.makedirs(os.path.dirname(GOLDEN_FILE), exist_ok=True)
        with open(GOLDEN_FILE, "w", encoding="utf-8") as fh:
            json.dump(actual, fh, indent=2, sort_keys=True)
            fh.write("\n")
        pytest.skip(f"{UPDATE_ENV} set: rewrote {GOLDEN_FILE}")
    goldens = _load_goldens()
    mismatched = sorted(n for n in actual if goldens.get(n) != actual[n])
    stale = sorted(set(goldens) - set(actual))
    if not mismatched and not stale:
        return
    out_dir = os.environ.get("HERDECK_GOLDEN_OUT") or str(tmp_path / "golden-actual")
    os.makedirs(out_dir, exist_ok=True)
    lines = []
    for name in mismatched:
        path = os.path.join(out_dir, f"{name}.png")
        rendered[name].convert("RGB").save(path)
        lines.append(
            f"  {name}: expected {goldens.get(name, '<missing>')[:16]}"
            f" got {actual[name][:16]}  -> {path}"
        )
    lines += [f"  {name}: golden has no matching render (remove it)" for name in stale]
    pytest.fail(
        "Rendered pixels changed:\n"
        + "\n".join(lines)
        + "\n\nIf the change is intended (a composition change also bumps"
        " icons.TILE_VERSION), look at the PNGs above, then regenerate with\n"
        f"  {UPDATE_ENV}=1 python -m pytest tests/test_render_golden.py\n"
        "and commit tests/goldens/render.json. The goldens are OS-independent"
        " (vendored Inter font, pre-baked marks): a mismatch on only one OS is a"
        " determinism bug, not a golden to regenerate.\n"
        "New digests:\n" + json.dumps({n: actual[n] for n in mismatched}, indent=2),
        pytrace=False,
    )
