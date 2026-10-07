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


# Every panel kind signal draws.
DRAWN = {"spotlight", "calm", "offline", "config_error", "detail", "usage", "menu", "sent"}


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize(
    "name", [n for n, p in panel_variants().items() if p.kind in DRAWN]
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
    assert "+2" in drawn  # the queue's remainder is never dropped
    # (the offline chip gives way to its glyph to make room for it)


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
        # no offline note: chips fill the row, each one leaves room for "+N"
        ([96], [0], 0, (1, None, 0)),
        # (a second 96 px chip would push the "+2" past the edge: 1 chip + "+2")
        ([96, 96, 96], [30, 30, 0], 0, (1, None, 0)),
        ([60, 60, 60], [30, 30, 0], 0, (3, None, 0)),
        # the note fits whole beside the first chip (and its "+N")
        ([60, 60], [30, 0], 100, (1, 344, 100)),
        # too wide: cut to the room left after the first chip and its "+N"
        ([60, 60], [30, 0], 300, (1, 316, 128)),
        # the "+N" after the first chip takes the room: glyph only
        ([96, 96], [30, 0], 125, (1, 402, 42)),
        # under NOTE_MIN of room: only the glyph
        ([150], [0], 300, (1, 402, 42)),
        # not even the glyph fits: the first chip keeps its place, no note
        ([200], [0], 300, (1, None, 0)),
        # nobody else waits: the note may take the whole row
        ([], [], 300, (0, 198, 246)),
        # one chip, nothing after it: no "+N" reserved
        ([96], [0], 300, (1, 306, 138)),
    ],
)
def test_queue_row_layout(chip_ws, more_ws, note_w, expect):
    assert sp.queue_row_layout(chip_ws, more_ws, 198, 444, note_w) == expect


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("name,rest", [("spotlight_queue_note", 2), ("spotlight_many_no_note", None)])
def test_spotlight_draws_the_remaining_count(provider, log, name, rest, width):
    panel = panel_variants()[name]
    render(provider, name, width)
    drawn = log.take()
    waiting = len(panel.queue) - 1
    plus = [t for _, t, _ in drawn if t.startswith("+") and t[1:].isdigit()]
    assert len(plus) == 1, (name, width, [t for _, t, _ in drawn])
    if rest is not None:
        assert plus == [f"+{rest}"]
    assert plus  # chips shown + "+N" account for the whole queue
    seam = SEAM if width < 420 else None
    assert_text_sane(drawn, (width, S), seam=seam, name=f"{name}@{width}")
    assert int(plus[0][1:]) <= waiting


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("name", ["spotlight_queue_note", "spotlight_many_no_note"])
def test_spotlight_chips_and_plus_n_account_for_the_whole_queue(provider, log, monkeypatch, name, width):
    seen = []
    layout = sp.queue_row_layout

    def spy(*args):
        out = layout(*args)
        seen.append(out[0])
        return out

    monkeypatch.setattr(sp, "queue_row_layout", spy)
    render(provider, name, width)
    plus = [t for t in texts(log) if t.startswith("+") and t[1:].isdigit()]
    waiting = len(panel_variants()[name].queue) - 1
    assert seen and plus
    assert seen[0] + int(plus[0][1:]) == waiting


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


@pytest.mark.parametrize("n, shown", [(7, "7"), (99, "99"), (100, "99+"), (1000, "99+"), (123456, "99+")])
def test_ring_counts_cap_at_99_plus_inside_the_ring(provider, log, n, shown):
    """A 3+ digit count would touch the ring and "1000" would ellipsize to a
    misleading "1…": counts over 99 read "99+", always inside the ring."""
    render(provider, "calm_note", 458, counts={"working": n}, note="")
    entries = log.take()
    assert [t for _, t, _ in entries] == [shown]
    (box, _, _), = entries
    cx, cy, inner = sp.counter_slots(458, False)[0], 98, sp.COUNTER_R - 8
    for x, y in ((box[0], box[1]), (box[2], box[1]), (box[0], box[3]), (box[2], box[3])):
        assert (x - cx) ** 2 + (y - cy) ** 2 <= inner**2, (shown, box)


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


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("width", WIDTHS)
def test_offline_names_the_machine_else_says_reconnecting(provider, log, width, lang):
    seam = SEAM if width < 420 else None
    # layout's lines[0] is "<servers>[ · <blocked count>]": the count is
    # already on the line above, so only the machine is drawn
    render(provider, "offline", width, lang, lines=["macbench · 5 waiting for you"])
    entries = log.take()
    assert [t for _, t, _ in entries][-1] == "macbench"
    assert_text_sane(entries, (width, S), seam=seam)
    render(provider, "offline", width, lang, lines=[])
    entries = log.take()
    assert [t for _, t, _ in entries][-1] == "Reconnecting…"
    assert_text_sane(entries, (width, S), seam=seam)


