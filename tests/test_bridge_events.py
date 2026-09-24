"""Bridge lifecycle events (events.py): episodes, prompt, replay, answers."""

from __future__ import annotations

import asyncio
import contextlib
import json

import websockets

from herdeck import events as ev
from herdeck.bridge import StubHerdr, _serve_connection, _wire_panes, start_local_bridge
from herdeck.decisions import decision_revision
from herdeck.events import EventHub, episode_id, pane_episode_id, prompt_revision
from herdeck.status_since import StatusSinceTracker

PROMPT = "Allow edit?\n1. Yes\n2. No"


def raw_pane(pane_id="w1:p1", status="blocked", terminal_id="t1"):
    return {
        "pane_id": pane_id,
        "workspace_id": "w1",
        "cwd": "/tmp/api",
        "foreground_cwd": "/tmp/api",
        "agent_status": status,
        "agent": "claude",
        "terminal_id": terminal_id,
    }


class Clock:
    def __init__(self, now=1_000.0):
        self.now = now

    def __call__(self):
        return self.now


def wire(herdr: StubHerdr, since: StatusSinceTracker) -> list[dict]:
    panes = _wire_panes(herdr.panes)
    since.stamp(panes)
    EventHub.stamp(panes)
    return panes


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


def hub_for(herdr, clock=None, **kw):
    return EventHub(herdr, "srv", clock=clock or Clock(), epoch="e1", **kw)


# --- episode identity ---------------------------------------------------------


def test_episode_id_is_stable_across_a_bridge_restart(tmp_path):
    path = str(tmp_path / "since.json")
    clock = Clock(1_000.0)
    herdr = StubHerdr(panes=[raw_pane()])
    first = StatusSinceTracker(path, clock=clock)
    before = wire(herdr, first)[0]["episode_id"]
    first.close()
    clock.now = 1_300.0  # the bridge restarts five minutes later
    after = wire(herdr, StatusSinceTracker(path, clock=clock))[0]["episode_id"]
    assert before and before == after
    assert before == episode_id("w1:p1", "t1", "blocked", 1_000_000)


def test_episode_id_changes_with_a_new_episode_or_terminal():
    clock = Clock()
    herdr = StubHerdr(panes=[raw_pane()])
    since = StatusSinceTracker(clock=clock)
    first = wire(herdr, since)[0]["episode_id"]
    herdr.panes[0]["agent_status"] = "working"
    clock.now += 5
    assert wire(herdr, since)[0]["episode_id"] == ""
    herdr.panes[0]["agent_status"] = "blocked"
    clock.now += 5
    second = wire(herdr, since)[0]["episode_id"]
    assert second and second != first
    assert pane_episode_id({"pane_id": "p", "status": "idle", "status_since_ms": 1}) == ""
    # a done pane under a waiting_on token is WAITING, not a done episode
    assert pane_episode_id(
        {"pane_id": "p", "status": "done", "waiting_on": "ci", "status_since_ms": 1}
    ) == ""


# --- transitions and the prompt ---------------------------------------------


async def test_blocked_event_carries_the_sanitized_prompt_and_revision():
    herdr = StubHerdr(panes=[raw_pane()])
    herdr.detection["w1:p1"] = "\x1b[1mAllow edit?\x1b[0m\r\n1. Yes\n2. No"
    hub = hub_for(herdr)
    since = StatusSinceTracker(clock=Clock())
    hub.observe(wire(herdr, since))
    await settle()
    (event,) = hub.events()
    assert event["kind"] == "blocked"
    assert event["prompt"] == PROMPT
    assert event["prompt_revision"] == prompt_revision(PROMPT)
    assert event["at_ms"] == 1_000_000
    assert event["episode_id"] == episode_id("w1:p1", "t1", "blocked", 1_000_000)
    await hub.close()


