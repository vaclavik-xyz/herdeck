import io as _io
import logging
import os

import pytest
from PIL import Image, ImageDraw

from herdeck.driver.base import TileView as _TileView
from herdeck.icons import IconProvider
from herdeck.project_icon_discovery import icon_hash
from herdeck.project_icons import ProjectIconStore, default_store


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


def test_backend_label_is_plain_text_without_server_accent_box(tmp_path):
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
    assert p.render_tile_bytes(tile) == p.render_tile_bytes(other)


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


def test_compose_usage_panel_draws_instrument_cards_and_rails():
    from herdeck.driver.base import PanelGauge, PanelView
    from herdeck.icons import compose_panel

    panel = PanelView(
        "4 agents",
        ["W2 · I2 · D0"],
        gauges=[
            PanelGauge("Claude", "5H", 50, color="orange"),
            PanelGauge("Claude", "7D", 70, color="orange"),
            PanelGauge("Codex", "5H", 10, color="teal"),
            PanelGauge("Codex", "7D", 90, color="teal"),
        ],
    )
    image = compose_panel(panel)

    assert image.getpixel((0, 0)) == (49, 55, 65)  # lighter slate usage canvas
    assert image.getpixel((18, 58)) == (66, 73, 85)  # first instrument card
    assert image.getpixel((28, 107)) == (220, 115, 35)  # Claude rail
    assert image.getpixel((200, 107)) == (104, 112, 126)  # unused rail segment


def test_single_usage_gauge_spans_panel_and_draws_reset_hint():
    from herdeck.driver.base import PanelGauge, PanelView
    from herdeck.icons import compose_panel

    without_reset = compose_panel(
        PanelView("11 agents", gauges=[PanelGauge("Codex", "7D", 38, color="teal")])
    )
    with_reset = compose_panel(
        PanelView(
            "11 agents",
            gauges=[PanelGauge("Codex", "7D", 38, hint="obnova 19.7. 20:59", color="teal")],
        )
    )

    assert with_reset.getpixel((440, 60)) == (66, 73, 85)  # one card uses full width
    assert with_reset.tobytes() != without_reset.tobytes()