def test_sent_wins_over_calm_and_spotlight(provider):
    a = render(provider, "spotlight_sent", 458)
    b = render(provider, "spotlight_sent", 458, kind="sent")
    assert a.tobytes() == b.tobytes()


# --- config error / detail / usage / menu / sent ---------------------------------


def test_detail_shows_answer_on_the_keys_only_when_it_fits(provider, log):
    render(provider, "detail_blocked_short", 458)
    assert "answer on the keys" in texts(log)
    long = dict(hint="answer on the keys")
    render(provider, "detail_long", 458, **long)
    assert "answer on the keys" not in texts(log)  # the prompt needs every line


def test_detail_at_392_moves_the_time_beside_the_chip(provider, log):
    render(provider, "detail_blocked_short", 392)
    drawn = texts(log)
    assert drawn[0] == "BLOCKED"
    assert "claude · herdeck" in drawn  # which agent it is is never lost
    assert "4m" in drawn  # nor how long it waits (it leads the text)


def _tr(provider, lang, key, **fmt):
    text = make_ctx(provider, lang).tr(key)
    return text.format(**fmt) if fmt else text


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("width", WIDTHS)
def test_flow_overflow_ends_in_an_ellipsis(provider, log, width, lang):
    render(provider, "detail_long", width, lang)
    drawn = texts(log)
    assert drawn[-1].endswith("…"), drawn


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("width", WIDTHS)
def test_config_error_draws_the_whole_fix_hint(provider, log, width, lang):
    hint = _tr(provider, lang, "config_error_hint")
    render(
        provider, "config_error", width, lang,
        title=_tr(provider, lang, "config_error_title"),
        headline=_tr(provider, lang, "config_error_token", name="macbench"),
        lines=[hint],
    )
    entries = log.take()
    drawn = [t for _, t, _ in entries]
    assert not any(t.endswith("…") for t in drawn), drawn
    assert hint.split() == " ".join(drawn).split()[-len(hint.split()):], drawn
    assert_text_sane(entries, (width, S), seam=SEAM if width < 420 else None)


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("fields", [("aside",), ("sent",), ("aside", "sent")])
def test_detail_footer_draws_aside_and_sent_before_the_hint(provider, log, width, lang, fields):
    values = {
        "aside": _tr(provider, lang, "others_blocked", n=2),
        "sent": _tr(provider, lang, "sent", label="lumen"),
    }
    changes = {f: values[f] for f in fields}
    render(provider, "detail_blocked_short", width, lang, **changes)
    entries = log.take()
    drawn = [t for _, t, _ in entries]
    for f in fields:
        assert values[f] in drawn, (f, drawn)
    assert_text_sane(entries, (width, S), seam=SEAM if width < 420 else None)


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("width", WIDTHS)
def test_armed_stop_confirmation_is_never_cut(provider, log, width, lang):
    title = _tr(provider, lang, "press_to_confirm")
    render(provider, "detail_blocked_short", width, lang, title=title, color="red", hint="")
    entries = log.take()
    drawn = [t for _, t, _ in entries]
    assert not any(t.endswith("…") and title.startswith(t[:-1]) for t in drawn), drawn
    assert title.split() == " ".join(drawn).split()[: len(title.split())], drawn
    assert "claude · herdeck" in drawn
    assert_text_sane(entries, (width, S), seam=SEAM if width < 420 else None)


def test_usage_side_text_falls_back_to_the_reset_time(provider, log):
    panel = panel_variants()["usage"]
    gauges = [dataclasses.replace(panel.gauges[0], hint="reset Mon"), *panel.gauges[1:]]
    render(provider, "usage", 392, gauges=gauges)
    drawn = texts(log)
    assert "full ~40m early" not in drawn  # no room for the pace in the right key
    assert drawn.count("reset Mon") == 2  # the session row falls back to its reset


