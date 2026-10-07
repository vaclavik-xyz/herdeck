"""A minimal herdeck deck theme — the example docs/themes.md walks through.

Install: copy this file to ~/.config/herdeck/themes/minimal.py (the folder next
to your config.toml) and set  [view] deck_theme = "minimal".

It redraws only agent tiles (status colour fill, project mark, task title) and
returns None everywhere else, so its base (classic) draws the other keys and
the status window. tests/test_theme_registry.py loads this exact file.
"""

from PIL import Image, ImageDraw

from herdeck.themes.api import Theme


class Minimal(Theme):
    api_version = 1
    id = "minimal"  # must match the file name
    name = {"en": "Minimal", "cs": "Minimální"}
    base = "classic"

    def agent_tile(self, tile, ctx, frame):
        if tile.kind != "agent":
            return None  # e.g. an Elgato action key: let the base draw it
        bg = ctx.status_color(tile.status)
        img = Image.new("RGB", (ctx.TILE, ctx.TILE), bg)
        draw = ImageDraw.Draw(img)
        ink = ctx.ink_for(bg)
        mark = ctx.project_mark(tile, 56)
        img.paste(mark, (14, 14), mark)
        text = tile.title or tile.workspace or tile.repo or tile.label
        font = ctx.font(26)
        y = 86
        for line in ctx.wrap(draw, text, font, ctx.TILE - 28, max_lines=3):
            draw.text((14, y), line, font=font, fill=ink)
            y += 32
        return img


THEME = Minimal()