def test_panel_cache_key_includes_usage_gauges():
    from herdeck.driver.base import PanelGauge, PanelView

    low = PanelView("usage", gauges=[PanelGauge("Codex", "5H", 10, color="teal")])
    high = PanelView("usage", gauges=[PanelGauge("Codex", "5H", 90, color="teal")])
    assert low.cache_key() != high.cache_key()
    localized = PanelView(
        "usage",
        gauges=[PanelGauge("Codex", "5H", 10, color="teal")],
        gauge_meta="využito / obnova",
    )
    assert low.cache_key() != localized.cache_key()


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
        color="green",
        label="repo",
        subtext=None,
        agent_type="claude",
        spinner=None,
        repo="repo",
        branch="main",
        status_text="idle",
        time_text="1m",
        server_tag=None,
        server_accent=None,
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
    (the Q1-on-upgrade regression seen after an in-place upgrade)."""
    a = _assets_dir(tmp_path, "a", "codex.svg")
    b = _assets_dir(tmp_path, "b", "codex.svg", "claude.svg")  # one extra bundled mark

    tile = _tile_ns()
    name_a = _provider(tmp_path / "ca", a).render_tile(tile)
    name_b = _provider(tmp_path / "cb", b).render_tile(tile)
    name_a2 = _provider(tmp_path / "ca2", a).render_tile(tile)

    assert name_a != name_b  # different bundled-asset set -> distinct cache key
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
        color="green",
        label="",
        subtext=None,
        agent_type="claude",
        spinner=1,
        repo="api",
        branch="main",
        status_text="WORKING",
        time_text="1m",
        server_tag=None,
        server_accent=None,
        working_animation="spin",
        tile_fill="none",
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


def test_solid_fill_text_takes_the_higher_contrast_ink():
    from herdeck.driver.base import COLORS
    from herdeck.icons import DARK_INK, LIGHT_INK, _tile_text_colors

    # red is dark enough for white ink; the whole text block follows it
    repo, branch, time_c, word = _tile_text_colors("solid", COLORS["red"], COLORS["red"])
    assert repo == word == time_c == LIGHT_INK
    assert min(branch) > 200  # quieter, but still near-white
    # a bright solid (green) flips to dark text instead
    _, gbranch, gtime, gword = _tile_text_colors("solid", COLORS["green"], COLORS["green"])
    assert gword == gtime == DARK_INK and max(gbranch) < 80
    # violet/grey sat on the old Rec.601 threshold: their elapsed time read at
    # ~2.9/3.1:1 — it now uses the same ink as the repo name
    for colour in ("violet", "grey"):
        repo, _, time_c, _ = _tile_text_colors("solid", COLORS[colour], COLORS[colour])
        assert time_c == repo
    # none keeps the dim-grey subtext + the accent status word where it passes
    _, nbranch, _, nword = _tile_text_colors("none", (26, 26, 30), COLORS["green"])
    assert nbranch == (180, 180, 188) and nword == COLORS["green"]


def _all_tile_backgrounds():
    from herdeck.driver.base import COLORS
    from herdeck.icons import TILE_BG, _tint_bg

    for name, accent in COLORS.items():
        for fill, bg in (("none", TILE_BG), ("tint", _tint_bg(accent)), ("solid", accent)):
            yield name, fill, accent, bg


def test_every_status_text_meets_wcag_aa_on_every_fill():
    """Status word, repo, branch and elapsed time reach 4.5:1 for every palette
    colour x tile_fill (IDLE on none read at 4.0:1, UNKNOWN on tint 3.3:1)."""
    from herdeck.icons import TEXT_CONTRAST, _contrast, _tile_text_colors

    failures = []
    for name, fill, accent, bg in _all_tile_backgrounds():
        colors = _tile_text_colors(fill, bg, accent)
        for role, c in zip(("repo", "branch", "time", "word"), colors, strict=True):
            ratio = _contrast(c, bg)
            if ratio < TEXT_CONTRAST:
                failures.append(f"{name}/{fill}/{role}: {ratio:.2f}")
    assert not failures, failures


def test_status_word_keeps_its_hue_where_it_already_passes():
    from herdeck.driver.base import COLORS
    from herdeck.icons import TILE_BG, _tile_text_colors

    _, _, _, word = _tile_text_colors("none", TILE_BG, COLORS["blue"])
    assert word != COLORS["blue"]  # lightened (4.0:1 raw)
    assert word[2] > word[0]  # ...but still reads blue, not white
    assert word != (255, 255, 255)


def test_label_tiles_flip_to_dark_ink_on_bright_colours(tmp_path):
    """Drill/label tiles were always white: 2.1:1 on amber, 2.0:1 on cyan."""
    import io

    from herdeck.driver.base import COLORS, TileView
    from herdeck.icons import TEXT_CONTRAST, _contrast

    p = make_provider(tmp_path)
    for colour in ("amber", "cyan", "green", "blue", "red", "grey", "dim"):
        bg = COLORS[colour]
        for tile in (
            TileView(0, "Back", colour),
            TileView(0, "2", colour, subtext="No, and tell Claude what to do"),
        ):
            img = Image.open(io.BytesIO(p.render_tile_bytes(tile))).convert("RGB")
            # the most contrasting pixel on the tile is the text ink
            ink = max((c for _, c in img.getcolors(1 << 16)), key=lambda px: _contrast(px, bg))
            assert _contrast(ink, bg) >= TEXT_CONTRAST, (colour, ink)


def test_solid_fill_sweep_still_animates(tmp_path):
    p = _anim_provider(tmp_path / "c", _assets_dir(tmp_path, "a", "claude.svg"))
    # solid drops the static bottom bar, but a sweeping working tile must still
    # animate — the sweep is drawn in contrasting colours over the solid fill.
    a = p.render_tile_bytes(_agent_tile(tile_fill="solid", working_animation="sweep", spinner=1))
    b = p.render_tile_bytes(_agent_tile(tile_fill="solid", working_animation="sweep", spinner=4))
    idle = p.render_tile_bytes(
        _agent_tile(tile_fill="solid", working_animation="sweep", spinner=None)
    )
    assert a != b  # moves across phases
    assert a != idle  # and differs from the static (non-working) tile


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


# --- cache eviction + in-memory bytes cache (audit: cache-unbounded) ---


def test_init_prunes_stale_generated_pngs(tmp_path):
    import time as _time

    stale_tile = tmp_path / "tile_deadbeef.png"
    stale_icon = tmp_path / "icon_v2_0_stale_green.png"
    # Panel PNGs are content-keyed too (usage percentages/reset times mint
    # fresh names regularly) — exempting them grew the cache without bound.
    stale_panel = tmp_path / "panel_abc123def456.png"
    stale_panel_half = tmp_path / "panel_abc123def456_l.png"
    fresh_tile = tmp_path / "tile_fresh.png"
    foreign = tmp_path / "user_custom.png"
    for f in (stale_tile, stale_icon, stale_panel, stale_panel_half, fresh_tile, foreign):
        f.write_bytes(b"png")
    old = _time.time() - 48 * 3600
    for f in (stale_tile, stale_icon, stale_panel, stale_panel_half, foreign):
        os.utime(f, (old, old))
    make_provider(tmp_path)
    assert not stale_tile.exists()
    assert not stale_icon.exists()
    assert not stale_panel.exists()
    assert not stale_panel_half.exists()
    assert fresh_tile.exists()  # fresh generated files survive the age cutoff
    assert foreign.exists()  # non-generated names are never touched, however old


def test_render_tile_bytes_serves_from_memory_without_recreating_file(tmp_path):
    p = make_provider(tmp_path)
    tile = _tile_ns()
    first = p.render_tile_bytes(tile)
    for f in tmp_path.glob("tile_*.png"):
        f.unlink()
    assert p.render_tile_bytes(tile) == first
    # memory-cache hit must not touch the disk cache at all
    assert not list(tmp_path.glob("tile_*.png"))


def test_render_tile_recreates_pruned_file_for_device_path(tmp_path):
    p = make_provider(tmp_path)
    tile = _tile_ns()
    name = p.render_tile(tile)
    (tmp_path / name).unlink()
    assert p.render_tile(tile) == name
    assert (tmp_path / name).exists()  # strmdck reads the file by name


def test_cache_hit_refreshes_old_mtime_so_active_files_survive_prune(tmp_path):
    import time as _time

    from herdeck.icons import prune_generated

    p = make_provider(tmp_path)
    tile = _tile_ns()
    name = p.render_tile(tile)
    path = tmp_path / name
    old = _time.time() - 2 * 3600
    os.utime(path, (old, old))
    assert p.render_tile(tile) == name  # cache hit on a stale-mtime file
    assert prune_generated(str(tmp_path)) == 0  # hit refreshed mtime -> not stale
    assert path.exists()


def test_wrap_marks_cut_tail_with_ellipsis():
    """Dropping words beyond max_lines must be visible — an unmarked truncation
    of a permission scope reads as the complete text (audit: wrap-ellipsis)."""
    from PIL import Image, ImageDraw

    from herdeck.icons import _font, _wrap

    d = ImageDraw.Draw(Image.new("RGB", (196, 196)))
    f = _font(22)
    cut = _wrap(d, "Yes, and don't ask again for rm commands in /home/user/projects", f, 180, 3)
    assert len(cut) == 3
    assert cut[-1].endswith("…")
    intact = _wrap(d, "Yes", f, 180, 3)
    assert intact == ["Yes"]  # nothing cut -> no spurious ellipsis


def test_panel_body_lines_wrap_by_pixel_width_without_losing_words():
    """The panel body wraps logical lines with the ACTUAL font and pixel budget
    (audit: panel-pixel-wrap)."""
    from PIL import Image, ImageDraw

    from herdeck.icons import PANEL_W, _font, _panel_body_lines

    d = ImageDraw.Draw(Image.new("RGB", (PANEL_W, 196)))
    f = _font(24)
    text = "Claude needs your permission to run the following command right now"
    # max_lines high enough for ANY platform font (CI's Linux fonts are wider
    # than macOS ones), so the no-words-lost assertion is metric-independent
    lines = _panel_body_lines(d, [text], f, PANEL_W - 32, max_lines=10)
    assert len(lines) >= 2
    for line in lines:
        assert d.textlength(line, font=f) <= PANEL_W - 32
    assert " ".join(lines) == text  # nothing silently dropped between lines
    overflow = text + " and then some more words that cannot possibly fit on this panel"
    lines = _panel_body_lines(d, [overflow], f, PANEL_W - 32)
    assert len(lines) == 3 and lines[-1].endswith("…")


def test_solid_bright_fill_darkens_the_agent_mark(tmp_path):
    """The white mark washed out on bright solid fills (amber 2.1:1) while the
    text flipped dark (audit: solid-mark-contrast)."""
    import io

    p = make_provider(tmp_path)
    bright = _tile_ns(color="amber", tile_fill="solid")
    img = Image.open(io.BytesIO(p.render_tile_bytes(bright))).convert("RGB")
    assert sum(img.getpixel((35, 35))) < 200  # dark ink inside the mark's box
    normal = _tile_ns(color="amber", tile_fill="none")
    img2 = Image.open(io.BytesIO(p.render_tile_bytes(normal))).convert("RGB")
    assert sum(img2.getpixel((35, 35))) > 500  # stays white on the dark bg


def test_launcher_tile_renders_dark_not_status_green(tmp_path):
    """The full-green launcher was pixel-identical to a WORKING agent tile
    under solid fill (audit: launcher-distinct-color)."""
    import io
    from types import SimpleNamespace

    p = make_provider(tmp_path)
    launcher = SimpleNamespace(
        color="launcher",
        label="+ New",
        subtext=None,
        agent_type=None,
        spinner=None,
        repo=None,
        branch=None,
        status_text=None,
        time_text=None,
        server_tag=None,
        server_accent=None,
    )
    img = Image.open(io.BytesIO(p.render_tile_bytes(launcher))).convert("RGB")
    assert img.getpixel((5, 5)) == (26, 26, 30)  # dark management background


def test_colour_override_marks_are_not_flattened_on_bright_solid(tmp_path):
    """A full-colour user override must render as supplied — only white
    mark-style glyphs get the solid-fill contrast flip (roborev 8280ace)."""
    import io

    overrides = tmp_path / "ov"
    overrides.mkdir()
    Image.new("RGBA", (196, 196), (200, 30, 30, 255)).save(overrides / "claude.png")
    p = make_provider(tmp_path / "cache", overrides=overrides)
    tile = _tile_ns(color="amber", tile_fill="solid")
    img = Image.open(io.BytesIO(p.render_tile_bytes(tile))).convert("RGB")
    r, g, b = img.getpixel((35, 35))
    assert r > 150 and g < 90  # the red override survived, no dark silhouette


def test_animation_none_ignores_spinner_phase_in_the_cache_name(tmp_path):
    """Style 'none' draws no spinner, so the phase must not mint new (pixel
    identical) filenames — that defeated the D200 identical-frame skip and
    caused a full page reload per tick (visible flicker)."""
    from herdeck.driver.base import TileView

    provider = make_provider(tmp_path)
    base = dict(index=0, label="repo", color="green", repo="repo", branch="main")
    t1 = TileView(**base, spinner=1, working_animation="none")
    t2 = TileView(**base, spinner=2, working_animation="none")
    assert provider._tile_name(t1)[0] == provider._tile_name(t2)[0]
    # animated styles DO vary by phase (that is the animation)
    s1 = TileView(**base, spinner=1, working_animation="spin")
    s2 = TileView(**base, spinner=2, working_animation="spin")
    assert provider._tile_name(s1)[0] != provider._tile_name(s2)[0]


def test_pulse_is_a_slow_low_churn_animation(tmp_path):
    """Pulse advances once per PULSE_SLOWDOWN ticks through PULSE_STATES frames —
    every animation frame is a full page reload on the D200, so the calm style
    must also be the cheapest one."""
    from herdeck.driver.base import TileView
    from herdeck.icons import PULSE_SLOWDOWN, PULSE_STATES

    provider = make_provider(tmp_path)
    base = dict(index=0, label="repo", color="green", repo="repo", branch="main")

    def name(phase):
        return provider._tile_name(TileView(**base, spinner=phase, working_animation="pulse"))[0]

    # within one slowdown window the name is stable (no per-tick churn)
    assert name(0) == name(PULSE_SLOWDOWN - 1)
    # across the window boundary it advances
    assert name(0) != name(PULSE_SLOWDOWN)
    # the whole cycle uses exactly PULSE_STATES distinct frames
    distinct = {name(p) for p in range(PULSE_SLOWDOWN * PULSE_STATES * 2)}
    assert len(distinct) == PULSE_STATES


def test_project_name_shrinks_before_truncating():
    from herdeck.icons import _fit_project_name
    draw = ImageDraw.Draw(Image.new("RGB", (196, 196)))
    short_font, short_lines = _fit_project_name(draw, "herdeck", 172)
    assert short_font.size == 31 and short_lines == ["herdeck"]
    font, lines = _fit_project_name(draw, "macdoktor-crm", 172)
    assert 20 <= font.size < 31
    assert lines == ["macdoktor-crm"]
    assert draw.textlength(lines[0], font=font) <= 172


def test_project_name_wraps_long_identifiers_without_losing_the_suffix():
    from herdeck.icons import _fit_project_name
    draw = ImageDraw.Draw(Image.new("RGB", (196, 196)))
    name = "macdoktor-crm-production"
    font, lines = _fit_project_name(draw, name, 172)
    assert font.size == 20 and len(lines) == 2
    assert "".join(lines) == name
    assert all(draw.textlength(line, font=font) <= 172 for line in lines)


def test_project_name_keeps_the_suffix_with_a_wide_font():
    # Regression for CI on Linux: DejaVu Bold is wide enough that the word
    # boundary split ("macdoktor-" + "crm-production") overflowed line two and
    # truncated the suffix. A width function that makes every glyph 13 px
    # reproduces that without depending on which fonts the host has.
    from herdeck.icons import _fit_project_name

    class WideDraw:
        def textlength(self, text, font=None):
            return 13.0 * len(text)

    font, lines = _fit_project_name(WideDraw(), "macdoktor-crm-production", 172)
    assert "".join(lines) == "macdoktor-crm-production"
    assert all(13.0 * len(line) <= 172 for line in lines)


def test_extreme_project_names_keep_a_readable_minimum_and_ellipsis():
    from herdeck.icons import _fit_project_name
    draw = ImageDraw.Draw(Image.new("RGB", (196, 196)))
    font, lines = _fit_project_name(draw, "W" * 100, 172)
    # never below 20px: at 18px the repo matched the branch line's size
    assert font.size == 20 and len(lines) == 2 and lines[-1].endswith("…")
    assert all(draw.textlength(line, font=font) <= 172 for line in lines)


def _draw():
    return ImageDraw.Draw(Image.new("RGB", (196, 196)))


def test_wrap_never_inserts_spaces_around_slashes():
    """The old ' / ' split rendered an approval as 'rm -rf / tmp / build'."""
    from herdeck.icons import _font, _wrap

    d, f = _draw(), _font(22)
    for text in ("rm -rf /tmp/build", "/home/user/x", "fix/x"):
        assert _wrap(d, text, f, 1000, 3) == [text]


def test_wrap_breaks_after_slash_and_glues_the_pieces_back():
    from herdeck.icons import _font, _wrap

    d, f = _draw(), _font(22)
    text = "rm -rf /home/user/projects/herdeck/build"
    lines = _wrap(d, text, f, 150, 5)
    assert len(lines) > 1
    assert all(d.textlength(line, font=f) <= 150 for line in lines)
    assert " / " not in " ".join(lines)
    # the pieces re-join to the original once the line-break spaces go
    assert "".join(lines).replace(" ", "") == text.replace(" ", "")
    # a path break keeps the slash on the upper line
    assert any(line.endswith("/") for line in lines[:-1])


def test_wrap_hard_splits_an_unbreakable_token_and_marks_the_cut():
    from herdeck.icons import _font, _wrap

    d, f = _draw(), _font(22)
    lines = _wrap(d, "x" * 200, f, 150, 2)
    assert len(lines) == 2 and lines[-1].endswith("…")
    assert all(d.textlength(line, font=f) <= 150 for line in lines)
    # a path that is cut at max_lines still ends in an ellipsis
    cut = _wrap(d, "rm -rf /tmp/build/and/a/lot/more/of/this/path", f, 120, 2)
    assert cut[-1].endswith("…")


def test_branch_keeps_short_names_and_drops_the_prefix_of_long_ones():
    from herdeck.icons import _fit_branch, _font

    d, f = _draw(), _font(18, bold=False)
    assert _fit_branch(d, "fix/x", f, 172) == ["fix/x"]
    assert _fit_branch(d, "main", f, 172) == ["main"]
    # the first line is no longer spent on "feature /"
    lines = _fit_branch(d, "feature/login-page-polish", f, 120)
    assert lines == ["…/login-page-polish"] or lines[0].startswith("…/login-")
    long = _fit_branch(d, "feature/very-long-branch-name-for-testing-truncation", f, 172)
    assert long[0].startswith("…/very-") and len(long) == 2
    assert all(d.textlength(line, font=f) <= 172 for line in long)


def test_bright_solid_comet_ring_is_dark_ink(tmp_path):
    """A white comet ring all but vanished on bright solid fills."""
    import io

    p = make_provider(tmp_path)
    ring = p._comet_overlay(62, 0, 2, 4, (0, 0, 0))
    opaque = [c for _, c in ring.getcolors(1 << 16) if c[3] > 200]
    assert opaque and all(max(px[:3]) < 40 for px in opaque)

    def ring_pixels(fill):
        tile = _tile_ns(color="green", tile_fill=fill, spinner=0,
                        working_animation="comet", status_text="WORKING")
        img = Image.open(io.BytesIO(p.render_tile_bytes(tile))).convert("RGB")
        # the ring's outer edge just right of the 46px logo box (x 58..64)
        return [img.getpixel((x, y)) for x in range(59, 65) for y in range(20, 50)]

    assert min(sum(px) for px in ring_pixels("solid")) < 150  # dark ring on green


def test_gauge_labels_are_neutral_and_upper_case(monkeypatch):
    from herdeck.driver.base import COLORS, PanelGauge, PanelView
    from herdeck.icons import _GAUGE_CARD, _GAUGE_LABEL, _contrast, compose_panel

    drawn = []
    real_text = ImageDraw.ImageDraw.text

    def spy(self, xy, text, *a, **kw):
        drawn.append((text, kw.get("fill")))
        return real_text(self, xy, text, *a, **kw)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", spy)
    compose_panel(PanelView("Usage", gauges=[PanelGauge("Claude", "5h", 42, color="violet")]))
    text, fill = next((t, c) for t, c in drawn if "CLAUDE" in t)
    assert text == "CLAUDE  5H"  # consistent case
    assert fill == _GAUGE_LABEL and fill != COLORS["violet"]
    assert _contrast(_GAUGE_LABEL, _GAUGE_CARD) >= 4.5


def test_render_tile_bytes_does_not_touch_the_disk(tmp_path):
    p = make_provider(tmp_path)
    data = p.render_tile_bytes(_tile_ns(time_text="7m"))
    assert data[:4] == b"\x89PNG"
    assert not list(tmp_path.glob("tile_*.png"))


def test_layered_frames_match_a_full_render(tmp_path):
    """Cached static base + per-frame motion must be pixel-identical to a
    fresh composition, frame by frame, for every animation style."""
    from herdeck.icons import SPINNER_FRAMES

    for anim in ("spin", "comet", "pulse", "sweep"):
        for fill in ("none", "solid"):
            warm = make_provider(tmp_path / f"w{anim}{fill}")
            for phase in range(SPINNER_FRAMES):
                tile = _tile_ns(spinner=phase, working_animation=anim, tile_fill=fill,
                                status_text="WORKING")
                cold = make_provider(tmp_path / f"c{anim}{fill}{phase}")
                assert warm.render_tile_bytes(tile) == cold.render_tile_bytes(tile), (
                    anim, fill, phase)
            # the whole cycle composed the static base exactly once
            assert len(warm._base_cache) == 1


def test_agent_tile_bottom_band_shows_tag_and_pin_at_readable_size(tmp_path):
    import io

    p = make_provider(tmp_path)
    plain = _tile_ns(branch="x", pinned=False)
    tagged = _tile_ns(branch="x", pinned=True, server_tag="macbench")
    a = Image.open(io.BytesIO(p.render_tile_bytes(plain))).convert("RGB")
    b = Image.open(io.BytesIO(p.render_tile_bytes(tagged))).convert("RGB")
    ys = [
        y
        for x in range(196)
        for y in range(158, 188)
        if a.getpixel((x, y)) != b.getpixel((x, y))
    ]
    assert ys and max(ys) - min(ys) >= 14  # ~16px-tall content, not a 12px speck


RED = (220, 20, 20, 255)


def _img_bytes(color, size=(32, 32), fmt="PNG", **save):
    buf = _io.BytesIO()
    Image.new("RGBA", size, color).save(buf, fmt, **save)
    return buf.getvalue()


def _stored(store, data, mime="image/png"):
    h = icon_hash(data)
    store.put(h, mime, data)
    return h


def _project_provider(tmp_path, store, rasterize=_fake_rasterize):
    return IconProvider(
        cache_dir=str(tmp_path / "cache"),
        slug_map={"claude": None},
        fetch=lambda s: None,
        rasterize=rasterize,
        assets_dir=None,
        project_icons=store,
    )


def _project_tile(**over):
    base = dict(
        index=0,
        label="",
        color="blue",
        agent_type="claude",
        repo="shop",
        branch="main",
        status_text="IDLE",
        time_text="1m",
        tile_icon="project",
        project_icon=None,
        project_name="shop",
    )
    base.update(over)
    return _TileView(**base)


def _px(png, xy):
    return Image.open(_io.BytesIO(png)).convert("RGB").getpixel(xy)


def _close(a, b, tol=4):
    return all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


def test_provider_defaults_to_the_shared_store(tmp_path):
    p = IconProvider(cache_dir=str(tmp_path), slug_map={}, fetch=lambda s: None)
    assert p._project_icons is default_store()


def test_each_tile_icon_mode_renders_distinctly(tmp_path):
    store = ProjectIconStore()
    h = _stored(store, _img_bytes(RED))
    p = _project_provider(tmp_path, store)
    out = {
        mode: p.render_tile_bytes(_project_tile(tile_icon=mode, project_icon=h))
        for mode in ("agent", "project", "both")
    }
    assert len(set(out.values())) == 3


def test_project_mode_shows_the_favicon_in_the_logo_box(tmp_path):
    store = ProjectIconStore()
    p = _project_provider(tmp_path, store)
    png = p.render_tile_bytes(_project_tile(project_icon=_stored(store, _img_bytes(RED))))
    assert _close(_px(png, (35, 35)), RED[:3])


def test_tile_icon_and_hash_are_part_of_the_cache_key(tmp_path):
    store = ProjectIconStore()
    a = _stored(store, _img_bytes(RED))
    b = _stored(store, _img_bytes((20, 20, 220, 255)))
    p = _project_provider(tmp_path, store)
    names = {
        p._tile_name(_project_tile(tile_icon=mode, project_icon=h))[0]
        for mode, h in (("agent", a), ("project", a), ("project", b), ("both", a))
    }
    assert len(names) == 4


def test_spin_renders_as_comet_around_a_project_icon(tmp_path):
    store = ProjectIconStore()
    h = _stored(store, _img_bytes(RED))
    p = _project_provider(tmp_path, store)

    def render(mode, anim):
        return p.render_tile_bytes(
            _project_tile(
                tile_icon=mode, project_icon=h, color="green", status_text="WORKING",
                spinner=3, working_animation=anim,
            )
        )

    assert render("project", "spin") == render("project", "comet")
    assert render("agent", "spin") != render("agent", "comet")


def test_pulse_scales_the_project_icon(tmp_path):
    store = ProjectIconStore()
    h = _stored(store, _img_bytes(RED))
    p = _project_provider(tmp_path, store)
    frames = {
        p.render_tile_bytes(
            _project_tile(project_icon=h, color="green", spinner=raw, working_animation="pulse")
        )
        for raw in (0, 5)  # effective pulse phases 0 and 1
    }
    assert len(frames) == 2


def test_monogram_is_deterministic_per_repo():
    from herdeck.icons import _monogram_image

    assert _monogram_image("herdeck").tobytes() == _monogram_image("herdeck").tobytes()
    assert _monogram_image("herdeck").tobytes() != _monogram_image("api").tobytes()
    assert _monogram_image("").size == (196, 196)


def test_missing_icon_renders_a_per_repo_monogram(tmp_path):
    p = _project_provider(tmp_path, ProjectIconStore())
    shop = p.render_tile_bytes(_project_tile(project_name="shop"))
    blog = p.render_tile_bytes(_project_tile(project_name="blog"))
    assert shop != blog


def test_corrupt_icon_falls_back_to_the_monogram_and_logs_once(tmp_path, caplog):
    store = ProjectIconStore()
    bad = _stored(store, b"definitely not an image")
    p = _project_provider(tmp_path, store)
    with caplog.at_level(logging.WARNING, logger="herdeck.icons"):
        broken = p.render_tile_bytes(_project_tile(project_icon=bad))
        p.render_tile_bytes(_project_tile(project_icon=bad, color="green"))
    mono = p.render_tile_bytes(_project_tile(project_icon=None))
    decoded = Image.open(_io.BytesIO(broken)).tobytes()
    assert decoded == Image.open(_io.BytesIO(mono)).tobytes()
    assert sum("could not be decoded" in r.getMessage() for r in caplog.records) == 1


def test_ico_decodes_its_largest_frame(tmp_path):
    store = ProjectIconStore()
    ico = _img_bytes((10, 200, 60, 255), size=(64, 64), fmt="ICO", sizes=[(16, 16), (64, 64)])
    p = _project_provider(tmp_path, store)
    png = p.render_tile_bytes(_project_tile(project_icon=_stored(store, ico, "image/x-icon")))
    assert _close(_px(png, (35, 35)), (10, 200, 60))


def test_svg_uses_the_rasteriser_when_available(tmp_path):
    store = ProjectIconStore()
    svg = _stored(store, b"<svg>project</svg>", "image/svg+xml")

    def raster(text, size):
        assert text == "<svg>project</svg>"
        return Image.new("RGBA", (size, size), (40, 200, 240, 255))

    png = _project_provider(tmp_path, store, rasterize=raster).render_tile_bytes(
        _project_tile(project_icon=svg)
    )
    assert _close(_px(png, (35, 35)), (40, 200, 240))


def test_svg_without_a_rasteriser_falls_back_to_the_monogram(tmp_path):
    store = ProjectIconStore()
    svg = _stored(store, b"<svg/>", "image/svg+xml")

    def no_cairo(text, size):
        raise OSError("no library called cairo was found")

    p = _project_provider(tmp_path, store, rasterize=no_cairo)
    broken = p.render_tile_bytes(_project_tile(project_icon=svg))
    mono = p.render_tile_bytes(_project_tile(project_icon=None))
    assert Image.open(_io.BytesIO(broken)).tobytes() == Image.open(_io.BytesIO(mono)).tobytes()


def test_dark_icon_gets_a_light_plate_but_a_bright_one_does_not(tmp_path):
    store = ProjectIconStore()
    p = _project_provider(tmp_path, store)
    dark = p.render_tile_bytes(_project_tile(project_icon=_stored(store, _img_bytes((0, 0, 0, 255)))))
    assert min(_px(dark, (13, 35))) >= 200  # plate edge left of the inset icon
    red = p.render_tile_bytes(_project_tile(project_icon=_stored(store, _img_bytes(RED))))
    assert _close(_px(red, (13, 35)), RED[:3])  # no plate: the icon fills the box


def test_opaque_favicon_with_its_own_background_is_not_plated_on_a_bright_fill(tmp_path):
    # Regression: a white app-tile favicon with a dark letter (mean colour
    # greyish) got a dark plate on a solid green WORKING tile and shrank inside
    # a black frame. Its white edge contrasts fine with green, so no plate.
    store = ProjectIconStore()
    p = _project_provider(tmp_path, store)
    icon = Image.new("RGBA", (64, 64), (250, 250, 250, 255))
    ImageDraw.Draw(icon).rectangle([16, 8, 48, 56], fill=(20, 20, 20, 255))
    buf = _io.BytesIO()
    icon.save(buf, "PNG")
    tile = _project_tile(project_icon=_stored(store, buf.getvalue()), color="green", tile_fill="solid")
    png = p.render_tile_bytes(tile)
    assert min(_px(png, (13, 35))) >= 200  # the icon's own white edge, not a dark plate


def test_transparent_glyph_still_gets_a_plate_on_a_matching_fill(tmp_path):
    store = ProjectIconStore()
    p = _project_provider(tmp_path, store)
    glyph = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(glyph).ellipse([20, 20, 44, 44], fill=(255, 255, 255, 255))
    buf = _io.BytesIO()
    glyph.save(buf, "PNG")
    tile = _project_tile(project_icon=_stored(store, buf.getvalue()), color="cyan", tile_fill="solid")
    png = p.render_tile_bytes(tile)
    assert max(_px(png, (13, 35))) <= 60  # dark plate behind the white glyph


def test_both_mode_keeps_the_agent_mark_and_adds_a_badge(tmp_path):
    store = ProjectIconStore()
    h = _stored(store, _img_bytes(RED))
    p = _project_provider(tmp_path, store)
    agent = Image.open(_io.BytesIO(p.render_tile_bytes(_project_tile(tile_icon="agent"))))
    both = Image.open(
        _io.BytesIO(p.render_tile_bytes(_project_tile(tile_icon="both", project_icon=h)))
    )
    box = (12, 12, 36, 36)  # the part of the logo box the badge does not cover
    assert agent.crop(box).tobytes() == both.crop(box).tobytes()
    assert _close(both.convert("RGB").getpixel((50, 50)), RED[:3])  # badge centre


def test_monogram_for_an_evicted_hash_is_not_pinned_in_the_render_caches(tmp_path):
    # the bytes can be LRU-evicted between resolve() and the render: the
    # monogram drawn then must not stick under the real icon's tile name
    data = _img_bytes(RED)
    h = icon_hash(data)
    store = ProjectIconStore()
    p = _project_provider(tmp_path, store)
    tile = _project_tile(project_icon=h)
    assert not _close(_px(p.render_tile_bytes(tile), (35, 35)), RED[:3])
    fallback = p.render_tile(tile)
    # the fallback frame is filed under the monogram's name, not the icon's
    assert fallback == p.render_tile(_project_tile(project_icon=None))
    store.put(h, "image/png", data)
    assert _close(_px(p.render_tile_bytes(tile), (35, 35)), RED[:3])
    name = p.render_tile(tile)
    assert name != fallback
    with open(os.path.join(str(tmp_path / "cache"), name), "rb") as f:
        assert _close(_px(f.read(), (35, 35)), RED[:3])


def _zero_png(side):
    buf = _io.BytesIO()
    Image.new("L", (side, side), 0).save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _png_ico(png, side_byte=0):
    import struct

    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", side_byte, side_byte, 0, 0, 1, 32, len(png), 6 + 16)
    return header + entry + png


def _spy_on_load(monkeypatch):
    from PIL import ImageFile

    calls = []
    real = ImageFile.ImageFile.load

    def spy(self):
        calls.append(self.size)
        return real(self)

    monkeypatch.setattr(ImageFile.ImageFile, "load", spy)
    return calls


def test_decompression_bomb_png_falls_back_without_decoding(tmp_path, monkeypatch, caplog):
    from herdeck.icons import PROJECT_ICON_MAX_SIDE

    side = PROJECT_ICON_MAX_SIDE * 4  # far over the cap, still a tiny file
    data = _zero_png(side)
    assert len(data) < 256 * 1024
    store = ProjectIconStore()
    h = _stored(store, data)
    p = _project_provider(tmp_path, store)
    calls = _spy_on_load(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="herdeck.icons"):
        assert p._decode_project_icon(h, store.get(h)) is None
    assert calls == []  # rejected from the header, pixels never decoded
    monkeypatch.undo()
    broken = p.render_tile_bytes(_project_tile(project_icon=h))
    mono = p.render_tile_bytes(_project_tile(project_icon=None))
    assert Image.open(_io.BytesIO(broken)).tobytes() == Image.open(_io.BytesIO(mono)).tobytes()
    assert sum("could not be decoded" in r.getMessage() for r in caplog.records) == 1


def test_decompression_bomb_inside_an_ico_frame_is_rejected(tmp_path, monkeypatch):
    from herdeck.icons import PROJECT_ICON_MAX_SIDE

    # The ICO directory claims 256x256 but the embedded PNG frame is huge.
    data = _png_ico(_zero_png(PROJECT_ICON_MAX_SIDE * 2))
    assert len(data) < 256 * 1024
    store = ProjectIconStore()
    h = _stored(store, data, "image/x-icon")
    p = _project_provider(tmp_path, store)
    calls = _spy_on_load(monkeypatch)
    assert p._decode_project_icon(h, store.get(h)) is None
    assert calls == []


def test_png_icon_within_the_pixel_cap_still_decodes(tmp_path):
    store = ProjectIconStore()
    h = _stored(store, _png_ico(_img_bytes(RED, size=(64, 64)), side_byte=64), "image/x-icon")
    p = _project_provider(tmp_path, store)
    assert p._decode_project_icon(h, store.get(h)) is not None


def test_resvg_rasterize_centres_a_non_square_svg():
    pytest.importorskip("resvg_py")
    from herdeck.icons import resvg_rasterize

    wide = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">'
        '<rect width="200" height="100" fill="red"/></svg>'
    )
    img = resvg_rasterize(wide, 64)
    assert img.size == (64, 64)
    assert img.getpixel((32, 32))[:3] == (255, 0, 0)  # centre band is the rect
    assert img.getpixel((32, 2))[3] == 0  # letterboxed, not stretched


def test_svg_favicon_decodes_with_the_default_rasterizer():
    pytest.importorskip("resvg_py")
    from herdeck.icons import decode_project_icon
    from herdeck.project_icons import StoredIcon

    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
        b'<circle cx="5" cy="5" r="5" fill="#0a0"/></svg>'
    )
    img = decode_project_icon(StoredIcon("image/svg+xml", svg))
    assert img.getpixel((img.width // 2, img.height // 2))[:3] == (0, 170, 0)


@pytest.mark.parametrize(
    "ref",
    [
        '<image href="/etc/secret.png" width="1" height="1"/>',
        '<image xlink:href="file:///Users/x/a.png" width="1" height="1"/>',
        '<image href="https://example.com/a.png" width="1" height="1"/>',
        '<rect style="fill:url(\'/tmp/p.svg#g\')" width="1" height="1"/>',
    ],
)
def test_svg_favicon_with_outside_references_is_refused(ref):
    from herdeck.icons import decode_project_icon
    from herdeck.project_icons import StoredIcon

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 1 1">{ref}</svg>'
    )
    calls = []
    with pytest.raises(ValueError):
        decode_project_icon(
            StoredIcon("image/svg+xml", svg.encode()), lambda s, n: calls.append(s)
        )
    assert calls == []  # never handed to a rasterizer


def test_svg_favicon_with_internal_references_is_allowed():
    from herdeck.icons import _svg_references_outside

    assert not _svg_references_outside(
        '<svg><defs><linearGradient id="g"/></defs><rect fill="url(#g)"/>'
        '<use href="#g"/><image href="data:image/png;base64,AAAA"/></svg>'
    )
