"""headline (variant A): name first — agent tiles only; every other key and
the status window come from its base, ``classic``."""

from __future__ import annotations

import pytest
from theme_support import (
    LANGS,
    STATUSES,
    TextLog,
    agent,
    agent_variants,
    assert_text_sane,
    key,
    key_variants,
    make_ctx,
    make_provider,
    panel_variants,
)

from herdeck.driver.base import COLORS
from herdeck.themes import headline
from herdeck.themes._kit import S


@pytest.fixture
def provider(tmp_path):
    return make_provider(tmp_path)


@pytest.fixture
def log(monkeypatch):
    return TextLog().install(monkeypatch)


def texts(log) -> list[str]:
    return [t for _, t, _ in log.take()]


def test_identity():
    assert (headline.THEME.id, headline.THEME.base) == ("headline", "classic")
    assert headline.THEME.name == {"en": "Headline", "cs": "Titulek"}


def test_only_agent_tiles_are_drawn(provider):
    ctx = make_ctx(provider)
    action = key("Approve", "green", "control", repo="herdeck", status_text="APPROVE")
    assert headline.THEME.agent_tile(action, ctx, None) is None  # Elgato action key
    for tile in key_variants().values():
        assert headline.THEME.key_tile(tile, ctx) is None
    for panel in panel_variants().values():
        for width in (458, 392):
            assert headline.THEME.panel(panel, ctx, width) is None


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("name", list(agent_variants()))
def test_every_agent_tile_renders(provider, log, name, lang):
    img = headline.THEME.agent_tile(agent_variants()[name], make_ctx(provider, lang), None)
    assert img.size == (S, S)
    assert img.mode == "RGB"
    assert_text_sane(log.take(), img.size, name=f"headline:{name}/{lang}")


def test_headline_fills_the_key_with_the_status_colour(provider):
    ctx = make_ctx(provider)
    for status in STATUSES:
        tile = agent(status)
        img = headline.THEME.agent_tile(tile, ctx, None)
        assert img.getpixel((S - 3, S // 2)) == COLORS[tile.color], status


def test_headline_name_is_the_project_and_time_always_shows(provider, log):
    ctx = make_ctx(provider)
    for status in STATUSES:
        headline.THEME.agent_tile(agent(status), ctx, None)
        drawn = texts(log)
        assert "herdeck" in drawn and "12m" in drawn, status
        assert not any("Fix the" in t for t in drawn), status


def test_headline_status_word_only_where_it_is_news(provider, log):
    ctx = make_ctx(provider)
    for status in STATUSES:
        headline.THEME.agent_tile(agent(status), ctx, None)
        drawn = " ".join(texts(log))
        has_word = any(w in drawn for w in ("BLOCKED", "CI", "OFFLINE"))
        assert has_word == (status in ("blocked", "waiting", "offline")), status
