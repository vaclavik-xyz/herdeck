"""Drawing helpers shared by the built-in themes (signal, headline, quiet).

NOT part of the theme API: user themes must not import this module (it may
change with any release). Everything here draws through the ``ThemeContext``
a theme receives, so [theme.colors] and the language are always respected.

Every text helper enforces MIN_TEXT: the D200 shows a 196 px key image on a
~72 px LCD key, so text under 22 px (in 196-space) is unreadable there.
"""

from __future__ import annotations

from types import SimpleNamespace

from PIL import Image, ImageDraw

S = 196  # a key image's edge (ThemeContext.TILE)
MIN_TEXT = 22  # smallest text size any built-in theme draws
SEAM = 196  # the Elgato key pair splits a 392 px panel here
NARROW = 420  # panels narrower than this are the two-key (392) layout

DARK = (16, 17, 20)
EMPTY = (10, 11, 13)
LIGHT = (240, 242, 246)
MUTE = (170, 176, 186)
TRACK = (44, 48, 56)
EDGE = (40, 44, 52)
PLATE = (255, 255, 255)
PILL_DARK = (24, 18, 4)

STATUSES = ("blocked", "working", "idle", "done", "waiting", "offline")


def t(ctx, key: str, **fmt) -> str:
    """``ctx.tr(key)`` formatted with ``fmt`` (a missing key renders as the
    key itself, never raises)."""
    text = ctx.tr(key)
    try:
        return text.format(**fmt) if fmt else text
    except (KeyError, IndexError, ValueError):
        return text


def status_of(tile) -> str:
    """The tile's status for drawing: a Status value or ``offline``; anything
    else (None on an Elgato pending tile, ``unknown``) is ``unknown``."""
    status = getattr(tile, "status", None) or "unknown"
    return status if status in STATUSES else "unknown"


def text_line(tile) -> str:
    """The agent tile's one text: the task title, else the workspace, else
    the repo line, else the label."""
    for value in (
        getattr(tile, "title", ""),
        getattr(tile, "workspace", ""),
        getattr(tile, "repo", ""),
        getattr(tile, "label", ""),
    ):
        if value and str(value).strip():
            return str(value).strip()
    return ""


def holder_label(ctx, tile) -> str:
    """What holds a waiting agent (CI, REVIEW +1): ``wait_reason``, empty
    when the time field is hidden, then the status word (WAITING)."""
    for value in (getattr(tile, "wait_reason", ""), getattr(tile, "status_text", "")):
        if value and str(value).strip():
            return str(value).strip()
    return t(ctx, "status.waiting")


def name_line(tile) -> str:
    """The project-first name (headline): the repo line, else the label."""
    for value in (getattr(tile, "repo", ""), getattr(tile, "label", "")):
        if value and str(value).strip():
            return str(value).strip()
    return ""


def ellipsize(ctx, draw, text: str, size: int, max_w: float, *, bold=True) -> str:
    """``text`` cut with an ellipsis to fit ``max_w`` at exactly ``size`` px."""
    return ctx.fit_text(draw, text, max_w, size, size, bold)[1]


def fit(ctx, draw, text: str, max_w: float, largest: int, smallest: int = MIN_TEXT, *, bold=True):
    """(font, text): the largest size in [smallest, largest] at which ``text``
    fits whole, else ``smallest`` with the text cut to an ellipsis. Never
    below MIN_TEXT. Delegates to ``ctx.fit_text``."""
    smallest = max(MIN_TEXT, smallest)
    return ctx.fit_text(draw, text, max_w, max(largest, smallest), smallest, bold)


def block(ctx, draw, text: str, max_w: float, *, largest: int, max_lines: int = 2, bold=True):
    """(font, lines): one line at the largest size that fits whole, else the
    text wrapped onto <= ``max_lines`` lines at 24 px, else at MIN_TEXT with
    an ellipsis on the last line."""
    if not text:
        return ctx.font(MIN_TEXT, bold=bold), []
    font, whole = ctx.fit_text(draw, text, max_w, max(largest, MIN_TEXT), MIN_TEXT, bold)
    if whole == text:
        return font, [text]
    for size in (24, MIN_TEXT):
        font = ctx.font(size, bold=bold)
        lines = ctx.wrap(draw, text, font, max_w, max_lines)
        if size == MIN_TEXT or (lines and not lines[-1].endswith("…")):
            return font, lines


def draw_lines(draw, lines, font, x, y, fill, *, lh=None, center_w=None) -> float:
    """Draw ``lines`` from (x, y) down; centred inside [x, x + center_w] when
    given. Returns the y under the last line."""
    lh = lh or font.size + 6
    for line in lines:
        dx = (center_w - draw.textlength(line, font=font)) / 2 if center_w else 0
        draw.text((x + dx, y), line, font=font, fill=fill)
        y += lh
    return y


