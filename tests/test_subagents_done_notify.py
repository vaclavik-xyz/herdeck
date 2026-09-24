"""[notifications].subagents_done: one alert when an agent's subagents have all
finished and the agent itself is idle, blocked or done (runtime side)."""

from dataclasses import replace

import pytest

from herdeck.model import Status, Subagent
from herdeck.notify_events import SubagentBursts
from tests.test_deckapp_live import agent, notify_config
from tests.test_notify_hygiene import _live

T0 = 1_790_000_000_000


def sub(id_, status="running", started=T0):
    return Subagent(id=id_, provider="claude", status=status, started_ms=started)


def with_subs(server, pane, status, *subs, token=None):
    state = agent(server.id, pane, status)
    if token is not None:
        running, total = token
        return replace(state, subagents_running=running, subagents_total=total)
    running = sum(1 for s in subs if s.status == "running")
    return replace(state, subagents=tuple(subs), subagents_running=running, subagents_total=len(subs))


def _source(lang="en", enabled=True, clock=None, idle=600.0):
    config, server = notify_config()
    config.notifications.subagents_done = enabled
    config.view.language = lang
    src, notifier, _probe = _live(config, server, clock=clock, idle=idle)
    return src, server, notifier


def titles(notifier):
    return [c[0] for c in notifier.calls if "subagents" in c[0] or "subagenti" in c[0]]


def test_off_by_default():
    src, server, notifier = _source(enabled=False)
    src._on_snapshot(server.id, [with_subs(server, "p0", Status.WORKING, sub("a"))])
    src._on_event(server.id, with_subs(server, "p0", Status.IDLE, sub("a", "done")))
    assert titles(notifier) == []


def test_waits_for_the_parent_to_stop_working_then_alerts_once():
    src, server, notifier = _source()
    src._on_snapshot(server.id, [with_subs(server, "p0", Status.WORKING, sub("a"), sub("b"))])
    src._on_event(server.id, with_subs(server, "p0", Status.WORKING, sub("a", "done"), sub("b", "done")))
    assert titles(notifier) == []  # parent still working on the results
    src._on_event(server.id, with_subs(server, "p0", Status.IDLE, sub("a", "done"), sub("b", "done")))
    assert titles(notifier) == ["claude · subagents done (2)"]
    meta = notifier.metas[-1]
    assert meta == {"agent": {"server_id": server.id, "pane_id": "p0"}, "event": "subagents_done"}
    src._on_snapshot(server.id, [with_subs(server, "p0", Status.IDLE, sub("a", "done"), sub("b", "done"))])
    assert titles(notifier) == ["claude · subagents done (2)"]  # once per burst


@pytest.mark.parametrize("status", [Status.IDLE, Status.BLOCKED, Status.DONE])
def test_an_idle_blocked_or_done_parent_alerts_right_away(status):
    src, server, notifier = _source()
    src._on_snapshot(server.id, [with_subs(server, "p0", Status.WORKING, sub("a"))])
    src._on_event(server.id, with_subs(server, "p0", status, sub("a", "failed")))
    assert titles(notifier) == ["claude · subagents done (1)"]


@pytest.mark.parametrize("status", [Status.WORKING, Status.UNKNOWN])
def test_no_alert_while_the_parent_is_not_resting(status):
    src, server, notifier = _source()
    src._on_snapshot(server.id, [with_subs(server, "p0", Status.WORKING, sub("a"))])
    src._on_event(server.id, with_subs(server, "p0", status, sub("a", "done")))
    assert titles(notifier) == []


def test_a_subagent_started_while_pending_joins_the_same_burst():
    src, server, notifier = _source()
    src._on_snapshot(server.id, [with_subs(server, "p0", Status.WORKING, sub("a"))])
    src._on_event(server.id, with_subs(server, "p0", Status.WORKING, sub("a", "done")))
    src._on_event(server.id, with_subs(server, "p0", Status.WORKING, sub("a", "done"), sub("b", started=T0 + 5)))
    src._on_event(server.id, with_subs(server, "p0", Status.IDLE, sub("a", "done"), sub("b", "done", T0 + 5)))
    assert titles(notifier) == ["claude · subagents done (2)"]