async def test_blocked_event_goes_without_prompt_when_the_read_fails():
    class Failing(StubHerdr):
        async def read_pane(self, pane_id, source):
            raise RuntimeError("herdr down")

    herdr = Failing(panes=[raw_pane()])
    hub = hub_for(herdr)
    hub.observe(wire(herdr, StatusSinceTracker(clock=Clock())))
    await settle()
    (event,) = hub.events()
    assert event["kind"] == "blocked" and "prompt" not in event
    await hub.close()


async def test_prompt_revision_change_reemits_within_the_same_episode():
    herdr = StubHerdr(panes=[raw_pane()])
    herdr.detection["w1:p1"] = PROMPT
    mono = Clock(0.0)
    hub = hub_for(herdr, monotonic=mono, prompt_poll_s=5.0)
    since = StatusSinceTracker(clock=Clock())
    hub.observe(wire(herdr, since))
    await settle()
    assert hub.poll_prompts() == 0  # not due yet
    mono.now = 6.0
    assert hub.poll_prompts() == 1
    await settle()
    assert len(hub.events()) == 1  # same prompt: no second event
    herdr.detection["w1:p1"] = "Run tests?\n1. Yes\n2. No"
    mono.now = 12.0
    hub.poll_prompts()
    await settle()
    first, second = hub.events()
    assert second["kind"] == "blocked" and second["episode_id"] == first["episode_id"]
    assert second["prompt_revision"] != first["prompt_revision"]
    await hub.close()


async def test_leaving_an_episode_emits_unblocked_and_cleared():
    herdr = StubHerdr(panes=[raw_pane(), raw_pane("w1:p2", status="done", terminal_id="t2")])
    clock = Clock()
    hub = hub_for(herdr, clock)
    since = StatusSinceTracker(clock=clock)
    hub.observe(wire(herdr, since))
    await settle()
    assert sorted(e["kind"] for e in hub.events()) == ["blocked", "done"]
    herdr.panes[0]["agent_status"] = "working"
    herdr.panes[1]["agent_status"] = "idle"
    clock.now += 1
    hub.observe(wire(herdr, since))
    kinds = [e["kind"] for e in hub.events()]
    assert kinds[2:] == ["unblocked", "cleared"]
    assert hub.open_episode("w1:p1") is None
    await hub.close()


async def test_episode_that_ends_before_its_prompt_is_read_stays_silent():
    gate = asyncio.Event()

    class Slow(StubHerdr):
        async def read_pane(self, pane_id, source):
            await gate.wait()
            return PROMPT

    herdr = Slow(panes=[raw_pane()])
    clock = Clock()
    hub = hub_for(herdr, clock)
    since = StatusSinceTracker(clock=clock)
    hub.observe(wire(herdr, since))
    herdr.panes[0]["agent_status"] = "working"
    clock.now += 1
    hub.observe(wire(herdr, since))
    gate.set()
    await settle()
    assert hub.events() == []
    await hub.close()


# --- the ring ---------------------------------------------------------------


async def _ring_hub():
    herdr = StubHerdr(panes=[raw_pane(), raw_pane("w1:p2", status="done", terminal_id="t2")])
    herdr.detection["w1:p1"] = PROMPT
    clock = Clock()
    hub = hub_for(herdr, clock)
    since = StatusSinceTracker(clock=clock)
    hub.observe(wire(herdr, since))
    await settle()
    herdr.panes[1]["agent_status"] = "working"
    clock.now += 1
    hub.observe(wire(herdr, since))
    return hub  # seq 1 done, 2 blocked, 3 cleared (order: done is immediate)


async def test_replay_after_seq_same_epoch_is_exact():
    hub = await _ring_hub()
    frames, gap = hub._replay_frames(1, "e1")
    assert [f["seq"] for f in frames] == [2, 3] and not gap
    assert hub._replay_frames(3, "e1") == ([], False)
    await hub.close()


