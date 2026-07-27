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
from herdeck.ui_tokens import (
    OFFLINE_TEXT_MIX_RATIO,
    SHARED_TOKENS,
    STATUS_ALIASES,
    _mix,
    css_variables,
    derived_tones,
    xterm_theme,
)

THEME_CSS = Path(__file__).resolve().parents[1] / "desktop" / "src" / "lib" / "theme.css"
ELGATO_PI = (
    Path(__file__).resolve().parents[1]
    / "streamdeck"
    / "xyz.vaclavik.herdeck.sdPlugin"
    / "ui"
    / "herdeck.html"
)


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


def test_derived_error_text_resolves_to_the_desktop_theme_value() -> None:
    """theme.css writes this tone as a `color-mix()`; the Python surfaces emit
    the RESOLVED literal (they are served to browsers that may lack color-mix).
    The two forms must still name the same colour, so resolve theme.css's
    expression and compare — this also catches a ratio change on either side.
    """
    theme_css = THEME_CSS.read_text(encoding="utf-8")
    match = re.search(
        r"--st-offline-text:\s*color-mix\(in srgb, var\((--st-[a-z]+)\) (\d+)%, (\w+)\)",
        theme_css,
    )
    assert match, "theme.css no longer derives --st-offline-text from a color-mix"
    alias, percent, second = match.group(1), int(match.group(2)), match.group(3)
    assert alias == "--st-offline", "the derivation changed its base colour"
    assert second == "white", "the derivation changed what it mixes with"
    assert percent / 100 == OFFLINE_TEXT_MIX_RATIO, "the mix ratio drifted from ui_tokens"
    assert derived_tones()["--st-offline-text"] == _mix(
        COLORS["red"], (255, 255, 255), percent / 100
    )


def test_every_token_the_simulator_references_is_emitted() -> None:
    """The mirror ⊆ theme direction is not enough: dropping a token from
    SHARED_TOKENS to satisfy that test would leave `var(--key)` in the page
    resolving to nothing. Assert the consumer side too."""
    from herdeck.driver import web

    emitted = set(_tokens(css_variables()))
    # layout variables the page declares on #deck itself, not theme tokens
    page_local = {"--cols", "--gap", "--pad", "--cell"}
    # scan the MODULE, not just _PAGE: the 403 page is a second inline surface
    # that also injects css_variables()
    source = Path(web.__file__).read_text(encoding="utf-8")
    referenced = set(re.findall(r"var\((--[a-z0-9-]+)", source))
    missing = referenced - emitted - page_local
    assert not missing, f"the simulator references tokens nobody emits: {sorted(missing)}"


def test_every_token_the_plugin_panel_references_is_declared() -> None:
    if not ELGATO_PI.exists():  # pragma: no cover - only in a source checkout
        pytest.skip("streamdeck plugin not present in this checkout")
    html = ELGATO_PI.read_text(encoding="utf-8")
    declared = set(_tokens(html))
    referenced = set(re.findall(r"var\((--[a-z0-9-]+)", html))
    missing = referenced - declared
    assert not missing, f"the panel references undeclared tokens: {sorted(missing)}"


def test_xterm_theme_is_derived_from_the_tokens() -> None:
    theme_colors = xterm_theme()
    assert theme_colors["background"] == SHARED_TOKENS["--canvas"]
    assert theme_colors["foreground"] == SHARED_TOKENS["--text"]
    assert theme_colors["brightBlack"] == SHARED_TOKENS["--text-faint"]
    assert theme_colors["blue"] == SHARED_TOKENS["--accent"]
    # the readable error red is the resolved value of --st-offline-text
    assert theme_colors["red"] == derived_tones()["--st-offline-text"]
    assert all(v.startswith("#") for v in theme_colors.values()), "xterm needs literals"


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
