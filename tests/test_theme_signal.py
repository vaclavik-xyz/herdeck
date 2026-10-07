"""signal — the built-in favicon + urgency theme: every key kind, every
status, every panel kind, at 458 (D200) and 392 (Elgato pair / web)."""

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
    overlaps,
)

from herdeck.driver.base import COLORS
from herdeck.themes import signal
from herdeck.themes._kit import EMPTY, MIN_TEXT, S

FRAME_BOX = (signal.FRAME, signal.FRAME, S - signal.FRAME, S - signal.FRAME)


@pytest.fixture
def provider(tmp_path):
    return make_provider(tmp_path)


@pytest.fixture
def log(monkeypatch):
    return TextLog().install(monkeypatch)


def texts(log) -> list[str]:
    return [t for _, t, _ in log.take()]


# --- identity -----------------------------------------------------------------


def test_theme_identity():
    theme = signal.THEME
    assert theme.id == "signal"
    assert theme.name == {"en": "Signal", "cs": "Signál"}
    assert theme.base == "classic"


# --- agent tiles --------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("name", list(agent_variants()))
def test_every_agent_tile_renders_inside_its_frame(provider, log, lang, name):
    tile = agent_variants()[name]
    img = signal.THEME.agent_tile(tile, make_ctx(provider, lang), None)
    assert img.size == (S, S)
    assert img.mode == "RGB"
    inside = None if tile.status == "blocked" else FRAME_BOX
    assert_text_sane(log.take(), img.size, inside=inside, name=f"{name}/{lang}")


def test_status_is_shape_coded_not_only_coloured(provider):
    """Colour-blind safety: with every status drawn in the SAME colour the
    tiles must still differ (frame shape, dot, pill, fill)."""
    ctx = make_ctx(provider)
    seen = {}
    for status in ["blocked", "working", "idle", "done", "waiting", "offline"]:
        img = signal.THEME.agent_tile(agent(status, color="grey"), ctx, None)
        seen[status] = img.tobytes()
    assert len(set(seen.values())) == len(seen)


def test_time_shows_only_on_blocked_and_waiting(provider, log):
    ctx = make_ctx(provider)
    for status in STATUSES:
        signal.THEME.agent_tile(agent(status), ctx, None)
        shown = "12m" in texts(log)
        assert shown == (status in ("blocked", "waiting")), status


def test_no_branch_and_no_backend_tag(provider, log):
    ctx = make_ctx(provider)
    for status in STATUSES:
        signal.THEME.agent_tile(agent(status, server_tag="HERDR"), ctx, None)
        drawn = " ".join(texts(log))
        assert "feat/deck-themes" not in drawn and "HERDR" not in drawn, status


def test_text_line_is_title_then_workspace_then_repo(provider, log):
    ctx = make_ctx(provider)
    signal.THEME.agent_tile(agent("working", title="Ship it"), ctx, None)
    assert texts(log) == ["Ship it"]
    signal.THEME.agent_tile(agent("working", title="", workspace="infra"), ctx, None)
    assert texts(log) == ["infra"]
    signal.THEME.agent_tile(agent("working", title="", workspace=""), ctx, None)
    assert texts(log) == ["herdeck"]


def test_subagents_are_a_small_pill(provider, log):
    signal.THEME.agent_tile(agent("working", subagents=3), make_ctx(provider), None)
    entries = log.take()
    pill = [e for e in entries if e[1] == "+3"]
    assert pill and pill[0][2] == MIN_TEXT


def test_waiting_shows_holder_label_and_time(provider, log):
    ctx = make_ctx(provider)
    signal.THEME.agent_tile(agent("waiting"), ctx, None)
    drawn = texts(log)
    assert "CI" in drawn and "12m" in drawn
    # a long holder label stays whole; the time gives way
    signal.THEME.agent_tile(agent("waiting", status_text="REVIEW +1"), ctx, None)
    drawn = texts(log)
    assert "REVIEW +1" in drawn and "12m" not in drawn


def _mark_box(tile):
    compact = tile.status == "waiting" or bool(tile.subagents)
    size, top = (signal.MARK_COMPACT, signal.MARK_Y_COMPACT) if compact else (signal.MARK, signal.MARK_Y)
    mx = (S - size) // 2
    return (mx, top, mx + size, top + size)


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("name", [n for n, t in agent_variants().items() if t.status != "blocked"])
def test_no_pill_or_text_covers_the_mark(provider, log, monkeypatch, name, lang):
    """The top pills (time, holder label, "+N") sit above or beside the mark,
    never on it: with a pill in the top row the mark shrinks under it."""
    tile = agent_variants()[name]
    pills = []
    pill = signal.k.pill

    def spy(*args, **kw):
        box = pill(*args, **kw)
        pills.append(box)
        return box

    monkeypatch.setattr(signal.k, "pill", spy)
    signal.THEME.agent_tile(tile, make_ctx(provider, lang), None)
    mark = _mark_box(tile)
    for box, text, _ in log.take():
        assert not overlaps(box, mark), (name, text, box, mark)
    for box in pills:
        assert not overlaps(box, mark), (name, box, mark)


def test_waiting_keeps_its_subagents_when_they_fit(provider, log):
    ctx = make_ctx(provider)
    # time + holder label in the top row, "+2" beside the mark
    signal.THEME.agent_tile(agent("waiting", subagents=2), ctx, None)
    assert {"12m", "CI", "+2"} <= set(texts(log))
    # a long holder label leaves no room for the time; "+2" still shows
    signal.THEME.agent_tile(agent("waiting", status_text="REVIEW +1", subagents=2), ctx, None)
    drawn = texts(log)
    assert "REVIEW +1" in drawn and "+2" in drawn and "12m" not in drawn


