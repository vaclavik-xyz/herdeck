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


def test_headline_mark_has_a_contour_in_the_ink_colour(provider):
    """A monogram in the status colour (green on the green working fill)
    would vanish: the mark carries a contour in the ink colour."""
    ctx = make_ctx(provider)
    tile = agent("working")
    img = headline.THEME.agent_tile(tile, ctx, None)
    ink = ctx.ink_for(COLORS[tile.color])
    assert img.getpixel((11, 42)) == ink
    assert img.getpixel((42, 11)) == ink


def test_headline_subagents_are_a_pill_apart_from_the_holder_label(provider, log, monkeypatch):
    pills = []
    pill = headline.k.pill

    def spy(ctx, d, x, top, text, *args, **kw):
        pills.append(text)
        return pill(ctx, d, x, top, text, *args, **kw)

    monkeypatch.setattr(headline.k, "pill", spy)
    headline.THEME.agent_tile(agent("waiting", wait_reason="REVIEW +1", subagents=2), make_ctx(provider), None)
    drawn = texts(log)
    assert "REVIEW +1" in drawn  # whole, never "REVIEW +1  +2"
    assert pills == ["+2"]


def test_headline_word_wins_over_the_subagent_pill(provider, log):
    long_word = "WAITING ON REVIEW"
    headline.THEME.agent_tile(agent("waiting", wait_reason=long_word, subagents=12), make_ctx(provider), None)
    drawn = texts(log)
    assert "+12" not in drawn
    assert any(t.startswith("WAITING") for t in drawn)


@pytest.mark.parametrize("elapsed", ["0s", "55s", "1m", "59m", "1h", "23h", "999h"])
def test_headline_time_is_never_cut(provider, log, elapsed):
    """Every orchestrator._fmt_elapsed shape fits the time slot whole (the
    font shrinks first)."""
    headline.THEME.agent_tile(agent("working", time_text=elapsed), make_ctx(provider), None)
    entries = log.take()
    assert elapsed in [t for _, t, _ in entries]
    assert_text_sane(entries, (S, S))
