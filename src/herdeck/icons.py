from __future__ import annotations

import hashlib
import math
import os
import re
import time
from collections import OrderedDict
from collections.abc import Callable

from PIL import Image, ImageDraw

from .driver.base import COLORS, PanelView

ICON_SIZE = 196
PANEL_W, PANEL_H = 392, 196  # the D200 status window spans two 196px cells
# The spinner has this many distinct frames; keep the cache bounded so a
# long-running working tile reuses frames instead of writing forever.
SPINNER_FRAMES = 8
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


def prune_generated(cache_dir: str, max_age_s: float = PRUNE_MAX_AGE_S) -> int:
    """Delete generated ``tile_*``/``icon_v*`` PNGs older than ``max_age_s``.

    Only the content-addressed names this module writes are touched; anything
    else in the dir (panel_left.png, user files) is left alone. Returns the
    number of files removed."""
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
            if not (name.startswith("tile_") or name.startswith("icon_v")):
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
    a newly bundled agent (the Q1-on-upgrade staleness seen on macbench).
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


# agent type -> Simple Icons slug (None => generated glyph fallback)
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
_GLYPH_FONT_SIZE = 120
_font_cache: dict[int, object] = {}  # size -> font (a TrueType or sized default)

# Bump when tile composition changes so stale cached tile PNGs are ignored.
# 2: tile_fill (none/tint/solid) — solid contrast + solid-sweep composition.
# 3: readable subtext (branch/time) on solid dark-colour fills (e.g. blue).
# 4: wrapped text marks a cut-off tail with an ellipsis.
# 5: the agent mark flips dark on bright solid fills (like the text).
# 6: larger type scale (repo 31px, sub-labels 18-19px) spread down the tile.
# 7: the dark flip applies only to light-monochrome marks (colour overrides
#    render as supplied again).
TILE_VERSION = 7
TILE_BG = (26, 26, 30)  # dark agent-tile background
SPIN_DEG = 360 / SPINNER_FRAMES  # degrees per rotation phase


def _lum(c: tuple[int, int, int]) -> float:
    """Perceived luminance (Rec. 601) of an RGB colour, on a 0-255 scale."""
    return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]


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


def _tile_text_colors(fill, bg_col, accent):
    """(repo, branch, time, status-word) colours for an agent tile, picked for
    contrast against the fill background.

    none/tint sit on a dark background -> white repo + dim-grey subtext, and the
    status word keeps the accent colour. A solid fill flips by background
    brightness: a bright colour (green/amber/cyan) takes dark text; a darker
    colour (e.g. blue) keeps light text but with a near-white subtext so the
    branch + elapsed time stay readable on the colour instead of washing out."""
    solid = fill == "solid"
    if solid and _lum(bg_col) > 120:  # bright colour -> dark text
        return (18, 18, 22), (45, 45, 50), (55, 55, 60), (18, 18, 22)
    if solid:  # darker colour -> light text, brighter subtext than on the dark bg
        return (255, 255, 255), (230, 230, 236), (215, 215, 222), (255, 255, 255)
    return (255, 255, 255), (180, 180, 188), (165, 165, 170), accent  # none / tint



SERVER_CHIP_COLORS: dict[str, tuple[int, int, int]] = {
    "teal": (24, 150, 145),
    "violet": (135, 100, 235),
    "orange": (220, 115, 35),
    "pink": (215, 80, 135),
    "lime": (125, 175, 45),
}


