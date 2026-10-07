"""signal's status window: every panel kind at 458 (D200) and 392 (Elgato
key pair / web), with the Elgato seam at x = 196 kept free of text."""

from __future__ import annotations

import dataclasses

import pytest
from theme_support import (
    LANGS,
    WIDTHS,
    TextLog,
    assert_text_sane,
    make_ctx,
    make_provider,
    panel_variants,
)

from herdeck.driver.base import COLORS, PanelGauge, PanelView
from herdeck.themes import _signal_panel as sp
from herdeck.themes import signal
from herdeck.themes._kit import SEAM, S


@pytest.fixture
def provider(tmp_path):
    return make_provider(tmp_path)


@pytest.fixture
def log(monkeypatch):
    return TextLog().install(monkeypatch)


def texts(log) -> list[str]:
    return [t for _, t, _ in log.take()]


def render(provider, name, width, lang="en", **changes):
    panel = panel_variants()[name]
    if changes:
        panel = dataclasses.replace(panel, **changes)
    return signal.THEME.panel(panel, make_ctx(provider, lang), width)


# The kinds drawn so far (Task 5 widens this to every kind).
DRAWN = {"spotlight", "calm", "offline"}


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize(
    "name", [n for n, p in panel_variants().items() if p.kind in DRAWN and not p.sent]
)
def test_every_panel_kind_renders_at_both_widths(provider, log, name, width, lang):
    img = render(provider, name, width, lang)
    assert img.size == (width, S)
    assert img.mode == "RGB"
    seam = SEAM if width < 420 else None
    assert_text_sane(log.take(), img.size, seam=seam, name=f"{name}@{width}/{lang}")


def test_unknown_panel_kind_is_left_to_the_base(provider):
    assert signal.THEME.panel(PanelView("x", kind="mystery"), make_ctx(provider), 458) is None


# --- spotlight ---------------------------------------------------------------


def test_spotlight_names_the_head_and_its_wait(provider, log):
    render(provider, "spotlight_queue_note", 458)
    drawn = texts(log)
    assert drawn[0] == "lumen"
    assert "4m waiting" in drawn
    assert "1m" in drawn  # the next in the queue, as a favicon chip + age
    assert "laptop" in drawn  # the offline machine chip, without the word


def test_spotlight_is_amber(provider):
    img = render(provider, "spotlight_one", 458)
    assert img.getpixel((S + 100, S - 6)) == COLORS["amber"]


def test_spotlight_at_392_collapses_the_offline_chip_to_its_glyph(provider, log):
    render(provider, "spotlight_queue_note", 392)
    drawn = texts(log)
    assert "1m" in drawn  # the first queued chip always keeps its place
    assert "laptop" not in drawn


@pytest.mark.parametrize(
    "chip_ws, more_ws, note_w, expect",
    [
        # no offline note: chips fill the row, a later chip also needs its "+N"
        ([96], [0], 0, (1, None, 0)),
        ([96, 96, 96], [30, 30, 0], 0, (2, None, 0)),
        # the note fits whole beside the first chip
        ([96, 96], [30, 0], 125, (1, 319, 125)),
        # too wide: cut to the room left after the first chip
        ([96, 96], [30, 0], 300, (1, 306, 138)),
        # under NOTE_MIN of room: only the glyph
        ([150], [0], 300, (1, 402, 42)),
        # not even the glyph fits: the first chip keeps its place, no note
        ([200], [0], 300, (1, None, 0)),
        # nobody else waits: the note may take the whole row
        ([], [], 300, (0, 198, 246)),
    ],
)
def test_queue_row_layout(chip_ws, more_ws, note_w, expect):
    assert sp.queue_row_layout(chip_ws, more_ws, 198, 444, note_w) == expect


def test_queue_row_never_runs_into_the_note():
    for n in range(1, 6):
        for note_w in (0, 42, 120, 260):
            chips = [96] * n
            more = [30] * (n - 1) + [0]
            shown, note_x0, _ = sp.queue_row_layout(chips, more, 198, 444, note_w)
            end = 198 + sum(w + 16 for w in chips[:shown]) - 16
            if note_x0 is not None and shown > 1:
                assert end <= note_x0 - 12


@pytest.mark.parametrize("lang", LANGS)
def test_machine_name_drops_the_offline_word(provider, lang):
    ctx = make_ctx(provider, lang)
    assert sp.machine_name(ctx, ctx.tr("server_offline").format(name="laptop")) == "laptop"
    assert sp.machine_name(ctx, "something else") == "something else"


# --- calm ----------------------------------------------------------------------


def test_counter_slots_keep_the_elgato_seam_free():
    for paged in (False, True):
        for cx in sp.counter_slots(392, paged):
            assert not (cx - sp.COUNTER_R < SEAM < cx + sp.COUNTER_R), cx
        assert max(sp.counter_slots(458, paged)) + sp.COUNTER_R <= 458 - 24


def test_calm_counts_in_priority_order_and_pages_take_a_slot(provider, log):
    render(provider, "calm_usage_paged", 458)
    drawn = texts(log)
    assert drawn[:4] == ["3", "9", "1", "2"]  # offline, working, waiting, done; idle paged off
    render(provider, "calm_usage_paged", 392)
    assert texts(log)[:3] == ["3", "9", "1"]


def test_calm_shows_one_usage_window_per_provider(provider, log):
    render(provider, "calm_usage_paged", 458)
    drawn = texts(log)
    assert "Claude 5H" in drawn and "Claude 7D" not in drawn
    assert "Codex 5H" in drawn and "91%" in drawn


def test_one_per_provider_keeps_the_fullest_window():
    gauges = [PanelGauge("Claude", "5H", 30), PanelGauge("Claude", "7D", 80), PanelGauge("Codex", "5H", 5)]
    assert [(g.label, g.window) for g in sp.one_per_provider(gauges)] == [
        ("Claude", "7D"),
        ("Codex", "5H"),
    ]


def test_calm_counters_follow_theme_colors(provider):
    panel = panel_variants()["calm_note"]
    ctx = make_ctx(provider, colors={"working": "pink", "idle": "blue", "offline": "red"})
    img = signal.THEME.panel(panel, ctx, 458)
    assert COLORS["pink"] in {c for _, c in img.getcolors(1 << 16)}


def test_calm_many_pages_falls_back_to_a_page_label(provider, log):
    render(provider, "calm_many_pages", 458)
    assert "5/9" in texts(log)


def test_calm_without_agents_says_so(provider, log):
    render(provider, "calm_no_agents", 392)
    assert texts(log) == ["No agents"]


# --- offline -----------------------------------------------------------------


def test_offline_says_how_long_and_who_waits(provider, log):
    render(provider, "offline", 458)
    assert texts(log)[:2] == ["Offline", "2m · 2 waiting"]
