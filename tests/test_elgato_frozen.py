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


def test_prerasterize_writes_content_keyed_pngs(tmp_path):
    import pytest

    pytest.importorskip("cairosvg")  # build-time dep; present in the dev extra

    src = tmp_path / "assets"
    src.mkdir()
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        '<rect width="10" height="10" fill="#fff"/></svg>'
    )
    (src / "codex.svg").write_text(svg, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()

    written = frozen.prerasterize_assets(str(src), str(out), frozen.BAKE_SIZE)

    expected = frozen.glyph_png_name(svg)
    assert written == [expected]
    baked = out / expected
    assert baked.exists()
    with Image.open(baked) as im:
        assert im.size == (frozen.BAKE_SIZE, frozen.BAKE_SIZE)
    # The runtime loader round-trips against what the baker wrote.
    assert frozen.make_png_rasterizer(str(out))(svg, frozen.BAKE_SIZE).size == (
        frozen.BAKE_SIZE,
        frozen.BAKE_SIZE,
    )


def test_prerasterize_into_same_dir_is_idempotent(tmp_path, monkeypatch):
    cairosvg = __import__("pytest").importorskip("cairosvg")
    src = tmp_path / "assets"
    src.mkdir()
    (src / "x.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    first = frozen.prerasterize_assets(str(src), str(src), frozen.BAKE_SIZE)
    mtime = (src / first[0]).stat().st_mtime_ns

    # The second run over an already-baked dir re-encodes nothing: stable return,
    # no svg2png call, and the existing PNG's mtime is untouched.
    calls = []
    real_svg2png = cairosvg.svg2png
    monkeypatch.setattr(
        cairosvg, "svg2png", lambda *a, **k: calls.append(1) or real_svg2png(*a, **k)
    )
    second = frozen.prerasterize_assets(str(src), str(src), frozen.BAKE_SIZE)
    assert second == first  # re-running over the same dir is stable
    assert calls == []  # nothing re-encoded
    assert (src / first[0]).stat().st_mtime_ns == mtime  # file left untouched