async def test_replay_for_another_epoch_or_evicted_cursor_sends_all_with_gap():
    hub = await _ring_hub()
    frames, gap = hub._replay_frames(2, "old-epoch")
    assert [f["seq"] for f in frames] == [1, 2, 3] and gap
    hub._ring.popleft()  # seq 1 evicted
    frames, gap = hub._replay_frames(0, "e1")
    assert [f["seq"] for f in frames] == [2, 3] and gap
    await hub.close()


async def test_replay_for_a_fresh_client_is_the_open_episodes_only():
    hub = await _ring_hub()
    frames, gap = hub._replay_frames(None, None)
    assert [(f["seq"], f["kind"]) for f in frames] == [(2, "blocked")] and not gap
    await hub.close()


async def test_ring_is_bounded_by_count_and_age(monkeypatch):
    monkeypatch.setattr(ev, "RING_MAX_EVENTS", 3)
    clock = Clock()
    hub = hub_for(StubHerdr(panes=[]), clock)
    fake = ev.Episode("x", "done", "p", "t", 1)
    for _ in range(5):
        hub._emit(fake, "done", at_ms=1)
    assert [f["seq"] for f in hub.events()] == [3, 4, 5]
    clock.now += ev.RING_MAX_AGE_MS / 1000 + 1
    hub._emit(fake, "cleared", at_ms=1)
    assert [f["seq"] for f in hub.events()] == [6]
    await hub.close()


# --- over the wire ----------------------------------------------------------


@contextlib.asynccontextmanager
async def _bridge(herdr, hub):
    clients: dict = {}

    async def handler(ws):
        await _serve_connection(
            ws, herdr, "srv", "tok", clients, "/unused.sock", events=hub
        )

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


async def _connect(url, *, after=None, epoch=None, client="deck@test"):
    ws = await websockets.connect(url, additional_headers={"Authorization": "Bearer tok"})
    first = json.loads(await ws.recv())
    assert first["type"] == "snapshot" and "events" in first["capabilities"]
    await ws.send(
        json.dumps({"type": "list", "events": {"after": after, "epoch": epoch, "client": client}})
    )
    return ws


async def _until(ws, predicate, window=3.0):
    frames = []
    while True:
        frame = json.loads(await asyncio.wait_for(ws.recv(), window))
        frames.append(frame)
        if predicate(frame):
            return frames


async def _drain(ws, window=0.2):
    out = []
    with contextlib.suppress(TimeoutError):
        while True:
            out.append(json.loads(await asyncio.wait_for(ws.recv(), window)))
    return out


async def _answer(ws, msg):
    await ws.send(json.dumps(msg))
    frames = await _until(ws, lambda f: f.get("type") == "result" and f.get("req") == msg["req"])
    return frames[-1]["data"], frames[:-1]


async def test_subscription_replays_then_streams_live_events():
    herdr = StubHerdr(panes=[raw_pane()])
    herdr.detection["w1:p1"] = PROMPT
    clock = Clock()
    hub = hub_for(herdr, clock)
    since = StatusSinceTracker(clock=clock)
    hub.observe(wire(herdr, since))
    await settle()
    async with _bridge(herdr, hub) as url:
        ws = await _connect(url, after=0, epoch="e1")
        frames = await _until(ws, lambda f: f["type"] == "event_sync")
        replayed = [f for f in frames if f["type"] == "event"]
        assert [(f["kind"], f.get("replay")) for f in replayed] == [("blocked", True)]
        assert frames[0]["type"] == "snapshot"  # the list reply comes first
        assert frames[-1] == {
            "type": "event_sync", "server_id": "srv", "epoch": "e1", "seq": 1, "gap": False
        }
        herdr.panes[0]["agent_status"] = "working"
        clock.now += 1
        hub.observe(wire(herdr, since))
        (live,) = await _until(ws, lambda f: f["type"] == "event")
        assert live["kind"] == "unblocked" and live["seq"] == 2 and "replay" not in live
        await ws.close()
    await hub.close()


