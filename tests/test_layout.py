from herdeck.config import TILE_LINE_TOKENS, ViewConfig
from herdeck.driver.base import PanelView, TileView
from herdeck.layout import (
    Counts,
    compose_line,
    order_agents,
    page,
    panel_detail,
    panel_overview,
    resolve_tile_lines,
    status_color,
    summary,
)
from herdeck.model import AgentKey, AgentState, Status


def _astate(repo="herdeck", branch="main", workspace="herdeck", tab="2", label="herdeck", agent="claude"):
    s = AgentState(AgentKey("dev", "w2:p1"), agent, label, Status.WORKING)
    s.repo, s.branch, s.workspace, s.tab = repo, branch, workspace, tab
    return s


def test_compose_line_joins_tokens_with_separator():
    assert compose_line(_astate(), ["tab", "branch"]) == "›2 · main"


def test_compose_line_omits_empty_values():
    s = _astate(branch="")
    assert compose_line(s, ["repo", "branch"]) == "herdeck"


def test_compose_line_tab_only_when_present():
    assert compose_line(_astate(tab=""), ["tab", "branch"]) == "main"
    assert compose_line(_astate(tab="3"), ["tab"]) == "›3"


def test_compose_line_repo_falls_back_to_label():
    s = _astate(repo="", label="api")
    assert compose_line(s, ["repo"]) == "api"


def test_compose_line_empty_when_all_values_empty():
    s = _astate(workspace="", tab="")
    assert compose_line(s, ["workspace", "tab"]) == ""


def test_compose_line_handles_every_valid_token():
    s = _astate()
    for tok in TILE_LINE_TOKENS:
        # every valid token must map to a non-empty value for a populated state,
        # so a renamed token that compose_line no longer handles is caught here
        assert compose_line(s, [tok]) != ""


def test_resolve_tile_lines_uses_fallback_when_none():
    view = ViewConfig()  # tile_primary/secondary default None
    primary, secondary = resolve_tile_lines(view, ["repo"], ["branch"])
    assert primary == ["repo"]
    assert secondary == ["branch"]


def test_resolve_tile_lines_explicit_wins_per_key_including_empty():
    view = ViewConfig()
    view.tile_primary = ["workspace"]
    view.tile_secondary = []  # explicit off
    primary, secondary = resolve_tile_lines(view, ["repo"], ["branch"])
    assert primary == ["workspace"]  # explicit wins
    assert secondary == []           # explicit [] wins over fallback


def test_resolve_tile_lines_partial_override_one_key():
    view = ViewConfig()
    view.tile_primary = ["workspace"]  # secondary stays None -> fallback
    primary, secondary = resolve_tile_lines(view, ["repo"], ["branch"])
    assert primary == ["workspace"]
    assert secondary == ["branch"]


def a(pane, status, agent_type="claude", label="p", server="dev"):
    return AgentState(AgentKey(server, pane), agent_type, label, status)


def test_order_blocked_then_done_then_working_then_idle():
    # done = finished but unseen -> sorts to the top (just below blocked, above
    # working) so a completed agent surfaces where the eye is, not off-page.
    agents = [
        a("p1", Status.IDLE),
        a("p2", Status.BLOCKED),
        a("p3", Status.DONE),
        a("p4", Status.WORKING),
    ]
    ordered = order_agents(agents, ["dev"])
    assert [s.status for s in ordered] == [Status.BLOCKED, Status.DONE, Status.WORKING, Status.IDLE]


def test_order_stable_by_pane_within_status():
    agents = [a("p2", Status.WORKING), a("p1", Status.WORKING)]
    assert [s.key.pane_id for s in order_agents(agents, ["dev"])] == ["p1", "p2"]


def test_page_slices_and_counts():
    items = list(range(30))
    sl, pages = page(items, 0, 13)
    assert sl == list(range(13)) and pages == 3
    sl2, _ = page(items, 1, 13)
    assert sl2 == list(range(13, 26))


