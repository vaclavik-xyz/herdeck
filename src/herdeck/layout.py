from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .driver.base import PanelView
from .model import AgentState, Status

# A numbered choice line in an agent prompt, e.g. "❯ 1. Yes" or "2. Cenotvorba".
# Leading markers (cursor caret, bullets, whitespace) are skipped before the digit.
_OPTION_RE = re.compile(r"^[\s>❯❱*\-)(]*(\d+)[.)]\s+(\S.*?)\s*$")

# lower = higher priority (shown first)
_STATUS_PRIORITY = {
    Status.BLOCKED: 0,
    Status.WORKING: 1,
    Status.IDLE: 2,
    Status.DONE: 3,
    Status.UNKNOWN: 4,
}

STATUS_COLOR = {
    Status.WORKING: "green",
    Status.IDLE: "blue",
    Status.BLOCKED: "amber",
    Status.DONE: "dim",
    Status.UNKNOWN: "grey",
}


def status_color(status: Status) -> str:
    return STATUS_COLOR.get(status, "grey")


def order_agents(agents, overview_order: list[str]) -> list[AgentState]:
    order = {sid: i for i, sid in enumerate(overview_order)}
    return sorted(
        agents,
        key=lambda s: (_STATUS_PRIORITY.get(s.status, 9),
                       order.get(s.key.server_id, 999), s.key.pane_id),
    )


def page(items: list, page_index: int, tile_count: int) -> tuple[list, int]:
    pages = max(1, math.ceil(len(items) / tile_count)) if items else 1
    pi = page_index % pages
    start = pi * tile_count
    return items[start:start + tile_count], pages


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


def panel_overview(counts: Counts, page_index: int, page_count: int,
                   down: set[str]) -> PanelView:
    return PanelView(
        title=f"page {page_index + 1}/{page_count}",
        lines=[f"B{counts.blocked} W{counts.working} I{counts.idle}",
               "offline" if down else "online"],
        color="red" if down else "grey",
    )


@dataclass
class Option:
    key: str        # the keystroke to send (the option's number)
    label: str      # human text of the choice


def parse_options(text: str) -> list[Option]:
    """Extract numbered choices from an agent prompt (permission menu, question).

    Matches lines like ``1. Yes`` / ``❯ 2. Cenotvorba`` / ``3) No``. Duplicate
    numbers keep their first occurrence; order follows appearance.
    """
    options: list[Option] = []
    seen: set[str] = set()
    for line in (text or "").splitlines():
        m = _OPTION_RE.match(line)
        if not m:
            continue
        key = m.group(1)
        if key in seen:
            continue
        seen.add(key)
        options.append(Option(key, m.group(2).strip()))
    return options


def panel_detail(agent: AgentState, text: str) -> PanelView:
    # Split into real lines so embedded newlines don't reach the renderer as one
    # blob; keep a bounded set of short lines for the small panel.
    lines = [ln.strip()[:80] for ln in text.splitlines() if ln.strip()][:2] if text else []
    return PanelView(
        title=f"{agent.agent_type}: {agent.label}",
        lines=lines,
        color=status_color(agent.status),
    )
