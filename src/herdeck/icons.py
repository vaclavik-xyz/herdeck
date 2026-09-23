from __future__ import annotations

import hashlib
import io
import math
import os
import re
import time
from collections import OrderedDict
from collections.abc import Callable

from PIL import Image, ImageDraw

from .driver.base import COLORS, PanelView

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


def _default_rasterize(svg: str, size: int) -> Image.Image:
    import cairosvg  # build-time only; not needed in tests

    png = cairosvg.svg2png(bytestring=svg.encode(), output_width=size, output_height=size)
    import io

    return Image.open(io.BytesIO(png)).convert("RGBA")


# Candidate scalable fonts for the letter fallback (macOS, then Linux).
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
TILE_VERSION = 14
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
    from PIL import ImageStat

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


def _panel_bg(color: str) -> tuple[int, int, int]:
    if color == "amber":
        return (40, 30, 12)
    if color == "grey":
        return (30, 30, 34)
    rgb = COLORS.get(color)
    if rgb is None:
        return (30, 30, 34)
    return tuple(max(12, int(channel * 0.28)) for channel in rgb)


_PANEL_BODY_LINES = 3


def _panel_body_lines(draw, lines, font, max_w, max_lines=_PANEL_BODY_LINES) -> list[str]:
    """Pixel-wrapped display lines for the panel body (<= max_lines total).

    Panel lines are LOGICAL lines; wrapping them here with the actual font is
    what keeps a long prompt readable — character-count wrapping upstream
    systematically overflowed the pixel budget and ellipsized every full line."""
    out: list[str] = []
    for line in lines:
        if len(out) == max_lines:
            break
        out.extend(_wrap(draw, line, font, max_w, max_lines - len(out)))
    return out[:max_lines]


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


def compose_panel(panel: PanelView, width: int = PANEL_W) -> Image.Image:
    """Render a PanelView to a width x 196 image with large, readable text.

    The default width is the D200 small window's native 458px. The Elgato
    driver composes at 392 so the image splits into two exact 196x196 key
    images; the web simulator and desktop window display the PNG at its true
    aspect either way.
    """
    if panel.gauges:
        return _compose_gauge_panel(panel, width)
    bg = _panel_bg(panel.color)
    img = Image.new("RGB", (width, PANEL_H), bg)
    d = ImageDraw.Draw(img)
    title_f = _font(30)
    d.text(
        (16, 12),
        _truncate(d, panel.title, title_f, width - 32),
        font=title_f,
        fill=(255, 255, 255),
    )
    line_f = _font(24)
    y = 60
    for line in _panel_body_lines(d, panel.lines, line_f, width - 32):
        # _truncate is a safety net for unbreakable tokens wider than the panel.
        d.text((16, y), _truncate(d, line, line_f, width - 32), font=line_f, fill=(232, 232, 236))
        y += 40
    return img


_GAUGE_BG = (49, 55, 65)
_GAUGE_CARD = (66, 73, 85)
_GAUGE_LINE = (104, 112, 126)
_GAUGE_MUTED = (200, 205, 215)
_GAUGE_TEXT = (251, 252, 253)
# Gauge labels are neutral: the palette colour (violet 'CLAUDE 5h' read at
# 2.2:1 on the card) lives only in the rail, which also shifts to amber/red as
# the limit nears — a coloured label then disagreed with its own bar.
_GAUGE_LABEL = _GAUGE_MUTED


def _gauge_tone(color: str, used_percent: int) -> tuple[int, int, int]:
    if used_percent >= 85:
        return COLORS["red"]
    if used_percent >= 65:
        return COLORS["amber"]
    return COLORS.get(color, COLORS["violet"])


