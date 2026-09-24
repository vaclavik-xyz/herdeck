from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath

from .driver.base import PanelGauge, PanelStat, PanelView, TileView
from .i18n import tr
from .model import AgentState, Status
from .project_icons import ProjectIconStore, tile_icon_fields

# A numbered choice line in an agent prompt, e.g. "❯ 1. Yes" or "2. Cenotvorba".
# Leading markers (cursor caret, bullets, whitespace) are skipped before the digit.
_OPTION_RE = re.compile(r"^[\s>❯❱*\-)(]*(\d+)[.)]\s+(\S.*?)\s*$")
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
# A box-drawing run (U+2500–U+257F) and everything after it: a side panel drawn
# in the same terminal row as an option, to be trimmed off the option's label.
_BOX_DRAWING_RE = re.compile("\\s*[─-╿].*$")
_DETAIL_MAX_LINES = 3

# lower = higher priority (shown first). done = finished-but-unseen ranks just
# below blocked and above working, so a completed agent surfaces at the top of
# the deck (where the eye is) instead of being buried after the idle agents.
# waiting (pane held pending background CI/review by herdwatch) sits between
# working and idle: nothing to do there, but it isn't finished either.
_STATUS_PRIORITY = {
    Status.BLOCKED: 0,
    Status.DONE: 1,
    Status.WORKING: 2,
    Status.WAITING: 3,
    Status.IDLE: 4,
    Status.UNKNOWN: 5,
}


def status_rank(status: Status) -> int:
    """Overview sort rank of a status (lower = shown first)."""
    return _STATUS_PRIORITY.get(status, 99)


STATUS_COLOR = {
    Status.WORKING: "green",
    Status.IDLE: "blue",
    Status.BLOCKED: "amber",
    Status.DONE: "cyan",
    Status.WAITING: "violet",
    Status.UNKNOWN: "grey",
}


def status_color(status: Status) -> str:
    return STATUS_COLOR.get(status, "grey")


def agent_tile_view(
    index: int,
    state: AgentState,
    view,
    *,
    color: str,
    repo: str,
    branch: str,
    status_text: str | None,
    project_icons: ProjectIconStore | None = None,
    spinner: int | None = None,
    time_text: str | None = None,
    pinned: bool = False,
    server_tag: str | None = None,
    server_accent: str | None = None,
    section: str | None = None,
) -> TileView:
    """The one TileView for an agent tile, shared by the deck (orchestrator)
    and the Elgato plugin session.

    Everything that comes from the agent or from ``[view]`` (label, agent
    type, working_animation, tile_fill, the tile_icon fields) is filled here,
    so a new view setting cannot reach one surface and be missed by the other.
    The keyword extras are what only some surfaces show (animation phase,
    elapsed time, pins, backend tags, click-to-jump section)."""
    return TileView(
        index,
        state.label,
        color,
        icon=None,
        agent_type=state.agent_type,
        spinner=spinner,
        working_animation=view.working_animation,
        tile_fill=view.tile_fill,
        repo=repo,
        branch=branch,
        status_text=status_text,
        time_text=time_text,
        pinned=pinned,
        server_tag=server_tag,
        server_accent=server_accent,
        section=section,
        subagents=state.subagents_running,
        **tile_icon_fields(view, state, project_icons),
    )


def project_name(state: AgentState) -> str:
    """Editable backend name for display; repository and routing identity stay intact."""
    if state.backend == "t3":
        candidates = (state.project, PurePosixPath(state.repo).name, state.label)
    else:
        candidates = (state.workspace, state.repo, state.label)
    return next((value.strip() for value in candidates if value.strip()), "")