def test_counts_subagents_that_were_never_seen_running():
    src, server, notifier = _source()
    src._on_snapshot(server.id, [with_subs(server, "p0", Status.WORKING, sub("old", "done", T0 - 10), sub("a"))])
    # "b" started and finished between two snapshots; "old" predates the burst
    src._on_event(
        server.id,
        with_subs(server, "p0", Status.IDLE, sub("old", "done", T0 - 10), sub("a", "done"), sub("b", "done", T0 + 1)),
    )
    assert titles(notifier) == ["claude · subagents done (2)"]


def test_token_only_bridges_count_the_peak():
    src, server, notifier = _source()
    src._on_snapshot(server.id, [with_subs(server, "p0", Status.WORKING, token=(1, 1))])
    src._on_event(server.id, with_subs(server, "p0", Status.WORKING, token=(3, 4)))
    src._on_event(server.id, with_subs(server, "p0", Status.IDLE, token=(0, 4)))
    assert titles(notifier) == ["claude · subagents done (3)"]


def test_a_new_burst_alerts_again_after_the_cooldown():
    now = [1000.0]
    src, server, notifier = _source(clock=lambda: now[0])
    for _ in range(2):
        src._on_event(server.id, with_subs(server, "p0", Status.WORKING, sub("a")))
        src._on_event(server.id, with_subs(server, "p0", Status.IDLE, sub("a", "done")))
    assert len(titles(notifier)) == 1  # the second burst came within the cooldown
    now[0] += 61
    src._on_event(server.id, with_subs(server, "p0", Status.WORKING, sub("c")))
    src._on_event(server.id, with_subs(server, "p0", Status.IDLE, sub("c", "done")))
    assert len(titles(notifier)) == 2


def test_a_vanished_pane_forgets_its_burst():
    src, server, notifier = _source()
    src._on_snapshot(server.id, [with_subs(server, "p0", Status.WORKING, sub("a"))])
    src._on_snapshot(server.id, [])
    src._on_snapshot(server.id, [with_subs(server, "p0", Status.IDLE)])
    assert titles(notifier) == []


def test_title_is_localized():
    src, server, notifier = _source(lang="cs")
    src._on_snapshot(server.id, [with_subs(server, "p0", Status.WORKING, sub("a"), sub("b"), sub("c"))])
    src._on_event(server.id, with_subs(server, "p0", Status.DONE, sub("a", "done"), sub("b", "done"), sub("c", "done")))
    assert "claude · subagenti hotovi (3)" in [c[0] for c in notifier.calls]


def test_the_focused_pane_is_skipped_while_you_use_the_mac():
    src, server, notifier = _source(idle=3.0)
    src._on_snapshot(server.id, [with_subs(server, "p0", Status.WORKING, sub("a"))])
    src._on_event(server.id, replace(with_subs(server, "p0", Status.IDLE, sub("a", "done")), focused=True))
    assert titles(notifier) == []


def test_alerts_even_when_the_bridge_drives_lifecycle_events(monkeypatch):
    src, server, notifier = _source()
    monkeypatch.setattr(src, "_bridge_events", lambda server_id: True)
    src._on_snapshot(server.id, [with_subs(server, "p0", Status.WORKING, sub("a"))])
    src._on_event(server.id, with_subs(server, "p0", Status.IDLE, sub("a", "done")))
    assert titles(notifier) == ["claude · subagents done (1)"]


def test_subagents_done_parses_as_an_opt_in_flag():
    from herdeck.config import ConfigError, parse_notifications
    from herdeck.settings import _notifications_config

    for parse in (parse_notifications, _notifications_config):
        assert parse({}).subagents_done is False
        assert parse({"subagents_done": True}).subagents_done is True
        with pytest.raises(ConfigError, match="notifications.subagents_done"):
            parse({"subagents_done": "yes"})


def test_bursts_ignore_agents_without_subagents():
    bursts = SubagentBursts()
    config, server = notify_config()
    assert bursts.observe(agent(server.id, "p0", Status.IDLE)) is None
    assert bursts.tracked() == set()
