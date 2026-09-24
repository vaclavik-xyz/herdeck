"""Event-notification bookkeeping shared by the runtime's notification engine.

Which agent states alert (``NOTIFY_EVENT_STATUSES``), the per-event "who just
entered" diff, the one-line alert body and the set of agents a deck press
touched (for the "done right after you answered it" suppression).
"""

from __future__ import annotations

from .model import AgentKey, AgentState, Status

# Agent states that can fire a notification, mapped to their Status. Keys must
# match [notifications] `on` entries (validated against config.NOTIFY_EVENTS).
NOTIFY_EVENT_STATUSES: dict[str, Status] = {
    "blocked": Status.BLOCKED,
    "done": Status.DONE,
}


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
