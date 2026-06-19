import os
from PIL import Image

from herdeck.icons import IconProvider


def _fake_fetch(slug):           # pretend Simple Icons returns an SVG for known slugs
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
    im = Image.open(path)
    assert im.size == (196, 196)


def test_unknown_slug_falls_back_to_glyph(tmp_path):
    p = make_provider(tmp_path)
    name = p.icon_for("codex", "blue")     # slug None -> glyph
    assert os.path.exists(os.path.join(str(tmp_path), name))


def test_user_override_takes_precedence(tmp_path):
    overrides = tmp_path / "ov"
    overrides.mkdir()
    Image.new("RGBA", (196, 196), (1, 2, 3, 255)).save(overrides / "claude.png")
    p = make_provider(tmp_path, overrides=overrides)
    name = p.icon_for("claude", "green")
    # the produced icon must derive from the override (a specific pixel survives)
    im = Image.open(os.path.join(str(tmp_path), name)).convert("RGBA")
    assert im.size == (196, 196)


def test_results_are_cached(tmp_path):
    calls = []
    p = IconProvider(cache_dir=str(tmp_path),
                     slug_map={"claude": "claude"},
                     overrides_dir=None,
                     fetch=lambda s: (calls.append(s), "<svg/>")[1],
                     rasterize=_fake_rasterize)
    p.icon_for("claude", "green")
    p.icon_for("claude", "green")
    assert calls.count("claude") <= 1     # fetched at most once


def test_spinner_cache_is_bounded_to_frame_set():
    from herdeck.icons import SPINNER_FRAMES

    seen = set()
    p = IconProvider(cache_dir="/tmp/herdeck_spin_test",
                     slug_map={"claude": None}, overrides_dir=None,
                     fetch=_fake_fetch, rasterize=_fake_rasterize)
    import shutil
    shutil.rmtree("/tmp/herdeck_spin_test", ignore_errors=True)
    os.makedirs("/tmp/herdeck_spin_test", exist_ok=True)
    for phase in range(0, SPINNER_FRAMES * 3):
        seen.add(p.icon_for("claude", "green", spinner=phase))
    # phases cycle: at most SPINNER_FRAMES distinct files, not 3x as many
    assert len(seen) == SPINNER_FRAMES
    # phase 0 and phase SPINNER_FRAMES produce the same cached file
    assert p.icon_for("claude", "green", 0) == p.icon_for("claude", "green", SPINNER_FRAMES)


def test_agent_type_with_path_chars_is_sanitized(tmp_path):
    p = IconProvider(cache_dir=str(tmp_path),
                     slug_map={}, overrides_dir=None,
                     fetch=_fake_fetch, rasterize=_fake_rasterize)
    name = p.icon_for("../../evil", "green")
    # no traversal: the written file stays inside cache_dir
    assert "/" not in name and ".." not in name
    assert os.path.exists(os.path.join(str(tmp_path), name))
