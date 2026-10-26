"""Frozen bundles must carry the built-in deck themes: the registry imports
them by name, which PyInstaller's static analysis cannot see."""

from __future__ import annotations

from pathlib import Path

import pytest

from herdeck.themes.registry import BUILTIN_MODULES

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("spec", ["desktop/herdeck-deckapp.spec", "streamdeck/herdeck-backend.spec"])
def test_specs_collect_every_theme_module(spec):
    text = (ROOT / spec).read_text(encoding="utf-8")
    assert "from PyInstaller.utils.hooks import collect_submodules" in text
    assert '*collect_submodules("herdeck.themes")' in text


def test_builtin_theme_modules_live_in_the_collected_package():
    assert all(module.startswith("herdeck.themes.") for module in BUILTIN_MODULES.values())