def test_page_index_wraps():
    items = list(range(5))
    sl, pages = page(items, 3, 13)  # only 1 page -> wraps to 0
    assert pages == 1 and sl == items


def test_summary_counts():
    agents = [
        a("p1", Status.BLOCKED),
        a("p2", Status.WORKING),
        a("p3", Status.WORKING),
        a("p4", Status.IDLE),
    ]
    c = summary(agents)
    assert (c.blocked, c.working, c.idle, c.done) == (1, 2, 1, 0)


def test_status_color():
    assert status_color(Status.BLOCKED) == "amber"
    assert status_color(Status.WORKING) == "green"
    assert status_color(Status.IDLE) == "blue"
    assert status_color(Status.DONE) == "cyan"
    assert status_color(Status.UNKNOWN) == "grey"


def test_tileview_server_fields_default_none():
    tile = TileView(0, "x", "blue")

    assert tile.server_tag is None and tile.server_accent is None


def test_panel_overview_offline_takes_priority():
    pv = panel_overview(Counts(1, 0, 0, 0), 0, 1, {"srv"}, 5, ("api", "2m"))
    assert isinstance(pv, PanelView)
    assert pv.title == "OFFLINE"
    assert pv.color == "red"


def test_panel_overview_blocked_spotlight():
    pv = panel_overview(Counts(1, 3, 6, 0), 0, 1, set(), 11, ("macdoktor-crm", "4m"))
    assert pv.title == "▲ needs you"
    assert pv.lines[0] == "macdoktor-crm"
    assert pv.lines[1] == "blocked 4m"
    assert pv.color == "amber"


def test_panel_overview_blocked_without_elapsed():
    pv = panel_overview(Counts(1, 0, 0, 0), 0, 1, set(), 1, ("api", ""))
    assert pv.lines[1] == "blocked"


def test_panel_overview_calm():
    pv = panel_overview(Counts(0, 3, 6, 2), 0, 1, set(), 11, None)
    assert pv.title == "11 agents"
    assert pv.lines[0] == "W3 · I6 · D2"
    assert pv.lines[1] == "online"
    assert pv.color == "grey"


def test_panel_overview_page_suffix_only_when_multipage():
    multi = panel_overview(Counts(0, 1, 0, 0), 1, 3, set(), 5, None)
    assert multi.lines[-1].endswith(" · 2/3")
    single = panel_overview(Counts(0, 1, 0, 0), 0, 1, set(), 5, None)
    assert "/" not in single.lines[-1]


def test_panel_overview_calm_swaps_online_for_usage_lines():
    usage = ["Claude 5h 19% · 7d 43%", "Codex 5h 2% · 7d 30%"]
    pv = panel_overview(Counts(0, 3, 6, 2), 0, 1, set(), 11, None, usage_lines=usage)
    assert pv.lines == ["W3 · I6 · D2", *usage]  # "online" replaced, counts kept


def test_panel_overview_usage_moves_page_marker_to_counts_line():
    usage = ["Claude 5h 19% · 7d 43%"]
    pv = panel_overview(Counts(0, 1, 0, 0), 1, 3, set(), 5, None, usage_lines=usage)
    assert pv.lines[0] == "W1 · I0 · D0 · 2/3"
    assert pv.lines[1] == usage[0]  # the marker never glues onto a usage line


def test_panel_overview_usage_hidden_when_blocked_or_offline():
    usage = ["Claude 5h 19%"]
    blocked = panel_overview(Counts(1, 0, 0, 0), 0, 1, set(), 1, ("api", "2m"), usage_lines=usage)
    assert all("Claude" not in ln for ln in blocked.lines)
    offline = panel_overview(Counts(0, 0, 0, 0), 0, 1, {"srv"}, 1, None, usage_lines=usage)
    assert all("Claude" not in ln for ln in offline.lines)


