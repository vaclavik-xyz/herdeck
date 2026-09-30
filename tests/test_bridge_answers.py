"""execute_answer: bridge-side answers share the client answer guard."""

from __future__ import annotations

import asyncio

from herdeck.bridge import StubHerdr, _wire_panes
from herdeck.bridge_answers import answer_frame, execute_answer
from herdeck.events import EventHub
from herdeck.status_since import StatusSinceTracker

PROMPT = "Allow edit?\n1. Yes\n2. No"


def raw_pane(status="blocked"):
    return {
        "pane_id": "w1:p1",
        "workspace_id": "w1",
        "cwd": "/tmp/api",
        "foreground_cwd": "/tmp/api",
        "agent_status": status,
        "agent": "claude",
        "terminal_id": "t1",
    }


async def open_episode():
    herdr = StubHerdr(panes=[raw_pane()])
    herdr.detection["w1:p1"] = PROMPT
    since = StatusSinceTracker(None)
    hub = EventHub(herdr, "srv", clock=lambda: 1000.0, epoch="e1")
    panes = _wire_panes(herdr.panes)
    since.stamp(panes)
    EventHub.stamp(panes)
    hub.observe(panes)
    for _ in range(5):
        await asyncio.sleep(0)
    return herdr, hub, panes[0]["episode_id"]


def act(episode):
    return {"type": "act", "req": "r", "pane_id": "w1:p1", "keys": ["1"], "episode_id": episode}


async def test_execute_answer_sends_keys_and_marks_answered_by_telegram():
    herdr, hub, ep = await open_episode()
    data = await execute_answer(herdr, "srv", act(ep), "telegram", events=hub)
    assert data == {"sent": True}
    assert herdr.sent == [("w1:p1", ["1"])]
    answered = [e for e in hub.events() if e["kind"] == "answered"]
    assert len(answered) == 1
    assert answered[0]["by"] == "telegram" and answered[0]["via"] == "telegram"
    await hub.close()


async def test_second_answer_to_the_same_episode_is_stale():
    herdr, hub, ep = await open_episode()
    assert (await execute_answer(herdr, "srv", act(ep), "telegram", events=hub)) == {"sent": True}
    again = await execute_answer(herdr, "srv", act(ep), "telegram", events=hub)
    assert again == {"skipped": True, "message": "stale"}
    assert len(herdr.sent) == 1
    await hub.close()


async def test_client_answer_then_execute_answer_is_stale_and_vice_versa():
    import json

    herdr, hub, ep = await open_episode()
    msg = act(ep)
    out = await answer_frame(herdr, "srv", json.dumps(msg), msg, "deck", events=hub)
    assert json.loads(out)["data"] == {"sent": True}
    assert await execute_answer(herdr, "srv", act(ep), "telegram", events=hub) == {
        "skipped": True,
        "message": "stale",
    }
    # client answer after a bridge-side one: stale frame keeps the req
    herdr2, hub2, ep2 = await open_episode()
    await execute_answer(herdr2, "srv", act(ep2), "telegram", events=hub2)
    msg2 = act(ep2)
    out2 = json.loads(
        await answer_frame(herdr2, "srv", json.dumps(msg2), msg2, "deck", events=hub2)
    )
    assert out2 == {"type": "result", "req": "r", "data": {"skipped": True, "message": "stale"}}
    assert len(herdr2.sent) == 1
    await hub.close()
    await hub2.close()


async def test_explicit_via_is_kept_and_errors_are_returned():
    herdr, hub, ep = await open_episode()
    msg = {**act(ep), "via": "phone"}
    assert await execute_answer(herdr, "srv", msg, "telegram", events=hub) == {"sent": True}
    answered = [e for e in hub.events() if e["kind"] == "answered"][0]
    assert answered["by"] == "telegram" and answered["via"] == "phone"
    await hub.close()
    herdr2, hub2, ep2 = await open_episode()
    bad = {"type": "act", "req": "r", "pane_id": "w1:p1", "episode_id": ep2}  # no keys
    out = await execute_answer(herdr2, "srv", bad, "telegram", events=hub2)
    assert "error" in out
    await hub2.close()
