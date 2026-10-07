"""quiet — calm unless it needs you (built-in deck theme; agent tiles only).

Only a blocked agent is loud: the whole key in the status colour with a huge
wait time. A done agent is an outlined "unseen" key; offline is a dark red key;
everything else (working, idle, waiting) is dark and quiet with a colour edge
on the left — the deck's hierarchy is the design. The text is the task title
(fallback workspace -> repo), like ``signal``. The status word (DONE,
OFFLINE, the holder label) and the running subagents' "+N" stack beside the
mark; a word too wide for that gets its own row under the mark.

Every other key and the status window come from ``signal`` (``base``).
"""

from __future__ import annotations

from . import _kit as k
from .api import Theme

S = k.S
OFFLINE_BG = (70, 22, 22)
OFFLINE_INK = (255, 140, 140)
MARK = 44
MARK_X, MARK_Y = 30, 28
# the room beside the mark: "OFFLINE" / "HOTOVO" fit at >= 22 px, inside the
# done outline (its inner edge is at S - 13) with a little air
BESIDE_X0, BESIDE_X1 = MARK_X + MARK + 6, S - 16


class QuietTheme(Theme):
    id = "quiet"
    name = {"en": "Quiet", "cs": "Klid"}
    base = "signal"

    def agent_tile(self, tile, ctx, frame):
        if getattr(tile, "kind", "agent") != "agent":
            return None  # an Elgato action key: the base draws it
        status = k.status_of(tile)
        accent = ctx.color(tile.color)
        if status == "blocked":
            return _loud(tile, ctx, accent)
        img, d = k.new_image((S, S), OFFLINE_BG if status == "offline" else (20, 22, 26))
        if status == "done":
            d.rounded_rectangle((4, 4, S - 5, S - 5), 14, outline=accent, width=9)
        elif status != "offline":
            d.rectangle((0, 0, 12, S), fill=accent)
        mark = k.identity_mark(ctx, tile, MARK)
        if status == "offline":
            mark = k.greyed(mark)
        img.paste(mark, (MARK_X, MARK_Y), mark)
        word = {
            "done": k.t(ctx, "status.done"),
            "offline": k.t(ctx, "status.offline"),
            "waiting": (tile.status_text or "").strip() or k.t(ctx, "status.waiting"),
        }.get(status)
        fill = OFFLINE_INK if status == "offline" else ctx.readable(accent, (20, 22, 26))
        # beside the mark: two rows (the status word, then "+N"); a word too
        # wide for them even at MIN_TEXT ("V POZADÍ") gets its own row under it
        rows = [MARK_Y - 2, MARK_Y + 28]
        if word:
            font, beside = k.fit(ctx, d, word, BESIDE_X1 - BESIDE_X0, 26)
            if beside == word:
                d.text((BESIDE_X0, rows.pop(0)), word, font=font, fill=fill)
            else:
                font, word = k.fit(ctx, d, word, S - 44, 24)
                d.text((MARK_X, 84), word, font=font, fill=fill)
        if tile.subagents:
            subs = f"+{tile.subagents}"
            font, shown = k.fit(ctx, d, subs, BESIDE_X1 - BESIDE_X0, 26)
            if shown == subs:  # never a cut count
                d.text((BESIDE_X0, rows[0]), subs, font=font, fill=fill)
        light = (236, 238, 242) if status in ("done", "waiting", "offline") else (190, 196, 206)
        font, lines = k.block(ctx, d, k.text_line(tile), S - 48, largest=34)
        lh = font.size + 6
        k.draw_lines(d, lines, font, 30, S - 22 - lh * len(lines), light, lh=lh)
        return img


def _loud(tile, ctx, accent):
    img, d = k.new_image((S, S), accent)
    ink = ctx.ink_for(accent)
    if tile.time_text:
        font, time = k.fit(ctx, d, tile.time_text, S - 28, 64, 34)
        d.text((14, 4), time, font=font, fill=ink)
    font, lines = k.block(ctx, d, k.text_line(tile), S - 28, largest=36)
    lh = font.size + 6
    top = S - 16 - lh * len(lines)
    k.draw_lines(d, lines, font, 14, top, ink, lh=lh)
    mark = k.identity_mark(ctx, tile, 44, on_light=ink == (0, 0, 0))
    img.paste(mark, (S - 58, int(top) - 52), mark)
    if tile.subagents:
        k.pill(ctx, d, 14, int(top) - 46, f"+{tile.subagents}", k.PILL_DARK, align="left")
    return img


THEME = QuietTheme()
