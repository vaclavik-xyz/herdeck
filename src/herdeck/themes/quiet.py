"""quiet — calm unless it needs you (built-in deck theme; agent tiles only).

Only a blocked agent is loud: the whole key in the status colour with a huge
wait time. A done agent is an outlined "unseen" key; offline is a dark red key;
everything else (working, idle, waiting) is dark and quiet with a colour edge
on the left — the deck's hierarchy is the design. The text is the task title
(fallback workspace -> repo), like ``signal``.

Every other key and the status window come from ``signal`` (``base``).
"""

from __future__ import annotations

from . import _kit as k
from .api import Theme

S = k.S
OFFLINE_BG = (70, 22, 22)
OFFLINE_INK = (255, 140, 140)


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
        mark = k.identity_mark(ctx, tile, 52)
        if status == "offline":
            mark = k.greyed(mark)
        img.paste(mark, (30, 26), mark)
        word = {
            "done": k.t(ctx, "status.done"),
            "offline": k.t(ctx, "status.offline"),
            "waiting": (tile.status_text or "").strip() or k.t(ctx, "status.waiting"),
        }.get(status)
        if tile.subagents and status in ("working", "idle", "unknown"):
            word = f"+{tile.subagents}"
        if word:
            fill = OFFLINE_INK if status == "offline" else ctx.readable(accent, (20, 22, 26))
            font, top_right = k.fit(ctx, d, word, S - 18 - 92, 26)
            if top_right == word:
                d.text((S - 18 - d.textlength(word, font=font), 40), word, font=font, fill=fill)
            else:  # too wide beside the mark ("V POZADÍ"): its own row under it
                font, word = k.fit(ctx, d, word, S - 44, 24)
                d.text((30, 84), word, font=font, fill=fill)
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
