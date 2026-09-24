from __future__ import annotations

import hashlib
import io
import logging
import math
import os
import re
import struct
import sys
import time
from collections import OrderedDict
from collections.abc import Callable

from PIL import Image, ImageChops, ImageDraw, ImageStat

from .driver.base import COLORS, PanelView
from .project_icons import ProjectIconStore, StoredIcon, default_store

log = logging.getLogger(__name__)

ICON_SIZE = 196
# The D200 small window's NATIVE resolution (one 3_2-slot background icon,
# displayed 1:1 by the firmware). Composing at the old two-cell 392px and
# letting the firmware stretch it to 458px made all panel text ~17% wider.
PANEL_W, PANEL_H = 458, 196
# Two-cell composite width for surfaces that show the panel in a 2-cells-wide
# box (Elgato's key pair, the web simulator, the desktop window grid).
PANEL_W_TWO_CELL = 2 * ICON_SIZE
# The spinner has this many distinct frames; keep the cache bounded so a
# long-running working tile reuses frames instead of writing forever.
SPINNER_FRAMES = 8
# pulse = a slow "breath": the phase advances once per PULSE_SLOWDOWN ticks
# through PULSE_STATES frames (at the 0.4s default tick: a step every 2s, a
# full breath every 8s). Every animation frame is a full page reload on the
# D200, so the calmest style must also be the cheapest one.
PULSE_SLOWDOWN = 5
PULSE_STATES = 4
# Bump when the rendered icon output changes so stale cached PNGs from older
# versions are ignored, not reused.
CACHE_VERSION = 3

# Generated tile/icon PNGs are content-addressed (the filename encodes the full
# render signature), so eviction is always safe — at worst the next render
# recreates the file. Without eviction a 24/7 deck grows the cache dir without
# bound: the elapsed-time text in the signature mints fresh filenames forever.
PRUNE_MAX_AGE_S = 3600.0
_PRUNE_EVERY_WRITES = 4096  # opportunistic prune cadence for long-running processes
_BYTES_CACHE_MAX = 512  # in-memory PNG-bytes LRU entries (~a few MB)
_BASE_CACHE_MAX = 48  # static agent-tile bases (196x196 RGBA, ~150 KB each)
_LAYER_CACHE_MAX = 512  # small per-frame logo/comet layers


def prune_generated(cache_dir: str, max_age_s: float = PRUNE_MAX_AGE_S) -> int:
    """Delete generated ``tile_*``/``icon_v*``/``panel_*`` PNGs older than
    ``max_age_s``.

    Only the content-addressed names herdeck writes are touched; anything else
    in the dir (user files) is left alone. Panel PNGs are content-keyed too
    (d200 driver: ``panel_<hash>.png`` + fallback ``_l``/``_r`` halves) and
    usage percentages/reset times in the panel now mint fresh names regularly,
    so exempting them grew the cache without bound on a 24/7 deck; consumers
    re-check file existence per frame and refresh mtime on reuse, so pruning
    old ones is safe. Returns the number of files removed."""
    try:
        entries = os.scandir(cache_dir)
    except OSError:
        return 0
    cutoff = time.time() - max_age_s
    removed = 0
    with entries:
        for entry in entries:
            name = entry.name
            if not name.endswith(".png"):
                continue
            if not (
                name.startswith("tile_") or name.startswith("icon_v") or name.startswith("panel_")
            ):
                continue
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    os.unlink(entry.path)
                    removed += 1
            except OSError:
                pass  # raced/unreadable entry: skip
    return removed


# The agent mark is inset (not edge-to-edge) so the comet ring has clean room
# around it and tiles look deliberate rather than cramped.
LOGO_SCALE = 0.62
# Comet working-ring geometry, in ICON_SIZE pixels.
RING_INSET = 12
RING_WIDTH = 7
RING_SPAN = 150  # degrees of comet tail
_SS = 4  # supersample factor for an anti-aliased ring

# Bundled SVG marks for agents that Simple Icons does not carry (e.g. codex →
# the OpenAI logo). Shipped under the package's assets/ dir.
_ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_-]+")


def _safe_name(agent_type: str) -> str:
    """Filesystem-safe, collision-resistant token for an inbound agent type.

    Known agent types (alphanumerics) pass through unchanged for readable
    filenames; anything else is sanitized and disambiguated with a short hash so
    distinct raw values (e.g. ``a/b`` vs ``a_b``) never share a cache file.
    """
    safe = _UNSAFE_NAME.sub("_", agent_type)
    if safe == agent_type and safe:
        return safe
    # Encoded names carry a '.' delimiter, which the passthrough branch can never
    # produce (it only emits [A-Za-z0-9_-]); the two namespaces are thus disjoint.
    digest = hashlib.sha1(agent_type.encode()).hexdigest()[:8]
    return f"{safe or '_'}.{digest}"


def _fingerprint_assets(assets_dir: str | None) -> str:
    """A short content digest of the bundled-glyph set in ``assets_dir`` (each
    file's name + its bytes). Folded into the render-cache keys so that adding,
    removing, OR re-baking/editing a bundled mark invalidates stale cached
    tiles — otherwise an UPGRADED app reuses a pre-bundle letter-glyph tile for
    a newly bundled agent (the Q1-on-upgrade staleness seen after an in-place upgrade).
    Hashes contents (not just name+size) so a same-name same-length re-bake is
    caught too. The asset set is small (a handful of KB), so the one read per
    provider construction is negligible. Returns ``"0"`` when there is no
    assets dir or it cannot be listed."""
    if not assets_dir:
        return "0"
    try:
        names = sorted(os.listdir(assets_dir))
    except OSError:
        return "0"
    h = hashlib.sha1()
    for n in names:
        h.update(n.encode())
        h.update(b"\0")
        try:
            with open(os.path.join(assets_dir, n), "rb") as fh:
                h.update(fh.read())
        except OSError:
            pass  # unreadable entry (e.g. a subdir): name alone still contributes
        h.update(b"\0")
    return h.hexdigest()[:10]


def _baked_glyph_png_name(svg_text: str) -> str:
    """Content-addressed filename shared with the frozen asset baker."""
    return hashlib.sha1(svg_text.encode("utf-8")).hexdigest() + ".png"


# Every interactive agent kind advertised by Herdr 0.8.2. Keep this in sync with
# ``herdr agent start --help``; the asset regression test makes missing marks a
# deliberate decision instead of a silent letter-glyph downgrade.
BUNDLED_AGENT_TYPES = (
    "agy",
    "amp",
    "claude",
    "cline",
    "codex",
    "copilot",
    "cursor",
    "devin",
    "droid",
    "gemini",
    "grok",
    "hermes",
    "kilo",
    "kimi",
    "kiro",
    "maki",
    "mastracode",
    "omp",
    "opencode",
    "pi",
    "qodercli",
    "qwen",
)

# agent type -> Simple Icons slug (None => generated glyph fallback). Bundled
# assets are preferred; this map is only a source-install compatibility path.
DEFAULT_AGENT_SLUGS: dict[str, str | None] = {
    "claude": "claude",
    "codex": None,  # no Simple Icons entry -> glyph
    "cursor": "cursor",
    "copilot": "githubcopilot",
    "gemini": "googlegemini",
    "opencode": "opencode",
    "default": None,
}