def _draw_gauge_rail(draw, box, used_percent: int, tone) -> None:
    x0, y0, x1, y1 = box
    radius = max(1, (y1 - y0) // 2)
    draw.rounded_rectangle(box, radius=radius, fill=_GAUGE_LINE)
    fill_w = round((x1 - x0) * max(0, min(100, used_percent)) / 100)
    if fill_w:
        draw.rounded_rectangle((x0, y0, x0 + max(fill_w, 4), y1), radius=radius, fill=tone)


def _compose_gauge_panel(panel: PanelView, width: int) -> Image.Image:
    """Render usage as compact instrument gauges for the physical status window."""
    img = Image.new("RGB", (width, PANEL_H), _GAUGE_BG)
    draw = ImageDraw.Draw(img)
    detail = bool(panel.gauge_meta)
    title_font = _font(25)
    draw.text(
        (16, 10),
        _truncate(draw, panel.title, title_font, width * 0.56),
        font=title_font,
        fill=_GAUGE_TEXT,
    )
    meta = panel.gauge_meta if detail else panel.lines[0] if panel.lines else ""
    if meta:
        meta_font = _font(15)
        meta = meta.upper()
        meta = _truncate(draw, meta, meta_font, width * 0.38)
        meta_w = draw.textlength(meta, font=meta_font)
        draw.text((width - 16 - meta_w, 17), meta, font=meta_font, fill=_GAUGE_MUTED)
    draw.line((16, 45, width - 16, 45), fill=_GAUGE_LINE, width=1)

    columns = min(3, len(panel.gauges)) if detail else min(2, len(panel.gauges))
    rows = math.ceil(len(panel.gauges) / columns)
    gap = 8
    left = 16
    top = 54
    available_w = width - 2 * left - gap * (columns - 1)
    available_h = PANEL_H - top - 10 - gap * (rows - 1)
    cell_w = available_w / columns
    cell_h = available_h / rows

    for index, gauge in enumerate(panel.gauges):
        row, col = divmod(index, columns)
        x0 = round(left + col * (cell_w + gap))
        y0 = round(top + row * (cell_h + gap))
        x1 = round(x0 + cell_w)
        y1 = round(y0 + cell_h)
        draw.rounded_rectangle((x0, y0, x1, y1), radius=8, fill=_GAUGE_CARD)
        tone = _gauge_tone(gauge.color, gauge.used_percent)

        if detail:
            label_font = _font(16)
            value_font = _font(31)
            hint_font = _font(13)
            label = f"{gauge.label} · {gauge.window}".upper()
            draw.text(
                (x0 + 10, y0 + 9),
                _truncate(draw, label, label_font, cell_w - 20),
                font=label_font,
                fill=_GAUGE_LABEL,
            )
            draw.text(
                (x0 + 10, y0 + 34), f"{gauge.used_percent}%", font=value_font, fill=_GAUGE_TEXT
            )
            if gauge.hint:
                draw.text(
                    (x0 + 10, y1 - 36),
                    _truncate(draw, gauge.hint, hint_font, cell_w - 20),
                    font=hint_font,
                    fill=_GAUGE_MUTED,
                )
            _draw_gauge_rail(draw, (x0 + 10, y1 - 15, x1 - 10, y1 - 10), gauge.used_percent, tone)
        else:
            roomy = cell_h >= 90
            label_font = _font(16 if roomy else 14)
            value_font = _font(36 if roomy else 22)
            hint_font = _font(14 if roomy else 12)
            label = f"{gauge.label}  {gauge.window}".upper()
            label_space = cell_w - 18 if roomy else cell_w - 62
            draw.text(
                (x0 + 9, y0 + (10 if roomy else 7)),
                _truncate(draw, label, label_font, label_space),
                font=label_font,
                fill=_GAUGE_LABEL,
            )
            value = f"{gauge.used_percent}%"
            if roomy:
                draw.text((x0 + 9, y0 + 35), value, font=value_font, fill=_GAUGE_TEXT)
            else:
                value_w = draw.textlength(value, font=value_font)
                draw.text(
                    (x1 - 9 - value_w, y0 + 3),
                    value,
                    font=value_font,
                    fill=_GAUGE_TEXT,
                )
            if gauge.hint:
                hint = gauge.hint.upper()
                hint_y = y1 - (43 if roomy else 31)
                draw.text(
                    (x0 + 9, hint_y),
                    _truncate(draw, hint, hint_font, cell_w - 18),
                    font=hint_font,
                    fill=_GAUGE_MUTED,
                )
            _draw_gauge_rail(
                draw,
                (x0 + 9, y1 - (17 if roomy else 12), x1 - 9, y1 - (11 if roomy else 7)),
                gauge.used_percent,
                tone,
            )
    return img


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
    # then a hard cut at the widest first line: with a wide font (DejaVu on
    # Linux) the boundary split can push the suffix past the edge, and losing
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
                # Source installs using the ``elgato`` extra intentionally omit
                # CairoSVG. They can still consume the same committed baked PNGs
                # as the frozen app instead of degrading a bundled mark to text.
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
    def _static_sig(self, tile) -> list:
        """Every TileView input that shapes the STATIC part of a tile (all but
        the animation phase/style)."""
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
        if tile.server_tag or tile.server_accent:
            parts.extend([tile.server_tag, tile.server_accent])
        return parts

    def _tile_name(self, tile) -> tuple[str, int | None]:
        """The content-addressed cache filename for a TileView (and its bounded
        spinner phase). The rotation phase is bounded to SPINNER_FRAMES so the
        cache reuses a fixed set of frames instead of minting a new PNG per tick."""
        animation = getattr(tile, "working_animation", "spin")
        spinner = _anim_phase(tile.spinner, animation)
        sig_parts = self._static_sig(tile) + [spinner]
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
        self._compose(tile, spinner).convert("RGB").save(path)
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
        buf = io.BytesIO()
        self._compose(tile, spinner).convert("RGB").save(buf, "PNG")
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
        anim = getattr(tile, "working_animation", "spin")
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
        if tile.server_tag:
            fc = _font(16, bold=False)
            tag = _truncate(d, tile.server_tag, fc, ICON_SIZE - 24 - (26 if pinned else 0))
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

    def _draw_agent_motion(self, img: Image.Image, tile, spinner, anim) -> None:
        """The per-frame layer: the logo (top-left; animated while working per
        the chosen style), the comet ring and the sweep segment."""
        fill, accent, bg_col = self._tile_bg(tile)
        working = spinner is not None
        agent = tile.agent_type or "default"
        # A white mark / ring washes out on bright solid fills (amber 2.1:1,
        # cyan 2.0:1 — below the 3:1 non-text minimum) while the text correctly
        # flips dark: recolour them to the same dark ink. Full-colour user
        # overrides are left as supplied (the flip would flatten them).
        dark_fill = fill == "solid" and _ink_for(bg_col) == DARK_INK
        dark_logo = dark_fill and self._is_light_glyph(agent)
        if working and anim == "pulse":
            # slow "breath": scale the mark between ~0.82x and 1.0x across the
            # PULSE_STATES frames (the spinner here is already the SLOW phase
            # from _anim_phase — one step per PULSE_SLOWDOWN ticks)
            f = 0.82 + 0.18 * (0.5 + 0.5 * math.sin(2 * math.pi * spinner / PULSE_STATES))
            s = max(1, round(46 * f))
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