def compose_line(state: AgentState, tokens: list[str]) -> str:
    """Render an agent-tile text line from a token list.

    Tokens map to AgentState values; empty values are dropped and the rest are
    joined with " · ". `tab` is shown only when present, prefixed with ›.
    """
    parts: list[str] = []
    for token in tokens:
        if token == "project":
            value = project_name(state)
        elif token == "repo":
            # T3 projects have editable display names; workspaceRoot is a path,
            # not the project label shown in the T3 sidebar.
            if state.backend == "t3":
                value = state.project or PurePosixPath(state.repo).name or state.label
            else:
                value = state.repo or state.label
        elif token == "title":
            value = state.title
        elif token == "branch":
            value = state.branch
        elif token == "workspace":
            value = state.workspace
        elif token == "tab":
            value = state.title if state.backend == "t3" else (f"›{state.tab}" if state.tab else "")
        elif token == "agent":
            value = state.agent_type
        elif token == "source":
            value = state.work.source
        elif token == "work_item":
            value = state.work.item
        elif token == "run":
            value = state.work.run
        elif token.startswith("$"):
            value = state.metadata.get(token[1:], "")
        else:
            value = ""
        if value:
            parts.append(value)
    return " · ".join(parts)


def compose_tile_lines(state: AgentState, primary_tokens: list[str], secondary_tokens: list[str]) -> tuple[str, str]:
    primary = compose_line(state, primary_tokens)
    secondary = compose_line(state, secondary_tokens)
    if not primary and primary_tokens == ["tab"]:
        primary = compose_line(state, ["project"])
        if secondary == primary:
            secondary = ""
    return primary, secondary


def resolve_tile_lines(
    view, fallback_primary: list[str], fallback_secondary: list[str]
) -> tuple[list[str], list[str]]:
    """Resolve (primary, secondary) token lists.

    Per key: an explicit config value (including an empty list) wins; an absent
    key (None) uses the render path's fallback.
    """
    primary = view.tile_primary if view.tile_primary is not None else fallback_primary
    secondary = view.tile_secondary if view.tile_secondary is not None else fallback_secondary
    return primary, secondary


def order_agents(
    agents,
    overview_order: list[str],
    agent_order: str = "status",
    blocked_since=None,
) -> list[AgentState]:
    """Overview order: attention priority (status rank) first.

    ``blocked_since`` maps an AgentKey to when that agent entered BLOCKED (any
    monotonic clock). BLOCKED agents are then ordered longest-waiting first —
    in ``status`` order across servers (attention beats grouping), in
    ``herdr`` order after the mirrored workspace/tab position. Agents without
    a known start sort after the known ones, then by pane id as before."""
    order = {sid: i for i, sid in enumerate(overview_order)}
    since = blocked_since or {}

    def waited(state: AgentState) -> float:
        if state.status is not Status.BLOCKED:
            return 0.0
        return since.get(state.key, math.inf)

    first = agent_order != "herdr"

    def herdr_position(state: AgentState) -> tuple[int, int]:
        if agent_order != "herdr":
            return (0, 0)
        missing = 2**31 - 1
        return (
            state.workspace_order if state.workspace_order is not None else missing,
            state.tab_order if state.tab_order is not None else missing,
        )

    return sorted(
        agents,
        key=lambda s: (
            0 if s.attention == "error" else _STATUS_PRIORITY.get(s.status, 9),
            waited(s) if first else 0.0,
            order.get(s.key.server_id, 999),
            *herdr_position(s),
            0.0 if first else waited(s),
            s.key.pane_id,
        ),
    )


def page(items: list, page_index: int, tile_count: int) -> tuple[list, int]:
    pages = max(1, math.ceil(len(items) / tile_count)) if items else 1
    pi = page_index % pages
    start = pi * tile_count
    return items[start : start + tile_count], pages


@dataclass
class Counts:
    blocked: int
    working: int
    idle: int
    done: int
    waiting: int = 0


def summary(agents) -> Counts:
    c = Counts(0, 0, 0, 0)
    for s in agents:
        if s.lifecycle != "active":
            continue
        if s.status is Status.BLOCKED:
            c.blocked += 1
        elif s.status is Status.WORKING:
            c.working += 1
        elif s.status is Status.IDLE:
            c.idle += 1
        elif s.status is Status.DONE:
            c.done += 1
        elif s.status is Status.WAITING:
            c.waiting += 1
    return c


