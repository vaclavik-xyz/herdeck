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


def test_frozen_icons_use_distinct_cache_namespace(tmp_path, monkeypatch):
    """Frozen and non-frozen providers must use separate cache dirs so stale
    PNGs from one render path never contaminate the other."""
    import os

    svg = "<svg>x</svg>"
    from PIL import Image

    from herdeck.icons import ICON_SIZE

    Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (1, 2, 3, 255)).save(
        tmp_path / frozen.glyph_png_name(svg)
    )

    monkeypatch.setattr(frozen, "is_frozen", lambda: True)
    monkeypatch.setattr(frozen, "baked_assets_dir", lambda: str(tmp_path))
    frozen_icons = server._default_icons()

    monkeypatch.setattr(frozen, "is_frozen", lambda: False)
    non_frozen_icons = server._default_icons()

    assert os.path.basename(frozen_icons._cache_dir) == "herdeck-deckapp-icons-frozen"
    assert os.path.basename(non_frozen_icons._cache_dir) == "herdeck-deckapp-icons"