async def test_old_client_without_events_field_gets_no_event_frames():
    herdr = StubHerdr(panes=[raw_pane()])
    herdr.detection["w1:p1"] = PROMPT
    hub = hub_for(herdr)
    hub.observe(wire(herdr, StatusSinceTracker(clock=Clock())))
    await settle()
    async with _bridge(herdr, hub) as url:
        async with websockets.connect(url, additional_headers={"Authorization": "Bearer tok"}) as ws:
            await ws.recv()
            await ws.send(json.dumps({"type": "list"}))
            assert {f["type"] for f in await _drain(ws)} == {"snapshot"}
    await hub.close()


async def test_answer_is_broadcast_and_a_second_answer_is_stale():
    herdr = StubHerdr(panes=[raw_pane()])
    herdr.detection["w1:p1"] = PROMPT
    clock = Clock()
    hub = hub_for(herdr, clock)
    since = StatusSinceTracker(clock=clock)
    panes = wire(herdr, since)
    hub.observe(panes)
    await settle()
    episode = panes[0]["episode_id"]
    revision = prompt_revision(PROMPT)
    async with _bridge(herdr, hub) as url:
        deck = await _connect(url, client="deck@desk")
        phone = await _connect(url, client="telegram@desk")
        await _until(deck, lambda f: f["type"] == "event_sync")
        await _until(phone, lambda f: f["type"] == "event_sync")
        base = {"pane_id": "w1:p1", "terminal_id": "t1", "episode_id": episode}
        data, _ = await _answer(
            deck,
            {"type": "act", "req": "a1", "keys": ["1"], "prompt_revision": revision, **base},
        )
        assert data == {"sent": True}
        (answered,) = await _until(phone, lambda f: f["type"] == "event")
        assert answered["kind"] == "answered" and answered["episode_id"] == episode
        assert answered["by"] == "deck@desk"
        # the same episode answered again, from the other client: refused
        data, _ = await _answer(
            phone,
            {"type": "send_text", "req": "b1", "text": "yes", "prompt_revision": revision, **base},
        )
        assert data == {"skipped": True, "message": "stale"}
        assert herdr.sent == [("w1:p1", ["1"])]
        # an unknown / old episode id: refused too
        data, _ = await _answer(
            phone, {"type": "act", "req": "b2", "keys": ["1"], **base, "episode_id": "nope"}
        )
        assert data["message"] == "stale"
        # the prompt changes in place (next question): a new revision may answer
        herdr.detection["w1:p1"] = "Run tests?\n1. Yes\n2. No"
        hub._episodes["w1:p1"].read_at = -100
        hub.poll_prompts()
        (reblocked,) = await _until(phone, lambda f: f["type"] == "event")
        assert reblocked["kind"] == "blocked"
        new_rev = reblocked["prompt_revision"]
        data, _ = await _answer(
            phone,
            {"type": "act", "req": "b3", "keys": ["1"], "prompt_revision": new_rev, **base},
        )
        assert data == {"sent": True}
        frames = await _until(deck, lambda f: f["type"] == "event" and f["seq"] == 4)
        kinds = [(f["seq"], f["kind"]) for f in frames if f["type"] == "event"]
        assert kinds[-2:] == [(3, "blocked"), (4, "answered")]
        assert frames[-1]["by"] == "telegram@desk"
        await deck.close()
        await phone.close()
    await hub.close()


async def test_legacy_answer_without_episode_is_never_refused_but_reported():
    herdr = StubHerdr(panes=[raw_pane()])
    herdr.detection["w1:p1"] = PROMPT
    hub = hub_for(herdr)
    hub.observe(wire(herdr, StatusSinceTracker(clock=Clock())))
    await settle()
    async with _bridge(herdr, hub) as url:
        watcher = await _connect(url)
        await _until(watcher, lambda f: f["type"] == "event_sync")
        async with websockets.connect(url, additional_headers={"Authorization": "Bearer tok"}) as old:
            await old.recv()
            for req in ("1", "2"):
                data, _ = await _answer(
                    old, {"type": "act", "req": req, "pane_id": "w1:p1", "keys": ["1"]}
                )
                assert data == {"sent": True}
        answered = [f for f in await _drain(watcher) if f["type"] == "event"]
        assert [(f["kind"], f["by"]) for f in answered] == [
            ("answered", "client"),
            ("answered", "client"),
        ]
        await watcher.close()
    await hub.close()


