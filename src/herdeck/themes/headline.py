"""headline — name first (built-in deck theme; agent tiles only).

The project is the headline, the time in the current status is the second
fact, and the status is the whole key's fill. The project mark sits small in
the top-left corner; branch and backend tag are gone (they shrank to ~6 px on
the D200's 72 px key). A status word appears only where it is news (blocked,
waiting's holder label, offline), plus "+N" for running subagents.

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
        img.paste(mark, (14, 14), mark)
        if tile.time_text:
            font, time = k.fit(ctx, d, tile.time_text, S - 14 - 56 - 12 - 14, 40, 28)
            d.text((S - 14 - d.textlength(time, font=font), 18), time, font=font, fill=ink)
        name_font, name = k.fit(ctx, d, k.name_line(tile), S - 28, 44, 28)
        name_y = S - 18 - name_font.size
        d.text((14, name_y), name, font=name_font, fill=ink)
        words = []
        if status in ("blocked", "waiting", "offline"):
            word = (tile.status_text or "").strip() or k.t(ctx, f"status.{status}")
            words.append(word)
        if tile.subagents:
            words.append(f"+{tile.subagents}")
        if words:
            font, sub = k.fit(ctx, d, "  ".join(words), S - 28, 24)
            d.text((14, name_y - font.size - 10), sub, font=font, fill=ink)
        return img


THEME = HeadlineTheme()
