"""quiet (variant C): calm unless it needs you — agent tiles only; every
other key and the status window come from its base, ``signal``."""

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
from herdeck.themes import quiet
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
    assert (quiet.THEME.id, quiet.THEME.base) == ("quiet", "signal")
    assert quiet.THEME.name == {"en": "Quiet", "cs": "Klid"}


def test_only_agent_tiles_are_drawn(provider):
    ctx = make_ctx(provider)
    action = key("Approve", "green", "control", repo="herdeck", status_text="APPROVE")
    assert quiet.THEME.agent_tile(action, ctx, None) is None  # Elgato action key
    for tile in key_variants().values():
        assert quiet.THEME.key_tile(tile, ctx) is None
    for panel in panel_variants().values():
        for width in (458, 392):
            assert quiet.THEME.panel(panel, ctx, width) is None


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("name", list(agent_variants()))
def test_every_agent_tile_renders(provider, log, name, lang):
    img = quiet.THEME.agent_tile(agent_variants()[name], make_ctx(provider, lang), None)
    assert img.size == (S, S)
    assert img.mode == "RGB"
    assert_text_sane(log.take(), img.size, name=f"quiet:{name}/{lang}")


def test_quiet_only_blocked_is_loud(provider):
    ctx = make_ctx(provider)
    for status in STATUSES:
        tile = agent(status)
        corner = quiet.THEME.agent_tile(tile, ctx, None).getpixel((S - 3, S - 3))
        if status == "blocked":
            assert corner == COLORS["amber"]
        else:
            assert corner != COLORS[tile.color], status


def test_quiet_done_is_outlined_and_says_so(provider, log):
    img = quiet.THEME.agent_tile(agent("done"), make_ctx(provider), None)
    assert img.getpixel((6, S // 2)) == COLORS["cyan"]
    assert "DONE" in texts(log)


def test_quiet_long_status_word_moves_under_the_mark_whole(provider, log):
    quiet.THEME.agent_tile(agent("waiting", status_text=""), make_ctx(provider, "cs"), None)
    (box, _, _), = [e for e in log.take() if e[1] == "V POZADÍ"]
    assert quiet.MARK_X <= box[0] < quiet.MARK_X + 4 and box[1] >= 84  # own row, under the mark


def test_quiet_blocked_shows_the_wait_big(provider, log):
    quiet.THEME.agent_tile(agent("blocked"), make_ctx(provider), None)
    entries = log.take()
    assert entries[0][1] == "12m" and entries[0][2] >= 48


@pytest.mark.parametrize("status", ["waiting", "done", "offline", "working", "idle"])
def test_quiet_shows_subagents_on_every_quiet_status(provider, log, status):
    quiet.THEME.agent_tile(agent(status, subagents=3), make_ctx(provider, "cs"), None)
    assert "+3" in texts(log), status


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("status", ["done", "offline"])
def test_quiet_status_word_sits_beside_the_mark(provider, log, lang, status):
    """Like the prototype: DONE / HOTOVO / OFFLINE beside the mark (>= 22 px)."""
    ctx = make_ctx(provider, lang)
    quiet.THEME.agent_tile(agent(status, subagents=3), ctx, None)
    entries = log.take()
    word = ctx.tr(f"status.{status}")
    (box, _, px), = [e for e in entries if e[1] == word]
    assert box[0] >= quiet.BESIDE_X0 and box[1] < quiet.MARK_Y + quiet.MARK
    assert px >= 22


@pytest.mark.parametrize("lang", LANGS)
def test_quiet_done_texts_stay_inside_the_ring(provider, log, lang):
    for tile in (agent("done"), agent("done", subagents=12), agent("done", title="x" * 80)):
        quiet.THEME.agent_tile(tile, make_ctx(provider, lang), None)
        assert_text_sane(log.take(), (S, S), inside=(13, 13, S - 13, S - 13), name=lang)
