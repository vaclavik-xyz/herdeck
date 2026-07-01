import os

from PIL import Image, ImageDraw

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


def test_theme_server_accent_color_renders(tmp_path):
    from herdeck.driver.base import TileView

    p = make_provider(tmp_path)
    tile = TileView(
        0,
        "",
        "blue",
        agent_type="claude",
        repo="api",
        branch="",
        status_text="IDLE",
        server_tag="DEV",
        server_accent="#334455",
    )
    other = TileView(
        0,
        "",
        "blue",
        agent_type="claude",
        repo="api",
        branch="",
        status_text="IDLE",
        server_tag="DEV",
        server_accent="#553344",
    )

    assert p.render_tile_bytes(tile)[:4] == b"\x89PNG"
    assert p.render_tile_bytes(tile) != p.render_tile_bytes(other)


def test_theme_status_color_name_renders_distinct_from_dim(tmp_path):
    from herdeck.driver.base import TileView

    p = make_provider(tmp_path)
    pink = TileView(0, "", "pink", agent_type="claude", repo="api", status_text="IDLE")
    dim = TileView(0, "", "dim", agent_type="claude", repo="api", status_text="IDLE")

    assert p.render_tile_bytes(pink) != p.render_tile_bytes(dim)


def test_compose_panel_uses_theme_color_background():
    from herdeck.driver.base import PanelView
    from herdeck.icons import compose_panel

    themed = compose_panel(PanelView("needs you", [], "pink"))
    default = compose_panel(PanelView("agents", [], "grey"))

    assert themed.getpixel((0, 0)) != default.getpixel((0, 0))


def test_drill_option_subtext_is_drawn_under_label(tmp_path):
    # A drill choice tile renders the big number (label) AND the small choice
    # text (subtext) under it, so the subtext must change the rendered bytes.
    from herdeck.driver.base import TileView

    p = make_provider(tmp_path)
    plain = p.render_tile_bytes(TileView(0, "1", "blue"))
    with_sub = p.render_tile_bytes(TileView(0, "1", "blue", subtext="Yes, proceed and apply"))
    assert plain != with_sub


