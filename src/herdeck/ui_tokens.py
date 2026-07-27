"""Shared visual tokens for herdeck's non-desktop web surfaces.

The desktop window owns the canonical token layer in
``desktop/src/lib/theme.css``. The web simulator is served from Python and
cannot import that file, so the shared subset is mirrored here and
``tests/test_ui_tokens.py`` fails if the two drift apart.

Only the tokens a Python-served surface actually needs live here — the
desktop-only ones (field label widths, form measures, sidebar) stay in
``theme.css``.

Status colours are NOT duplicated: they are derived from
:data:`herdeck.driver.base.COLORS`, which is already the single source of
truth for what green means on a key.
"""

from __future__ import annotations

import math

from .driver.base import COLORS

# Surfaces, text, accent, type, radius and motion — values are byte-identical
# to theme.css (enforced by tests/test_ui_tokens.py).
SHARED_TOKENS: dict[str, str] = {
    "--canvas": "#0a0c10",
    "--panel": "#12161c",
    "--panel-raised": "#171c24",
    "--key": "#1e242d",
    "--field": "#0e1218",
    "--line": "#232a34",
    "--line-strong": "#303945",
    "--text": "#e9edf2",
    "--text-dim": "#98a2af",
    "--text-faint": "#6b7583",
    "--accent": "#5b93f5",
    "--accent-strong": "#7aa9ff",
    "--accent-soft": "rgb(91 147 245 / .14)",
    "--accent-ring": "rgb(91 147 245 / .38)",
    "--font-ui": '-apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif',
    "--font-mono": '"SF Mono", ui-monospace, SFMono-Regular, monospace',
    "--r-control": "7px",
    "--r-panel": "12px",
    "--r-stage": "16px",
    "--dur": "120ms",
    "--ease": "cubic-bezier(.2, .6, .3, 1)",
}

# Semantic status aliases, mirroring theme.css. Every value must name a key of
# COLORS so the alias resolves to a real palette token.
STATUS_ALIASES: dict[str, str] = {
    "working": "green",
    "idle": "blue",
    "blocked": "amber",
    "done": "cyan",
    "waiting": "violet",
    "offline": "red",
    "unknown": "grey",
}


# theme.css writes the readable error copy as this mix; the desktop runs in a
# bundled WKWebView where color-mix() is guaranteed. THIS module serves arbitrary
# phone and tablet browsers over Tailscale, so it emits the RESOLVED literal
# instead: a declaration containing var() is only validated after substitution,
# and an unsupported color-mix() then makes the property unset rather than
# falling back to an earlier declaration — an invisible error banner.
OFFLINE_TEXT_MIX_RATIO = 0.55


def _rgb(value: tuple[int, int, int]) -> str:
    """Render a backend COLORS triple the way theme.css writes it."""
    r, g, b = value
    return f"rgb({r},{g},{b})"


def _hex(triple: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{int(math.floor(c + 0.5)):02x}" for c in triple)


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], ratio: float) -> str:
    """`color-mix(in srgb, a <ratio>%, b)` resolved to a literal.

    CSS rounds half away from zero; :func:`_hex` does the same, so the emitted
    literal matches what a browser would compute.
    """
    return _hex(tuple(ca * ratio + cb * (1 - ratio) for ca, cb in zip(a, b, strict=True)))


def css_variables(selector: str = ":root") -> str:
    """The shared tokens as a CSS custom-property block.

    Emits the palette straight from ``COLORS`` (including ``empty``, which the
    desktop picker deliberately omits but a deck surface may still render), the
    semantic aliases, and the readable error-text derivative.
    """
    lines = [f"{selector}{{color-scheme:dark;"]
    for name, value in SHARED_TOKENS.items():
        lines.append(f"{name}:{value};")
    for name, triple in COLORS.items():
        lines.append(f"--st-{name}:{_rgb(triple)};")
    for alias, palette_name in STATUS_ALIASES.items():
        lines.append(f"--st-{alias}:var(--st-{palette_name});")
    # Derived tones, emitted RESOLVED (see the note above OFFLINE_TEXT_MIX_RATIO):
    # the palette is tuned for LED keys and falls under WCAG AA as dark-UI text,
    # so error copy uses a lightened derivative while fills and dots keep the raw
    # colour.
    for name, value in derived_tones().items():
        lines.append(f"{name}:{value};")
    lines.append("}")
    return "".join(lines)


def _triple(hex_value: str) -> tuple[int, int, int]:
    """A `#rrggbb` token value as an rgb triple."""
    h = hex_value.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def derived_tones() -> dict[str, str]:
    """Tones the served page needs as literals.

    ``--st-offline-text`` mirrors the same-named token in theme.css (written
    there as a `color-mix()`, checked by tests/test_ui_tokens.py). The other
    three are simulator-only tones that the page previously wrote inline as
    `color-mix()` and that have no counterpart in theme.css.
    """
    white = (255, 255, 255)
    canvas = _triple(SHARED_TOKENS["--canvas"])
    # follow the alias, not a hardcoded palette entry: repointing
    # STATUS_ALIASES["offline"] must move the banner with the dots.
    offline = COLORS[STATUS_ALIASES["offline"]]
    return {
        "--st-offline-text": _mix(offline, white, OFFLINE_TEXT_MIX_RATIO),
        # the error banner's wash, and the terminal overlay's backdrop
        "--tint-offline": _mix(offline, canvas, 0.14),
        "--overlay": "rgb({} {} {} / .94)".format(*canvas),
        "--overlay-shadow": "rgb({} {} {} / .8)".format(*canvas),
    }


def xterm_theme() -> dict[str, str]:
    """Terminal colours for the simulator's xterm.js preview.

    xterm paints to a canvas and cannot read CSS custom properties, so it needs
    literals. Deriving them here keeps the terminal in step with the tokens
    instead of leaving a hand-copied palette to drift.
    """
    canvas = SHARED_TOKENS["--canvas"]
    return {
        "background": canvas,
        "foreground": SHARED_TOKENS["--text"],
        "cursor": SHARED_TOKENS["--text-dim"],
        # bespoke: a selection wash has no token of its own.
        "selectionBackground": "#25405f",
        "black": canvas,
        "brightBlack": SHARED_TOKENS["--text-faint"],
        "blue": SHARED_TOKENS["--accent"],
        "brightBlue": SHARED_TOKENS["--accent-strong"],
        # the readable error red — the resolved value of --st-offline-text
        "red": derived_tones()["--st-offline-text"],
    }