async def test_forced_act_and_skipped_answers_do_not_mark_the_episode():
    herdr = StubHerdr(panes=[raw_pane()])
    herdr.detection["w1:p1"] = PROMPT
    hub = hub_for(herdr)
    hub.observe(wire(herdr, StatusSinceTracker(clock=Clock())))
    await settle()
    assert await hub.begin_answer({"type": "act", "pane_id": "w1:p1", "guard": False}, "x") is None
    assert await hub.begin_answer({"type": "focus", "pane_id": "w1:p1"}, "x") is None
    ticket = await hub.begin_answer({"type": "send_text", "pane_id": "w1:p1"}, "x")
    # a concurrent second answer naming the episode while the first is in flight
    claimed = {"type": "act", "pane_id": "w1:p1", "episode_id": ticket.episode.id}
    assert await hub.begin_answer(claimed, "y") == "stale"
    hub.end_answer(ticket, sent=False)  # skipped by herdr: not answered
    assert not ticket.episode.answered
    assert [e["kind"] for e in hub.events()] == ["blocked"]
    await hub.close()


async def test_choose_if_blocked_accepts_a_revision_over_the_sanitized_prompt():
    from herdeck.bridge import handle_client_message

    raw = "\x1b[1mAllow edit?\x1b[0m\r\n1. Yes\n2. No"
    herdr = StubHerdr(panes=[raw_pane()])
    herdr.detection["w1:p1"] = raw
    for text in (raw, PROMPT):
        msg = {
            "type": "choose_if_blocked",
            "req": "c",
            "pane_id": "w1:p1",
            "terminal_id": "t1",
            "choice": "1",
            "decision_revision": decision_revision("srv", "w1:p1", "t1", text),
        }
        out = json.loads(await handle_client_message(herdr, "srv", json.dumps(msg)))
        assert out["data"] == {"sent": True}


async def test_local_bridge_advertises_events_and_announces_a_blocked_pane():
    herdr = StubHerdr(panes=[raw_pane()])
    herdr.detection["w1:p1"] = PROMPT
    host, port, token, (server, btask) = await start_local_bridge("unused.sock", herdr=herdr)
    try:
        async with websockets.connect(
            f"ws://{host}:{port}", additional_headers={"Authorization": f"Bearer {token}"}
        ) as ws:
            first = json.loads(await asyncio.wait_for(ws.recv(), 3))
            assert "events" in first["capabilities"] and first["panes"][0]["episode_id"]
            await ws.send(json.dumps({"type": "list", "events": {"after": 0, "epoch": None}}))
            frames = await _until(
                ws, lambda f: f["type"] == "event" and f["kind"] == "blocked", window=5
            )
            blocked = frames[-1]
            assert blocked["prompt"] == PROMPT
            assert blocked["episode_id"] == first["panes"][0]["episode_id"]
    finally:
        btask.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await btask
        server.close()
        await server.wait_closed()


# --- multi-step prompts, answer pinning, poll caps, subscribe on failure -----

Q2 = "Run tests?\n1. Yes\n2. No"


async def _answered_q1(**kw):
    herdr = StubHerdr(panes=[raw_pane()])
    herdr.detection["w1:p1"] = PROMPT
    hub = hub_for(herdr, **kw)
    hub.observe(wire(herdr, StatusSinceTracker(clock=Clock())))
    await settle()
    ep = hub.open_episode("w1:p1")
    msg = {"type": "act", "pane_id": "w1:p1", "episode_id": ep.id, "prompt_revision": ep.revision}
    ticket = await hub.begin_answer(msg, "deck")
    hub.end_answer(ticket, sent=True)
    return herdr, hub, ep, msg