def _tile_ns(**over):
    from types import SimpleNamespace

    base = dict(
        color="green", label="repo", subtext=None, agent_type="claude", spinner=None,
        repo="repo", branch="main", status_text="idle", time_text="1m",
        server_tag=None, server_accent=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _provider(cache_dir, assets_dir):
    return IconProvider(
        cache_dir=str(cache_dir),
        slug_map={"claude": None},
        fetch=lambda s: None,
        rasterize=_fake_rasterize,
        assets_dir=str(assets_dir),
    )


def _assets_dir(tmp_path, sub, *svgs):
    d = tmp_path / sub
    d.mkdir()
    for s in svgs:
        (d / s).write_text(f"<svg>{s}</svg>")
    return d


def test_render_cache_key_changes_when_bundled_asset_set_changes(tmp_path):
    """Adding a bundled glyph must invalidate the on-disk render cache, else an
    upgraded app serves the stale letter-glyph tile for the newly bundled agent
    (the Q1-on-upgrade regression seen on macbench)."""
    a = _assets_dir(tmp_path, "a", "codex.svg")
    b = _assets_dir(tmp_path, "b", "codex.svg", "claude.svg")  # one extra bundled mark

    tile = _tile_ns()
    name_a = _provider(tmp_path / "ca", a).render_tile(tile)
    name_b = _provider(tmp_path / "cb", b).render_tile(tile)
    name_a2 = _provider(tmp_path / "ca2", a).render_tile(tile)

    assert name_a != name_b   # different bundled-asset set -> distinct cache key
    assert name_a == name_a2  # same asset set -> stable cache key (still cacheable)


def test_icon_for_cache_key_changes_with_bundled_asset_set(tmp_path):
    a = _assets_dir(tmp_path, "a", "codex.svg")
    b = _assets_dir(tmp_path, "b", "codex.svg", "claude.svg")

    name_a = _provider(tmp_path / "ca", a).icon_for("claude", "green")
    name_b = _provider(tmp_path / "cb", b).icon_for("claude", "green")
    assert name_a != name_b


def test_render_cache_key_changes_when_same_named_asset_content_changes(tmp_path):
    """Same filename + same byte size but DIFFERENT content (a re-baked/edited
    glyph) must still invalidate the cache, so the fingerprint hashes file
    contents — not just name+size (roborev)."""
    a = tmp_path / "a"
    a.mkdir()
    (a / "claude.svg").write_text("<svg>aaa</svg>")
    b = tmp_path / "b"
    b.mkdir()
    (b / "claude.svg").write_text("<svg>bbb</svg>")  # same length, different bytes
    assert (a / "claude.svg").stat().st_size == (b / "claude.svg").stat().st_size
    tile = _tile_ns()
    name_a = _provider(tmp_path / "ca", a).render_tile(tile)
    name_b = _provider(tmp_path / "cb", b).render_tile(tile)
    assert name_a != name_b


def test_comet_overlay_is_phase_distinct_and_sized(tmp_path):
    from PIL import Image as _Image

    p = _provider(tmp_path / "co", _assets_dir(tmp_path, "a", "claude.svg"))
    a = p._comet_overlay(62, 0, 2, 4)
    b = p._comet_overlay(62, 2, 2, 4)
    assert isinstance(a, _Image.Image) and a.size == (62, 62)
    assert a.tobytes() != b.tobytes()  # the comet head sweeps with the phase


def _asym_rasterize(svg, size):
    # Left half white, right half transparent — so a rotation or a rescale
    # visibly changes the pixels (a uniform square would not under a 90° turn).
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle([0, 0, size // 2, size], fill=(255, 255, 255, 255))
    return img


def _anim_provider(cache_dir, assets_dir):
    return IconProvider(
        cache_dir=str(cache_dir),
        slug_map={"claude": None},
        fetch=lambda s: None,
        rasterize=_asym_rasterize,
        assets_dir=str(assets_dir),
    )


def _agent_tile(**over):
    from types import SimpleNamespace

    base = dict(
        color="green", label="", subtext=None, agent_type="claude", spinner=1,
        repo="api", branch="main", status_text="WORKING", time_text="1m",
        server_tag=None, server_accent=None, working_animation="spin", tile_fill="none",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_each_working_animation_renders_distinctly(tmp_path):
    p = _anim_provider(tmp_path / "c", _assets_dir(tmp_path, "a", "claude.svg"))
    styles = ("spin", "comet", "pulse", "sweep", "none")
    out = {s: p.render_tile_bytes(_agent_tile(working_animation=s)) for s in styles}
    assert len(set(out.values())) == 5  # all five working styles are mutually distinct


def test_each_tile_fill_renders_distinctly(tmp_path):
    p = _anim_provider(tmp_path / "c", _assets_dir(tmp_path, "a", "claude.svg"))
    fills = ("none", "tint", "solid")
    # idle (spinner=None) so the only variable is the fill style, not animation
    out = {f: p.render_tile_bytes(_agent_tile(tile_fill=f, spinner=None)) for f in fills}
    assert len(set(out.values())) == 3  # none / tint / solid are mutually distinct


def test_tile_fill_is_part_of_cache_key(tmp_path):
    p = _anim_provider(tmp_path / "c", _assets_dir(tmp_path, "a", "claude.svg"))
    a = p.render_tile(_agent_tile(tile_fill="none", spinner=None))
    b = p.render_tile(_agent_tile(tile_fill="solid", spinner=None))
    assert a != b  # the fill style is part of the tile cache key


def test_solid_fill_paints_whole_tile_the_status_colour(tmp_path):
    import io

    from PIL import Image

    from herdeck.driver.base import COLORS

    p = _anim_provider(tmp_path / "c", _assets_dir(tmp_path, "a", "claude.svg"))
    png = p.render_tile_bytes(
        _agent_tile(color="cyan", tile_fill="solid", spinner=None, status_text="DONE")
    )
    img = Image.open(io.BytesIO(png)).convert("RGB")
    assert img.getpixel((2, 2)) == COLORS["cyan"]  # top-left bg = full status colour


def test_none_working_matches_static_idle_and_differs_from_spin(tmp_path):
    p = _anim_provider(tmp_path / "c", _assets_dir(tmp_path, "a", "claude.svg"))
    none_working = p.render_tile_bytes(_agent_tile(working_animation="none", spinner=1))
    idle_static = p.render_tile_bytes(_agent_tile(working_animation="none", spinner=None))
    assert none_working == idle_static  # "none" disables animation -> renders like idle
    spin = p.render_tile_bytes(_agent_tile(working_animation="spin", spinner=1))
    assert none_working != spin


def test_idle_tile_renders_identically_across_styles(tmp_path):
    p = _anim_provider(tmp_path / "c", _assets_dir(tmp_path, "a", "claude.svg"))
    a = p.render_tile_bytes(_agent_tile(working_animation="spin", spinner=None))
    b = p.render_tile_bytes(_agent_tile(working_animation="sweep", spinner=None))
    assert a == b  # idle tiles ignore the style entirely


def test_working_tile_cache_key_includes_animation(tmp_path):
    p = _anim_provider(tmp_path / "c", _assets_dir(tmp_path, "a", "claude.svg"))
    spin = p.render_tile(_agent_tile(working_animation="spin", spinner=1))
    pulse = p.render_tile(_agent_tile(working_animation="pulse", spinner=1))
    assert spin != pulse  # style is part of the working-tile cache key


def test_idle_tile_cache_key_ignores_animation(tmp_path):
    p = _anim_provider(tmp_path / "c", _assets_dir(tmp_path, "a", "claude.svg"))
    a = p.render_tile(_agent_tile(working_animation="spin", spinner=None))
    b = p.render_tile(_agent_tile(working_animation="pulse", spinner=None))
    assert a == b  # idle tiles share one cache key regardless of style (no churn)