def _rgb_color(name: str, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    if isinstance(name, str) and name.startswith("#") and len(name) == 7:
        try:
            return tuple(int(name[i : i + 2], 16) for i in (1, 3, 5))
        except ValueError:
            return fallback
    return SERVER_CHIP_COLORS.get(name, fallback)


def _font(size: int):
    """A scalable font at the given size; None only if nothing is available."""
    if size in _font_cache:
        return _font_cache[size]
    from PIL import ImageFont

    font = None
    for path in _FONT_CANDIDATES:
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
    _font_cache[size] = font
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


def compose_panel(panel: PanelView) -> Image.Image:
    """Render a PanelView to a 392x196 image with large, readable text.

    Shared by the D200 driver (split into two cells) and the web simulator.
    """
    bg = _panel_bg(panel.color)
    img = Image.new("RGB", (PANEL_W, PANEL_H), bg)
    d = ImageDraw.Draw(img)
    title_f = _font(30)
    d.text(
        (16, 12),
        _truncate(d, panel.title, title_f, PANEL_W - 32),
        font=title_f,
        fill=(255, 255, 255),
    )
    line_f = _font(24)
    y = 60
    for line in _panel_body_lines(d, panel.lines, line_f, PANEL_W - 32):
        # _truncate is a safety net for unbreakable tokens wider than the panel.
        d.text((16, y), _truncate(d, line, line_f, PANEL_W - 32), font=line_f, fill=(232, 232, 236))
        y += 40
    return img


def _truncate(draw, text, font, max_w):
    if not text or draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def _wrap(draw, text, font, max_w, max_lines=2):
    """Wrap text (splitting on '/' too, for branch names) to <= max_lines.

    A cut-off tail is ALWAYS marked with an ellipsis: silently dropping words
    turned e.g. the drill option "…don't ask again for rm commands in
    /Users/admin/projects" into an apparent approval for "rm commands in /"."""
    words = text.replace("/", " / ").split()
    lines: list[str] = []
    cur = ""
    truncated = False
    for w in words:
        test = (cur + " " + w).strip()
        if draw.textlength(test, font=font) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
        if len(lines) == max_lines:
            truncated = True  # cur (and any remaining words) no longer fit
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if lines:
        if truncated:
            last = lines[-1]
            while last and draw.textlength(last + "…", font=font) > max_w:
                last = last[:-1]
            lines[-1] = last + "…"
        else:
            lines[-1] = _truncate(draw, lines[-1], font, max_w)
    return lines[:max_lines]


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

    def _base_glyph(self, agent_type: str) -> Image.Image:
        """A monochrome mark for an agent type.

        Precedence: user override PNG > bundled SVG asset > Simple Icons > letter.
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
                try:
                    with open(asset, encoding="utf-8") as fh:
                        img = self._rasterize(fh.read(), ICON_SIZE)
                except Exception:
                    img = None  # e.g. cairosvg missing -> fall back to a letter
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

    def _comet_overlay(self, size: int, phase: int, inset: int, width: int) -> Image.Image:
        """A transparent ``size``×``size`` overlay holding an anti-aliased comet
        ring — a bright head with a fading tail — at rotation ``phase``, drawn
        supersampled then downscaled. ``inset`` and ``width`` are in final
        (pre-supersample) pixels. Shared by the full-tile spinner (``icon_for``)
        and the per-logo comet animation (``_compose_agent_tile``)."""
        z = size * _SS
        ov = Image.new("RGBA", (z, z), (0, 0, 0, 0))
        d = ImageDraw.Draw(ov)
        inset_s, w = inset * _SS, width * _SS
        box = [inset_s, inset_s, z - inset_s, z - inset_s]
        head = phase * (360 / SPINNER_FRAMES)
        step = 4
        for i in range(0, RING_SPAN, step):
            alpha = int(235 * (1 - i / RING_SPAN))
            d.arc(box, head - i - step, head - i, fill=(255, 255, 255, alpha), width=w)
        return ov.resize((size, size), Image.LANCZOS)

    def _draw_spinner(self, img: Image.Image, phase: int) -> None:
        """Composite the full-tile comet ring used by ``icon_for``."""
        img.alpha_composite(self._comet_overlay(ICON_SIZE, phase, RING_INSET, RING_WIDTH))

    # --- rich tile rendering (full tile incl. text; device label left empty) ---
    def _tile_name(self, tile) -> tuple[str, int | None]:
        """The content-addressed cache filename for a TileView (and its bounded
        spinner phase). The rotation phase is bounded to SPINNER_FRAMES so the
        cache reuses a fixed set of frames instead of minting a new PNG per tick."""
        # Style "none" renders NO spinner — the phase must not reach the
        # signature, or a working tile mints a new (pixel-identical) filename
        # every tick and the D200's identical-frame skip never fires (a full-set
        # page reload per tick = the visible flicker).
        animation = getattr(tile, "working_animation", "spin")
        spinner = (
            None
            if tile.spinner is None or animation == "none"
            else tile.spinner % SPINNER_FRAMES
        )
        sig_parts = [
            TILE_VERSION,
            self._asset_fp,
            tile.color,
            tile.label,
            tile.subtext,
            tile.agent_type,
            spinner,
            tile.repo,
            tile.branch,
            tile.status_text,
            tile.time_text,
            getattr(tile, "tile_fill", "none"),
        ]
        if spinner is not None:
            sig_parts.append(animation)
        if tile.server_tag or tile.server_accent:
            sig_parts.extend([tile.server_tag, tile.server_accent])
        sig = "|".join(str(x) for x in sig_parts)
        return "tile_" + hashlib.sha1(sig.encode()).hexdigest()[:16] + ".png", spinner

    def render_tile(self, tile) -> str:
        """Render a full TileView (logo, repo, branch, status, time) to a cached
        PNG and return its filename. Agent tiles (tile.repo set) get the rich
        layout; control tiles render their centred label on a colour."""
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
        img = (
            self._compose_agent_tile(tile, spinner)
            if tile.repo is not None
            else self._compose_label_tile(tile)
        )
        img.convert("RGB").save(path)
        self._writes_since_prune += 1
        if self._writes_since_prune >= _PRUNE_EVERY_WRITES:
            self._writes_since_prune = 0
            prune_generated(self._cache_dir)
        return name

    def render_tile_bytes(self, tile) -> bytes:
        """Render a tile and return its PNG bytes (web simulator / HTTP state).

        Served from a small in-memory LRU so the per-tick full-frame render
        never touches the filesystem for tiles it has already produced."""
        name, _ = self._tile_name(tile)
        cached = self._bytes_cache.get(name)
        if cached is not None:
            self._bytes_cache.move_to_end(name)
            return cached
        name = self.render_tile(tile)
        with open(os.path.join(self._cache_dir, name), "rb") as fh:
            data = fh.read()
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
        bg = Image.new(
            "RGBA", (ICON_SIZE, ICON_SIZE), COLORS.get(tile.color, COLORS["dim"]) + (255,)
        )
        if tile.subtext:
            # Drill choice tile: big number (label) up top, small wrapped choice
            # text underneath — readable instead of a truncated "1 Yes…" line.
            d = ImageDraw.Draw(bg)
            nf = _font(58)
            nw = d.textlength(tile.label, font=nf)
            d.text(((ICON_SIZE - nw) / 2, 16), tile.label, font=nf, fill=(255, 255, 255))
            sf = _font(22)
            y = 92
            for line in _wrap(d, tile.subtext, sf, ICON_SIZE - 16, 3):
                lw = d.textlength(line, font=sf)
                d.text(((ICON_SIZE - lw) / 2, y), line, font=sf, fill=(235, 235, 240))
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
                fill=(255, 255, 255),
            )
        return bg

    def _compose_agent_tile(self, tile, spinner=None) -> Image.Image:
        accent = COLORS.get(tile.color, COLORS["dim"])
        # tile_fill: how much of the tile the status colour covers.
        #   none  -> dark background (colour lives in the word + bottom bar)
        #   tint  -> whole tile a darkened shade of the colour + bright bottom edge
        #   solid -> whole tile the full colour
        fill = getattr(tile, "tile_fill", "none")
        if fill == "solid":
            bg_col = accent
        elif fill == "tint":
            bg_col = _tint_bg(accent)
        else:
            bg_col = TILE_BG
        bg = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), bg_col + (255,))
        d = ImageDraw.Draw(bg)
        # text colours chosen for contrast against the fill (see _tile_text_colors)
        repo_fill, branch_fill, time_fill, word_fill = _tile_text_colors(fill, bg_col, accent)
        anim = getattr(tile, "working_animation", "spin")
        working = spinner is not None
        # logo top-left; while working it animates per the chosen style
        base_logo = self._base_glyph(tile.agent_type or "default")
        if fill == "solid" and _lum(bg_col) > 120 and _is_light_monochrome(base_logo):
            # A white mark washes out on bright solid fills (amber 2.1:1, cyan
            # 2.0:1 — below the 3:1 non-text minimum) while the text correctly
            # flips dark. Recolour it via its alpha mask to the same dark ink.
            # Full-colour user overrides are left as supplied (the flip would
            # flatten them to a silhouette).
            dark = Image.new("RGBA", base_logo.size, (18, 18, 22, 0))
            dark.putalpha(base_logo.getchannel("A"))
            base_logo = dark
        if working and anim == "pulse":
            # "breathe": scale the mark between ~0.82x and 1.0x by the spinner phase
            f = 0.82 + 0.18 * (0.5 + 0.5 * math.sin(2 * math.pi * spinner / SPINNER_FRAMES))
            s = max(1, round(46 * f))
            logo = base_logo.resize((s, s), Image.LANCZOS)
            off = 12 + (46 - s) // 2  # keep the smaller mark centred in its 46px box
            bg.alpha_composite(logo, (off, off))
        else:
            logo = base_logo.resize((46, 46), Image.LANCZOS)
            if working and anim == "spin":
                logo = logo.rotate(-spinner * SPIN_DEG, resample=Image.BICUBIC)
            bg.alpha_composite(logo, (12, 12))
            if working and anim == "comet":
                # thin comet ring orbiting the static mark; the 62px overlay is
                # centred over the 46px logo box at (12,12) -> composite at (4,4)
                bg.alpha_composite(self._comet_overlay(62, spinner, 2, 4), (4, 4))
        # status word + elapsed time, top-right. Sizes were tuned for a screen,
        # not a ~25mm physical key: the old 23px repo (~2.9mm cap height) and
        # 15-16px sub-labels were legible only when leaning in, while the
        # bottom ~40% of the tile sat empty.
        if tile.status_text:
            fs = _font(19)
            d.text(
                (ICON_SIZE - 12 - d.textlength(tile.status_text, font=fs), 13),
                tile.status_text,
                font=fs,
                fill=word_fill,
            )
        if tile.time_text:
            ft = _font(18)
            d.text(
                (ICON_SIZE - 12 - d.textlength(tile.time_text, font=ft), 38),
                tile.time_text,
                font=ft,
                fill=time_fill,
            )
        # repo (primary) + branch (secondary, wrapped) — spread down the tile
        # so the composition is optically centred between the logo row and the
        # accent bar instead of leaving a dead band across the bottom third.
        fr = _font(31)
        d.text(
            (12, 74),
            _truncate(d, tile.repo or "", fr, ICON_SIZE - 24),
            font=fr,
            fill=repo_fill,
        )
        if tile.branch:
            fb = _font(18)
            y = 112
            for line in _wrap(d, tile.branch, fb, ICON_SIZE - 24, 2):
                d.text((12, y), line, font=fb, fill=branch_fill)
                y += 22
        if tile.server_tag:
            chip_fill = _rgb_color(tile.server_accent or "", (95, 95, 105))
            fc = _font(14)
            tag = _truncate(d, tile.server_tag, fc, 48)
            text_w = d.textlength(tag, font=fc)
            bb = d.textbbox((0, 0), tag, font=fc)
            x, y, pad_x, chip_h = 12, ICON_SIZE - 40, 6, 22
            chip_w = int(text_w + pad_x * 2)
            d.rounded_rectangle([x, y, x + chip_w, y + chip_h], radius=4, fill=chip_fill)
            text_y = y + (chip_h - (bb[3] - bb[1])) / 2 - bb[1]
            d.text((x + pad_x, text_y), tag, font=fc, fill=(255, 255, 255))
        # bottom accent bar. "sweep" is a moving segment along the bottom edge; it
        # must stay visible on any fill, so its colours adapt — on a solid tile
        # (background already = accent) it uses a dark base + a bright segment; on
        # none/tint a dimmed base + the accent segment. The plain static bar is
        # drawn only when the fill isn't solid (on solid it would be invisible).
        y0 = ICON_SIZE - 8
        if working and anim == "sweep":
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
        elif fill != "solid":
            d.rectangle([0, y0, ICON_SIZE, ICON_SIZE], fill=accent)
        return bg
