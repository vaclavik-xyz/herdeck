import sys

from PIL import Image

from herdeck.elgato import frozen
from herdeck.icons import ICON_SIZE


def test_bake_size_matches_icon_size():
    assert frozen.BAKE_SIZE == ICON_SIZE


def test_glyph_png_name_is_stable_and_content_keyed():
    a = frozen.glyph_png_name("<svg>codex</svg>")
    assert a == frozen.glyph_png_name("<svg>codex</svg>")  # deterministic
    assert a != frozen.glyph_png_name("<svg>other</svg>")  # content-keyed
    assert a.endswith(".png") and "/" not in a


def test_is_frozen_reflects_sys_frozen(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert frozen.is_frozen() is False
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert frozen.is_frozen() is True


def test_png_rasterizer_loads_prebaked_glyph(tmp_path):
    svg = "<svg>codex</svg>"
    baked = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (10, 20, 30, 255))
    baked.save(tmp_path / frozen.glyph_png_name(svg))
    rasterize = frozen.make_png_rasterizer(str(tmp_path))
    img = rasterize(svg, ICON_SIZE)
    assert isinstance(img, Image.Image)
    assert img.mode == "RGBA" and img.size == (ICON_SIZE, ICON_SIZE)
    assert img.getpixel((0, 0)) == (10, 20, 30, 255)


def test_png_rasterizer_resizes_to_requested_size(tmp_path):
    svg = "<svg>x</svg>"
    Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (1, 2, 3, 255)).save(
        tmp_path / frozen.glyph_png_name(svg)
    )
    img = frozen.make_png_rasterizer(str(tmp_path))(svg, 64)
    assert img.size == (64, 64)


def test_png_rasterizer_never_imports_cairosvg(tmp_path, monkeypatch):
    import builtins

    svg = "<svg>x</svg>"
    Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 255)).save(
        tmp_path / frozen.glyph_png_name(svg)
    )
    real_import = builtins.__import__

    def guard(name, *a, **k):
        assert name != "cairosvg", "frozen rasterizer must not import cairosvg"
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", guard)
    frozen.make_png_rasterizer(str(tmp_path))(svg, ICON_SIZE)
