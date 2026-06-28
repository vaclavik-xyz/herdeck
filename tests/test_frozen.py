import sys

from PIL import Image

from herdeck import frozen
from herdeck.icons import ICON_SIZE


def test_neutral_module_exposes_helpers():
    for name in (
        "BAKE_SIZE",
        "is_frozen",
        "baked_assets_dir",
        "glyph_png_name",
        "make_png_rasterizer",
        "prerasterize_assets",
    ):
        assert hasattr(frozen, name), name


def test_is_frozen_reflects_sys_frozen(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert frozen.is_frozen() is False
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert frozen.is_frozen() is True


def test_png_rasterizer_loads_prebaked_glyph(tmp_path):
    svg = "<svg>codex</svg>"
    Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (10, 20, 30, 255)).save(
        tmp_path / frozen.glyph_png_name(svg)
    )
    img = frozen.make_png_rasterizer(str(tmp_path))(svg, ICON_SIZE)
    assert img.mode == "RGBA" and img.size == (ICON_SIZE, ICON_SIZE)
    assert img.getpixel((0, 0)) == (10, 20, 30, 255)


def test_elgato_shim_reexports_same_objects():
    from herdeck.elgato import frozen as efrozen

    assert efrozen.is_frozen is frozen.is_frozen
    assert efrozen.make_png_rasterizer is frozen.make_png_rasterizer
    assert efrozen.BAKE_SIZE == frozen.BAKE_SIZE
