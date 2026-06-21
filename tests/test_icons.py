import os

from PIL import Image

from herdeck.icons import IconProvider


def _fake_fetch(slug):  # pretend Simple Icons returns an SVG for known slugs
    return f"<svg>{slug}</svg>" if slug in ("claude", "cursor") else None


def _fake_rasterize(svg, size):  # pretend rasterizer makes a transparent glyph
    return Image.new("RGBA", (size, size), (255, 255, 255, 255))


def make_provider(tmp_path, overrides=None):
    return IconProvider(
        cache_dir=str(tmp_path),
        slug_map={"claude": "claude", "cursor": "cursor", "codex": None},
        overrides_dir=str(overrides) if overrides else None,
        fetch=_fake_fetch,
        rasterize=_fake_rasterize,
    )


def test_icon_for_known_slug_writes_png(tmp_path):
    p = make_provider(tmp_path)
    name = p.icon_for("claude", "green")
    path = os.path.join(str(tmp_path), name)
    assert os.path.exists(path)
    with Image.open(path) as im:
        assert im.size == (196, 196)


def test_unknown_slug_falls_back_to_glyph(tmp_path):
    p = make_provider(tmp_path)
    name = p.icon_for("codex", "blue")  # slug None -> glyph
    assert os.path.exists(os.path.join(str(tmp_path), name))


def test_user_override_takes_precedence(tmp_path):
    overrides = tmp_path / "ov"
    overrides.mkdir()
    Image.new("RGBA", (196, 196), (1, 2, 3, 255)).save(overrides / "claude.png")
    p = make_provider(tmp_path, overrides=overrides)
    name = p.icon_for("claude", "green")
    # the produced icon must derive from the override (a specific pixel survives)
    with Image.open(os.path.join(str(tmp_path), name)) as im:
        img = im.convert("RGBA")
        assert img.size == (196, 196)
        assert img.getpixel((98, 98))[:3] == (1, 2, 3)


def test_results_are_cached(tmp_path):
    calls = []
    p = IconProvider(
        cache_dir=str(tmp_path),
        slug_map={"claude": "claude"},
        overrides_dir=None,
        fetch=lambda s: (calls.append(s), "<svg/>")[1],
        rasterize=_fake_rasterize,
    )
    p.icon_for("claude", "green")
    p.icon_for("claude", "green")
    assert calls.count("claude") <= 1  # fetched at most once


def test_spinner_cache_is_bounded_to_frame_set(tmp_path):
    from herdeck.icons import SPINNER_FRAMES

    seen = set()
    p = IconProvider(
        cache_dir=str(tmp_path / "spin"),
        slug_map={"claude": None},
        overrides_dir=None,
        fetch=_fake_fetch,
        rasterize=_fake_rasterize,
    )
    for phase in range(0, SPINNER_FRAMES * 3):
        seen.add(p.icon_for("claude", "green", spinner=phase))
    # phases cycle: at most SPINNER_FRAMES distinct files, not 3x as many
    assert len(seen) == SPINNER_FRAMES
    # phase 0 and phase SPINNER_FRAMES produce the same cached file
    assert p.icon_for("claude", "green", 0) == p.icon_for("claude", "green", SPINNER_FRAMES)


def test_agent_type_with_path_chars_is_sanitized(tmp_path):
    p = IconProvider(
        cache_dir=str(tmp_path),
        slug_map={},
        overrides_dir=None,
        fetch=_fake_fetch,
        rasterize=_fake_rasterize,
    )
    name = p.icon_for("../../evil", "green")
    # no traversal: the written file stays inside cache_dir
    assert "/" not in name and ".." not in name
    assert os.path.exists(os.path.join(str(tmp_path), name))


def test_sanitized_names_do_not_collide(tmp_path):
    p = IconProvider(
        cache_dir=str(tmp_path),
        slug_map={},
        overrides_dir=None,
        fetch=_fake_fetch,
        rasterize=_fake_rasterize,
    )
    n1 = p.icon_for("a/b", "green")
    n2 = p.icon_for("a_b", "green")
    assert n1 != n2  # distinct raw types -> distinct cache files


def test_letter_glyph_is_large_when_font_available(tmp_path):
    from herdeck.icons import _load_big_font

    if _load_big_font() is None:
        return  # no scalable font on this system; bitmap fallback is acceptable
    p = make_provider(tmp_path)
    name = p.icon_for("zeta", "blue")  # unknown agent -> letter glyph
    with Image.open(os.path.join(str(tmp_path), name)) as src:
        lum = src.convert("L")
    white = sum(lum.histogram()[201:])  # bright (near-white letter) pixels
    assert white > 800  # a big bold letter (inset) covers a real area


def test_render_tile_agent_and_control(tmp_path):
    from herdeck.driver.base import TileView

    p = make_provider(tmp_path)
    agent = TileView(
        0,
        "",
        "amber",
        agent_type="claude",
        repo="api",
        branch="feat/x",
        status_text="BLOCKED",
        time_text="1m",
    )
    name = p.render_tile(agent)
    with Image.open(os.path.join(str(tmp_path), name)) as im:
        assert im.size == (196, 196)
    n_stop = p.render_tile(TileView(0, "Stop", "red"))
    n_back = p.render_tile(TileView(0, "Back", "grey"))
    assert n_stop != n_back and n_stop != name


def test_agent_tile_with_server_tag_renders(tmp_path):
    from herdeck.driver.base import TileView

    p = make_provider(tmp_path)
    base = TileView(
        0,
        "",
        "blue",
        agent_type="claude",
        repo="api",
        branch="x",
        status_text="IDLE",
        time_text="1m",
    )
    tagged = TileView(
        0,
        "",
        "blue",
        agent_type="claude",
        repo="api",
        branch="x",
        status_text="IDLE",
        time_text="1m",
        server_tag="WBX",
        server_accent="teal",
    )

    assert p.render_tile_bytes(base) != p.render_tile_bytes(tagged)
