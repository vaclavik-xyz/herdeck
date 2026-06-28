from PIL import Image

from herdeck import frozen
from herdeck.deckapp import server
from herdeck.icons import _ASSETS_DIR, ICON_SIZE


def test_default_icons_frozen_uses_baked_assets_and_png_rasterizer(tmp_path, monkeypatch):
    svg = "<svg>codex</svg>"
    Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (5, 6, 7, 255)).save(
        tmp_path / frozen.glyph_png_name(svg)
    )
    monkeypatch.setattr(frozen, "is_frozen", lambda: True)
    monkeypatch.setattr(frozen, "baked_assets_dir", lambda: str(tmp_path))

    icons = server._default_icons()

    assert icons._assets_dir == str(tmp_path)
    # The frozen rasterizer loads the pre-baked PNG (no cairosvg).
    img = icons._rasterize(svg, ICON_SIZE)
    assert img.size == (ICON_SIZE, ICON_SIZE)
    assert img.getpixel((0, 0)) == (5, 6, 7, 255)


def test_default_icons_non_frozen_keeps_cairosvg_defaults(monkeypatch):
    monkeypatch.setattr(frozen, "is_frozen", lambda: False)
    icons = server._default_icons()
    assert icons._assets_dir == _ASSETS_DIR  # default source-tree assets dir