def test_usage_gauges(provider, log):
    render(provider, "usage", 458)
    drawn = texts(log)
    assert {"42%", "87%", "10%", "1/2", "press · more"} <= set(drawn)


@pytest.mark.parametrize("width", WIDTHS)
def test_usage_hint_sits_in_the_title_row_left_of_the_page_label(provider, log, width):
    render(provider, "usage", width)
    entries = {t: box for box, t, _ in log.take()}
    if "press · more" in entries:  # whole or not at all
        assert entries["press · more"][2] < entries["1/2"][0]
        assert entries["press · more"][3] < 52  # the gauges start at y = 52
    else:
        assert width < 420  # only the right key of the pair may lack the room


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("width", WIDTHS)
def test_config_error_draws_its_hint_only_on_free_rows(provider, log, width, lang):
    hint = "the deck recovers once it's fixed"
    seam = SEAM if width < 420 else None
    render(provider, "config_error", width, lang, lines=[])
    entries = log.take()
    drawn = " ".join(t for _, t, _ in entries)
    assert drawn.endswith(hint) or width < 420, drawn
    assert not any(t.endswith("…") for _, t, _ in entries)
    assert_text_sane(entries, (width, S), seam=seam)
    render(provider, "config_error", width, lang)  # the fix instruction needs the rows
    entries = log.take()
    assert hint not in " ".join(t for _, t, _ in entries)
    assert_text_sane(entries, (width, S), seam=seam)


def test_flow_moves_on_until_a_column_has_room(provider, log):
    """A fresh column can be shorter than one line (the last one ends above a
    footer): the line must not be placed there anyway."""
    from PIL import Image, ImageDraw

    d = ImageDraw.Draw(Image.new("RGB", (392, S)))
    ctx = make_ctx(provider)
    items = [("word " * 40, 22, False, (255, 255, 255), 6)]
    free = sp._flow(ctx, d, items, [(14, 186), (206, 378)], 58, 188, last_bottom=70)
    entries = log.take()
    assert free == 0
    assert entries and all(box[2] <= 186 and box[3] <= 188 for box, _, _ in entries)
    assert entries[-1][1].endswith("…")


def test_detail_footer_wraps_at_its_row_size(provider):
    from PIL import Image, ImageDraw

    d = ImageDraw.Draw(Image.new("RGB", (392, S)))
    panel = dataclasses.replace(
        panel_variants()["detail_blocked_short"],
        aside="lumen, orbit, atlas and ember also wait for your answer",
    )
    lines = sp._footer_lines(make_ctx(provider), d, panel, 172)
    assert len(lines) == 2
    assert {font.size for _, font, _ in lines} == {22}


def test_menu_and_config_error_text(provider, log):
    render(provider, "menu", 458)
    assert texts(log) == ["New agent", "Pick a type", "on macbench"]
    render(provider, "config_error", 458)
    drawn = texts(log)
    assert drawn[0] == "!"  # the glyph
    assert drawn[1:3] == ["Config error", "No token for macbench"]


def test_sent_names_who_got_the_answer(provider, log):
    render(provider, "sent", 458)
    assert texts(log) == ["Sent to herdeck", "1 left"]


LONG_ASIDE = "lumen, orbit, atlas, ember, nova and quark also wait for your answer here"
LONG_SENT = "sent to lumen-orbit-atlas-ember-nova-quark-and-more as the last answer"


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("width", WIDTHS)
def test_detail_keeps_a_prompt_line_under_a_two_line_footer(provider, log, width, lang):
    """aside and sent both wrapping to two lines must not squeeze the prompt
    out: at least one prompt line is drawn (ellipsized if need be)."""
    render(provider, "detail_blocked_short", width, lang, aside=LONG_ASIDE, sent=LONG_SENT)
    entries = log.take()
    prompt = panel_variants()["detail_blocked_short"].lines[0]
    first_word = prompt.split()[0]
    assert any(t.startswith(first_word) or t.endswith("\u2026") and prompt.startswith(t[:-1].rstrip())
               for _, t, _ in entries), [t for _, t, _ in entries]
    assert_text_sane(entries, (width, S), seam=SEAM if width < 420 else None)
