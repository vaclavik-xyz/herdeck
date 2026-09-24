"""Runtime side of bridge lifecycle events (deckapp/live_events.py)."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from herdeck.config import DEFAULT_PROFILES, SafetyConfig
from herdeck.connector import Connector
from herdeck.deckapp.event_cursor import EventCursorStore
from herdeck.deckapp.live import LiveSource
from herdeck.model import AgentKey, Status
from herdeck.notify import runtime_sink
from herdeck.orchestrator import binary_answer
from herdeck.protocol import EventSync, LifecycleEvent, decode_inbound
from tests.test_banner_actions import CLAUDE_PROMPT
from tests.test_deckapp_live import FakeRunner, agent, notify_config
from tests.test_notify_hygiene import FakeIdle

EP = "a1b2c3d4e5f60718"
EP2 = "0f1e2d3c4b5a6978"
REV = "1122334455667788"


class Caps:
    def __init__(self, *caps, protocol=3):
        self.capabilities = frozenset(caps)
        self.protocol = protocol


def make(tmp_path, *, events=True, store=None, remind_after=0, clock=None):
    config, server = notify_config()
    config.notifications.banner_actions = True
    config.notifications.remind_after = remind_after
    src = LiveSource(
        config,
        server,
        notify_schedule=lambda fn: fn(),
        notify_sink_factory=lambda feed, gate: runtime_sink(feed, gate, fallback=lambda *a: None),
        idle_probe=FakeIdle(0.0),
        prompt_wait_s=0.01,
        notify_clock=clock,
        event_store=store or EventCursorStore(str(tmp_path / "cursor.json")),
    )
    src.set_notify_gate(lambda: True, features=lambda: frozenset({"withdraw"}))
    src.timers = []
    src._ev_timer = lambda delay, fn: src.timers.append((delay, fn))
    runner = FakeRunner(connector=Caps("events") if events else Caps())
    src.attach_runner(runner, server.id)
    src._on_connection(server.id, True)
    return src, server, runner


def items(src):
    return [
        (i["kind"], i.get("event"), i.get("agent", {}).get("pane_id"))
        for i in src._notify_feed.state()["items"]
    ]


def alerts(src):
    return [(event, pane) for kind, event, pane in items(src) if kind == "alert"]


def blocked(sid, pane="p0", episode=EP):
    return replace(agent(sid, pane, Status.BLOCKED), episode_id=episode)


def ev(kind, seq, *, episode=EP, pane="p0", prompt=None, revision=None, at_ms=None, **kw):
    return LifecycleEvent(
        "prod",
        "e1",
        seq,
        kind,
        episode,
        pane,
        "",
        at_ms if at_ms is not None else int(time.time() * 1000),
        prompt=prompt,
        prompt_revision=revision,
        **kw,
    )


def subscribe(src, server, *replayed, seq=None):
    """The connect-time subscription: cursor asked, replay, event_sync."""
    cursor = src._events_cursor(server.id)
    for msg in replayed:
        src._on_lifecycle(server.id, msg)
    last = seq if seq is not None else max((m.seq for m in replayed), default=0)
    src._on_lifecycle(server.id, EventSync(server.id, "e1", last, False))
    return cursor


# --- the switch: bridge events OR local detection, never both ---------------


def test_events_bridge_alerts_from_events_only_once(tmp_path):
    src, server, runner = make(tmp_path)
    assert subscribe(src, server)["after"] is None  # first ever: a baseline
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    src._on_snapshot(server.id, [blocked(server.id)])  # the local diff stays quiet
    assert alerts(src) == []
    src._on_lifecycle(server.id, ev("blocked", 1, prompt=CLAUDE_PROMPT, revision=REV))
    assert alerts(src) == [("blocked", "p0")]
    # a legacy per-pane event frame does not alert a second time
    src._on_event(server.id, blocked(server.id))
    src._on_lifecycle(server.id, ev("blocked", 2, prompt=CLAUDE_PROMPT, revision=REV))
    assert alerts(src) == [("blocked", "p0")]
    # the bridge prompt is the pre-read: no local read round trip
    assert [m for m in runner.sent if m["type"] == "read"] == []
    assert src._preread[AgentKey(server.id, "p0")] == CLAUDE_PROMPT
    assert src._block_episode[AgentKey(server.id, "p0")] == EP


def test_old_bridge_keeps_local_detection(tmp_path):
    src, server, runner = make(tmp_path, events=False)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])
    assert alerts(src) == [("blocked", "p0")]
    assert [m["type"] for m in runner.sent].count("read") == 1  # local pre-read


def test_blocked_event_without_prompt_falls_back_to_a_local_read(tmp_path):
    src, server, runner = make(tmp_path)
    subscribe(src, server)
    src._on_snapshot(server.id, [blocked(server.id)])
    assert [m for m in runner.sent if m["type"] == "read"] == []
    src._on_lifecycle(server.id, ev("blocked", 1))
    assert [m["pane_id"] for m in runner.sent if m["type"] == "read"] == ["p0"]


def test_done_and_cleared_alert_and_withdraw(tmp_path):
    src, server, _runner = make(tmp_path)
    subscribe(src, server)
    src._on_snapshot(server.id, [replace(agent(server.id, "p0", Status.DONE), episode_id=EP)])
    src._on_lifecycle(server.id, ev("done", 1))
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    src._on_lifecycle(server.id, ev("cleared", 2))
    assert items(src) == [("alert", "done", "p0"), ("withdraw", None, "p0")]


# --- answered elsewhere -----------------------------------------------------


def test_answered_elsewhere_withdraws_and_makes_the_banner_stale(tmp_path):
    from herdeck.decisions import decision_revision
    from herdeck.deckapp import DeckApp
    from tests.test_deckapp_live import StubIcons

    src, server, runner = make(tmp_path)
    app = DeckApp(src, serve=False, icon_provider=StubIcons())
    subscribe(src, server)
    src._on_snapshot(server.id, [blocked(server.id)])
    src._on_lifecycle(server.id, ev("blocked", 1, prompt=CLAUDE_PROMPT, revision=REV))
    key = AgentKey(server.id, "p0")
    src._on_lifecycle(server.id, ev("answered", 2, by="telegram@phone"))
    assert items(src) == [("alert", "blocked", "p0"), ("withdraw", None, "p0")]
    sig = binary_answer(CLAUDE_PROMPT, DEFAULT_PROFILES["claude"], SafetyConfig()).sig
    assert src.answer_agent(key, EP, choice="approve", sig=sig) == "stale"
    detail = src.card_detail(server.id, "p0")
    assert detail["options"] == [] and detail["prompt"]
    # The card itself does not refuse: the bridge decides (it may already
    # show the next question) and answers a repeat with "stale".
    sent = []
    src._card_send = lambda cmd: sent.append(cmd) or {"ok": True, "code": "pending", "message": ""}
    rev = decision_revision(server.id, "p0", "", CLAUDE_PROMPT)
    assert src.card_answer(server.id, "p0", "1", rev)["code"] == "pending"
    assert [c.kind for c in sent] == ["choose_if_blocked"]
    app.close()


def test_a_new_prompt_in_an_answered_episode_alerts_again(tmp_path):
    now = [100.0]
    src, server, _runner = make(tmp_path, clock=lambda: now[0])
    subscribe(src, server)
    src._on_snapshot(server.id, [blocked(server.id)])
    src._on_lifecycle(server.id, ev("blocked", 1, prompt=CLAUDE_PROMPT, revision=REV))
    src._on_lifecycle(server.id, ev("answered", 2, by="deck@desk"))
    now[0] += 30  # past the blocked flap guard
    src._on_lifecycle(server.id, ev("blocked", 3, prompt="Next?\n1. Yes\n", revision="99" * 8))
    assert alerts(src) == [("blocked", "p0"), ("blocked", "p0")]
    assert EP not in src._answered_episodes


def test_bridge_stale_refusal_maps_to_a_stale_card_outcome():
    from herdeck.deckapp.agent_card import result_outcome

    assert result_outcome({"skipped": True, "message": "stale"})["code"] == "stale"


def test_answers_are_stamped_with_the_bridge_episode(tmp_path):
    src, server, runner = make(tmp_path)
    subscribe(src, server)
    src._on_snapshot(server.id, [blocked(server.id)])
    src._on_lifecycle(server.id, ev("blocked", 1, prompt=CLAUDE_PROMPT, revision=REV))
    wrapped = src._runners[server.id]
    wrapped.send({"type": "act", "req": "r", "pane_id": "p0", "keys": ["1"], "guard": True})
    wrapped.send({"type": "act", "req": "s", "pane_id": "p0", "keys": ["esc"], "guard": False})
    wrapped.send({"type": "focus", "req": "f", "pane_id": "p0"})
    act, stop, focus = runner.sent[-3:]
    assert act["episode_id"] == EP and act["prompt_revision"] == REV
    assert "episode_id" not in stop and "episode_id" not in focus
    assert wrapped.connector is runner.connector  # the wrapper is transparent


# --- replay and restarts ------------------------------------------------------


def test_runtime_restart_is_replayed_without_re_alerting(tmp_path):
    path = str(tmp_path / "cursor.json")
    first, server, _ = make(tmp_path, store=EventCursorStore(path))
    subscribe(first, server)
    first._on_snapshot(server.id, [blocked(server.id)])
    first._on_lifecycle(server.id, ev("blocked", 1, prompt=CLAUDE_PROMPT, revision=REV))
    assert alerts(first) == [("blocked", "p0")]

    second, server, _ = make(tmp_path, store=EventCursorStore(path))
    second._on_snapshot(
        server.id,
        [blocked(server.id), replace(agent(server.id, "p1", Status.DONE), episode_id=EP2)],
    )
    cursor = second._events_cursor(server.id)
    assert cursor["after"] == 1 and cursor["epoch"] == "e1" and cursor["client"]
    # the bridge restarted meanwhile (gap): it repeats what it knows, and a
    # done that happened while this runtime was down is new
    for msg in (
        ev("blocked", 1, prompt=CLAUDE_PROMPT, revision=REV, replay=True),
        ev("done", 2, episode=EP2, pane="p1", replay=True),
    ):
        second._on_lifecycle(server.id, msg)
    second._on_lifecycle(server.id, EventSync(server.id, "e1", 2, True))
    assert alerts(second) == [("done", "p1")]
    assert second._ev_store.cursor(server.id) == ("e1", 2)
    saved = json.loads((tmp_path / "cursor.json").read_text())
    assert set(saved["servers"]["prod"]["known"]) == {EP, EP2}


def test_replayed_episode_that_already_ended_does_not_alert(tmp_path):
    src, server, _ = make(tmp_path)
    src._ev_store.note(server.id, epoch="e1", seq=0)  # a returning runtime
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    subscribe(
        src,
        server,
        ev("blocked", 1, prompt=CLAUDE_PROMPT, revision=REV, replay=True),
        ev("answered", 2, by="telegram@phone", replay=True),
        ev("unblocked", 3, replay=True),
    )
    assert alerts(src) == []


def test_first_subscription_is_a_silent_baseline(tmp_path):
    src, server, _ = make(tmp_path)
    src._on_snapshot(server.id, [blocked(server.id)])
    subscribe(src, server, ev("blocked", 7, prompt=CLAUDE_PROMPT, revision=REV, replay=True))
    assert alerts(src) == []
    assert src._preread[AgentKey(server.id, "p0")] == CLAUDE_PROMPT  # state still applied


def test_reminders_count_from_bridge_time(tmp_path):
    now = [1000.0]
    src, server, _ = make(tmp_path, remind_after=10, clock=lambda: now[0])
    subscribe(src, server)
    src._on_snapshot(server.id, [blocked(server.id)])
    at_ms = int((time.time() - 25 * 60) * 1000)  # blocked 25 minutes ago
    src._on_lifecycle(server.id, ev("blocked", 1, prompt=CLAUDE_PROMPT, at_ms=at_ms))
    key = AgentKey(server.id, "p0")
    episode, _since, sent = src._reminders[key]
    assert episode == EP and sent == 2  # the 10 and 20 minute reminders are past
    assert src.check_reminders() == 0
    now[0] += 5 * 60 + 1  # 30 minutes after the block
    assert src.check_reminders() == 1


# --- wire ---------------------------------------------------------------------


def test_decode_lifecycle_frames():
    frame = {
        "type": "event", "server_id": "b", "epoch": "e1", "seq": 3, "kind": "blocked",
        "episode_id": EP, "pane_id": "p0", "terminal_id": "t", "at_ms": 5,
        "prompt": "Allow?", "prompt_revision": REV, "replay": True,
    }
    msg = decode_inbound(json.dumps(frame))
    assert isinstance(msg, LifecycleEvent)
    assert (msg.kind, msg.prompt, msg.prompt_revision, msg.replay) == ("blocked", "Allow?", REV, True)
    sync = decode_inbound(
        json.dumps({"type": "event_sync", "server_id": "b", "epoch": "e1", "seq": 3, "gap": True})
    )
    assert sync == EventSync("b", "e1", 3, True)
    with pytest.raises(ValueError):
        decode_inbound(json.dumps({**frame, "kind": "exploded"}))
    with pytest.raises(ValueError):
        decode_inbound(json.dumps({**frame, "episode_id": "not hex"}))


def test_connector_subscribes_only_when_the_consumer_wants_events():
    server = SimpleNamespace(id="prod", url="ws://x", token="t", backend="herdr")
    plain = Connector(server, lambda *a: None, lambda *a: None, lambda *a: None)
    assert plain._resync_message() == {"type": "list"}
    got = []
    wired = Connector(
        server,
        lambda *a: None,
        lambda *a: None,
        lambda *a: None,
        on_lifecycle=lambda sid, msg: got.append((sid, msg)),
        events_cursor=lambda sid: {"after": 4, "epoch": "e1", "client": "c"},
    )
    assert wired._resync_message() == {
        "type": "list", "events": {"after": 4, "epoch": "e1", "client": "c"}
    }
    frame = {
        "type": "event", "server_id": "bridge-label", "epoch": "e1", "seq": 5,
        "kind": "answered", "episode_id": EP, "pane_id": "p0", "at_ms": 1, "by": "x",
    }
    wired._dispatch(json.dumps(frame))
    assert got[0][0] == "prod" and got[0][1].server_id == "prod" and got[0][1].by == "x"


def test_event_cursor_store_survives_a_corrupt_file(tmp_path):
    path = tmp_path / "cursor.json"
    path.write_text("{not json")
    store = EventCursorStore(str(path))
    assert store.cursor("prod") == (None, None)
    assert store.note("prod", epoch="e1", seq=3, episodes=(EP,)) == {EP}
    assert store.note("prod", episodes=(EP,)) == set()
    assert EventCursorStore(str(path)).cursor("prod") == ("e1", 3)
    assert oct(path.stat().st_mode & 0o777) == "0o600"


async def test_real_bridge_frames_drive_the_runtime(tmp_path):
    """Bridge EventHub frames, through the wire decoder, into a LiveSource."""
    import asyncio

    from herdeck.bridge import StubHerdr, _snapshot_message, _wire_panes
    from herdeck.events import EventHub
    from herdeck.protocol import Snapshot, encode
    from herdeck.status_since import StatusSinceTracker

    raw = {
        "pane_id": "p0", "workspace_id": "w", "cwd": "/x/api", "agent_status": "blocked",
        "agent": "claude", "terminal_id": "t1",
    }
    herdr = StubHerdr(panes=[raw])
    herdr.detection["p0"] = CLAUDE_PROMPT
    hub = EventHub(herdr, "bridge-label")
    panes = _wire_panes(herdr.panes)
    StatusSinceTracker().stamp(panes)
    hub.stamp(panes)
    hub.observe(panes)
    for _ in range(5):
        await asyncio.sleep(0)
    snap = decode_inbound(encode(_snapshot_message("prod", panes, ("events",))))
    assert isinstance(snap, Snapshot) and snap.states[0].episode_id == panes[0]["episode_id"]
    src, server, runner = make(tmp_path)
    subscribe(src, server)
    src._on_snapshot(server.id, snap.states)
    (frame,) = hub.events()
    msg = decode_inbound(encode(frame))
    src._on_lifecycle(server.id, replace(msg, server_id=server.id))
    assert alerts(src) == [("blocked", "p0")]
    assert src._preread[AgentKey(server.id, "p0")] == CLAUDE_PROMPT
    await hub.close()


# --- a subscription that never syncs ------------------------------------------


def test_no_event_sync_falls_back_to_local_alerts_and_resubscribes(tmp_path):
    src, server, runner = make(tmp_path)
    src._events_cursor(server.id)  # connect-time subscribe, no event_sync follows
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    src._on_snapshot(server.id, [blocked(server.id)])
    assert alerts(src) == []  # within the grace period: the events path owns alerts
    (delay, watchdog), = src.timers
    assert delay == 10.0
    watchdog()
    resubscribe = runner.sent[-1]
    assert resubscribe["type"] == "list" and "events" in resubscribe
    assert len(src.timers) == 2  # and it keeps trying
    # local detection runs now
    src._on_snapshot(server.id, [agent(server.id, "p1", Status.WORKING), blocked(server.id)])
    src._on_snapshot(
        server.id, [blocked(server.id, "p1", episode=EP2), blocked(server.id)]
    )
    assert alerts(src) == [("blocked", "p1")]
    # the bridge answers the re-subscribe: its replay repeats p1's episode,
    # which the local path already alerted -> no second alert
    src._on_lifecycle(server.id, ev("blocked", 1, episode=EP2, pane="p1", replay=True))
    src._on_lifecycle(server.id, EventSync(server.id, "e1", 1, False))
    assert src._bridge_events(server.id)
    assert alerts(src) == [("blocked", "p1")]


def test_watchdog_of_an_old_connection_does_nothing(tmp_path):
    src, server, runner = make(tmp_path)
    src._events_cursor(server.id)
    (_delay, watchdog), = src.timers
    src._on_connection(server.id, False)
    src._on_connection(server.id, True)
    src._events_cursor(server.id)
    sent = len(runner.sent)
    watchdog()  # the first connection's timer
    assert len(runner.sent) == sent and src._bridge_events(server.id)


# --- multi-step prompt on the runtime side ------------------------------------


def test_next_question_after_an_answer_offers_options_again(tmp_path):
    from herdeck.deckapp import DeckApp
    from tests.test_deckapp_live import StubIcons

    now = [100.0]
    src, server, _runner = make(tmp_path, clock=lambda: now[0])
    app = DeckApp(src, serve=False, icon_provider=StubIcons())
    subscribe(src, server)
    src._on_snapshot(server.id, [blocked(server.id)])
    src._on_lifecycle(server.id, ev("blocked", 1, prompt=CLAUDE_PROMPT, revision=REV))
    src._on_lifecycle(server.id, ev("answered", 2, by="deck@desk"))
    assert src.card_detail(server.id, "p0")["options"] == []
    q2 = CLAUDE_PROMPT.replace("app.py", "b.py")
    src._on_lifecycle(server.id, ev("blocked", 3, prompt=q2, revision="99" * 8))
    detail = src.card_detail(server.id, "p0")
    assert detail["options"] and "b.py" in detail["prompt"]
    stamped = src._stamp_answer(server.id, {"type": "act", "pane_id": "p0", "keys": ["1"]})
    assert stamped["prompt_revision"] == "99" * 8
    app.close()


# --- per-runtime cursor file --------------------------------------------------


def test_each_runtime_keeps_its_own_cursor_file(monkeypatch):
    from herdeck.deckapp import event_cursor, live_events

    monkeypatch.setattr(live_events.sys, "argv", ["/usr/bin/herdeck-web"])
    assert live_events.runtime_tag("work") == "herdeck-web-work"
    monkeypatch.setattr(live_events.sys, "argv", [""])
    assert live_events.runtime_tag("Default Profile") == "herdeck-default-profile"
    monkeypatch.undo()
    assert event_cursor.default_path("a") != event_cursor.default_path("b")