def test_panel_detail_with_and_without_text():
    p = panel_detail(
        a("p1", Status.BLOCKED, agent_type="claude", label="api"), "Allow edit to config.py?"
    )
    assert "claude" in p.title and "api" in p.title
    assert p.lines and "Allow edit" in p.lines[0]
    assert p.color == "amber"
    p2 = panel_detail(a("p1", Status.WORKING), "")
    assert p2.lines == []  # no text yet


def test_panel_detail_blocked_without_text_shows_loading_line():
    p = panel_detail(a("p1", Status.BLOCKED, agent_type="claude", label="api"), "")
    assert p.lines == ["reading prompt..."]


def test_panel_detail_shows_question_not_option_lines():
    agent = AgentState(AgentKey("s", "p"), "claude", "api", Status.BLOCKED)
    panel = panel_detail(agent, "Do you want to proceed?\n1. Yes\n2. No")
    assert panel.lines == ["Do you want to proceed?"]


def test_panel_detail_keeps_long_question_as_one_logical_line():
    # Wrapping happens at render time (compose_panel) by PIXEL width — the old
    # 36-character wrap overflowed the panel's 360px budget on every full line.
    agent = AgentState(AgentKey("s", "p"), "claude", "api", Status.BLOCKED)
    q = "Allow this long filesystem edit request that needs more room to be readable?"
    panel = panel_detail(agent, q + "\n1. Yes\n2. No")
    assert panel.lines == [q]


def test_panel_detail_strips_ansi_and_still_skips_options():
    agent = AgentState(AgentKey("s", "p"), "claude", "api", Status.BLOCKED)
    panel = panel_detail(agent, "\x1b[33mAllow edit?\x1b[0m\n\x1b[32m1. Yes\x1b[0m\n2. No")
    assert panel.lines == ["Allow edit?"]


def test_panel_detail_all_options_falls_back_to_first_line():
    agent = AgentState(AgentKey("s", "p"), "claude", "api", Status.BLOCKED)
    panel = panel_detail(agent, "1. Yes\n2. No")
    assert panel.lines and "1." in panel.lines[0]


def test_parse_options_numbered():
    from herdeck.layout import parse_options

    txt = "Do you want to proceed?\n❯ 1. Yes\n  2. Yes, and don't ask again\n  3. No"
    opts = parse_options(txt)
    assert [(o.key, o.label) for o in opts] == [
        ("1", "Yes"),
        ("2", "Yes, and don't ask again"),
        ("3", "No"),
    ]


def test_parse_options_question_list_and_dedup():
    from herdeck.layout import parse_options

    txt = (
        "Kde?\n1. Dodavatelské doklady\n   detail line\n"
        "2. Cenotvorba / sledování trhu\n2. duplicate ignored\n5. Type something"
    )
    opts = parse_options(txt)
    assert [o.key for o in opts] == ["1", "2", "5"]
    assert opts[0].label == "Dodavatelské doklady"


def test_parse_options_uses_last_menu_not_stale_scrollback():
    from herdeck.layout import parse_options

    # Detected pane text carries old numbered lists scrolled up above the live
    # prompt; the current menu is the last block starting at 1, and a side panel
    # sharing the option rows must be trimmed from the labels.
    txt = (
        "1. Groq vrací přepis\n2. Přidá se LLM krok\n3. Tím se naplní\n4. V UI odznak\n"
        "\nsome later output\n"
        "1. Textová diarizace\n2. Časování\n"
        "\n❯ 1. Délka obsahu (rozhovoru)     ┌────────────┐\n"
        "  2. Čas zpracování (per fáze)    │ DÉLKA      │\n"
        "  3. Obojí                        │ ───────    │\n"
        "Enter to select · Esc to cancel"
    )
    opts = parse_options(txt)
    assert [(o.key, o.label) for o in opts] == [
        ("1", "Délka obsahu (rozhovoru)"),
        ("2", "Čas zpracování (per fáze)"),
        ("3", "Obojí"),
    ]


