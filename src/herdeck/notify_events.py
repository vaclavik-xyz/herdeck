"""Event-notification bookkeeping shared by the runtime's notification engine.

Which agent states alert (``NOTIFY_EVENT_STATUSES``), the per-event "who just
entered" diff, the one-line alert body and the set of agents a deck press
touched (for the "done right after you answered it" suppression).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import AgentKey, AgentState, Status

# Agent states that can fire a notification, mapped to their Status. Keys must
# match [notifications] `on` entries (validated against config.NOTIFY_EVENTS).
NOTIFY_EVENT_STATUSES: dict[str, Status] = {
    "blocked": Status.BLOCKED,
    "done": Status.DONE,
}


# [notifications].subagents_done fires once the parent is no longer working.
SUBAGENTS_DONE_PARENT_STATUSES = frozenset({Status.IDLE, Status.BLOCKED, Status.DONE})


def running_subagents(agent: AgentState) -> tuple[int, frozenset[str]]:
    """(running count, running ids). The bridge's subagent list (capability
    "subagents") is fresher than the metadata token, so it wins when present;
    the token alone gives a count without ids."""
    if agent.subagents:
        ids = frozenset(s.id for s in agent.subagents if s.status == "running")
        return len(ids), ids
    return agent.subagents_running, frozenset()


@dataclass
class _Burst:
    start_ms: int | None  # earliest start among the subagents seen running
    ids: set[str] = field(default_factory=set)
    peak: int = 0


class SubagentBursts:
    """"All subagents finished" bookkeeping ([notifications].subagents_done).

    A burst begins when an agent has at least one subagent running and ends
    when none are running any more AND the agent itself is idle, blocked or
    done: ``observe`` then returns how many subagents the burst had (once).
    While the agent keeps working after its last subagent finished, the burst
    stays pending; a new subagent joins the same burst. Connector-thread only.
    """

    def __init__(self) -> None:
        self._bursts: dict[AgentKey, _Burst] = {}

    def observe(self, agent: AgentState) -> int | None:
        running, ids = running_subagents(agent)
        burst = self._bursts.get(agent.key)
        if running > 0:
            starts = [s.started_ms for s in agent.subagents if s.id in ids]
            if burst is None:
                burst = self._bursts[agent.key] = _Burst(min(starts) if starts else None)
            elif starts and (burst.start_ms is None or min(starts) < burst.start_ms):
                burst.start_ms = min(starts)
            burst.ids |= ids
            burst.peak = max(burst.peak, running)
            return None
        if burst is None or agent.status not in SUBAGENTS_DONE_PARENT_STATUSES:
            return None
        del self._bursts[agent.key]
        # Subagents that started and finished between two snapshots were never
        # seen running: count the finished ones that began within the burst.
        finished = {
            s.id
            for s in agent.subagents
            if s.status in ("done", "failed")
            and burst.start_ms is not None
            and s.started_ms >= burst.start_ms
        }
        return max(len(burst.ids | finished), burst.peak, 1)

    def forget(self, keys) -> None:
        for key in keys:
            self._bursts.pop(key, None)

    def tracked(self) -> set[AgentKey]:
        return set(self._bursts)


def newly_entered(status, prev, states):
    """Keys that just entered `status` (vs prev), and the updated seen set.
    Eligibility resets when a key leaves the status, so re-entry notifies again."""
    entered_now = {s.key for s in states if s.status is status}
    to_notify = entered_now - prev
    return to_notify, entered_now


def interaction_keys(orch, cmds) -> set[AgentKey]:
    """Agents the user just touched on the deck: the drilled agent plus every
    pane a press command targets. Feeds the "done right after you answered it"
    notification suppression."""
    keys = {
        AgentKey(cmd.server_id, cmd.pane_id)
        for cmd in cmds
        if cmd.pane_id is not None and cmd.kind not in ("list", "toggle_pin")
    }
    drilled = orch.drill_key()
    if drilled is not None:
        keys.add(drilled)
    return keys


def event_notification_body(agent: AgentState, *, multi_server: bool) -> str:
    """One-line notification body for an event alert.

    Leads with the Herdr workspace label the user actually sees (editable in
    Herdr), then the tab and the session title — not the raw server/pane ids.
    Falls back to the repo/pane label, and to the branch when no tab/title
    context exists (older bridges). The server id is appended only in
    multi-server setups.
    """
    head = (agent.workspace or agent.repo or agent.label).strip()
    parts = [head]
    for candidate in (agent.tab.strip(), agent.title.strip()):
        if not candidate:
            continue
        cf = candidate.casefold()
        # Drop only a candidate already CONTAINED in a kept part (a tab/title
        # duplicating the workspace). The reverse (head being a prefix of the
        # candidate) must NOT drop it: "monitor: log watcher" vs head
        # "monitor" is the session's only distinguishing detail.
        if not any(cf in p.casefold() for p in parts):
            parts.append(candidate)
    if len(parts) == 1 and agent.branch and agent.branch.strip() != head:
        parts.append(agent.branch.strip())
    if multi_server:
        parts.append(agent.key.server_id)
    return " · ".join(parts)
