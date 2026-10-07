"""signal — favicon + urgency, with shape-coded status (built-in deck theme).

A dark key per agent: its identity mark (the project's favicon or monogram via
``_kit.identity_mark``; the agent's own mark only for a tile with no project
data) centred, the task title
under it (fallback workspace -> repo), and the status carried twice — by colour
AND by the frame's shape, so it reads for colour-blind users and on the D200's
~72 px key:

    working  full frame with a gap at the top-right corner (open = in motion)
    idle     thin, dimmed frame; dimmed text
    done     full frame + a dot (finished, unseen)
    waiting  dashed frame, the holder label (CI / REVIEW) top-right, its time
             top-left when both fit (the label wins)
    blocked  the whole key in the status colour, the mark on a white plate,
             the elapsed time in a pill
    offline  full frame, the mark greyed with a slash across the mark only
    unknown  thin grey frame

Time shows only on blocked and waiting; no branch, no backend tag. Running
subagents are a small "+N" pill (a tight corner pill left of the full-size
mark; on a waiting tile beside the mark when the time took the corner). With a
time/label pill in the top row (or a "+N" too wide for the corner) the mark
shrinks and moves under the pill row, so no pill covers it. Every text is >= 22 px
(``_kit.MIN_TEXT``).

The status window is drawn by ``_signal_panel``.
"""

from __future__ import annotations

from PIL import Image

from . import _kit as k
from . import _signal_panel
from .api import Theme

S = k.S
FRAME = 12  # the status frame's stroke
RADIUS = 22
MARK = 84  # the identity mark on a dark key
MARK_Y = 20
# with a pill in the top row the mark shrinks and moves under the pill row
# (pills: y 16..48), so no pill ever covers it
MARK_COMPACT = 60
MARK_Y_COMPACT = 52
# a lone "+N" (working/idle/done) keeps the full-size mark: a tight pill in
# the top-left corner, left of the mark's box (mark x0 = (S - MARK) // 2)
CORNER_X = 13
CORNER_PAD = 5
CORNER_ROOM = (S - MARK) // 2 - 3 - CORNER_X
TEXT_W = S - 2 * (FRAME + 10)  # text column inside the frame