def test_parse_options_strips_ansi_sequences():
    from herdeck.layout import parse_options

    txt = "Allow edit?\n\x1b[32m1. Yes\x1b[0m\n\x1b[31m2. No\x1b[0m"
    opts = parse_options(txt)
    assert [(o.key, o.label) for o in opts] == [("1", "Yes"), ("2", "No")]


def test_parse_options_none():
    from herdeck.layout import parse_options

    assert parse_options("just some text, no options") == []
    assert parse_options("") == []


def test_spotlight_title_carries_the_blocked_count():
    """Three blocked agents must not look identical to one
    (audit: blocked-count-spotlight)."""
    pv = panel_overview(Counts(3, 1, 0, 0), 0, 1, set(), 4, ("api", "5m"))
    assert pv.title == "▲ 3 need you"
    assert pv.lines[0] == "api"  # the oldest blocked agent stays spotlighted
    single = panel_overview(Counts(1, 1, 0, 0), 0, 1, set(), 2, ("api", "5m"))
    assert single.title == "▲ needs you"


def test_offline_panel_still_reports_blocked_agents():
    pv = panel_overview(Counts(2, 0, 0, 0), 0, 1, {"down-server"}, 2, ("api", "5m"))
    assert pv.title == "OFFLINE"
    assert "▲ 2 blocked" in pv.lines


def test_panel_overview_renders_czech_when_asked():
    pv = panel_overview(Counts(2, 1, 0, 0), 0, 1, set(), 3, ("api", "4m"), lang="cs")
    assert pv.title == "▲ čeká: 2"
    assert pv.lines[1] == "čeká 4m"
    offline = panel_overview(Counts(1, 0, 0, 0), 0, 1, {"srv"}, 1, None, lang="cs")
    assert offline.lines[0] == "připojuji…"
    assert offline.lines[1] == "▲ blokováno: 1"


def test_panel_overview_default_language_stays_english():
    pv = panel_overview(Counts(0, 1, 1, 0), 0, 1, set(), 2, None)
    assert pv.title == "2 agents"


def test_waiting_ranks_between_working_and_idle():
    from herdeck.layout import status_rank

    assert (
        status_rank(Status.BLOCKED)
        < status_rank(Status.DONE)
        < status_rank(Status.WORKING)
        < status_rank(Status.WAITING)
        < status_rank(Status.IDLE)
        < status_rank(Status.UNKNOWN)
    )


def test_waiting_status_color_is_violet():
    from herdeck.layout import status_color

    assert status_color(Status.WAITING) == "violet"


def test_waiting_status_text_uses_holder_label():
    from herdeck.layout import waiting_status_text

    assert waiting_status_text("⏳ ci") == "CI"
    assert waiting_status_text("⏳ review +1") == "REVIEW +1"
    assert waiting_status_text("") == "WAITING"  # fallback word
    assert waiting_status_text("", lang="cs") == "V POZADÍ"
    assert len(waiting_status_text("⏳ a-very-long-marker-name")) <= 12


def test_panel_overview_counts_show_pending_only_when_nonzero():
    pv = panel_overview(Counts(0, 2, 3, 1, waiting=1), 0, 1, set(), 7, None)
    assert pv.lines[0] == "W2 · P1 · I3 · D1"
    pv = panel_overview(Counts(0, 2, 3, 1), 0, 1, set(), 6, None)
    assert pv.lines[0] == "W2 · I3 · D1"


def test_panel_detail_leads_with_waiting_label():
    from herdeck.layout import panel_detail

    agent = AgentState(
        AgentKey("dev", "p1"), "claude", "api", Status.WAITING, custom_status="⏳ review +1"
    )
    pv = panel_detail(agent, "", lang="en")
    assert pv.lines[0] == "waiting on: review +1"
    assert pv.color == "violet"
    pv_cs = panel_detail(agent, "", lang="cs")
    assert pv_cs.lines[0] == "čeká na: review +1"
