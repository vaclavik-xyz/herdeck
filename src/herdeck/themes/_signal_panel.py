"""signal's status window. Draws no panel kind yet: every panel falls back to
the base theme."""

from __future__ import annotations


def draw(panel, ctx, width):
    """The panel image, or None for a kind signal does not draw (the base
    theme draws it)."""
    kind = getattr(panel, "kind", "")
    if panel.sent and kind in ("calm", "spotlight"):
        kind = "sent"  # the answer acknowledgement wins while it shows
    fn = PANELS.get(kind)
    return fn(panel, ctx, width) if fn else None


PANELS: dict = {}
