from __future__ import annotations

import math
import re
import textwrap
from dataclasses import dataclass

from .driver.base import PanelView
from .model import AgentState, Status

# A numbered choice line in an agent prompt, e.g. "❯ 1. Yes" or "2. Cenotvorba".
# Leading markers (cursor caret, bullets, whitespace) are skipped before the digit.
_OPTION_RE = re.compile(r"^[\s>❯❱*\-)(]*(\d+)[.)]\s+(\S.*?)\s*$")
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_DETAIL_LINE_WIDTH = 36
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
    joined with " · ". `tab` is shown only when present, prefixed with ▸.
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
            value = f"▸{state.tab}" if state.tab else ""
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
    elif spotlight is not None:
        label, elapsed = spotlight
        title = "⚠ needs you"
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

    Matches lines like ``1. Yes`` / ``❯ 2. Cenotvorba`` / ``3) No``. Duplicate
    numbers keep their first occurrence; order follows appearance.
    """
    options: list[Option] = []
    seen: set[str] = set()
    for line in (text or "").splitlines():
        m = _OPTION_RE.match(_ANSI_RE.sub("", line).strip())
        if not m:
            continue
        key = m.group(1)
        if key in seen:
            continue
        seen.add(key)
        options.append(Option(key, m.group(2).strip()))
    return options


def _detail_lines(text: str) -> tuple[list[str], list[str]]:
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
        wrapped = textwrap.wrap(
            line,
            width=_DETAIL_LINE_WIDTH,
            break_long_words=True,
            break_on_hyphens=False,
        ) or [line[:_DETAIL_LINE_WIDTH]]
        for wrapped_line in wrapped:
            lines.append(wrapped_line)
            if len(lines) == _DETAIL_MAX_LINES:
                return raw_lines, lines
    if raw_lines and not lines:
        lines = textwrap.wrap(raw_lines[0], width=_DETAIL_LINE_WIDTH)[:_DETAIL_MAX_LINES]
    return raw_lines, lines


def panel_detail(agent: AgentState, text: str) -> PanelView:
    # Split into bounded display lines so embedded newlines and long prompts do
    # not reach the small panel as one unreadable blob.
    raw_lines, lines = _detail_lines(text) if text else ([], [])
    if agent.status is Status.BLOCKED and not raw_lines:
        lines = ["reading prompt..."]
    return PanelView(
        title=f"{agent.agent_type}: {agent.label}",
        lines=lines,
        color=status_color(agent.status),
    )