@pytest.mark.parametrize("lang", LANGS)
def test_pinned_hole_label_splits_on_its_separator(provider, log, lang):
    ctx = make_ctx(provider, lang)
    for key_name in ("pinned_missing", "pinned_absent"):
        label = ctx.tr(key_name)
        signal.THEME.key_tile(key(label, "grey", "pinned_hole", subtext="Unpin"), ctx)
        entries = log.take()
        assert [t for _, t, _ in entries] == [*label.split(" · "), "Unpin"], label
        assert_text_sane(entries, (S, S))


def test_offline_slash_crosses_only_the_mark(provider):
    img = signal.THEME.agent_tile(agent("offline"), make_ctx(provider), None).convert("RGB")
    red = COLORS["red"]
    top = signal.MARK_Y + signal.MARK + 2
    # inside the frame and clear of its rounded corners
    for y in range(top, S - 30):
        for x in range(30, S - 30):
            assert img.getpixel((x, y)) != red, (x, y)


def test_blocked_floods_the_key(provider):
    img = signal.THEME.agent_tile(agent("blocked"), make_ctx(provider), None)
    assert img.getpixel((2, S // 2)) == COLORS["amber"]
    assert img.getpixel((S - 3, S - 3)) == COLORS["amber"]


def test_project_mark_first_agent_mark_only_without_project_data(provider):
    """Ruling P1: the project mark (favicon/monogram) whatever tile_icon says;
    the agent's own mark only for a tile with no project data."""
    ctx = make_ctx(provider)
    as_agent = signal.THEME.agent_tile(agent("idle", tile_icon="agent"), ctx, None)
    as_project = signal.THEME.agent_tile(agent("idle", tile_icon="project"), ctx, None)
    assert as_agent.tobytes() == as_project.tobytes()
    no_project = signal.THEME.agent_tile(agent("idle", repo="", project_name=""), ctx, None)
    assert no_project.tobytes() != as_project.tobytes()
    # ... and what it shows is the agent's own logo
    from PIL import Image

    expected = Image.new("RGB", (signal.MARK, signal.MARK), signal.k.DARK)
    logo = ctx.agent_mark("claude", signal.MARK)
    expected.paste(logo, (0, 0), logo)
    mx = (S - signal.MARK) // 2
    shown = no_project.crop((mx, signal.MARK_Y, mx + signal.MARK, signal.MARK_Y + signal.MARK))
    assert shown.tobytes() == expected.tobytes()


# --- key tiles ----------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("name", list(key_variants()))
def test_every_key_kind_renders(provider, log, lang, name):
    img = signal.THEME.key_tile(key_variants()[name], make_ctx(provider, lang))
    assert img.size == (S, S)
    assert_text_sane(log.take(), img.size, name=f"{name}/{lang}")


def test_agent_kind_is_not_a_key_tile(provider):
    assert signal.THEME.key_tile(agent("idle"), make_ctx(provider)) is None


def test_empty_is_near_black(provider, log):
    img = signal.THEME.key_tile(key("", "empty", "empty"), make_ctx(provider))
    assert img.getextrema() == tuple((c, c) for c in EMPTY)
    assert texts(log) == []


def test_new_slot_is_a_plus_with_the_word(provider, log):
    ctx = make_ctx(provider)
    a = signal.THEME.key_tile(key("+ New", "launcher", "launcher"), ctx)
    assert texts(log) == ["New"]
    b = signal.THEME.key_tile(key("+ New", "launcher", "control"), ctx)
    assert a.tobytes() == b.tobytes()
    log.take()
    signal.THEME.key_tile(key("+ Nový", "launcher", "launcher"), ctx)
    assert texts(log) == ["Nový"]


def test_option_is_a_big_digit_and_wrapped_text_on_its_tone(provider, log):
    img = signal.THEME.key_tile(key_variants()["option_digit"], make_ctx(provider))
    entries = log.take()
    assert entries[0][1] == "1" and entries[0][2] >= 48
    assert len(entries) >= 3  # the choice text wraps
    assert entries[-1][1].endswith("…")  # a cut-off choice is always marked
    assert img.getpixel((S // 2, S - 8)) == COLORS["green"]


def test_controls_are_dark_with_a_coloured_outline(provider):
    img = signal.THEME.key_tile(key("Stop", "red", "control"), make_ctx(provider))
    assert img.getpixel((S // 2, 2)) == COLORS["red"]
    assert img.getpixel((S // 2, 30)) == signal.k.DARK


def test_unknown_kind_and_menu_entries_without_type_draw_as_controls(provider):
    ctx = make_ctx(provider)
    control = signal.THEME.key_tile(key("Profiles", "blue", "control"), ctx)
    for kind in ("menu", "mystery"):
        assert signal.THEME.key_tile(key("Profiles", "blue", kind), ctx).tobytes() == control.tobytes()


def test_elgato_action_key_routed_to_agent_tile_draws_as_a_control(provider):
    """Elgato action keys carry ``repo`` (so the platform calls agent_tile) but
    kind "control"."""
    ctx = make_ctx(provider)
    action = key("Approve", "green", "control", repo="herdeck", status_text="APPROVE")
    plain = signal.THEME.key_tile(key("Approve", "green", "control"), ctx)
    assert signal.THEME.agent_tile(action, ctx, None).tobytes() == plain.tobytes()


def test_launcher_entry_shows_the_agent_mark(provider):
    ctx = make_ctx(provider)
    with_mark = signal.THEME.key_tile(key("claude", "blue", "menu", agent_type="claude"), ctx)
    plain = signal.THEME.key_tile(key("claude", "blue", "control"), ctx)
    assert with_mark.tobytes() != plain.tobytes()