async def test_second_question_of_one_episode_is_answerable_at_once():
    # The client answers Q2 before the bridge re-announced it: it still names
    # Q1's revision. The bridge re-reads, sees the prompt moved on, accepts.
    herdr, hub, ep, q1 = await _answered_q1(answer_reread_delays=(60.0,))
    herdr.detection["w1:p1"] = Q2
    ticket = await hub.begin_answer(q1, "phone")
    assert ticket not in (None, "stale")
    hub.end_answer(ticket, sent=True)
    assert ep.answered_revision == prompt_revision(Q2)
    kinds = [e["kind"] for e in hub.events()]
    assert kinds == ["blocked", "answered", "blocked", "answered"]
    # the same Q2 answered once more: nothing changed, refused
    assert await hub.begin_answer(q1, "phone") == "stale"
    await hub.close()


async def test_answer_rereads_promptly_and_announces_the_next_question():
    herdr, hub, ep, _ = await _answered_q1(answer_reread_delays=(0.0, 0.0))
    herdr.detection["w1:p1"] = Q2
    await settle()
    last = hub.events()[-1]
    assert last["kind"] == "blocked" and last["prompt"] == Q2
    await hub.close()


async def test_answer_before_the_prompt_is_known_pins_its_revision():
    gate = asyncio.Event()

    class Slow(StubHerdr):
        reads = 0

        async def read_pane(self, pane_id, source):
            self.reads += 1
            if self.reads == 1:
                await gate.wait()  # the first pre-read hangs
            return PROMPT

    herdr = Slow(panes=[raw_pane()])
    hub = hub_for(herdr, prompt_wait_s=5.0)
    hub.observe(wire(herdr, StatusSinceTracker(clock=Clock())))
    await settle()
    ticket = await hub.begin_answer({"type": "send_text", "pane_id": "w1:p1"}, "old")
    hub.end_answer(ticket, sent=True)
    assert ticket.episode.answered_revision == prompt_revision(PROMPT)
    gate.set()
    await hub.close()


async def test_prompt_polling_backs_off_and_stops_after_an_answer():
    mono = Clock(0.0)
    herdr, hub, ep, _ = await _answered_q1(monotonic=mono, answer_reread_delays=())
    ep.answered_revision = None  # an unanswered episode first
    for n in range(ev.PROMPT_BACKOFF_AFTER):
        mono.now += ev.PROMPT_POLL_S
        assert hub.poll_prompts() == 1, n
        await settle()
    mono.now += ev.PROMPT_POLL_S
    assert hub.poll_prompts() == 0  # backed off
    mono.now += ev.PROMPT_BACKOFF_S
    assert hub.poll_prompts() == 1
    await settle()
    ep.answered_revision, ep.answered_at = ep.revision, mono.now
    mono.now += ev.ANSWERED_POLL_FOR_S + ev.PROMPT_BACKOFF_S
    assert hub.poll_prompts() == 0  # answered a minute ago: no more reads
    await hub.close()


async def test_subscription_survives_a_failing_list_snapshot():
    class Flaky(StubHerdr):
        fail = False

        async def snapshot(self):
            if self.fail:
                raise RuntimeError("herdr busy")
            return await super().snapshot()

    herdr = Flaky(panes=[raw_pane()])
    herdr.detection["w1:p1"] = PROMPT
    hub = hub_for(herdr)
    hub.observe(wire(herdr, StatusSinceTracker(clock=Clock())))
    await settle()
    async with _bridge(herdr, hub) as url:
        async with websockets.connect(url, additional_headers={"Authorization": "Bearer tok"}) as ws:
            await ws.recv()
            herdr.fail = True
            await ws.send(json.dumps({"type": "list", "events": {"after": None}}))
            frames = await _until(ws, lambda f: f["type"] == "event_sync")
            assert [f["type"] for f in frames] == ["error", "event", "event_sync"]
    await hub.close()
