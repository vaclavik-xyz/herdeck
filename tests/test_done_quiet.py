"""Quiet 'done' alerts: long runs alert at once, short runs late or never."""

from dataclasses import replace

from herdeck.deckapp.live import LiveSource
from herdeck.model import AgentKey, Status
from herdeck.notify_events import RunTracker
from tests.test_deckapp_live import RecordingNotifier, agent, notify_config

MIN = 60_000


def st(pane, status, since, server="s"):
    return replace(agent(server, pane, status), status_since_ms=since)


# --- RunTracker ----------------------------------------------------------------


def test_run_spans_blocked_and_ends_at_done():
    t = RunTracker()
    t.observe(st("p", Status.WORKING, 1000), 0)
    t.observe(st("p", Status.BLOCKED, 5000), 0)
    t.observe(st("p", Status.WORKING, 9000), 0)
    t.observe(st("p", Status.DONE, 1000 + 4 * MIN), 0)
    assert t.last(AgentKey("s", "p")) == (1000 + 4 * MIN, 4 * MIN)


def test_repeated_done_snapshot_keeps_the_run():
    t = RunTracker()
    t.observe(st("p", Status.WORKING, 0), 0)
    t.observe(st("p", Status.DONE, MIN), 0)
    t.observe(st("p", Status.DONE, MIN), 0)
    assert t.last(AgentKey("s", "p")) == (MIN, MIN)


def test_first_seen_done_is_unknown_and_idle_drops_the_run():
    t = RunTracker()
    t.observe(st("p", Status.DONE, 500), 0)
    assert t.last(AgentKey("s", "p")) == (500, None)
    t.observe(st("p", Status.IDLE, 600), 0)
    assert t.last(AgentKey("s", "p")) is None


def test_old_bridge_without_since_uses_first_sight():
    t = RunTracker()
    t.observe(agent("s", "p", Status.WORKING), 1000)
    t.observe(agent("s", "p", Status.DONE), 1000 + 2 * MIN)
    t.observe(agent("s", "p", Status.DONE), 1000 + 3 * MIN)  # no reset
    assert t.last(AgentKey("s", "p")) == (1000 + 2 * MIN, 2 * MIN)


def test_unknown_status_does_not_break_a_run():
    t = RunTracker()
    t.observe(st("p", Status.WORKING, 0), 0)
    t.observe(st("p", Status.UNKNOWN, MIN), 0)
    t.observe(st("p", Status.DONE, 5 * MIN), 0)
    assert t.last(AgentKey("s", "p")) == (5 * MIN, 5 * MIN)


# --- LiveSource gate -------------------------------------------------------------


class FakeTimers:
    def __init__(self):
        self.pending = []

    def __call__(self, delay, fn):
        entry = {"delay": delay, "fn": fn, "cancelled": False}

        class H:
            def cancel(_self):
                entry["cancelled"] = True

        self.pending.append(entry)
        return H()

    def fire_all(self):
        for e in list(self.pending):
            if not e["cancelled"]:
                e["fn"]()
        self.pending.clear()


def _live(min_work=3, delay=10, now_ms=10 * MIN):
    config, server = notify_config()
    config.notifications.done_min_work = min_work
    config.notifications.done_short_delay = delay
    timers = FakeTimers()
    src = LiveSource(config, server, notify_schedule=lambda fn: fn(), done_timer=timers)
    src._wall_ms = lambda: now_ms
    notifier = RecordingNotifier()
    src._notifier = notifier
    return src, server, notifier, timers


def test_long_run_alerts_at_once():
    src, server, notifier, timers = _live()
    src._on_snapshot(server.id, [st("p", Status.WORKING, 0, server.id)])
    src._on_event(server.id, st("p", Status.DONE, 5 * MIN, server.id))
    assert [c[0] for c in notifier.calls] == ["claude · done"]
    assert timers.pending == []


def test_short_run_is_deferred_and_fires_if_still_done():
    src, server, notifier, timers = _live(now_ms=MIN)
    src._on_snapshot(server.id, [st("p", Status.WORKING, 0, server.id)])
    src._on_event(server.id, st("p", Status.DONE, MIN, server.id))
    assert notifier.calls == []
    assert timers.pending[0]["delay"] == 600.0
    timers.fire_all()
    assert [c[0] for c in notifier.calls] == ["claude · done"]


def test_short_run_answered_in_time_never_alerts():
    src, server, notifier, timers = _live(now_ms=MIN)
    src._on_snapshot(server.id, [st("p", Status.WORKING, 0, server.id)])
    src._on_event(server.id, st("p", Status.DONE, MIN, server.id))
    src._on_event(server.id, st("p", Status.WORKING, 2 * MIN, server.id))
    timers.fire_all()
    assert notifier.calls == []


def test_second_short_done_replaces_the_first_timer():
    src, server, notifier, timers = _live(now_ms=MIN)
    src._on_snapshot(server.id, [st("p", Status.WORKING, 0, server.id)])
    src._on_event(server.id, st("p", Status.DONE, MIN, server.id))
    src._on_event(server.id, st("p", Status.WORKING, 2 * MIN, server.id))
    src._on_event(server.id, st("p", Status.DONE, 3 * MIN, server.id))
    assert [e["cancelled"] for e in timers.pending] == [True, False]
    timers.fire_all()
    assert len(notifier.calls) == 1


def test_short_run_with_zero_delay_never_alerts():
    src, server, notifier, timers = _live(delay=0)
    src._on_snapshot(server.id, [st("p", Status.WORKING, 0, server.id)])
    src._on_event(server.id, st("p", Status.DONE, MIN, server.id))
    assert notifier.calls == [] and timers.pending == []


def test_unknown_run_alerts_at_once():
    src, server, notifier, timers = _live()
    src._on_snapshot(server.id, [st("p", Status.IDLE, 0, server.id)])
    src._on_event(server.id, st("p", Status.DONE, MIN, server.id))
    assert [c[0] for c in notifier.calls] == ["claude · done"]


def test_overdue_short_done_fires_at_once_when_delay_already_passed():
    src, server, notifier, timers = _live(now_ms=60 * MIN)
    src._on_snapshot(server.id, [st("p", Status.WORKING, 0, server.id)])
    src._on_event(server.id, st("p", Status.DONE, MIN, server.id))
    assert timers.pending[0]["delay"] == 0.0


def test_rules_off_keeps_todays_behaviour():
    src, server, notifier, timers = _live(min_work=0)
    src._on_snapshot(server.id, [st("p", Status.WORKING, 0, server.id)])
    src._on_event(server.id, st("p", Status.DONE, MIN // 2, server.id))
    assert [c[0] for c in notifier.calls] == ["claude · done"]


# --- bridge lifecycle-event path -------------------------------------------------


def test_short_run_through_bridge_event_path_is_deferred(tmp_path):
    from tests.test_runtime_events import ev, make, subscribe

    src, server, _runner = make(tmp_path)
    src._config.notifications.done_min_work = 3
    src._config.notifications.done_short_delay = 10
    timers = FakeTimers()
    src._done_timer = timers
    src._wall_ms = lambda: MIN
    subscribe(src, server)
    src._on_snapshot(server.id, [st("p0", Status.WORKING, 0, server.id)])
    done = replace(st("p0", Status.DONE, MIN, server.id), episode_id="a1b2c3d4e5f60718")
    src._on_snapshot(server.id, [done])
    src._on_lifecycle(server.id, ev("done", 1))
    assert [i for i in src._notify_feed.state()["items"] if i["kind"] == "alert"] == []
    assert timers.pending[0]["delay"] == 600.0
