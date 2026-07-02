from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .driver.base import PanelView
from .model import AgentState, Status

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
_STATUS_PRIORITY = {
    Status.BLOCKED: 0,
    Status.DONE: 1,
    Status.WORKING: 2,
    Status.IDLE: 3,
    Status.UNKNOWN: 4,
}

STATUS_COLOR = {
    Status.WORKING: "green",
    Status.IDLE: "blue",
    Status.BLOCKED: "amber",
    Status.DONE: "cyan",
    Status.UNKNOWN: "grey",
}


def status_color(status: Status) -> str:
    return STATUS_COLOR.get(status, "grey")


def compose_line(state: AgentState, tokens: list[str]) -> str:
    """Render an agent-tile text line from a token list.

    Tokens map to AgentState values; empty values are dropped and the rest are
    joined with " · ". `tab` is shown only when present, prefixed with ›.
    """
    parts: list[str] = []
    for token in tokens:
        if token == "repo":
            value = state.repo or state.label
        elif token == "branch":
            value = state.branch
        elif token == "workspace":
            value = state.workspace
        elif token == "tab":
            value = f"›{state.tab}" if state.tab else ""
        elif token == "agent":
            value = state.agent_type
        else:
            value = ""
        if value:
            parts.append(value)
    return " · ".join(parts)


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


def order_agents(agents, overview_order: list[str]) -> list[AgentState]:
    order = {sid: i for i, sid in enumerate(overview_order)}
    return sorted(
        agents,
        key=lambda s: (
            _STATUS_PRIORITY.get(s.status, 9),
            order.get(s.key.server_id, 999),
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


def summary(agents) -> Counts:
    c = Counts(0, 0, 0, 0)
    for s in agents:
        if s.status is Status.BLOCKED:
            c.blocked += 1
        elif s.status is Status.WORKING:
            c.working += 1
        elif s.status is Status.IDLE:
            c.idle += 1
        elif s.status is Status.DONE:
            c.done += 1
    return c


def panel_overview(
    counts: Counts,
    page_index: int,
    page_count: int,
    down: set[str],
    total: int,
    spotlight: tuple[str, str] | None,
) -> PanelView:
    if down:
        title, lines, color = "OFFLINE", ["reconnecting…"], "red"
        if counts.blocked:
            # one dead server must not hide that agents are waiting for input
            lines.append(f"▲ {counts.blocked} blocked")
    elif spotlight is not None:
        label, elapsed = spotlight
        # With several agents blocked the deck must not look like just one:
        # the spotlight names the oldest, the title carries the count.
        title = "▲ needs you" if counts.blocked <= 1 else f"▲ {counts.blocked} need you"
        lines = [label, f"blocked {elapsed}".rstrip()]
        color = "amber"
    else:
        title = f"{total} agents"
        lines = [f"W{counts.working} · I{counts.idle} · D{counts.done}", "online"]
        color = "grey"
    if page_count > 1 and lines:
        lines[-1] = f"{lines[-1]} · {page_index + 1}/{page_count}"
    return PanelView(title=title, lines=lines, color=color)


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


def _detail_lines(text: str) -> tuple[list[str], list[str]]:
    """(all cleaned lines, the first non-option LOGICAL lines).

    Wrapping to the panel's width happens at render time (compose_panel) with
    the actual font — the old 36-character wrap systematically overflowed the
    360px pixel budget, so nearly every full line lost its tail mid-sentence."""
    raw_lines = [
        _ANSI_RE.sub("", ln).strip()
        for ln in text.splitlines()
        if _ANSI_RE.sub("", ln).strip()
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


def panel_detail(agent: AgentState, text: str) -> PanelView:
    # Logical prompt lines (options stripped); the renderer wraps them to the
    # panel's pixel width so long prompts stay readable without losing words.
    raw_lines, lines = _detail_lines(text) if text else ([], [])
    if agent.status is Status.BLOCKED and not raw_lines:
        lines = ["reading prompt..."]
    return PanelView(
        title=f"{agent.agent_type}: {agent.label}",
        lines=lines,
        color=status_color(agent.status),
    )
