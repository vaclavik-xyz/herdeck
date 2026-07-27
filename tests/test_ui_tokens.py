"""The Python-served surfaces must look like the desktop window.

`desktop/src/lib/theme.css` is the canonical token layer; `herdeck.ui_tokens`
mirrors the subset the web simulator needs. These tests fail on drift, the same
way `desktop/src/lib/theme.test.ts` fails when theme.css drifts from the
backend palette.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from herdeck.driver.base import COLORS
from herdeck.ui_tokens import SHARED_TOKENS, STATUS_ALIASES, css_variables

THEME_CSS = Path(__file__).resolve().parents[1] / "desktop" / "src" / "lib" / "theme.css"


def _tokens(css: str) -> dict[str, str]:
    return {
        name: value.strip()
        for name, value in re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", css)
    }


@pytest.fixture(scope="module")
def theme() -> dict[str, str]:
    if not THEME_CSS.exists():  # pragma: no cover - only in a source checkout
        pytest.skip("desktop/ not present in this checkout")
    return _tokens(THEME_CSS.read_text(encoding="utf-8"))


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def test_shared_tokens_match_the_desktop_theme(theme: dict[str, str]) -> None:
    for name, value in SHARED_TOKENS.items():
        assert name in theme, f"{name} missing from theme.css"
        assert _norm(theme[name]) == _norm(value), f"{name} drifted from theme.css"


def test_status_aliases_match_the_desktop_theme(theme: dict[str, str]) -> None:
    for alias, palette_name in STATUS_ALIASES.items():
        assert theme.get(f"--st-{alias}") == f"var(--st-{palette_name})"


def test_emitted_palette_matches_the_backend(theme: dict[str, str]) -> None:
    emitted = _tokens(css_variables())
    for name, (r, g, b) in COLORS.items():
        assert emitted[f"--st-{name}"] == f"rgb({r},{g},{b})"
        # theme.css deliberately omits "empty" (a vacant-slot colour, not an
        # assignable agent status) — every other entry must agree.
        if name != "empty":
            assert _norm(theme[f"--st-{name}"]) == f"rgb({r},{g},{b})"


ELGATO_PI = (
    Path(__file__).resolve().parents[1]
    / "streamdeck"
    / "xyz.vaclavik.herdeck.sdPlugin"
    / "ui"
    / "herdeck.html"
)


def test_elgato_property_inspector_tokens_match_the_desktop_theme(
    theme: dict[str, str],
) -> None:
    """The plugin's settings panel is a static file with no build step, so its
    tokens are copied. Any name it shares with the theme must carry the same
    value; `--host-surface` is deliberately Elgato's grey and has no counterpart.
    """
    if not ELGATO_PI.exists():  # pragma: no cover - only in a source checkout
        pytest.skip("streamdeck plugin not present in this checkout")
    declared = _tokens(ELGATO_PI.read_text(encoding="utf-8"))
    assert declared, "the property inspector declares no tokens"
    shared = {name: value for name, value in declared.items() if name != "--host-surface"}
    assert shared, "the property inspector shares no tokens with the theme"
    for name, value in shared.items():
        assert name in theme, f"{name} is not a theme token"
        assert _norm(theme[name]) == _norm(value), f"{name} drifted from theme.css"


def test_css_variables_is_a_usable_block() -> None:
    css = css_variables()
    assert css.startswith(":root{")
    assert css.endswith("}")
    assert "--st-offline-text:" in css
    assert css_variables("#deck").startswith("#deck{")
