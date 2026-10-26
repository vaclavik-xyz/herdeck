"""headline — name first (built-in deck theme; agent tiles only).

The project is the headline, the time in the current status is the second
fact, and the status is the whole key's fill. The project mark sits small in
the top-left corner; branch and backend tag are gone (they shrank to ~6 px on
the D200's 72 px key). A status word appears only where it is news (blocked,
waiting's holder label, offline), plus a dark "+N" pill for running subagents.
The mark carries a contour in the ink colour so a monogram in the status
colour stays visible on the fill.

Every other key and the status window come from ``classic`` (``base``).
"""

from __future__ import annotations

from . import _kit as k
from .api import Theme

S = k.S


class HeadlineTheme(Theme):
    id = "headline"
    name = {"en": "Headline", "cs": "Titulek"}
    base = "classic"

    def agent_tile(self, tile, ctx, frame):
        if getattr(tile, "kind", "agent") != "agent":
            return None  # an Elgato action key: the base draws it
        status = k.status_of(tile)
        fill = ctx.color(tile.color)
        ink = ctx.ink_for(fill)
        img, d = k.new_image((S, S), fill)
        mark = k.identity_mark(ctx, tile, 56, on_light=ink == (0, 0, 0))
        # a contour in the ink colour: a monogram in the status colour (green
        # on a green working fill) would otherwise vanish into the fill
        d.rounded_rectangle((10, 10, 73, 73), 16, outline=ink, width=3)
        img.paste(mark, (14, 14), mark)
        if tile.time_text:
            # "55s" / "59m" / "999h" (orchestrator._fmt_elapsed): the size gives
            # way first, so a real elapsed time is never cut
            font, time = k.fit(ctx, d, tile.time_text, S - 14 - 64 - 10 - 14, 40, 28)
            d.text((S - 14 - d.textlength(time, font=font), 18), time, font=font, fill=ink)
        name_font, name = k.fit(ctx, d, k.name_line(tile), S - 28, 44, 28)
        name_y = S - 18 - name_font.size
        d.text((14, name_y), name, font=name_font, fill=ink)
        word = ""
        if status == "waiting":
            word = k.holder_label(ctx, tile)
        elif status in ("blocked", "offline"):
            word = (tile.status_text or "").strip() or k.t(ctx, f"status.{status}")
        subs = f"+{tile.subagents}" if tile.subagents else ""
        row_y = name_y - 32 - 10
        x1 = S - 10
        if subs:
            # running subagents are a dark pill, never glued to the word: a
            # holder label carries its own "+1" ("REVIEW +1"). The word is the
            # news: when both do not fit whole, the pill gives way.
            pill_w = k.pill_width(ctx, d, subs)
            room = x1 - pill_w - 6 - 14
            if not word or k.fit(ctx, d, word, room, 24)[1] == word:
                box = k.pill(ctx, d, x1, row_y, subs, k.PILL_DARK)
                x1 = box[0] - 6
        if word:
            font, sub = k.fit(ctx, d, word, x1 - 14, 24)
            d.text((14, row_y + 3), sub, font=font, fill=ink)
        return img


THEME = HeadlineTheme()