@dataclass
class Spotlight:
    """The overview's NEEDS YOU subject: the longest-waiting blocked agent."""

    label: str
    elapsed: str
    detail: str = ""  # "tab · agent type", shown under the name
    others: tuple[tuple[str, str], ...] = ()  # the next blocked agents (label, elapsed)


def _status_stat(counts: Counts, lang: str, colors: dict[str, str]) -> list[PanelStat]:
    rows = [(Status.WORKING, counts.working), (Status.WAITING, counts.waiting)]
    rows += [(Status.IDLE, counts.idle), (Status.DONE, counts.done)]
    return [
        PanelStat(tr(lang, f"status.{status.value}"), n, colors.get(status.value, status_color(status)))
        for status, n in rows
        # held (herdwatch) panes are rare: their card appears only when used
        if status is not Status.WAITING or n
    ]


def _compact_counts(counts: Counts) -> str:
    pending = f" · P{counts.waiting}" if counts.waiting else ""
    return f"W{counts.working}{pending} · I{counts.idle} · D{counts.done}"


def panel_overview(
    counts: Counts,
    page_index: int,
    page_count: int,
    down: set[str],
    total: int,
    spotlight: Spotlight | tuple[str, str] | None,
    lang: str = "en",
    usage_gauges: list[PanelGauge] | None = None,
    servers: int | None = None,
    *,
    colors: dict[str, str] | None = None,
    sent: str = "",
    down_for: str = "",
    left: int | None = None,
) -> PanelView:
    """The overview status panel: calm counts, NEEDS YOU, or OFFLINE.

    ``servers`` is the number of configured servers. When it is given and only
    SOME of them are in ``down`` (a partial outage), the normal calm/spotlight
    panel renders and a compact ``note`` names the offline server(s); the full
    OFFLINE panel is kept for "every server down". ``servers=None`` keeps the
    legacy rule (any down server -> OFFLINE). ``sent`` names the agent just
    answered (the header acknowledges it while the bridge catches up);
    ``down_for`` is how long the full outage has lasted; ``left`` is how many
    blocked agents still await an answer (answered episodes excluded — the
    bridge's status change lags the answer), defaulting to ``counts.blocked``.
    """
    colors = colors or {}
    page = (page_index, page_count) if page_count > 1 else None
    partial = bool(down) and servers is not None and len(down) < servers
    note = ""
    if partial:
        note = (
            tr(lang, "server_offline", name=next(iter(down)))
            if len(down) == 1
            else tr(lang, "servers_offline", n=len(down))
        )
    if down and not partial:
        sub = ", ".join(sorted(down))
        if counts.blocked:
            # one dead server must not hide that agents are waiting for input
            sub = f"{sub} · {tr(lang, 'blocked_count', n=counts.blocked)}"
        return PanelView(
            title=tr(lang, "offline_title"),
            headline=tr(lang, "reconnecting"),
            lines=[sub],
            color=colors.get("offline", "red"),
            solid=True,
            meta=down_for,
            hint=tr(lang, "panel.keep_running"),
            page=page,  # a press still pages the (offline) tiles
        )
    if spotlight is not None:
        spot = spotlight if isinstance(spotlight, Spotlight) else Spotlight(*spotlight)
        # With several agents blocked the deck must not look like just one:
        # the headline names the oldest, the chip carries the count.
        many = counts.blocked > 1
        if many and spot.others:
            items = " · ".join(f"{label} {elapsed}".strip() for label, elapsed in spot.others[:2])
            lines = [tr(lang, "panel.next", items=items)]
        else:
            lines = [spot.detail] if spot.detail else []
        if sent:
            n = counts.blocked if left is None else left
            meta = tr(lang, "panel.left", n=n) if n else ""
        elif many:
            meta = tr(lang, "panel.longest", t=spot.elapsed)
        else:
            meta = spot.elapsed
        return PanelView(
            title=tr(lang, "needs_you_many", n=counts.blocked) if many else tr(lang, "needs_you_one"),
            headline=spot.label,
            lines=lines,
            color=colors.get("blocked", "amber"),
            solid=True,
            meta=meta,
            hint=tr(lang, "panel.press_answer_longest" if many else "panel.press_answer"),
            page=page,
            note=note,
            sent=sent,
        )
    if page is not None:
        hint = tr(lang, "panel.press_next_page")
    elif usage_gauges:
        hint = tr(lang, "panel.press_limits")
    else:
        hint = ""
    view = PanelView(
        title=tr(lang, "panel.all_clear") if total else tr(lang, "panel.no_agents"),
        chip_dot="green" if total else "",
        meta=tr(lang, "agents_total", n=total) if total else "",
        hint=hint,
        page=page,
        note=note,
        sent=sent,
    )
    if usage_gauges:
        # Limits take the main area; the counts shrink into the header.
        view.gauges = list(usage_gauges)
        view.meta = tr(lang, "panel.counts_of", counts=_compact_counts(counts), n=total)
    else:
        view.stats = _status_stat(counts, lang, colors)
    return view