def pill(ctx, draw, x, top, text, bg, *, size=MIN_TEXT, align="right", pad=9):
    """A rounded label; ``x`` is its right edge (align="right") or its left
    edge. Returns the pill's box."""
    font = ctx.font(max(MIN_TEXT, size))
    w = draw.textlength(text, font=font) + 2 * pad
    x0 = x - w if align == "right" else x
    box = (x0, top, x0 + w, top + font.size + 10)
    draw.rounded_rectangle(box, radius=(font.size + 10) // 2, fill=bg)
    draw.text((x0 + pad, top + 3), text, font=font, fill=ctx.ink_for(bg))
    return box


def pill_width(ctx, draw, text, *, size=MIN_TEXT, pad=9) -> float:
    return draw.textlength(text, font=ctx.font(max(MIN_TEXT, size))) + 2 * pad


def monogram_source(name: str):
    """A minimal object ``ctx.project_mark`` can draw a monogram from."""
    return SimpleNamespace(project_icon=None, project_name=name or "?", repo=name, label=name)


def _has_project(item) -> bool:
    return bool(
        getattr(item, "project_icon", None)
        or getattr(item, "project_name", "")
        or getattr(item, "repo", "")
    )


def _project_mark(ctx, item, size: int, *, dark: bool, name: str):
    """A COPY of the project mark of ``item``. With the "agent" fallback and no
    favicon the agent logo is drawn here (so ``dark`` is honoured and the
    shared logo cache is never handed out); a tile with repo/label but no
    ``project_name`` and no favicon gets a monogram of ``name``, not "?"."""
    env = getattr(ctx, "_env", None)
    fallback = getattr(env, "project_icon_fallback", "monogram")
    agent_type = getattr(item, "agent_type", "") or ""
    icon = getattr(item, "project_icon", None)
    if not icon and agent_type and fallback == "agent":
        return ctx.agent_mark(agent_type, size, dark=dark).copy()
    if not icon and not getattr(item, "project_name", ""):
        return ctx.project_mark(monogram_source(name), size).copy()
    return ctx.project_mark(item, size).copy()


def identity_mark(ctx, tile, size: int, *, on_light: bool = False):
    """The tile's identity at ``size`` px (RGBA, always a copy): the PROJECT
    mark first (favicon, monogram, or the agent logo when
    [view].project_icon_fallback = "agent"); the agent's own logo (black when
    ``on_light``: it sits on a white plate) only for a tile with no project
    data; else a monogram of its text line. Not keyed on tile.tile_icon."""
    if _has_project(tile):
        return _project_mark(ctx, tile, size, dark=on_light, name=name_line(tile))
    agent_type = getattr(tile, "agent_type", None)
    if agent_type:
        return ctx.agent_mark(agent_type, size, dark=on_light).copy()
    return ctx.project_mark(monogram_source(text_line(tile)), size).copy()


def queue_mark(ctx, agent, size: int):
    """A PanelAgent's mark on a white plate (always a copy): the project mark,
    else the agent mark in black, else a monogram of its label."""
    if _has_project(agent):
        return _project_mark(ctx, agent, size, dark=True, name=name_line(agent))
    if getattr(agent, "agent_type", ""):
        return ctx.agent_mark(agent.agent_type, size, dark=True).copy()
    return ctx.project_mark(monogram_source(getattr(agent, "label", "")), size).copy()


def greyed(img: Image.Image) -> Image.Image:
    """A COPY of a mark desaturated and dimmed (offline: the project is out of
    reach); ``img`` (often a shared cached mark) is never modified."""
    grey = img.convert("LA").convert("RGBA")
    grey.putalpha(img.getchannel("A"))
    return Image.blend(grey, Image.new("RGBA", grey.size, (40, 40, 44, 0)), 0.35)


def dim(rgb, k: float = 0.6):
    return tuple(int(c * k) for c in rgb)


def dashed_frame(draw, col, width: int, *, inset: int, size=(S, S), dashes: int = 6) -> None:
    """Dashes along the four straight sides of a key-sized box (the corners
    stay open)."""
    w, h = size
    left, top, right, bottom = inset, inset, w - 1 - inset, h - 1 - inset
    for lo, hi, horizontal in ((left, right, True), (top, bottom, False)):
        span = hi - lo - 40
        step = span / dashes
        for i in range(dashes):
            a = lo + 20 + i * step
            b = a + step * 0.55
            if horizontal:
                draw.line(((a, top), (b, top)), fill=col, width=width)
                draw.line(((a, bottom), (b, bottom)), fill=col, width=width)
            else:
                draw.line(((left, a), (left, b)), fill=col, width=width)
                draw.line(((right, a), (right, b)), fill=col, width=width)


def offline_glyph(draw, cx, cy, r, ink, width) -> None:
    """A ring with a slash (the offline sign)."""
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=ink, width=width)
    k = r * 0.7
    draw.line((cx - k, cy + k, cx + k, cy - k), fill=ink, width=width)


def check_glyph(draw, cx, cy, r, ink, width) -> None:
    draw.line(
        (cx - r * 0.55, cy + r * 0.05, cx - r * 0.15, cy + r * 0.45, cx + r * 0.6, cy - r * 0.4),
        fill=ink,
        width=width,
        joint="curve",
    )


def new_image(size, fill) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", size, fill)
    return img, ImageDraw.Draw(img)
