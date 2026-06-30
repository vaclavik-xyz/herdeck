import glob
import os
import xml.etree.ElementTree as ET

from PIL import Image

from herdeck import frozen
from herdeck.deckapp import server
from herdeck.icons import _ASSETS_DIR, ICON_SIZE

# The 5 agent types that must ship a bundled monochrome mark (codex already does).
# Filenames are the agent_type keys, NOT Simple Icons slugs (see _base_glyph lookup).
BUNDLED_AGENT_ICONS = ("claude", "cursor", "copilot", "gemini", "opencode")


def test_bundled_svgs_exist_and_are_monochrome_white():
    for name in BUNDLED_AGENT_ICONS:
        path = os.path.join(_ASSETS_DIR, f"{name}.svg")
        assert os.path.exists(path), f"missing bundled SVG: {name}.svg"
        text = open(path, encoding="utf-8").read()
        root = ET.fromstring(text)  # parses as XML (raises on malformed)
        assert root.get("fill") == "#ffffff", f"{name}.svg root fill must be #ffffff"


def _all_bundled_svgs():
    return sorted(glob.glob(os.path.join(_ASSETS_DIR, "*.svg")))


def test_every_bundled_svg_has_committed_decodable_baked_png():
    """Invariant guard against the silent Q1 regression: every committed SVG must
    have its committed content-keyed baked PNG, decodable at 196x196 (what the
    frozen rasterizer loads, with NO cairosvg). Covers the 5 new marks + codex."""
    svgs = _all_bundled_svgs()
    assert len(svgs) >= 6  # codex + the 5 new marks
    for svg_path in svgs:
        svg = open(svg_path, encoding="utf-8").read()
        png = os.path.join(_ASSETS_DIR, frozen.glyph_png_name(svg))
        assert os.path.exists(png), f"missing committed baked PNG for {os.path.basename(svg_path)}"
        im = Image.open(png)
        im.load()  # full decode (raises on corrupt data)
        im = im.convert("RGBA")
        assert im.size == (ICON_SIZE, ICON_SIZE), f"{png} is {im.size}, want {(ICON_SIZE, ICON_SIZE)}"


def test_frozen_provider_renders_bundled_mark_not_letter(monkeypatch):
    """A frozen-style provider (PNG rasterizer + baked assets dir = the real source
    assets dir) returns the BUNDLED mark, not the letter fallback, for each type."""
    monkeypatch.setattr(frozen, "is_frozen", lambda: True)
    monkeypatch.setattr(frozen, "baked_assets_dir", lambda: _ASSETS_DIR)
    icons = server._default_icons()
    for name in BUNDLED_AGENT_ICONS:
        glyph = icons._base_glyph(name)
        letter = icons._letter_glyph(name)
        assert glyph.size == (ICON_SIZE, ICON_SIZE)
        # Compare raw pixel bytes (both RGBA, same size) — avoids the deprecated
        # Image.getdata() path that warns on every call.
        assert glyph.tobytes() != letter.tobytes(), (
            f"{name}: asset branch missed -> degraded to letter glyph"
        )