def _default_fetch(slug: str) -> str | None:
    """Fetch a white Simple Icons SVG (cached on disk by the caller). Network.

    A browser-like User-Agent is required: cdn.simpleicons.org returns 403 for
    the default urllib agent. The ``/white`` variant gives a monochrome white
    mark that stays legible on every status background colour.
    """
    import urllib.request

    req = urllib.request.Request(
        f"https://cdn.simpleicons.org/{slug}/white",
        headers={"User-Agent": "Mozilla/5.0 (herdeck)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            if r.status == 200:
                return r.read().decode()
    except Exception:
        return None
    return None


def resvg_rasterize(svg: str, size: int) -> Image.Image:
    """SVG -> ``size``x``size`` RGBA via resvg (self-contained wheel, also in
    the frozen bundles). resvg keeps the aspect ratio, so a non-square SVG is
    centred on a transparent square."""
    import resvg_py

    png = resvg_py.svg_to_bytes(svg_string=svg, width=size, height=size)
    with Image.open(io.BytesIO(png)) as im:
        img = im.convert("RGBA")
    if img.size == (size, size):
        return img
    img.thumbnail((size, size), Image.LANCZOS)
    square = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    square.paste(img, ((size - img.width) // 2, (size - img.height) // 2))
    return square


def _default_rasterize(svg: str, size: int) -> Image.Image:
    """The one SVG path: resvg (a self-contained wheel in every rendering extra).

    There is deliberately no cairosvg fallback — it needed the native cairo
    library, which broke fresh Macs and CI runners.
    """
    return resvg_rasterize(svg, size)


# The vendored tile font (Inter 4.1, OFL — see assets/fonts/VENDORED.md). Every
# tile and panel is drawn with it so text metrics — shrink-to-fit sizes, wrap
# and truncation points — are identical on macOS, Linux and in the frozen
# bundles (tests/test_render_golden.py pins the pixels).
_BUNDLED_FONTS = {True: "Inter-Bold.ttf", False: "Inter-Regular.ttf"}


def bundled_font_path(*, bold: bool = True) -> str | None:
    """The vendored font file, from the package assets or — inside a
    PyInstaller bundle, whose package dir has no assets/ — from
    ``sys._MEIPASS/herdeck_assets/fonts``. None when neither has it."""
    name = _BUNDLED_FONTS[bold]
    dirs = [os.path.join(_ASSETS_DIR, "fonts")]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(os.path.join(meipass, "herdeck_assets", "fonts"))
    for d in dirs:
        path = os.path.join(d, name)
        if os.path.isfile(path):
            return path
    return None


def check_bundled_font() -> None:
    """Raise unless both vendored font files are found — the frozen bundles'
    ``HERDECK_SELFTEST=imports`` runs this, so a spec that stops shipping
    assets/fonts fails the build instead of silently drawing a system font."""
    missing = [name for bold, name in _BUNDLED_FONTS.items() if bundled_font_path(bold=bold) is None]
    if missing:
        raise RuntimeError(f"vendored tile font missing: {', '.join(missing)}")


# System fonts: only a fallback when the vendored font is missing (a broken
# install). Metrics then differ per OS.
_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)
_REGULAR_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)
_GLYPH_FONT_SIZE = 120
_font_cache: dict[tuple[int, bool], object] = {}  # (size, bold) -> font

# Bump when tile composition changes so stale cached tile PNGs are ignored.
# 2: tile_fill (none/tint/solid) — solid contrast + solid-sweep composition.
# 3: readable subtext (branch/time) on solid dark-colour fills (e.g. blue).
# 4: wrapped text marks a cut-off tail with an ellipsis.
# 5: the agent mark flips dark on bright solid fills (like the text).
# 6: larger type scale (repo 31px, sub-labels 18-19px) spread down the tile.
# 7: the dark flip applies only to light-monochrome marks (colour overrides
#    render as supplied again).
# 8: pulse becomes a slow 4-state breath (its phase domain changed, so cached
#    frames keyed by the old raw phase must not be served).
# 9: the panel composes at the small window's native 458px (was 392) and the
#    D200 sends it as ONE 3_2 background icon instead of two stretched cells.
# 10: the usage panel uses a lighter slate palette and shows reset hints in the
#     overview cards.
# 14: WCAG-contrast inks (status word/subtext lightened on none/tint, black or
#     white ink on solid + label tiles), no spaces around '/' when wrapping,
#     branch left-truncation, bigger status word + 16px bottom band (tag/pin),
#     repo min 20px, dark comet ring on bright solid fills, neutral gauge labels.
# 15: tile_icon (agent/project/both) — a project favicon or monogram in the
#     logo box or as a corner badge; spin renders as comet around a favicon.
# 16: invalidates on-disk tiles an intermediate build may have written with a
#     missing-icon monogram under the real icon's tile name.
# 17: opaque favicons are plated by their edge, not their mean colour (no dark
#     frame around a white/red app-tile favicon on a bright solid fill).
# 18: all text is drawn with the vendored Inter font (was Arial/Helvetica on
#     macOS, DejaVu/Liberation on Linux).
# 19: running subagents draw a fork badge + count in the bottom band.
TILE_VERSION = 20
# The status word / elapsed time column: right of the logo box incl. the comet
# ring (x < 66), inside the 12px right margin.
STATUS_MAX_W = ICON_SIZE - 12 - 70
TILE_BG = (26, 26, 30)  # dark agent-tile background
SPIN_DEG = 360 / SPINNER_FRAMES  # degrees per rotation phase


# Text ink for coloured backgrounds. Pure black (not a soft near-black) is what
# guarantees the flip below always reaches WCAG AA: with black/white as the two
# candidates, the worse background (the mid-tone where both tie) still gives
# 4.58:1. A softer (18,18,22) ink left violet/grey solid tiles under 4.5:1.
DARK_INK = (0, 0, 0)
LIGHT_INK = (255, 255, 255)
# Minimum WCAG contrast every text colour on an agent/label tile aims for.
TEXT_CONTRAST = 4.5


def _rel_lum(c: tuple[int, int, int]) -> float:
    """WCAG 2 relative luminance (0..1) of an sRGB colour."""

    def lin(v: int) -> float:
        x = v / 255
        return x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(c[0]) + 0.7152 * lin(c[1]) + 0.0722 * lin(c[2])


def _contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """WCAG 2 contrast ratio between two colours (1..21)."""
    la, lb = _rel_lum(a), _rel_lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _ink_for(bg: tuple[int, int, int]) -> tuple[int, int, int]:
    """The ink (black or white) with the higher WCAG contrast on ``bg``.

    Replaces the old Rec.601 ``> 120`` threshold, which put violet (126) and
    grey (120) on the wrong side: their elapsed time read at ~2.9-3.1:1."""
    return DARK_INK if _contrast(DARK_INK, bg) > _contrast(LIGHT_INK, bg) else LIGHT_INK


def _mix(a, b, t: float) -> tuple[int, int, int]:
    """``a`` moved ``t`` (0..1) of the way toward ``b``."""
    return tuple(round(ca + (cb - ca) * t) for ca, cb in zip(a, b, strict=True))


def _readable(color, bg, target: float = TEXT_CONTRAST) -> tuple[int, int, int]:
    """``color`` lightened toward white just enough to reach ``target`` contrast
    on ``bg`` (a dark background). Keeps the hue where it already passes — green
    on the dark tile stays green — and only washes the dim ones (blue, violet,
    grey, red) as far as legibility needs."""
    for step in range(21):
        c = _mix(color, LIGHT_INK, step / 20)
        if _contrast(c, bg) >= target:
            return c
    return LIGHT_INK


def _soften(ink, bg, target: float = TEXT_CONTRAST, max_mix: float = 0.3):
    """``ink`` blended up to ``max_mix`` toward ``bg`` (a quieter secondary
    tone) while it still keeps ``target`` contrast. Returns ``ink`` itself when
    there is no headroom (violet/grey solids)."""
    best = ink
    for step in range(1, int(max_mix * 20) + 1):
        c = _mix(ink, bg, step / 20)
        if _contrast(c, bg) < target:
            break
        best = c
    return best


def _is_light_monochrome(img: Image.Image) -> bool:
    """Is this glyph a white/near-white mark on transparency (the shape every
    built-in mark has)? A full-colour user override must NOT be flattened to a
    dark silhouette by the solid-fill contrast flip."""
    alpha = img.getchannel("A")
    mask = alpha.point(lambda v: 255 if v > 32 else 0)
    if not mask.getbbox():
        return False  # fully transparent: nothing to recolour
    means = ImageStat.Stat(img.convert("RGB"), mask=mask).mean
    return min(means) > 180 and (max(means) - min(means)) < 40


def _tint_bg(accent: tuple[int, int, int]) -> tuple[int, int, int]:
    """A darkened shade of the status colour, used as the whole-tile background
    for tile_fill='tint' — clearly coloured but dark enough that the light text
    stays readable."""
    return tuple(int(c * 0.34) for c in accent)


_TEXT_COLORS_CACHE: dict[tuple, tuple] = {}


def _tile_text_colors(fill, bg_col, accent):
    """(repo, branch, time, status-word) colours for an agent tile, picked for
    WCAG contrast (>= TEXT_CONTRAST) against the fill background.

    none/tint sit on a dark background -> white repo, grey subtext and the
    status word in the accent colour, each lightened toward white only as far
    as needed (blue IDLE / violet WAITING / grey UNKNOWN / red OFFLINE read at
    3.1-4.2:1 in the raw accent). A solid fill takes whichever ink (black or
    white) contrasts more with the colour; the repo, status word and elapsed
    time use that ink, the branch a slightly quieter blend of it."""
    key = (fill, tuple(bg_col), tuple(accent))
    hit = _TEXT_COLORS_CACHE.get(key)
    if hit is not None:
        return hit
    if fill == "solid":
        ink = _ink_for(bg_col)
        out = (ink, _soften(ink, bg_col), ink, ink)
    else:  # none / tint
        out = (
            LIGHT_INK,
            _readable((180, 180, 188), bg_col),
            _readable((165, 165, 170), bg_col),
            _readable(accent, bg_col),
        )
    _TEXT_COLORS_CACHE[key] = out
    return out


def _font(size: int, *, bold: bool = True):
    """A scalable font at the given size; None only if nothing is available."""
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    from PIL import ImageFont

    font = None
    bundled = bundled_font_path(bold=bold)
    if bundled is not None:
        try:
            # BASIC layout: raqm ships in some Pillow builds and not others,
            # and would shape (kern) the same text differently per OS.
            font = ImageFont.truetype(bundled, size, layout_engine=ImageFont.Layout.BASIC)
        except Exception:
            font = None
    if font is None:
        for path in _FONT_CANDIDATES if bold else _REGULAR_FONT_CANDIDATES:
            try:
                font = ImageFont.truetype(path, size)
                break
            except Exception:
                continue
    if font is None:
        try:
            font = ImageFont.load_default(size=size)
        except Exception:
            font = None
    _font_cache[key] = font
    return font


def _load_big_font():
    """A large scalable font for the letter fallback; None if none is available."""
    return _font(_GLYPH_FONT_SIZE)


def _anim_phase(raw_phase, animation: str):
    """The EFFECTIVE animation phase for a tile: what both the cache signature
    and the composition must use (one source of truth, or pixel-identical
    frames get distinct names — every one a full page reload on the D200).

    none  -> None (no animation: the phase must not churn the signature)
    pulse -> slow breath: advances once per PULSE_SLOWDOWN ticks, PULSE_STATES frames
    other -> raw phase bounded to SPINNER_FRAMES (per-tick motion)"""
    if raw_phase is None or animation == "none":
        return None
    if animation == "pulse":
        return (raw_phase // PULSE_SLOWDOWN) % PULSE_STATES
    return raw_phase % SPINNER_FRAMES


# --- project favicons ([view].tile_icon) ---
PROJECT_BADGE = 24  # badge edge in px; overlaps the 46px logo box's corner
PROJECT_BADGE_XY = 38  # badge top-left: box (12..58) bottom-right corner, overhanging
PROJECT_RADIUS = 0.2  # rounded-corner radius as a fraction of the edge
_PLATE_MIN_CONTRAST = 2.2  # below this vs the tile background an icon gets a plate
# Icons at least this opaque (share of pixels, after corner rounding ~0.97 for a
# full square) carry their own background and are never plated.
_PLATE_OPAQUE_COVERAGE = 0.85
# ...and are plated only when their edge colour nearly merges with the tile's
# (RGB distance, not luminance contrast: a red square on a blue tile has ~1.1:1
# WCAG contrast yet is plainly visible; a black square on the dark tile is not).
_PLATE_OPAQUE_MIN_DISTANCE = 60.0
_PLATE_LIGHT = (236, 236, 240)
_PLATE_DARK = (22, 22, 26)
_PROJECT_CACHE_MAX = 64
MONOGRAM_PALETTE: tuple[tuple[int, int, int], ...] = (
    (66, 133, 244),
    (219, 68, 55),
    (244, 160, 0),
    (15, 157, 88),
    (171, 71, 188),
    (0, 172, 193),
    (255, 112, 67),
    (92, 107, 192),
)


def _effective_animation(tile) -> str:
    """The working animation actually drawn: a favicon never rotates, so
    ``spin`` becomes a comet ring around it in project mode. Used by both the
    cache signature and the composition (one source of truth)."""
    anim = getattr(tile, "working_animation", "spin")
    if anim == "spin" and getattr(tile, "tile_icon", "agent") == "project":
        return "comet"
    return anim


def _pulse_size(phase: int) -> int:
    """Edge of the pulsing mark (~0.82x..1.0x of the 46px box) at a slow phase."""
    f = 0.82 + 0.18 * (0.5 + 0.5 * math.sin(2 * math.pi * phase / PULSE_STATES))
    return max(1, round(46 * f))


def _round_corners(img: Image.Image) -> Image.Image:
    w, h = img.size
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, w - 1, h - 1], radius=round(min(w, h) * PROJECT_RADIUS), fill=255
    )
    img.putalpha(ImageChops.multiply(img.getchannel("A"), mask))
    return img


def _normalize_project_image(img: Image.Image) -> Image.Image:
    """Any decoded favicon -> ICON_SIZE RGBA square: transparent margins
    trimmed, centred on transparent padding (aspect kept), LANCZOS-resized,
    corners rounded."""
    img = img.convert("RGBA")
    # App-style favicons often sit inset on a transparent canvas, which drew
    # them smaller than their neighbours and hid their own square background
    # from the plate check; trim that margin first.
    bbox = img.getchannel("A").point(lambda v: 255 if v > 16 else 0).getbbox()
    if bbox:
        img = img.crop(bbox)
    w, h = img.size
    side = max(w, h, 1)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(img, ((side - w) // 2, (side - h) // 2))
    return _round_corners(canvas.resize((ICON_SIZE, ICON_SIZE), Image.LANCZOS))


def _monogram_image(name: str) -> Image.Image:
    """Fallback project mark: a rounded square coloured from sha1(repo) with
    the repo's first alphanumeric character, inked for contrast."""
    color = MONOGRAM_PALETTE[hashlib.sha1(name.encode("utf-8")).digest()[0] % len(MONOGRAM_PALETTE)]
    ch = next((c for c in name if c.isalnum()), "?").upper()
    img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), color + (255,))
    d = ImageDraw.Draw(img)
    font = _font(118)
    kw = {"font": font} if font is not None else {}
    bbox = d.textbbox((0, 0), ch, **kw)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(
        ((ICON_SIZE - w) // 2 - bbox[0], (ICON_SIZE - h) // 2 - bbox[1]),
        ch,
        fill=_ink_for(color) + (255,),
        **kw,
    )
    return _round_corners(img)


# Largest project icon decoded (per side, as a w*h pixel budget). Favicons are
# tiny; anything bigger is refused from its header and shows the monogram.
PROJECT_ICON_MAX_SIDE = 2048
PROJECT_ICON_MAX_PIXELS = PROJECT_ICON_MAX_SIDE * PROJECT_ICON_MAX_SIDE
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_ICO_MAGIC = b"\x00\x00\x01\x00"


def _check_dims(size: tuple[int, int]) -> None:
    w, h = size
    if w * h > PROJECT_ICON_MAX_PIXELS:
        raise ValueError(f"image is {w}x{h}, over the {PROJECT_ICON_MAX_PIXELS}-pixel cap")


def _ico_frame_dims(data: bytes) -> list[tuple[int, int]]:
    """Real sizes of an ICO's frames, read from each frame's own header. The
    ICO directory caps at 256x256 but an embedded PNG frame can be any size,
    and Pillow decodes a frame inside ``Image.open`` itself, so this parses
    the directory by hand."""
    dims = []
    if len(data) < 6:
        return dims
    count = struct.unpack("<H", data[4:6])[0]
    for i in range(count):
        entry = data[6 + 16 * i : 22 + 16 * i]
        if len(entry) < 16:
            break
        offset = struct.unpack("<I", entry[12:16])[0]
        head = data[offset : offset + 24]
        if head[:8] == _PNG_MAGIC and len(head) >= 24:
            dims.append(struct.unpack(">II", head[16:24]))
        elif len(head) >= 12:  # BITMAPINFOHEADER: height covers XOR + AND mask
            w, h = struct.unpack("<ii", head[4:12])
            dims.append((abs(w), abs(h) // 2))
    return dims


def _open_project_icon(data: bytes) -> Image.Image:
    """``Image.open`` that refuses an oversized image before any pixel is
    decoded (a 78 KB PNG can declare 9000x9000: >1 GB and 0.5 s to decode
    inside the raster lock / on the Elgato loop)."""
    if data[:4] == _ICO_MAGIC:
        for size in _ico_frame_dims(data):
            _check_dims(size)
    im = Image.open(io.BytesIO(data))  # PNG/JPEG/...: header only
    try:
        _check_dims(im.size)
    except Exception:
        im.close()
        raise
    return im


def _mean_rgb(img: Image.Image) -> tuple[int, int, int] | None:
    mask = img.getchannel("A").point(lambda v: 255 if v > 32 else 0)
    if not mask.getbbox():
        return None
    return tuple(round(c) for c in ImageStat.Stat(img.convert("RGB"), mask=mask).mean)


def _edge_rgb(img: Image.Image) -> tuple[int, int, int] | None:
    """Mean colour of the icon's opaque outer band (its visible silhouette)."""
    w, h = img.size
    band = max(2, min(w, h) // 12)
    ring = Image.new("L", img.size, 255)
    ImageDraw.Draw(ring).rectangle([band, band, w - 1 - band, h - 1 - band], fill=0)
    opaque = img.getchannel("A").point(lambda v: 255 if v > 200 else 0)
    mask = ImageChops.multiply(ring, opaque)
    if not mask.getbbox():
        return None
    return tuple(round(c) for c in ImageStat.Stat(img.convert("RGB"), mask=mask).mean)


def _opaque_coverage(img: Image.Image) -> float:
    """Share of the icon's pixels that are (nearly) opaque, 0..1."""
    alpha = img.getchannel("A").point(lambda v: 255 if v > 200 else 0)
    return ImageStat.Stat(alpha).mean[0] / 255


def _plate_for(img: Image.Image, bg) -> tuple[int, int, int] | None:
    """A plate colour when the icon's average colour would vanish on ``bg``
    (a black glyph favicon on the dark tile, a white one on a bright solid
    fill); the plate is whichever of light/dark contrasts more with the icon.

    An icon that fills its own square (a favicon with a baked-in background,
    e.g. a white or red app tile) shows as that square, so what matters is
    its EDGE against the tile, not the average of its artwork: a white square
    with a dark letter reads fine on a green tile. Such an icon is plated only
    when its edge nearly merges with the tile (black square on the dark tile);
    plating it otherwise just shrinks it inside a dark frame."""
    if _opaque_coverage(img) >= _PLATE_OPAQUE_COVERAGE:
        edge = _edge_rgb(img)
        if edge is None or math.dist(edge, tuple(bg)[:3]) >= _PLATE_OPAQUE_MIN_DISTANCE:
            return None
        return max((_PLATE_LIGHT, _PLATE_DARK), key=lambda plate: _contrast(plate, edge))
    mean = _mean_rgb(img)
    if mean is None or _contrast(mean, tuple(bg)) >= _PLATE_MIN_CONTRAST:
        return None
    return max((_PLATE_LIGHT, _PLATE_DARK), key=lambda plate: _contrast(plate, mean))


# --- status panel ---------------------------------------------------------
# One layout for every panel state: a header (state chip left, meta right), a
# main area (count cards, usage gauges, or a big headline with a line or two
# under it, or body text such as an agent's prompt) and a footer (what a press
# does, or a warning note, on the left; page dots or an accent aside on the
# right). "Needs you", "offline" and "config error" fill the whole panel with
# their colour; everything else sits on the dark calm background.
_PANEL_SS = 2  # supersampling: drawn at 2x, downsampled (smooth pills/cards)
_P_BG = (21, 23, 27)
_P_INK = (242, 242, 239)
_P_MUTE = (163, 168, 176)
_P_CARD = (42, 46, 53)
_P_DOT_DIM = (91, 96, 104)
_P_NOTE = (255, 138, 126)
_P_PAD_X, _P_PAD_Y = 18, 14
_P_CHIP_H = 26
_P_FOOT_H = 18


def _mix(a, b, t: float) -> tuple[int, int, int]:
    """``a`` moved ``t`` (0..1) of the way towards ``b``."""
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b, strict=True))


def _panel_palette(panel: PanelView) -> dict:
    """Background / ink / muted / chip colours for the panel's tone."""
    if panel.solid:
        bg = COLORS.get(panel.color, COLORS["grey"])
        dark_ink = _ink_for(bg) == (0, 0, 0)
        ink = _mix(bg, (0, 0, 0), 0.88) if dark_ink else _mix((255, 255, 255), bg, 0.06)
        mute = _mix(bg, ink, 0.72)
        return {"bg": bg, "ink": ink, "mute": mute, "chip": ink, "chip_ink": bg,
                "dim": _mix(bg, ink, 0.35), "note": ink, "aside": ink}
    chip = _P_CARD if panel.color == "grey" else COLORS.get(panel.color, _P_CARD)
    return {"bg": _P_BG, "ink": _P_INK, "mute": _P_MUTE, "chip": chip,
            "chip_ink": _P_INK if chip == _P_CARD else _ink_for(chip),
            "dim": _P_DOT_DIM, "note": _P_NOTE, "aside": COLORS["amber"]}


def _spaced_len(draw, text, font, spacing) -> float:
    return draw.textlength(text, font=font) + spacing * max(0, len(text) - 1)


def _draw_spaced(draw, xy, text, font, fill, spacing) -> None:
    """Letter-spaced text (the uppercase chip and card labels)."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + spacing


def _truncate_spaced(draw, text, font, max_w, spacing) -> str:
    if _spaced_len(draw, text, font, spacing) <= max_w:
        return text
    while text and _spaced_len(draw, text + "…", font, spacing) > max_w:
        text = text[:-1]
    return text + "…" if text else ""


_ink_mid_cache: dict[tuple[int, str], float] = {}


def _ink_mid(font, sample: str = "Hg") -> float:
    """The vertical middle of ``sample``'s ink box (fonts are cached forever,
    so their id is a stable key; measuring per call doubled the panel's
    text-raster cost)."""
    key = (id(font), sample)
    if key not in _ink_mid_cache:
        top, bottom = font.getbbox(sample)[1::2]
        _ink_mid_cache[key] = (top + bottom) / 2
    return _ink_mid_cache[key]


def _text_mid(draw, x, cy, text, font, fill) -> None:
    """Draw ``text`` with its ink box vertically centred on ``cy``."""
    draw.text((x, cy - _ink_mid(font)), text, font=font, fill=fill)


def _draw_check(draw, x, cy, size, fill, width) -> None:
    pts = [(x, cy), (x + size * 0.36, cy + size * 0.36), (x + size, cy - size * 0.42)]
    draw.line(pts, fill=fill, width=width, joint="curve")


def _panel_header(draw, panel, pal, w, k) -> None:
    cy = (_P_PAD_Y + _P_CHIP_H / 2) * k
    left, right = _P_PAD_X * k, w - _P_PAD_X * k
    meta_w = 0
    if panel.meta:
        font = _font(15 * k)
        meta = _truncate(draw, panel.meta, font, (w - 2 * _P_PAD_X * k) * 0.45)
        meta_w = draw.textlength(meta, font=font)
        _text_mid(draw, right - meta_w, cy, meta, font, pal["ink"] if panel.solid else pal["mute"])
    room = right - left - meta_w - 14 * k
    if panel.sent:
        font = _font(16 * k)
        _draw_check(draw, left + 2 * k, cy, 15 * k, pal["ink"], 3 * k)
        x = left + 25 * k
        _text_mid(draw, x, cy, _truncate(draw, panel.sent, font, room - 25 * k), font, pal["ink"])
        return
    if not panel.title:
        return
    font, spacing = _font(13 * k), 1.2 * k
    pad = 11 * k
    dot = 8 * k if panel.chip_dot and not panel.solid else 0
    inner = room - 2 * pad - (dot + 7 * k if dot else 0)
    text = _truncate_spaced(draw, panel.title.upper(), font, inner, spacing)
    chip_w = 2 * pad + _spaced_len(draw, text, font, spacing) + (dot + 7 * k if dot else 0)
    top = _P_PAD_Y * k
    draw.rounded_rectangle(
        (left, top, left + chip_w, top + _P_CHIP_H * k), radius=_P_CHIP_H * k / 2, fill=pal["chip"]
    )
    x = left + pad
    if dot:
        draw.ellipse((x, cy - dot / 2, x + dot, cy + dot / 2), fill=COLORS.get(panel.chip_dot, _P_INK))
        x += dot + 7 * k
    _draw_spaced(draw, (x, cy - _ink_mid(font, "H")), text, font, pal["chip_ink"], spacing)


def _panel_footer(draw, panel, pal, w, h, k) -> None:
    cy = h - (_P_PAD_Y + _P_FOOT_H / 2) * k
    left, right = _P_PAD_X * k, w - _P_PAD_X * k
    font = _font(14 * k)
    right_w = 0
    if panel.aside:
        aside = _truncate(draw, panel.aside, font, (right - left) * 0.45)
        right_w = draw.textlength(aside, font=font)
        _text_mid(draw, right - right_w, cy, aside, font, pal["aside"])
    elif panel.page is not None and panel.page[1] > 1:
        index, count = panel.page
        label_font = _font(13 * k)
        label = f"{index % count + 1} / {count}"
        label_w = draw.textlength(label, font=label_font)
        _text_mid(draw, right - label_w, cy, label, label_font, pal["mute"])
        r, gap = 3.5 * k, 6 * k
        dots = min(count, 8)
        x = right - label_w - 8 * k - dots * 2 * r - (dots - 1) * gap
        right_w = right - x
        for i in range(dots):
            fill = pal["ink"] if i == index % count else pal["dim"]
            draw.ellipse((x, cy - r, x + 2 * r, cy + r), fill=fill)
            x += 2 * r + gap
    room = right - left - right_w - 14 * k
    if panel.note:
        x = left
        if not panel.solid:
            r = 4 * k
            draw.ellipse((x, cy - r, x + 2 * r, cy + r), fill=COLORS["red"])
            x += 2 * r + 7 * k
        note = _truncate(draw, panel.note, font, room - (x - left))
        _text_mid(draw, x, cy, note, font, pal["note"])
    elif panel.hint:
        _text_mid(draw, left, cy, _truncate(draw, panel.hint, font, room), font, pal["mute"])


def _panel_stats(draw, panel, pal, box, k) -> None:
    x0, y0, x1, y1 = box
    n = len(panel.stats)
    gap = 8 * k
    card_w = (x1 - x0 - gap * (n - 1)) / n
    card_h = min(74 * k, y1 - y0)
    top = y0 + (y1 - y0 - card_h) / 2
    value_font = _font(34 * k)
    label_room = card_w - 30 * k
    # The label font shrinks (11 -> 8 px) until the widest label fits the
    # narrow Elgato cards ("NEČINNÝ" at 392 px) instead of truncating them all.
    label_font, spacing = _font(11 * k), 0.8 * k
    for size in range(11, 7, -1):
        label_font, spacing = _font(size * k), (0.8 if size > 9 else 0.3) * k
        widest = max(_spaced_len(draw, st.label.upper(), label_font, spacing) for st in panel.stats)
        if widest <= label_room:
            break
    for i, stat in enumerate(panel.stats):
        cx = x0 + i * (card_w + gap)
        draw.rounded_rectangle((cx, top, cx + card_w, top + card_h), radius=10 * k, fill=_P_CARD)
        r = 4 * k
        dot_cy = top + 17 * k
        draw.ellipse(
            (cx + 11 * k, dot_cy - r, cx + 11 * k + 2 * r, dot_cy + r),
            fill=COLORS.get(stat.color, _P_INK),
        )
        label = _truncate_spaced(draw, stat.label.upper(), label_font, label_room, spacing)
        _draw_spaced(
            draw, (cx + 24 * k, dot_cy - _ink_mid(label_font, "H")), label, label_font,
            pal["mute"], spacing,
        )
        value = _truncate(draw, str(stat.value), value_font, card_w - 22 * k)
        draw.text((cx + 11 * k, top + card_h - 12 * k), value, font=value_font,
                  fill=pal["ink"], anchor="ls")


def _panel_gauges(draw, panel, pal, box, k) -> None:
    x0, y0, x1, y1 = box
    gauges = panel.gauges[:6]
    columns = 1 if len(gauges) <= 3 else 2
    rows = math.ceil(len(gauges) / columns)
    col_gap = 18 * k
    col_w = (x1 - x0 - col_gap * (columns - 1)) / columns
    row_h = 29 * k
    gap = min(10 * k, (y1 - y0 - rows * row_h) / max(1, rows - 1)) if rows > 1 else 0
    top = y0 + (y1 - y0 - rows * row_h - (rows - 1) * gap) / 2
    label_font, pct_font, hint_font = _font(14 * k), _font(14 * k), _font(12 * k, bold=False)
    pace_font = _font(12 * k)
    for i, gauge in enumerate(gauges):
        row, col = divmod(i, columns)
        gx = x0 + col * (col_w + col_gap)
        gy = top + row * (row_h + gap)
        cy = gy + 8 * k
        pct = f"{gauge.used_percent}%"
        pct_w = draw.textlength(pct, font=pct_font)
        _text_mid(draw, gx + col_w - pct_w, cy, pct, pct_font, pal["ink"])
        rx = gx + col_w - pct_w - 10 * k
        label = f"{gauge.label} · {gauge.window}"
        label_w = min(draw.textlength(label, font=label_font), col_w * 0.62)
        # A reset hint that does not fit whole drops its leading word
        # ("reset 14:09" -> "14:09") before it is dropped altogether.
        short_hint = gauge.hint.split(" ", 1)[-1] if gauge.hint else ""
        for options, font, fill in (((gauge.pace,), pace_font, COLORS["amber"]),
                                    ((gauge.hint, short_hint), hint_font, pal["mute"])):
            for text in options:
                if not text:
                    continue
                tw = draw.textlength(text, font=font)
                # the label keeps its full width (+ the 6k truncation margin):
                # the window name matters more than the hint
                if rx - tw - 16 * k < gx + label_w:
                    continue
                _text_mid(draw, rx - tw, cy, text, font, fill)
                rx -= tw + 10 * k
                break
        _text_mid(draw, gx, cy, _truncate(draw, label, label_font, rx - gx - 6 * k), label_font,
                  pal["ink"])
        bar_y = gy + row_h - 6 * k
        _draw_gauge_rail(draw, (gx, bar_y, gx + col_w, bar_y + 6 * k), gauge.used_percent,
                         _gauge_tone(gauge.color, gauge.used_percent), track=_P_CARD)


def _panel_text(draw, panel, pal, box, k) -> None:
    x0, y0, x1, y1 = box
    width = x1 - x0
    if panel.headline:
        head_font = _fit_font(draw, panel.headline, width, 36 * k, 22 * k)
        head = _truncate(draw, panel.headline, head_font, width)
        sub_font = _font(16 * k)
        sub_lh = 21 * k
        head_h = head_font.size * 1.05
        room = int((y1 - y0 - head_h - 9 * k) // sub_lh)
        sub = _panel_wrapped(draw, panel.lines, sub_font, width, max(0, min(2, room)))
        block = head_h + (9 * k + len(sub) * sub_lh if sub else 0)
        y = y0 + (y1 - y0 - block) / 2
        draw.text((x0, y + head_h), head, font=head_font, fill=pal["ink"], anchor="ls")
        y += head_h + 9 * k
        for line in sub:
            draw.text((x0, y + sub_lh * 0.78), line, font=sub_font, fill=pal["mute"], anchor="ls")
            y += sub_lh
        return
    body_font = _font(18 * k, bold=False)
    lh = 24 * k
    body = _panel_wrapped(draw, panel.lines, body_font, width, int((y1 - y0) // lh))
    y = y0
    for line in body:
        draw.text((x0, y + lh * 0.8), line, font=body_font, fill=pal["ink"], anchor="ls")
        y += lh


def _panel_wrapped(draw, lines, font, max_w, max_lines) -> list[str]:
    """Pixel-wrapped display lines (<= max_lines). Panel lines are LOGICAL
    lines; wrapping them here with the actual font keeps a long prompt
    readable — character-count wrapping upstream overflowed the pixel budget."""
    out: list[str] = []
    for line in lines:
        if len(out) >= max_lines:
            break
        out.extend(_wrap(draw, line, font, max_w, max_lines - len(out)))
    # _truncate is a safety net for unbreakable tokens wider than the panel.
    return [_truncate(draw, line, font, max_w) for line in out[:max_lines]]


def compose_panel(panel: PanelView, width: int = PANEL_W) -> Image.Image:
    """Render a PanelView to a width x 196 image.

    The default width is the D200 small window's native 458px. The Elgato
    driver composes at 392 so the image splits into two exact 196x196 key
    images; the web simulator and desktop window display the PNG at its true
    aspect either way.
    """
    k = _PANEL_SS
    w, h = width * k, PANEL_H * k
    pal = _panel_palette(panel)
    img = Image.new("RGB", (w, h), pal["bg"])
    draw = ImageDraw.Draw(img)
    _panel_header(draw, panel, pal, w, k)
    has_footer = bool(panel.hint or panel.note or panel.aside or (panel.page and panel.page[1] > 1))
    top = (_P_PAD_Y + _P_CHIP_H + 10) * k
    bottom = h - (_P_PAD_Y + (_P_FOOT_H + 10 if has_footer else 0)) * k
    box = (_P_PAD_X * k, top, w - _P_PAD_X * k, bottom)
    if panel.stats:
        _panel_stats(draw, panel, pal, box, k)
    elif panel.gauges:
        _panel_gauges(draw, panel, pal, box, k)
    else:
        _panel_text(draw, panel, pal, box, k)
    _panel_footer(draw, panel, pal, w, h, k)
    return img.resize((width, PANEL_H), Image.LANCZOS)


def _gauge_tone(color: str, used_percent: int) -> tuple[int, int, int]:
    if used_percent >= 85:
        return COLORS["red"]
    if used_percent >= 65:
        return COLORS["amber"]
    return COLORS.get(color, COLORS["violet"])


def _draw_gauge_rail(draw, box, used_percent: int, tone, track=_P_CARD) -> None:
    x0, y0, x1, y1 = box
    radius = max(1, (y1 - y0) / 2)
    draw.rounded_rectangle(box, radius=radius, fill=track)
    fill_w = (x1 - x0) * max(0, min(100, used_percent)) / 100
    if fill_w:
        draw.rounded_rectangle((x0, y0, x0 + max(fill_w, 2 * radius), y1), radius=radius, fill=tone)


def _truncate(draw, text, font, max_w):
    if not text or draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def _fit_font(draw, text, max_w, largest, smallest, *, bold=True):
    """The largest font in [smallest, largest] that fits ``text`` into ``max_w``
    (``smallest`` when none does — the caller truncates)."""
    for size in range(largest, smallest - 1, -1):
        font = _font(size, bold=bold)
        if draw.textlength(text, font=font) <= max_w:
            return font
    return _font(smallest, bold=bold)


# The repo name never drops below this: at 18px it matched the branch line and
# the tile lost its primary/secondary hierarchy.
PROJECT_MIN_PX = 20


def _fit_project_name(draw, text, max_w):
    """Keep short names prominent; shrink, then wrap without microscopic text."""
    for size in range(31, PROJECT_MIN_PX - 1, -1):
        font = _font(size)
        if draw.textlength(text, font=font) <= max_w:
            return font, [text]
    # Two PROJECT_MIN_PX lines fit above the branch text. Split long identifiers
    # too, preferring a nearby word or path/name boundary when available.
    cut = 0
    while cut < len(text) and draw.textlength(text[:cut + 1], font=font) <= max_w:
        cut += 1
    boundaries = [i + 1 for i, c in enumerate(text[:cut]) if c in " -_/" and i >= cut // 2]
    # Prefer the latest word boundary whose remainder still fits on line two,
    # then a hard cut at the widest first line: with a wide name the boundary
    # split can push the suffix past the edge, and losing
    # the suffix loses what tells two repos apart.
    for split in [*reversed(boundaries), cut]:
        rest = text[split:].lstrip()
        if draw.textlength(rest, font=font) <= max_w:
            return font, [text[:split].rstrip(), rest]
    if boundaries:
        cut = boundaries[-1]
    return font, [text[:cut].rstrip(), _truncate(draw, text[cut:].lstrip(), font, max_w)]


def _wrap_tokens(text: str, break_after: str) -> list[tuple[str, bool]]:
    """Split text into (piece, space_before) tokens for wrapping.

    Words split on whitespace (space_before=True: a line may break there and
    the space is kept otherwise). Inside a word a line may also break AFTER any
    ``break_after`` char — the char stays glued to the piece before it and no
    space is ever inserted, so ``/tmp/build`` stays ``/tmp/build`` on one line
    and breaks as ``/tmp/`` + ``build`` across two."""
    cls = re.escape(break_after)
    piece_re = re.compile(f"[{cls}]*[^{cls}]+[{cls}]*|[{cls}]+")
    tokens: list[tuple[str, bool]] = []
    for word in text.split():
        pieces = piece_re.findall(word) if break_after else [word]
        for j, piece in enumerate(pieces):
            tokens.append((piece, j == 0))
    return tokens


def _wrap(draw, text, font, max_w, max_lines=2, break_after="/"):
    """Wrap text to <= max_lines lines no wider than ``max_w``.

    Lines break at spaces and after any ``break_after`` char (so paths and
    branch names wrap), and never gain characters: the old ``' / '`` split made
    an approval read ``rm -rf / tmp / build``. A piece wider than a whole line
    is split by characters rather than overflowing the tile.

    A cut-off tail is ALWAYS marked with an ellipsis: silently dropping words
    turned e.g. the drill option "…don't ask again for rm commands in
    /home/user/projects" into an apparent approval for "rm commands in /"."""
    tokens = _wrap_tokens(text, break_after)
    lines: list[str] = []
    cur = ""
    truncated = False
    i = 0
    while i < len(tokens):
        piece, space = tokens[i]
        test = (cur + " " + piece if space else cur + piece) if cur else piece
        if draw.textlength(test, font=font) <= max_w:
            cur = test
            i += 1
            continue
        if cur:
            lines.append(cur)
            cur = ""
        else:
            # the piece alone is wider than a line: hard-split it
            cut = 1
            while cut < len(piece) and draw.textlength(piece[: cut + 1], font=font) <= max_w:
                cut += 1
            lines.append(piece[:cut])
            tokens[i] = (piece[cut:], False)
        if len(lines) == max_lines:
            truncated = True  # tokens[i] (and anything after it) no longer fit
            break
    if cur:
        lines.append(cur)  # loop ended normally -> room for it
    if lines and truncated:
        last = lines[-1].rstrip()
        while last and draw.textlength(last + "…", font=font) > max_w:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines[:max_lines]


def _fit_branch(draw, branch, font, max_w):
    """Branch lines (<= 2) for an agent tile.

    A branch that fits is shown whole on one line. Otherwise the leading path
    segments give way to ``…/`` so the distinctive leaf leads — wrapping
    ``feature/very-long-name`` spent its whole first line on ``feature /``.
    A leaf still too long for one line wraps (at ``-``, ``_``, ``.`` too) onto
    a second, with an ellipsis if even that is cut."""
    if draw.textlength(branch, font=font) <= max_w:
        return [branch]
    head, sep, leaf = branch.rpartition("/")
    if sep and head and leaf:
        branch = "…/" + leaf
        if draw.textlength(branch, font=font) <= max_w:
            return [branch]
    return _wrap(draw, branch, font, max_w, 2, break_after="/-_.")


# href / xlink:href / url(...) values an SVG may carry: in-document ids and
# inline data only. A project's favicon comes from someone else's repo; the
# rasterizers would otherwise read an absolute path or URL on the deck machine.
_SVG_REF_RE = re.compile(r"""(?:href\s*=\s*["']|url\(\s*["']?)\s*([^"')\s]*)""", re.I)


def _svg_references_outside(svg: str) -> bool:
    return any(not ref.startswith(("#", "data:")) for ref in _SVG_REF_RE.findall(svg))


def decode_project_icon(
    stored: StoredIcon, rasterize: Callable[[str, int], Image.Image] = _default_rasterize
) -> Image.Image:
    """Stored favicon bytes -> the normalised ICON_SIZE RGBA mark. Raises
    when the bytes cannot be decoded (callers fall back to the monogram)."""
    if stored.mime == "image/svg+xml":
        svg = stored.data.decode("utf-8")
        if _svg_references_outside(svg):
            raise ValueError("SVG references external resources")
        # resvg (or the frozen rasterizer); either raises for
        # an SVG it cannot render -> monogram.
        raw = rasterize(svg, ICON_SIZE)
    else:
        with _open_project_icon(stored.data) as im:
            im.load()  # ICO: Pillow loads the largest frame
            _check_dims(im.size)  # the frame actually decoded
            raw = im.convert("RGBA")
    return _normalize_project_image(raw)


def project_mark_image(
    stored: StoredIcon | None,
    name: str,
    rasterize: Callable[[str, int], Image.Image] = _default_rasterize,
) -> Image.Image:
    """A project's mark outside a tile (e.g. a notification banner): the
    decoded favicon, else the same monogram the deck shows."""
    if stored is not None:
        try:
            return decode_project_icon(stored, rasterize)
        except Exception as exc:
            log.debug("project icon could not be decoded, using a monogram: %s", exc)
    return _monogram_image(name)


class IconProvider:
    """Resolves agent type -> composited tile-icon PNG in the strmdck icon dir.

    Precedence: user override PNG > Simple Icons (fetch+rasterize) > generated glyph.
    Output PNGs land in cache_dir (which must be the strmdck
    .cache/icons/_generated dir) and are referenced by bare filename.
    """

    def __init__(
        self,
        cache_dir: str,
        slug_map: dict[str, str | None],
        overrides_dir: str | None = None,
        fetch: Callable[[str], str | None] = _default_fetch,
        rasterize: Callable[[str, int], Image.Image] = _default_rasterize,
        assets_dir: str | None = _ASSETS_DIR,
        project_icons: ProjectIconStore | None = None,
    ):
        self._cache_dir = cache_dir
        self._slug_map = slug_map
        self._overrides_dir = overrides_dir
        self._fetch = fetch
        self._rasterize = rasterize
        self._assets_dir = assets_dir
        # Fold the bundled-glyph set into the cache keys so a changed asset set
        # (e.g. an app upgrade that bundles new marks) invalidates stale tiles.
        self._asset_fp = _fingerprint_assets(assets_dir)
        os.makedirs(cache_dir, exist_ok=True)
        prune_generated(cache_dir)
        self._glyph_cache: dict[str, Image.Image] = {}
        self._bytes_cache: OrderedDict[str, bytes] = OrderedDict()
        self._writes_since_prune = 0
        # Layered render caches (all bounded): the static part of an agent tile
        # keyed by its signature WITHOUT the animation phase, so a working
        # tile's 8 frames compose the background/text once and only paste the
        # moving logo / sweep per frame; plus the small per-frame layers.
        self._base_cache: OrderedDict[tuple, Image.Image] = OrderedDict()
        self._layer_cache: dict[tuple, Image.Image] = {}
        self._light_glyph: dict[str, bool] = {}
        # Favicon bytes arrive through the connector into this (shared) store;
        # decoded, normalised images are cached per hash + repo name.
        self._project_icons = project_icons if project_icons is not None else default_store()
        self._project_cache: OrderedDict[tuple[str, str], Image.Image] = OrderedDict()
        self._project_failed: set[str] = set()
        # Set by _project_layer when a tile's icon bytes were missing (evicted
        # between resolve() and the render): that frame is a monogram and must
        # not be cached under the real icon's tile name (see _compose_checked).
        self._render_uncacheable = False

    def _base_glyph(self, agent_type: str) -> Image.Image:
        """A monochrome mark for an agent type.

        Precedence: user override PNG > bundled SVG/PNG asset > Simple Icons > letter.
        """
        if agent_type in self._glyph_cache:
            return self._glyph_cache[agent_type]
        img: Image.Image | None = None
        if self._overrides_dir:
            ov = os.path.join(self._overrides_dir, f"{_safe_name(agent_type)}.png")
            if os.path.exists(ov):
                img = Image.open(ov).convert("RGBA").resize((ICON_SIZE, ICON_SIZE))
        if img is None and self._assets_dir:
            asset = os.path.join(self._assets_dir, f"{_safe_name(agent_type)}.svg")
            if os.path.exists(asset):
                svg = ""
                try:
                    with open(asset, encoding="utf-8") as fh:
                        svg = fh.read()
                    img = self._rasterize(svg, ICON_SIZE)
                except Exception:
                    img = None
                # If resvg is unavailable or fails (e.g. an install without a
                # rendering extra), still use the same committed baked PNGs as
                # the frozen app instead of degrading a bundled mark to text.
                if img is None and svg:
                    baked_asset = os.path.join(
                        self._assets_dir, _baked_glyph_png_name(svg)
                    )
                    try:
                        img = Image.open(baked_asset).convert("RGBA").resize(
                            (ICON_SIZE, ICON_SIZE), Image.LANCZOS
                        )
                    except OSError:
                        img = None
            if img is None:
                raster_asset = os.path.join(
                    self._assets_dir, f"{_safe_name(agent_type)}.png"
                )
                if os.path.exists(raster_asset):
                    try:
                        img = Image.open(raster_asset).convert("RGBA").resize(
                            (ICON_SIZE, ICON_SIZE), Image.LANCZOS
                        )
                    except OSError:
                        img = None
        if img is None:
            slug = self._slug_map.get(agent_type)
            if slug:
                svg = self._fetch(slug)
                if svg:
                    img = self._rasterize(svg, ICON_SIZE)
        if img is None:
            img = self._letter_glyph(agent_type)
        self._glyph_cache[agent_type] = img
        return img

    def _letter_glyph(self, agent_type: str) -> Image.Image:
        img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        ch = (agent_type[:1] or "?").upper()
        font = _load_big_font()
        kw = {"font": font} if font is not None else {}
        # Center manually — the default bitmap font does not support anchor="mm".
        bbox = d.textbbox((0, 0), ch, **kw)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        d.text(
            ((ICON_SIZE - w) // 2 - bbox[0], (ICON_SIZE - h) // 2 - bbox[1]),
            ch,
            fill=(255, 255, 255, 255),
            **kw,
        )
        return img

    def icon_for(self, agent_type: str, color: str, spinner: int | None = None) -> str:
        """Return a cached PNG filename (in cache_dir) of the mark on a status bg."""
        if spinner is not None:
            spinner %= SPINNER_FRAMES  # bound the cache to a fixed frame set
        key = f"{_safe_name(agent_type)}_{color}" + (f"_s{spinner}" if spinner is not None else "")
        name = f"icon_v{CACHE_VERSION}_{self._asset_fp}_{key}.png"
        path = os.path.join(self._cache_dir, name)
        if os.path.exists(path):
            return name
        bg = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), COLORS.get(color, COLORS["dim"]) + (255,))
        # Inset the agent mark so the working ring has clean room around it.
        size = int(ICON_SIZE * LOGO_SCALE)
        glyph = self._base_glyph(agent_type).resize((size, size), Image.LANCZOS)
        off = (ICON_SIZE - size) // 2
        bg.alpha_composite(glyph, (off, off))
        if spinner is not None:
            self._draw_spinner(bg, spinner)
        bg.convert("RGB").save(path)
        return name

    def _comet_overlay(
        self, size: int, phase: int, inset: int, width: int, color=LIGHT_INK
    ) -> Image.Image:
        """A transparent ``size``×``size`` overlay holding an anti-aliased comet
        ring — a bright head with a fading tail — at rotation ``phase``, drawn
        supersampled then downscaled. ``inset`` and ``width`` are in final
        (pre-supersample) pixels; ``color`` is the ring ink (dark on bright
        solid fills, where a white ring all but vanished). Shared by the
        full-tile spinner (``icon_for``) and the per-logo comet animation
        (``_compose_agent_tile``); cached, it is by far the costliest layer."""
        key = ("comet", size, phase, inset, width, tuple(color))
        hit = self._layer_cache.get(key)
        if hit is not None:
            return hit
        z = size * _SS
        ov = Image.new("RGBA", (z, z), (0, 0, 0, 0))
        d = ImageDraw.Draw(ov)
        inset_s, w = inset * _SS, width * _SS
        box = [inset_s, inset_s, z - inset_s, z - inset_s]
        head = phase * (360 / SPINNER_FRAMES)
        step = 4
        for i in range(0, RING_SPAN, step):
            alpha = int(235 * (1 - i / RING_SPAN))
            d.arc(box, head - i - step, head - i, fill=tuple(color) + (alpha,), width=w)
        out = ov.resize((size, size), Image.LANCZOS)
        self._remember_layer(key, out)
        return out

    def _remember_layer(self, key, img) -> None:
        # a handful of agents x inks x styles x 8 phases; the cap only guards
        # against an unbounded stream of distinct agent types
        if len(self._layer_cache) >= _LAYER_CACHE_MAX:
            self._layer_cache.clear()
        self._layer_cache[key] = img

    def _draw_spinner(self, img: Image.Image, phase: int) -> None:
        """Composite the full-tile comet ring used by ``icon_for``."""
        img.alpha_composite(self._comet_overlay(ICON_SIZE, phase, RING_INSET, RING_WIDTH))

    # --- rich tile rendering (full tile incl. text; device label left empty) ---
    def _static_sig(self, tile, *, drop_icon: bool = False) -> list:
        """Every TileView input that shapes the STATIC part of a tile (all but
        the animation phase/style). ``drop_icon`` keys the tile as its monogram
        (no icon hash) — where a fallback render for a missing icon belongs."""
        parts = [
            TILE_VERSION,
            getattr(tile, "pinned", False),
            self._asset_fp,
            tile.color,
            tile.label,
            tile.subtext,
            tile.agent_type,
            tile.repo,
            tile.branch,
            tile.status_text,
            tile.time_text,
            getattr(tile, "tile_fill", "none"),
        ]
        mode = getattr(tile, "tile_icon", "agent")
        parts.append(mode)
        if mode != "agent":
            parts.extend(
                [
                    "" if drop_icon else getattr(tile, "project_icon", None) or "",
                    getattr(tile, "project_name", "") or "",
                ]
            )
        if tile.server_tag or tile.server_accent:
            parts.extend([tile.server_tag, tile.server_accent])
        subagents = getattr(tile, "subagents", 0)
        if subagents:
            parts.append(f"sub{subagents}")
        return parts

    def _tile_name(self, tile, *, drop_icon: bool = False) -> tuple[str, int | None]:
        """The content-addressed cache filename for a TileView (and its bounded
        spinner phase). The rotation phase is bounded to SPINNER_FRAMES so the
        cache reuses a fixed set of frames instead of minting a new PNG per tick."""
        animation = _effective_animation(tile)
        spinner = _anim_phase(tile.spinner, animation)
        sig_parts = self._static_sig(tile, drop_icon=drop_icon) + [spinner]
        if spinner is not None:
            sig_parts.append(animation)
        sig = "|".join(str(x) for x in sig_parts)
        return "tile_" + hashlib.sha1(sig.encode()).hexdigest()[:16] + ".png", spinner

    def _compose(self, tile, spinner) -> Image.Image:
        """Agent tiles (tile.repo set) get the rich layout; control tiles render
        their centred label on a colour."""
        if tile.repo is not None:
            return self._compose_agent_tile(tile, spinner)
        return self._compose_label_tile(tile)

    def _compose_checked(self, tile, spinner, name: str) -> tuple[Image.Image, str]:
        """Compose a tile and return it with the name it may be cached under.

        When the tile's project icon bytes were missing at render time (the
        store evicted them after resolve()), the frame shows the monogram: it is
        filed under the monogram's name (the hash dropped from the signature),
        never under the real icon's, so the favicon appears as soon as its
        bytes return instead of the fallback sticking in the render caches."""
        self._render_uncacheable = False
        img = self._compose(tile, spinner)
        if self._render_uncacheable:
            name = self._tile_name(tile, drop_icon=True)[0]
        return img, name

    def render_tile(self, tile) -> str:
        """Render a full TileView (logo, repo, branch, status, time) to a cached
        PNG FILE and return its filename — for consumers that read the icon by
        name (strmdck/D200). HTTP surfaces use ``render_tile_bytes``."""
        name, spinner = self._tile_name(tile)
        path = os.path.join(self._cache_dir, name)
        try:
            st = os.stat(path)
        except OSError:
            st = None
        if st is not None:
            # Keep actively-served files out of prune's stale window: refresh a
            # sufficiently old mtime on hit, so a filename already handed to the
            # device path (strmdck reads it later, during set_buttons) can never
            # be deleted by the opportunistic prune mid-batch.
            if time.time() - st.st_mtime > PRUNE_MAX_AGE_S / 2:
                try:
                    os.utime(path)
                except OSError:
                    pass
            return name
        img, name = self._compose_checked(tile, spinner, name)
        path = os.path.join(self._cache_dir, name)
        img.convert("RGB").save(path)
        self._writes_since_prune += 1
        if self._writes_since_prune >= _PRUNE_EVERY_WRITES:
            self._writes_since_prune = 0
            prune_generated(self._cache_dir)
        return name

    def render_tile_bytes(self, tile) -> bytes:
        """Render a tile and return its PNG bytes (web simulator / HTTP state).

        Encoded in memory and served from a small in-memory LRU: the HTTP path
        never needs a file, so it no longer writes a PNG to disk only to read
        it straight back."""
        name, spinner = self._tile_name(tile)
        cached = self._bytes_cache.get(name)
        if cached is not None:
            self._bytes_cache.move_to_end(name)
            return cached
        img, name = self._compose_checked(tile, spinner, name)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "PNG")
        data = buf.getvalue()
        self._bytes_cache[name] = data
        while len(self._bytes_cache) > _BYTES_CACHE_MAX:
            self._bytes_cache.popitem(last=False)
        return data

    def _compose_label_tile(self, tile) -> Image.Image:
        if tile.color == "launcher":
            # A management tile, NOT a status: dark background + green accent
            # label. The old full-green launcher was pixel-identical to a
            # WORKING agent tile under solid fill — the deck read as having
            # one more running agent than it had.
            bg = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), TILE_BG + (255,))
            d = ImageDraw.Draw(bg)
            f = _font(30)
            t = _truncate(d, tile.label, f, ICON_SIZE - 16)
            w = d.textlength(t, font=f)
            bb = d.textbbox((0, 0), t, font=f)
            d.text(
                ((ICON_SIZE - w) / 2, (ICON_SIZE - (bb[3] - bb[1])) / 2 - bb[1]),
                t,
                font=f,
                fill=COLORS["green"],
            )
            return bg
        bg_col = COLORS.get(tile.color, COLORS["dim"])
        bg = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), bg_col + (255,))
        # Always-white text read at 2.1:1 on amber and 2.0:1 on cyan (a drill
        # choice on an amber approval tile); take the higher-contrast ink.
        ink = _ink_for(bg_col)
        if tile.subtext:
            # Drill choice tile: big number (label) up top, small wrapped choice
            # text underneath — readable instead of a truncated "1 Yes…" line.
            d = ImageDraw.Draw(bg)
            nf = _font(58)
            nw = d.textlength(tile.label, font=nf)
            d.text(((ICON_SIZE - nw) / 2, 16), tile.label, font=nf, fill=ink)
            sf = _font(22)
            sub_fill = _soften(ink, bg_col, max_mix=0.1)
            y = 92
            for line in _wrap(d, tile.subtext, sf, ICON_SIZE - 16, 3):
                lw = d.textlength(line, font=sf)
                d.text(((ICON_SIZE - lw) / 2, y), line, font=sf, fill=sub_fill)
                y += 26
        elif tile.label:
            d = ImageDraw.Draw(bg)
            f = _font(28)
            t = _truncate(d, tile.label, f, ICON_SIZE - 16)
            w = d.textlength(t, font=f)
            bb = d.textbbox((0, 0), t, font=f)
            d.text(
                ((ICON_SIZE - w) / 2, (ICON_SIZE - (bb[3] - bb[1])) / 2 - bb[1]),
                t,
                font=f,
                fill=ink,
            )
        return bg

    @staticmethod
    def _tile_bg(tile) -> tuple[str, tuple, tuple]:
        """(fill, accent, background colour) for an agent tile.

        tile_fill: how much of the tile the status colour covers.
          none  -> dark background (colour lives in the word + bottom bar)
          tint  -> whole tile a darkened shade of the colour + bright bottom edge
          solid -> whole tile the full colour"""
        accent = COLORS.get(tile.color, COLORS["dim"])
        fill = getattr(tile, "tile_fill", "none")
        if fill == "solid":
            return fill, accent, accent
        if fill == "tint":
            return fill, accent, _tint_bg(accent)
        return fill, accent, TILE_BG

    def _compose_agent_tile(self, tile, spinner=None) -> Image.Image:
        """Compose an agent tile in two layers: the static base (background,
        text, bar — cached by the signature WITHOUT the animation phase) and
        the per-frame motion (logo, comet ring, sweep segment)."""
        anim = _effective_animation(tile)
        sweep = spinner is not None and anim == "sweep"
        key = (*self._static_sig(tile), sweep)
        base = self._base_cache.get(key)
        if base is None:
            base = self._compose_agent_base(tile, static_bar=not sweep)
            self._base_cache[key] = base
            while len(self._base_cache) > _BASE_CACHE_MAX:
                self._base_cache.popitem(last=False)
        else:
            self._base_cache.move_to_end(key)
        img = base.copy()
        self._draw_agent_motion(img, tile, spinner, anim)
        return img

    def _compose_agent_base(self, tile, *, static_bar: bool) -> Image.Image:
        fill, accent, bg_col = self._tile_bg(tile)
        bg = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), bg_col + (255,))
        d = ImageDraw.Draw(bg)
        # text colours chosen for contrast against the fill (see _tile_text_colors)
        repo_fill, branch_fill, time_fill, word_fill = _tile_text_colors(fill, bg_col, accent)
        right = ICON_SIZE - 12
        # status word + elapsed time, top-right, beside the logo box (x < 66,
        # incl. the comet ring). Sized for a ~25mm physical key, where the old
        # 19px word shrank to ~7px at the D200's 72px key.
        if tile.status_text:
            fs = _fit_font(d, tile.status_text, STATUS_MAX_W, 24, 17)
            word = _truncate(d, tile.status_text, fs, STATUS_MAX_W)
            d.text((right - d.textlength(word, font=fs), 10), word, font=fs, fill=word_fill)
        if tile.time_text:
            ft = _fit_font(d, tile.time_text, STATUS_MAX_W, 20, 16)
            t = _truncate(d, tile.time_text, ft, STATUS_MAX_W)
            d.text((right - d.textlength(t, font=ft), 40), t, font=ft, fill=time_fill)
        # repo (primary) + branch (secondary) — spread down the tile so the
        # composition is optically centred between the logo row and the bottom
        # band instead of leaving a dead band across the bottom third.
        fr, project_lines = _fit_project_name(d, tile.repo or "", ICON_SIZE - 24)
        for i, line in enumerate(project_lines):
            d.text((12, 72 if len(project_lines) == 1 else 66 + i * 23),
                   line, font=fr, fill=repo_fill)
        if tile.branch:
            fb = _font(18, bold=False)
            y = 116
            for line in _fit_branch(d, tile.branch, fb, ICON_SIZE - 24):
                d.text((12, y), line, font=fb, fill=branch_fill)
                y += 22
        # bottom band: backend tag (left) + pin (right), at a size that still
        # reads on a 72px key (the old 12px tag / 12px pin vanished there).
        pinned = getattr(tile, "pinned", False)
        badge_w = self._draw_subagent_badge(
            d, getattr(tile, "subagents", 0), right - (26 if pinned else 0), time_fill
        )
        if tile.server_tag:
            fc = _font(16, bold=False)
            tag_w = ICON_SIZE - 24 - (26 if pinned else 0) - (badge_w + 8 if badge_w else 0)
            tag = _truncate(d, tile.server_tag, fc, tag_w)
            d.text((12, 164), tag, font=fc, fill=time_fill)
        if pinned:
            # pin silhouette (head, collar, needle), independent of backend labels
            cx, top = ICON_SIZE - 20, 163
            d.rectangle((cx - 5, top, cx + 5, top + 8), fill=time_fill)
            d.line((cx - 8, top + 10, cx + 8, top + 10), fill=time_fill, width=3)
            d.line((cx, top + 11, cx, top + 22), fill=time_fill, width=2)
        # the plain static bottom bar — only when the fill isn't solid (on
        # solid it would be invisible) and no sweep is drawn over it per frame
        if static_bar and fill != "solid":
            d.rectangle([0, ICON_SIZE - 8, ICON_SIZE, ICON_SIZE], fill=accent)
        return bg

    @staticmethod
    def _draw_subagent_badge(d, count: int, right: int, ink) -> int:
        """The "⑂N" running-subagents badge, right-aligned at ``right`` in the
        bottom band; returns its width (0 when nothing is drawn).

        The fork is drawn as lines (the vendored Inter has no U+2442 glyph):
        a stem splitting into two prongs, then the count."""
        if count <= 0:
            return 0
        text = str(count) if count < 100 else "99+"
        font = _font(16)
        text_w = int(d.textlength(text, font=font))
        fork_w = 12
        width = fork_w + 3 + text_w
        x0 = right - width
        top, mid, bottom = 167, 174, 182
        left_x, right_x = x0 + 1, x0 + fork_w - 1
        stem_x = x0 + fork_w // 2
        d.line((stem_x, mid, stem_x, bottom), fill=ink, width=2)
        d.line((left_x, mid, right_x, mid), fill=ink, width=2)
        d.line((left_x, top, left_x, mid), fill=ink, width=2)
        d.line((right_x, top, right_x, mid), fill=ink, width=2)
        d.text((x0 + fork_w + 3, 164), text, font=font, fill=ink)
        return width

    def _logo(self, agent_type: str, dark: bool, size: int, rotation: float = 0) -> Image.Image:
        """The agent mark at ``size`` px (dark-recoloured and/or rotated), cached."""
        key = ("logo", agent_type, dark, size, rotation)
        hit = self._layer_cache.get(key)
        if hit is not None:
            return hit
        glyph = self._base_glyph(agent_type)
        if dark:
            ink = Image.new("RGBA", glyph.size, DARK_INK + (0,))
            ink.putalpha(glyph.getchannel("A"))
            glyph = ink
        out = glyph.resize((size, size), Image.LANCZOS)
        if rotation:
            out = out.rotate(-rotation, resample=Image.BICUBIC)
        self._remember_layer(key, out)
        return out

    def _is_light_glyph(self, agent_type: str) -> bool:
        hit = self._light_glyph.get(agent_type)
        if hit is None:
            hit = self._light_glyph[agent_type] = _is_light_monochrome(
                self._base_glyph(agent_type)
            )
        return hit

    def _project_base(self, icon_hash: str, name: str) -> tuple[Image.Image, bool]:
        """(normalised ICON_SIZE image, cacheable) for a tile's project mark.
        A hash whose bytes are not in the store (evicted) renders the monogram
        but is not cached, so the icon shows as soon as its bytes return."""
        key = (icon_hash, name)
        hit = self._project_cache.get(key)
        if hit is not None:
            self._project_cache.move_to_end(key)
            return hit, True
        img: Image.Image | None = None
        cacheable = True
        if icon_hash:
            stored = self._project_icons.get(icon_hash)
            if stored is None:
                cacheable = False
            else:
                img = self._decode_project_icon(icon_hash, stored)
        if img is None:
            img = _monogram_image(name)
        if cacheable:
            self._project_cache[key] = img
            while len(self._project_cache) > _PROJECT_CACHE_MAX:
                self._project_cache.popitem(last=False)
        return img, cacheable

    def _decode_project_icon(self, icon_hash: str, stored: StoredIcon) -> Image.Image | None:
        try:
            return decode_project_icon(stored, self._rasterize)
        except Exception as exc:
            if icon_hash not in self._project_failed:
                self._project_failed.add(icon_hash)
                log.warning(
                    "project icon %s (%s) could not be decoded, showing a monogram: %s",
                    icon_hash,
                    stored.mime,
                    exc,
                )
            return None

    def _project_layer(self, tile, size: int, bg_col) -> Image.Image:
        """The project mark at ``size`` px on its contrast plate (if needed)."""
        icon_hash = getattr(tile, "project_icon", None) or ""
        name = getattr(tile, "project_name", "") or ""
        key = ("proj", icon_hash, name, size, tuple(bg_col))
        hit = self._layer_cache.get(key)
        if hit is not None:
            return hit
        base, cacheable = self._project_base(icon_hash, name)
        if not cacheable:
            self._render_uncacheable = True
        plate = _plate_for(base, bg_col)
        out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        if plate is not None:
            ImageDraw.Draw(out).rounded_rectangle(
                [0, 0, size - 1, size - 1], radius=round(size * PROJECT_RADIUS), fill=plate + (255,)
            )
            pad = max(2, size // 12)
            inner = max(1, size - 2 * pad)
            out.alpha_composite(base.resize((inner, inner), Image.LANCZOS), (pad, pad))
        else:
            out.alpha_composite(base.resize((size, size), Image.LANCZOS))
        if cacheable:
            self._remember_layer(key, out)
        return out

    def _draw_project_mark(self, img, tile, spinner, anim, dark_fill, bg_col) -> None:
        """tile_icon = project: the favicon in the 46px logo box. It never
        rotates (spin arrives here as comet); pulse scales it."""
        working = spinner is not None
        if working and anim == "pulse":
            s = _pulse_size(spinner)
            off = 12 + (46 - s) // 2
            img.alpha_composite(self._project_layer(tile, s, bg_col), (off, off))
            return
        img.alpha_composite(self._project_layer(tile, 46, bg_col), (12, 12))
        if working and anim == "comet":
            ring = DARK_INK if dark_fill else LIGHT_INK
            img.alpha_composite(self._comet_overlay(62, spinner, 2, 4, ring), (4, 4))

    def _draw_project_badge(self, img, tile, bg_col) -> None:
        """tile_icon = both: a static badge on the logo box's bottom-right
        corner, ringed in the tile colour so it reads apart from the mark."""
        x = PROJECT_BADGE_XY
        ImageDraw.Draw(img).rounded_rectangle(
            [x - 2, x - 2, x + PROJECT_BADGE + 1, x + PROJECT_BADGE + 1],
            radius=round((PROJECT_BADGE + 4) * PROJECT_RADIUS),
            fill=tuple(bg_col) + (255,),
        )
        img.alpha_composite(self._project_layer(tile, PROJECT_BADGE, bg_col), (x, x))

    def _draw_agent_motion(self, img: Image.Image, tile, spinner, anim) -> None:
        """The per-frame layer: the logo (top-left; animated while working per
        the chosen style), the comet ring and the sweep segment. With
        tile_icon = project the favicon takes the mark's place; with both, the
        static project badge is drawn LAST so neither the mark nor the ring
        covers it (the mark is composited here, above the cached base)."""
        fill, accent, bg_col = self._tile_bg(tile)
        working = spinner is not None
        agent = tile.agent_type or "default"
        mode = getattr(tile, "tile_icon", "agent")
        # A white mark / ring washes out on bright solid fills (amber 2.1:1,
        # cyan 2.0:1 — below the 3:1 non-text minimum) while the text correctly
        # flips dark: recolour them to the same dark ink. Full-colour user
        # overrides are left as supplied (the flip would flatten them).
        dark_fill = fill == "solid" and _ink_for(bg_col) == DARK_INK
        if mode == "project":
            self._draw_project_mark(img, tile, spinner, anim, dark_fill, bg_col)
        else:
            dark_logo = dark_fill and self._is_light_glyph(agent)
            if working and anim == "pulse":
                # slow "breath": scale the mark between ~0.82x and 1.0x across the
                # PULSE_STATES frames (the spinner here is already the SLOW phase
                # from _anim_phase — one step per PULSE_SLOWDOWN ticks)
                s = _pulse_size(spinner)
                off = 12 + (46 - s) // 2  # keep the smaller mark centred in its 46px box
                img.alpha_composite(self._logo(agent, dark_logo, s), (off, off))
            else:
                rotation = spinner * SPIN_DEG if working and anim == "spin" else 0
                img.alpha_composite(self._logo(agent, dark_logo, 46, rotation), (12, 12))
                if working and anim == "comet":
                    # thin comet ring orbiting the static mark; the 62px overlay is
                    # centred over the 46px logo box at (12,12) -> composite at (4,4)
                    ring = DARK_INK if dark_fill else LIGHT_INK
                    img.alpha_composite(self._comet_overlay(62, spinner, 2, 4, ring), (4, 4))
            if mode == "both":
                self._draw_project_badge(img, tile, bg_col)
        # "sweep" is a moving segment along the bottom edge; it must stay
        # visible on any fill, so its colours adapt — on a solid tile
        # (background already = accent) a dark base + a bright segment; on
        # none/tint a dimmed base + the accent segment.
        if working and anim == "sweep":
            d = ImageDraw.Draw(img)
            y0 = ICON_SIZE - 8
            if fill == "solid":
                base = tuple(int(c * 0.45) for c in accent)
                seg_col = tuple(min(255, c + 90) for c in accent)
            else:
                base = tuple(int(c * 0.4) for c in accent)
                seg_col = accent
            d.rectangle([0, y0, ICON_SIZE, ICON_SIZE], fill=base)
            seg_w = ICON_SIZE // 4
            left = int((spinner / SPINNER_FRAMES) * ICON_SIZE)
            d.rectangle([left, y0, min(left + seg_w, ICON_SIZE), ICON_SIZE], fill=seg_col)
            if left + seg_w > ICON_SIZE:  # wrap the bright segment past the right edge
                d.rectangle([0, y0, (left + seg_w) - ICON_SIZE, ICON_SIZE], fill=seg_col)
