import glob
import os
import xml.etree.ElementTree as ET

from PIL import Image

from herdeck import frozen
from herdeck.bridge import _MANAGED_AGENT_KINDS
from herdeck.deckapp import server
from herdeck.icons import _ASSETS_DIR, BUNDLED_AGENT_TYPES, ICON_SIZE


def test_bundled_marks_cover_every_managed_herdr_agent_kind():
    assert set(BUNDLED_AGENT_TYPES) == _MANAGED_AGENT_KINDS


def test_bundled_svgs_exist_and_are_monochrome_white():
    for name in BUNDLED_AGENT_TYPES:
        path = os.path.join(_ASSETS_DIR, f"{name}.svg")
        raster_path = os.path.join(_ASSETS_DIR, f"{name}.png")
        assert os.path.exists(path) or os.path.exists(raster_path), (
            f"missing bundled mark: {name}.svg or {name}.png"
        )
        if not os.path.exists(path):
            image = Image.open(raster_path).convert("RGBA")
            image.load()
            assert image.width >= 128 and image.height >= 128
            continue
        text = open(path, encoding="utf-8").read()
        root = ET.fromstring(text)  # parses as XML (raises on malformed)
        assert root.get("fill") == "#ffffff", f"{name}.svg root fill must be #ffffff"


def _all_bundled_svgs():
    return sorted(glob.glob(os.path.join(_ASSETS_DIR, "*.svg")))


def test_every_bundled_svg_has_committed_decodable_baked_png():
    """Invariant guard against the silent Q1 regression: every committed SVG must
    have its committed content-keyed baked PNG, decodable at 196x196 (what the
    frozen rasterizer loads, with NO cairosvg)."""
    svgs = _all_bundled_svgs()
    assert len(svgs) >= len(BUNDLED_AGENT_TYPES) - 1  # Maki intentionally uses PNG artwork
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
    for name in BUNDLED_AGENT_TYPES:
        glyph = icons._base_glyph(name)
        letter = icons._letter_glyph(name)
        assert glyph.size == (ICON_SIZE, ICON_SIZE)
        # Compare raw pixel bytes (both RGBA, same size) — avoids the deprecated
        # Image.getdata() path that warns on every call.
        assert glyph.tobytes() != letter.tobytes(), (
            f"{name}: asset branch missed -> degraded to letter glyph"
        )