class SignalTheme(Theme):
    id = "signal"
    name = {"en": "Signal", "cs": "Signál"}
    base = "classic"

    # --- agent tiles -------------------------------------------------------

    def agent_tile(self, tile, ctx, frame):
        if getattr(tile, "kind", "agent") != "agent":
            # an Elgato action key (repo set, kind "control"): a plain key
            return _control(tile, ctx)
        status = k.status_of(tile)
        accent = ctx.color(tile.color)
        if status == "blocked":
            return _blocked_tile(tile, ctx, accent)
        img, d = k.new_image((S, S), k.DARK)
        _status_frame(d, status, accent)
        left_pill, side_pill = _top_pills(ctx, d, tile, status)
        corner_pill = ""
        if left_pill and status != "waiting" and k.pill_width(ctx, d, left_pill, pad=CORNER_PAD) <= CORNER_ROOM:
            # a lone "+N" is a tight corner pill left of the full-size mark
            left_pill, corner_pill = "", left_pill
        # a top pill would cover the mark: the mark shrinks under the pill row
        compact = bool(left_pill or status == "waiting")
        size, top = (MARK_COMPACT, MARK_Y_COMPACT) if compact else (MARK, MARK_Y)
        mark = k.identity_mark(ctx, tile, size)
        mx = (S - size) // 2
        if status == "offline":
            mark = k.greyed(mark)
        img.paste(mark, (mx, top), mark)
        if status == "offline":
            # the slash crosses the mark only, never the text under it
            d.line((mx + 8, top + size - 8, mx + size - 8, top + 8), fill=accent, width=9)
        left = FRAME + 4
        if corner_pill:
            k.pill(ctx, d, CORNER_X, CORNER_X, corner_pill, k.EDGE, align="left", pad=CORNER_PAD)
        if left_pill:
            box = k.pill(ctx, d, left, FRAME + 4, left_pill, k.EDGE, align="left")
            left = box[2] + 6
        if side_pill:
            # beside the mark, right of it, vertically centred on it
            k.pill(ctx, d, S - FRAME - 4, top + (size - 32) // 2, side_pill, k.EDGE)
        if status == "done":
            d.ellipse((S - 48, 18, S - 20, 46), fill=accent)
        elif status == "waiting":
            _waiting_pill(ctx, d, tile, accent, left)
        fill = k.MUTE if status in ("idle", "unknown") else k.LIGHT
        _text_under_mark(ctx, d, tile, fill, top=top + size + 6, bottom=S - FRAME - 4)
        return img

    # --- every other key ---------------------------------------------------

    def key_tile(self, tile, ctx):
        kind = getattr(tile, "kind", "control")
        if kind == "agent":
            return None
        if tile.color == "launcher":  # the reserved "+ New" slot
            return _new_slot(tile, ctx)
        if kind == "empty" or tile.color == "empty":
            return Image.new("RGB", (S, S), k.EMPTY)
        if kind == "option":
            return _option(tile, ctx)
        if tile.agent_type and kind in ("launcher", "menu"):
            return _launcher_entry(tile, ctx)
        if kind == "idle_group":
            return _idle_group(tile, ctx)
        if kind == "pinned_hole":
            return _pinned_hole(tile, ctx)
        return _control(tile, ctx)  # control, menu, the New-agent menu's "Profiles"

    # --- the status window -------------------------------------------------

    def panel(self, panel, ctx, width):
        return _signal_panel.draw(panel, ctx, width)


# --- agent-tile parts -------------------------------------------------------


def _status_frame(d, status, accent) -> None:
    box = (0, 0, S - 1, S - 1)
    if status in ("idle", "unknown"):
        d.rounded_rectangle(box, RADIUS, outline=k.dim(accent), width=5)
        return
    if status == "waiting":
        k.dashed_frame(d, accent, FRAME, inset=FRAME // 2)
        return
    d.rounded_rectangle(box, RADIUS, outline=accent, width=FRAME)
    if status == "working":
        # open frame: a gap at the top-right corner (drawn before the mark)
        d.rectangle((S - 60, 0, S - 1, 60), fill=k.DARK)


def _top_pills(ctx, d, tile, status) -> tuple[str, str]:
    """(top-left pill, pill beside the mark). Top-left: the waiting time,
    else the running subagents, only when it leaves the holder label
    (top-right) whole. A waiting tile whose "+N" lost the top row shows it
    beside the (compact) mark when it fits there; else it is dropped (the
    holder label and the time matter more)."""
    subs = f"+{tile.subagents}" if tile.subagents else ""
    room = S - 2 * (FRAME + 4)
    if status != "waiting":
        return (subs if subs and k.pill_width(ctx, d, subs) <= room else ""), ""
    label_w = k.pill_width(ctx, d, _waiting_label(ctx, tile)) + 6
    left = ""
    for part in ((tile.time_text or "").strip(), subs):
        if part and k.pill_width(ctx, d, part) + label_w <= room:
            left = part
            break
    side = ""
    if subs and left != subs:
        # beside the compact mark: from its right edge (+4) to the frame
        side_room = S - FRAME - 4 - ((S + MARK_COMPACT) // 2 + 4)
        if k.pill_width(ctx, d, subs) <= side_room:
            side = subs
    return left, side


def _waiting_label(ctx, tile) -> str:
    return (tile.status_text or "").strip() or k.t(ctx, "status.waiting")


def _waiting_pill(ctx, d, tile, accent, left) -> None:
    """The holder label (CI / REVIEW +1) top-right, cut to the room left by
    the top-left pill."""
    label = _waiting_label(ctx, tile)
    room = S - FRAME - 4 - left
    k.pill(ctx, d, S - FRAME - 4, FRAME + 4, k.ellipsize(ctx, d, label, k.MIN_TEXT, room - 18), accent)


def _text_under_mark(ctx, d, tile, fill, *, top, bottom) -> None:
    font, lines = k.block(ctx, d, k.text_line(tile), TEXT_W, largest=30)
    lh = font.size + 6
    y = top + max(0, (bottom - top - lh * len(lines)) / 2)
    k.draw_lines(d, lines, font, FRAME + 10, y, fill, lh=lh, center_w=TEXT_W)


def _blocked_tile(tile, ctx, accent):
    img, d = k.new_image((S, S), accent)
    plate = 104
    px, py = (S - plate) // 2, 14
    d.rounded_rectangle((px, py, px + plate, py + plate), 24, fill=k.PLATE)
    mark = k.identity_mark(ctx, tile, 92, on_light=True)
    img.paste(mark, (px + 6, py + 6), mark)
    if tile.time_text:
        k.pill(ctx, d, S - 8, 8, tile.time_text, k.PILL_DARK)
    if tile.subagents:
        k.pill(ctx, d, 8, 8, f"+{tile.subagents}", k.PILL_DARK, align="left")
    font, lines = k.block(ctx, d, k.text_line(tile), S - 24, largest=30)
    lh = font.size + 6
    top, bottom = py + plate + 6, S - 6
    y = top + max(0, (bottom - top - lh * len(lines)) / 2)
    k.draw_lines(d, lines, font, 12, y, ctx.ink_for(accent), lh=lh, center_w=S - 24)
    return img


# --- key tiles -------------------------------------------------------------


def _outlined(ctx, tile, *, width=6):
    img, d = k.new_image((S, S), k.DARK)
    d.rounded_rectangle((0, 0, S - 1, S - 1), RADIUS, outline=ctx.color(tile.color), width=width)
    return img, d


def _centered_block(ctx, d, lines_spec, *, top=14, bottom=S - 14) -> None:
    """Vertically centre groups of (font, lines, fill) between top and bottom."""
    groups = [(f, ls, fill, f.size + 6) for f, ls, fill in lines_spec if ls]
    height = sum(lh * len(ls) for _, ls, _, lh in groups) + 6 * max(0, len(groups) - 1)
    y = top + max(0, (bottom - top - height) / 2)
    for font, lines, fill, lh in groups:
        y = k.draw_lines(d, lines, font, 16, y, fill, lh=lh, center_w=S - 32) + 6


def _control(tile, ctx):
    """Back / Stop / Pin / a management or menu entry: a dark key with a
    coloured outline."""
    img, d = _outlined(ctx, tile)
    font, lines = k.block(ctx, d, tile.label or "", S - 40, largest=34)
    spec = [(font, lines, k.LIGHT)]
    if tile.subtext:
        sfont, sub = k.fit(ctx, d, tile.subtext, S - 40, 24)
        spec.append((sfont, [sub], k.MUTE))
    _centered_block(ctx, d, spec)
    return img


def _option(tile, ctx):
    """A drill choice on its tone: the digit big, the choice text wrapped
    under it (an Approve/Deny label without text is centred instead)."""
    tone = ctx.color(tile.color)
    img, d = k.new_image((S, S), k.DARK)
    d.rounded_rectangle((0, 0, S - 1, S - 1), RADIUS, fill=tone)
    ink = ctx.ink_for(tone)
    if not tile.subtext:
        font, lines = k.block(ctx, d, tile.label or "", S - 32, largest=40, max_lines=3)
        _centered_block(ctx, d, [(font, lines, ink)])
        return img
    head_font, head = k.fit(ctx, d, tile.label or "", S - 36, 64, 30)
    d.text((18, 6), head, font=head_font, fill=ink)
    y = 6 + head_font.size + 12
    for size in (24, k.MIN_TEXT):
        font = ctx.font(size)
        lh = size + 5
        lines = ctx.wrap(d, tile.subtext, font, S - 36, int((S - 10 - y) // lh))
        if size == k.MIN_TEXT or not (lines and lines[-1].endswith("…")):
            break
    k.draw_lines(d, [k.ellipsize(ctx, d, ln, size, S - 36) for ln in lines], font, 18, y, ink, lh=lh)
    return img


def _launcher_entry(tile, ctx):
    """An agent type in the New-agent menu: its mark over its name."""
    img, d = _outlined(ctx, tile)
    mark = ctx.agent_mark(tile.agent_type, 76)
    img.paste(mark, ((S - 76) // 2, 22), mark)
    font, lines = k.block(ctx, d, tile.label or tile.agent_type, S - 40, largest=30)
    lh = font.size + 6
    y = 108 + max(0, (S - 14 - 108 - lh * len(lines)) / 2)
    k.draw_lines(d, lines, font, 20, y, k.LIGHT, lh=lh, center_w=S - 40)
    return img


def _new_slot(tile, ctx):
    """"+ New": an empty slot waiting to be filled — dashed outline, big plus."""
    img, d = k.new_image((S, S), k.DARK)
    k.dashed_frame(d, (96, 102, 112), 6, inset=10)
    d.line((S / 2, 42, S / 2, 108), fill=k.LIGHT, width=12)
    d.line((S / 2 - 33, 75, S / 2 + 33, 75), fill=k.LIGHT, width=12)
    word = (tile.label or "").lstrip("+").strip()
    if word:
        font, word = k.fit(ctx, d, word, S - 48, 28)
        d.text(((S - d.textlength(word, font=font)) / 2, 126), word, font=font, fill=k.MUTE)
    return img


def _idle_group(tile, ctx):
    """The folded idle agents ("+12" / "hide") in the idle ring language."""
    img, d = k.new_image((S, S), k.DARK)
    d.rounded_rectangle((0, 0, S - 1, S - 1), RADIUS, outline=k.dim(ctx.status_color("idle")), width=5)
    font, head = k.fit(ctx, d, tile.label or "", S - 40, 56, 28)
    spec = [(font, [head], k.LIGHT)]
    if tile.subtext:
        sfont, sub = k.fit(ctx, d, tile.subtext, S - 40, 24)
        spec.append((sfont, [sub], k.MUTE))
    _centered_block(ctx, d, spec)
    return img


def _pinned_hole(tile, ctx):
    """A pin whose agent is gone or offline: a dashed hole with a pin."""
    img, d = k.new_image((S, S), k.DARK)
    k.dashed_frame(d, k.MUTE, 5, inset=8)
    d.ellipse((S / 2 - 12, 22, S / 2 + 12, 46), outline=k.MUTE, width=5)
    d.line((S / 2, 46, S / 2, 66), fill=k.MUTE, width=5)
    label = tile.label or ""
    if " · " in label:
        # "Pinned · missing": one fact per line, never "Pinned ·" / "missing"
        parts = [p.strip() for p in label.split(" · ") if p.strip()]
        size = max(k.MIN_TEXT, min(k.fit(ctx, d, p, S - 40, 26)[0].size for p in parts))
        font = ctx.font(size)
        lines = [k.ellipsize(ctx, d, p, size, S - 40) for p in parts[:2]]
    else:
        font, lines = k.block(ctx, d, label, S - 40, largest=26)
    y = k.draw_lines(d, lines, font, 20, 76, k.LIGHT, center_w=S - 40)
    if tile.subtext:
        sfont, sub = k.fit(ctx, d, tile.subtext, S - 40, 24)
        k.draw_lines(d, [sub], sfont, 20, max(y + 4, 146), k.MUTE, center_w=S - 40)
    return img


THEME = SignalTheme()
