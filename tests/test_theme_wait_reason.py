"""signal / headline / quiet show what holds a waiting agent: the tile's
``wait_reason`` (the status word is always WAITING since the classic tile
moved the reason under it); a hidden reason (time field off) falls back to
the word."""

from __future__ import annotations

import pytest
from theme_support import TextLog, agent, make_ctx, make_provider

from herdeck.themes import headline, quiet, signal

THEMES = [signal.THEME, headline.THEME, quiet.THEME]


@pytest.fixture
def provider(tmp_path):
    return make_provider(tmp_path)


@pytest.fixture
def log(monkeypatch):
    return TextLog().install(monkeypatch)


@pytest.mark.parametrize("theme", THEMES, ids=lambda t: t.id)
def test_waiting_tile_shows_the_reason_not_the_word(theme, provider, log):
    tile = agent("waiting", status_text="WAITING", wait_reason="CI")
    theme.agent_tile(tile, make_ctx(provider), None)
    drawn = [t for _, t, _ in log.take()]
    assert "CI" in drawn
    assert "WAITING" not in drawn


@pytest.mark.parametrize("theme", THEMES, ids=lambda t: t.id)
def test_waiting_tile_without_a_reason_shows_the_word(theme, provider, log):
    tile = agent("waiting", status_text="WAITING", wait_reason="")
    theme.agent_tile(tile, make_ctx(provider), None)
    drawn = [t for _, t, _ in log.take()]
    assert "WAITING" in drawn
    assert "CI" not in drawn
