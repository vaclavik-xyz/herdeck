"""[view].deck_theme: shape-validated at parse time, resolved at render time."""

from __future__ import annotations

import pytest

from herdeck.config import ConfigError, ViewConfig
from herdeck.settings import _view_config


def test_deck_theme_defaults_to_classic():
    assert ViewConfig().deck_theme == "classic"
    assert _view_config({}).deck_theme == "classic"


@pytest.mark.parametrize("theme", ["classic", "signal", "my_theme-2"])
def test_deck_theme_accepts_any_well_formed_id(theme):
    # user themes are discovered at render time: an id not known yet is fine
    assert _view_config({"deck_theme": theme}).deck_theme == theme


@pytest.mark.parametrize("theme", ["", "Signal", "../evil", "a b", "x.py", 3, None])
def test_deck_theme_rejects_a_malformed_id(theme):
    with pytest.raises(ConfigError, match="view.deck_theme"):
        _view_config({"deck_theme": theme})


def test_example_config_documents_deck_theme():
    import tomllib
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "config.example.toml"
    view = tomllib.loads(path.read_text(encoding="utf-8"))["view"]
    assert view["deck_theme"] == "classic"
    assert _view_config(view).deck_theme == "classic"