def _fmt_reset(resets_at: str | None, now) -> str:
    """Short local reset time: 'HH:MM' today, 'D.M. HH:MM' otherwise."""
    if not resets_at:
        return ""
    from datetime import datetime

    try:
        dt = datetime.fromisoformat(resets_at.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return ""
    local_now = (now or datetime.now().astimezone()).astimezone()
    hm = f"{dt:%H:%M}"
    if dt.date() == local_now.date():
        return hm
    return f"{dt.day}.{dt.month}. {hm}"


def _fmt_early(seconds: int) -> str:
    """Compact pace margin: '40m', '3h', '2d' (fits a 3-column detail card)."""
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"{minutes}m"
    hours = round(seconds / 3600)
    if hours < 48:
        return f"{hours}h"
    return f"{round(seconds / 86400)}d"


def _pace_hint(window, lang: str) -> str:
    early = getattr(window, "full_early_s", None)
    return tr(lang, "usage_pace", t=_fmt_early(early)) if early else ""


def _provider_name(provider: str) -> str:
    return provider.capitalize() if provider.islower() else provider


def _provider_gauge_color(provider: str) -> str:
    return {"claude": "orange", "codex": "teal"}.get(provider.lower(), "violet")


def usage_summary_gauges(data, max_gauges: int = 4, now=None, lang: str = "en") -> list[PanelGauge]:
    """Structured overview gauges, including reset times when available."""
    gauges: list[PanelGauge] = []
    for provider in data:
        for window in provider.windows:
            reset = _fmt_reset(window.resets_at, now)
            gauges.append(
                PanelGauge(
                    label=_provider_name(provider.provider),
                    window=window.label.upper(),
                    used_percent=window.used_percent,
                    hint=f"{tr(lang, 'usage_reset')} {reset}" if reset else "",
                    color=_provider_gauge_color(provider.provider),
                )
            )
            if len(gauges) == max_gauges:
                return gauges
    return gauges


def usage_detail_pages(data) -> int:
    """How many panel pages the detail spans (one line per provider window)."""
    count = sum(len(p.windows) for p in data)
    return max(1, math.ceil(count / _DETAIL_MAX_LINES))


def usage_detail_gauges(data, now=None, page: int = 0, lang: str = "en") -> list[PanelGauge]:
    """One detail-page gauge per provider window, including its reset time and
    the pace hint when the recent burn rate fills the window before it resets
    (usage_alerts.UsageTracker sets ``full_early_s``)."""
    gauges: list[PanelGauge] = []
    for provider in data:
        for window in provider.windows:
            reset = _fmt_reset(window.resets_at, now)
            gauges.append(
                PanelGauge(
                    label=_provider_name(provider.provider),
                    window=window.label.upper(),
                    used_percent=window.used_percent,
                    hint=f"{tr(lang, 'usage_reset')} {reset}" if reset else "",
                    color=_provider_gauge_color(provider.provider),
                    pace=_pace_hint(window, lang),
                )
            )
    page = min(max(0, page), usage_detail_pages(data) - 1)
    start = page * _DETAIL_MAX_LINES
    return gauges[start : start + _DETAIL_MAX_LINES]


@dataclass
class Option:
    key: str  # the keystroke to send (the option's number)
    label: str  # human text of the choice


def parse_options(text: str) -> list[Option]:
    """Extract numbered choices from an agent prompt (permission menu, question).

    Matches lines like ``1. Yes`` / ``❯ 2. Cenotvorba`` / ``3) No``. The detected
    pane text can carry stale numbered lists scrolled up above the live prompt, so
    parsing anchors on the CURRENT menu — the last block that starts at ``1`` — to
    keep an older list from shadowing it. Duplicate numbers keep their first
    occurrence; a side panel sharing an option's row (box-drawing columns) is
    trimmed from the label.
    """
    matches: list[tuple[str, str]] = []
    for line in (text or "").splitlines():
        m = _OPTION_RE.match(_ANSI_RE.sub("", line).strip())
        if not m:
            continue
        label = _BOX_DRAWING_RE.sub("", m.group(2)).strip()
        matches.append((m.group(1), label))
    # Restart at the last option keyed "1": that is the most recent menu, so an
    # older numbered list higher in the scrollback can't win.
    start = 0
    for i, (key, _) in enumerate(matches):
        if key == "1":
            start = i
    options: list[Option] = []
    seen: set[str] = set()
    for key, label in matches[start:]:
        if key in seen:
            continue
        seen.add(key)
        options.append(Option(key, label))
    return options


# Characters never allowed into notification text: controls (incl. escape) and
# bidi overrides that could make a banner lie about its content.
_UNSAFE_TEXT_RE = re.compile("[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]")
_BOX_CHARS_RE = re.compile("[\u2500-\u257f]")  # TUI frame borders around a prompt
PROMPT_EXCERPT_MAX = 180


def prompt_excerpt(text: str, limit: int = PROMPT_EXCERPT_MAX) -> str:
    """A one-line excerpt of a blocked prompt for a notification body.

    The same lines the drill panel shows (the prompt's leading non-option
    lines, ANSI and frame borders stripped), joined, stripped of
    control / bidi characters and truncated to ``limit`` characters with "…".
    """
    _raw, lines = _detail_lines(text or "")
    joined = _BOX_CHARS_RE.sub(" ", " ".join(lines))
    joined = " ".join(_UNSAFE_TEXT_RE.sub(" ", joined).split())
    if len(joined) <= limit:
        return joined
    return joined[: max(0, limit - 1)].rstrip() + "…"


def _detail_lines(text: str) -> tuple[list[str], list[str]]:
    """(all cleaned lines, the first non-option LOGICAL lines).

    Wrapping to the panel's width happens at render time (compose_panel) with
    the actual font — the old 36-character wrap systematically overflowed the
    360px pixel budget, so nearly every full line lost its tail mid-sentence."""
    raw_lines = [
        _ANSI_RE.sub("", ln).strip() for ln in text.splitlines() if _ANSI_RE.sub("", ln).strip()
    ]
    option_keys = {opt.key for opt in parse_options("\n".join(raw_lines))}
    lines: list[str] = []
    for line in raw_lines:
        match = _OPTION_RE.match(line)
        if match and match.group(1) in option_keys:
            continue
        lines.append(line)
        if len(lines) == _DETAIL_MAX_LINES:
            break
    if raw_lines and not lines:
        lines = [raw_lines[0]]
    return raw_lines, lines


# Unicode categories the tile font cannot draw: pictographs and modifier
# symbols, private use (powerline/Nerd Font icons), surrogates, unassigned,
# controls, and enclosing marks (Me — a keycap-style combiner with no base
# glyph of its own). This is a proxy for glyph coverage, not a guarantee of
# it — the categories left in still admit plenty the font may lack (arrows,
# CJK), which costs a tofu box, not a frame. Sm is left in for the ASCII math
# punctuation a marker like "review +1" needs, not because the whole category
# is drawable. Mn/Mc are left in too — a label in Thai, Devanagari or Hebrew
# with niqqud needs them to spell a word — but the variation selectors are
# category Mn and are dropped by codepoint alongside Me below: they decorate
# a base character that already stands on its own, so without this a label
# spelled with VS16 (e.g. "⚠️ ci") drops the emoji's base but keeps its
# selector, which the font draws as a leading tofu box. Mirrors the same
# distinction `_is_attached_mark` in bridge.py draws for answer text, though
# the two rules answer different questions and are deliberately kept separate.
_UNDRAWABLE = frozenset({"So", "Sk", "Co", "Cs", "Cn", "Cc", "Cf", "Me"})


def _is_variation_selector(ch: str) -> bool:
    # The supplementary range (VS17-VS256) is unreachable here — the caller
    # already drops anything past the BMP before this runs — but it is kept
    # for parity with `_is_attached_mark` in bridge.py, whose caller has no
    # such limit.
    return 0xFE00 <= ord(ch) <= 0xFE0F or 0xE0100 <= ord(ch) <= 0xE01EF


def _short_status_label(text: str) -> str:
    """Normalise a foreign label into the tile's status slot: uppercase like
    the built-in status words, clipped to what the slot fits.

    The label is not herdeck's text — a `waiting_on` marker or one of herdr's
    state labels, either of which any integration may write — so it is scrubbed
    of what the tile cannot draw: everything the font has no glyph for (the
    hourglass herdwatch prefixes, emoji, the private-use area where powerline
    and Nerd Font icons live, controls, anything past the BMP) would render as
    a tofu box, and an embedded newline raises out of Pillow's single-line text
    measurement and would take the whole frame down with it.
    """
    kept = "".join(
        ch
        for ch in (text or "")
        # whitespace survives the filter so that words separated by a newline or
        # a tab stay separated once split() collapses the run to one space.
        if ch.isspace()
        or (
            ord(ch) <= 0xFFFF
            and not _is_variation_selector(ch)
            and unicodedata.category(ch) not in _UNDRAWABLE
        )
    )
    return " ".join(kept.split()).upper()[:12]


def tile_status_text(agent: AgentState, lang: str = "en", down: bool = False) -> str:
    """The agent tile's status word, in strict precedence order: the server
    being down, herdeck's own explicit `waiting_on` holder label, herdr's
    native per-status label (0.8.2+, what herdr's sidebar shows), and finally
    the generic translated status word.

    Shared by every agent-tile path so the deck, the window and the Elgato
    surface can't drift apart on which label wins.
    """
    if down:
        return tr(lang, "status.offline")
    if agent.status is Status.WAITING:
        held = _short_status_label(agent.waiting_on)
        if held:
            return held
    native = _short_status_label(agent.state_labels.get(agent.status.value, ""))
    if native:
        return native
    return tr(lang, f"status.{agent.status.value}")


def panel_detail(
    agent: AgentState, text: str, lang: str = "en", elapsed: str = "", color: str | None = None
) -> PanelView:
    """The drill panel: the agent's status chip (with ``elapsed``), who it is,
    and its prompt / pane text. Logical prompt lines (options stripped); the
    renderer wraps them to the panel's pixel width so long prompts stay
    readable without losing words."""
    raw_lines, lines = _detail_lines(text) if text else ([], [])
    if agent.status is Status.BLOCKED and not raw_lines:
        lines = [tr(lang, "reading_prompt")]
    headline = ""
    if agent.status is Status.WAITING and agent.waiting_on:
        # The full holder label leads the drill detail — the tile only fits
        # the short status word.
        label = agent.waiting_on.replace("⏳", "").strip()
        headline = tr(lang, "waiting_on", label=label)
    elif agent.status is Status.WORKING and agent.progress:
        headline = agent.progress
    status = tile_status_text(agent, lang)
    return PanelView(
        title=f"{status} · {elapsed}" if elapsed else status,
        lines=lines,
        color=color or status_color(agent.status),
        headline=headline,
        meta=f"{agent.agent_type} · {agent.label}" if agent.agent_type else agent.label,
        hint=tr(lang, "panel.answer_on_keys") if agent.status is Status.BLOCKED else "",
    )
