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


def _rgb(value: tuple[int, int, int]) -> str:
    """Render a backend COLORS triple the way theme.css writes it."""
    r, g, b = value
    return f"rgb({r},{g},{b})"


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
    # The palette is tuned for LED keys; on dark UI text it falls under WCAG AA,
    # so error COPY uses a lightened derivative (fills and dots keep the raw
    # colour). Same derivation as theme.css.
    lines.append("--st-offline-text:color-mix(in srgb, var(--st-offline) 55%, white);")
    lines.append("}")
    return "".join(lines)
