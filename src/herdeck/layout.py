from __future__ import annotations

import math
from dataclasses import dataclass

from .driver.base import PanelView
from .model import AgentState, Status

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


def panel_detail(agent: AgentState, text: str) -> PanelView:
    lines = [text.strip()[:80]] if text and text.strip() else []
    return PanelView(
        title=f"{agent.agent_type}: {agent.label}",
        lines=lines,
        color=status_color(agent.status),
    )
