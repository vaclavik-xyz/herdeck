from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable

from PIL import Image, ImageDraw

from .driver.base import COLORS, PanelView

ICON_SIZE = 196
PANEL_W, PANEL_H = 392, 196      # the D200 status window spans two 196px cells
# The spinner has this many distinct frames; keep the cache bounded so a
# long-running working tile reuses frames instead of writing forever.
SPINNER_FRAMES = 8
# Bump when the rendered icon output changes so stale cached PNGs from older
# versions are ignored, not reused.
CACHE_VERSION = 3

# The agent mark is inset (not edge-to-edge) so the comet ring has clean room
# around it and tiles look deliberate rather than cramped.
LOGO_SCALE = 0.62
# Comet working-ring geometry, in ICON_SIZE pixels.
RING_INSET = 12
RING_WIDTH = 7
RING_SPAN = 150          # degrees of comet tail
_SS = 4                  # supersample factor for an anti-aliased ring

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

# agent type -> Simple Icons slug (None => generated glyph fallback)
DEFAULT_AGENT_SLUGS: dict[str, str | None] = {
    "claude": "claude",
    "codex": None,            # no Simple Icons entry -> glyph
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
TILE_VERSION = 1
TILE_BG = (26, 26, 30)           # dark agent-tile background
SPIN_DEG = 360 / SPINNER_FRAMES  # degrees per rotation phase
SERVER_CHIP_COLORS: dict[str, tuple[int, int, int]] = {
    "teal": (24, 150, 145),
    "violet": (135, 100, 235),
    "orange": (220, 115, 35),
    "pink": (215, 80, 135),
    "lime": (125, 175, 45),
}


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


def compose_panel(panel: PanelView) -> Image.Image:
    """Render a PanelView to a 392x196 image with large, readable text.

    Shared by the D200 driver (split into two cells) and the web simulator.
    """
    bg = (40, 30, 12) if panel.color == "amber" else (30, 30, 34)
    img = Image.new("RGB", (PANEL_W, PANEL_H), bg)
    d = ImageDraw.Draw(img)
    title_f = _font(30)
    d.text((16, 12), _truncate(d, panel.title, title_f, PANEL_W - 32),
           font=title_f, fill=(255, 255, 255))
    line_f = _font(24)
    y = 60
    for line in panel.lines[:3]:
        d.text((16, y), _truncate(d, line, line_f, PANEL_W - 32),
               font=line_f, fill=(232, 232, 236))
        y += 40
    return img


def _truncate(draw, text, font, max_w):
    if not text or draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def _wrap(draw, text, font, max_w, max_lines=2):
    """Wrap text (splitting on '/' too, for branch names) to <= max_lines."""
    words = text.replace("/", " / ").split()
    lines: list[str] = []
    cur = ""
    for w in words:
        test = (cur + " " + w).strip()
        if draw.textlength(test, font=font) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
        if len(lines) == max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if lines:
        lines[-1] = _truncate(draw, lines[-1], font, max_w)
    return lines[:max_lines]


class IconProvider:
    """Resolves agent type -> composited tile-icon PNG in the strmdck icon dir.

    Precedence: user override PNG > Simple Icons (fetch+rasterize) > generated glyph.
    Output PNGs land in cache_dir (which must be the strmdck
    .cache/icons/_generated dir) and are referenced by bare filename.
    """

    def __init__(self, cache_dir: str, slug_map: dict[str, str | None],
                 overrides_dir: str | None = None,
                 fetch: Callable[[str], str | None] = _default_fetch,
                 rasterize: Callable[[str, int], Image.Image] = _default_rasterize,
                 assets_dir: str | None = _ASSETS_DIR):
        self._cache_dir = cache_dir
        self._slug_map = slug_map
        self._overrides_dir = overrides_dir
        self._fetch = fetch
        self._rasterize = rasterize
        self._assets_dir = assets_dir
        os.makedirs(cache_dir, exist_ok=True)
        self._glyph_cache: dict[str, Image.Image] = {}

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
                    img = None      # e.g. cairosvg missing -> fall back to a letter
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
        d.text(((ICON_SIZE - w) // 2 - bbox[0], (ICON_SIZE - h) // 2 - bbox[1]),
               ch, fill=(255, 255, 255, 255), **kw)
        return img

    def icon_for(self, agent_type: str, color: str, spinner: int | None = None) -> str:
        """Return a cached PNG filename (in cache_dir) of the mark on a status bg."""
        if spinner is not None:
            spinner %= SPINNER_FRAMES   # bound the cache to a fixed frame set
        key = f"{_safe_name(agent_type)}_{color}" + (
            f"_s{spinner}" if spinner is not None else "")
        name = f"icon_v{CACHE_VERSION}_{key}.png"
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

    def _draw_spinner(self, img: Image.Image, phase: int) -> None:
        """Composite an anti-aliased comet ring: a bright head with a fading tail
        sweeping around the tile, drawn supersampled then downscaled."""
        z = ICON_SIZE * _SS
        ov = Image.new("RGBA", (z, z), (0, 0, 0, 0))
        d = ImageDraw.Draw(ov)
        inset, w = RING_INSET * _SS, RING_WIDTH * _SS
        box = [inset, inset, z - inset, z - inset]
        head = phase * (360 / SPINNER_FRAMES)
        step = 4
        for i in range(0, RING_SPAN, step):
            alpha = int(235 * (1 - i / RING_SPAN))
            d.arc(box, head - i - step, head - i,
                  fill=(255, 255, 255, alpha), width=w)
        img.alpha_composite(ov.resize((ICON_SIZE, ICON_SIZE), Image.LANCZOS))

    # --- rich tile rendering (full tile incl. text; device label left empty) ---
    def render_tile(self, tile) -> str:
        """Render a full TileView (logo, repo, branch, status, time) to a cached
        PNG and return its filename. Agent tiles (tile.repo set) get the rich
        layout; control tiles render their centred label on a colour."""
        import hashlib
        # Bound the rotation phase so the cache reuses a fixed set of frames
        # instead of writing a new PNG on every tick.
        spinner = None if tile.spinner is None else tile.spinner % SPINNER_FRAMES
        sig_parts = [
            TILE_VERSION, tile.color, tile.label, tile.agent_type, spinner,
            tile.repo, tile.branch, tile.status_text, tile.time_text,
        ]
        if tile.server_tag or tile.server_accent:
            sig_parts.extend([tile.server_tag, tile.server_accent])
        sig = "|".join(str(x) for x in sig_parts)
        name = "tile_" + hashlib.sha1(sig.encode()).hexdigest()[:16] + ".png"
        path = os.path.join(self._cache_dir, name)
        if os.path.exists(path):
            return name
        img = (self._compose_agent_tile(tile, spinner) if tile.repo is not None
               else self._compose_label_tile(tile))
        img.convert("RGB").save(path)
        return name

    def render_tile_bytes(self, tile) -> bytes:
        """Render a tile and return its PNG bytes (for the web simulator)."""
        name = self.render_tile(tile)
        with open(os.path.join(self._cache_dir, name), "rb") as fh:
            return fh.read()

    def _compose_label_tile(self, tile) -> Image.Image:
        bg = Image.new("RGBA", (ICON_SIZE, ICON_SIZE),
                       COLORS.get(tile.color, COLORS["dim"]) + (255,))
        if tile.label:
            d = ImageDraw.Draw(bg)
            f = _font(28)
            t = _truncate(d, tile.label, f, ICON_SIZE - 16)
            w = d.textlength(t, font=f)
            bb = d.textbbox((0, 0), t, font=f)
            d.text(((ICON_SIZE - w) / 2, (ICON_SIZE - (bb[3] - bb[1])) / 2 - bb[1]),
                   t, font=f, fill=(255, 255, 255))
        return bg

    def _compose_agent_tile(self, tile, spinner=None) -> Image.Image:
        accent = COLORS.get(tile.color, COLORS["dim"])
        bg = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), TILE_BG + (255,))
        d = ImageDraw.Draw(bg)
        # logo top-left, rotated by the spinner phase while working
        logo = self._base_glyph(tile.agent_type or "default").resize((46, 46), Image.LANCZOS)
        if spinner is not None:
            logo = logo.rotate(-spinner * SPIN_DEG, resample=Image.BICUBIC)
        bg.alpha_composite(logo, (12, 12))
        # status word + elapsed time, top-right
        if tile.status_text:
            fs = _font(16)
            d.text((ICON_SIZE - 12 - d.textlength(tile.status_text, font=fs), 13),
                   tile.status_text, font=fs, fill=accent)
        if tile.time_text:
            ft = _font(15)
            d.text((ICON_SIZE - 12 - d.textlength(tile.time_text, font=ft), 35),
                   tile.time_text, font=ft, fill=(165, 165, 170))
        # repo (primary) + branch (secondary, wrapped)
        fr = _font(23)
        d.text((12, 68), _truncate(d, tile.repo or "", fr, ICON_SIZE - 24),
               font=fr, fill=(255, 255, 255))
        if tile.branch:
            fb = _font(16)
            y = 98
            for line in _wrap(d, tile.branch, fb, ICON_SIZE - 24, 2):
                d.text((12, y), line, font=fb, fill=(180, 180, 188))
                y += 20
        if tile.server_tag:
            chip_fill = SERVER_CHIP_COLORS.get(
                tile.server_accent or "", (95, 95, 105))
            fc = _font(14)
            tag = _truncate(d, tile.server_tag, fc, 48)
            text_w = d.textlength(tag, font=fc)
            bb = d.textbbox((0, 0), tag, font=fc)
            x, y, pad_x, chip_h = 12, ICON_SIZE - 40, 6, 22
            chip_w = int(text_w + pad_x * 2)
            d.rounded_rectangle(
                [x, y, x + chip_w, y + chip_h], radius=4, fill=chip_fill)
            text_y = y + (chip_h - (bb[3] - bb[1])) / 2 - bb[1]
            d.text((x + pad_x, text_y), tag, font=fc, fill=(255, 255, 255))
        d.rectangle([0, ICON_SIZE - 8, ICON_SIZE, ICON_SIZE], fill=accent)  # accent
        return bg
