"""Status-since: bridge tracking/persistence, wire decode, orchestrator use."""

import json
import os
import stat

from herdeck.bridge import HerdrEvents, StubHerdr, handle_client_message
from herdeck.config import AnswerProfile, Config, ServerConfig
from herdeck.layout import order_agents
from herdeck.model import AgentKey, AgentState, Status
from herdeck.orchestrator import Orchestrator
from herdeck.protocol import decode_inbound
from herdeck.status_since import MAX_FILE_BYTES, StatusSinceTracker

T0 = 1_700_000_000.0  # unix seconds


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


def pane(pid="w1:p1", status="working", terminal="term-1", waiting_on=""):
    return {"pane_id": pid, "status": status, "terminal_id": terminal, "waiting_on": waiting_on}


# --- bridge tracker ---


def test_tracker_keeps_time_while_status_unchanged_and_resets_on_change():
    clk = Clock(T0)
    tr = StatusSinceTracker(None, clock=clk)
    assert tr.stamp([pane()])[0]["status_since_ms"] == int(T0 * 1000)
    clk.t += 30
    assert tr.stamp([pane()])[0]["status_since_ms"] == int(T0 * 1000)
    clk.t += 30
    assert tr.stamp([pane(status="blocked")])[0]["status_since_ms"] == int((T0 + 60) * 1000)


def test_tracker_restarts_clock_when_pane_is_recycled_for_a_new_terminal():
    clk = Clock(T0)
    tr = StatusSinceTracker(None, clock=clk)
    tr.stamp([pane(terminal="term-1")])
    clk.t += 10
    assert tr.stamp([pane(terminal="term-2")])[0]["status_since_ms"] == int((T0 + 10) * 1000)


def test_tracker_uses_the_runtime_derived_status_for_waiting_panes():
    # herdr working/idle flips under a waiting_on token are one WAITING status
    # on the deck, so they must not restart the timer.
    clk = Clock(T0)
    tr = StatusSinceTracker(None, clock=clk)
    tr.stamp([pane(status="working", waiting_on="ci")])
    clk.t += 10
    assert tr.stamp([pane(status="idle", waiting_on="ci")])[0]["status_since_ms"] == int(T0 * 1000)


def test_tracker_forgets_panes_that_left_the_snapshot():
    clk = Clock(T0)
    tr = StatusSinceTracker(None, clock=clk)
    tr.stamp([pane()])
    clk.t += 10
    tr.stamp([])
    assert tr.stamp([pane()])[0]["status_since_ms"] == int((T0 + 10) * 1000)


def _restart(path, clk):
    """A new tracker (= a restarted bridge) reading the same state file."""
    return StatusSinceTracker(str(path), clock=clk)


def test_tracker_persists_and_restores_unchanged_panes(tmp_path):
    path = tmp_path / "state" / "since.json"
    clk = Clock(T0)
    tr = StatusSinceTracker(str(path), clock=clk)
    tr.stamp([pane("a", "blocked", "t-a"), pane("b", "working", "t-b"), pane("c", "idle", "t-c")])
    assert os.path.exists(path)  # no running loop -> written immediately
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    clk.t += 600
    restored = _restart(path, clk).stamp(
        [
            pane("a", "blocked", "t-a"),  # unchanged -> keeps its start
            pane("b", "idle", "t-b"),  # status changed while down -> now
            pane("c", "idle", "t-c2"),  # new terminal in the recycled pane -> now
            pane("d", "working", "t-d"),  # never seen -> now
        ]
    )
    since = {p["pane_id"]: p["status_since_ms"] for p in restored}
    now_ms = int(clk.t * 1000)
    assert since == {"a": int(T0 * 1000), "b": now_ms, "c": now_ms, "d": now_ms}


def test_tracker_does_not_restore_without_terminal_identity(tmp_path):
    path = tmp_path / "since.json"
    clk = Clock(T0)
    StatusSinceTracker(str(path), clock=clk).stamp([pane(terminal="")])
    clk.t += 60
    out = _restart(path, clk).stamp([pane(terminal="")])
    assert out[0]["status_since_ms"] == int(clk.t * 1000)


def test_tracker_ignores_corrupt_oversized_or_future_state(tmp_path):
    clk = Clock(T0)
    now_ms = int(T0 * 1000)
    path = tmp_path / "since.json"
    for content in (
        "{not json",
        json.dumps({"version": 2, "panes": {}}),
        " " * (MAX_FILE_BYTES + 1),
        json.dumps(
            {
                "version": 1,
                "panes": {
                    "w1:p1": {
                        "terminal_id": "term-1",
                        "status": "working",
                        "since_ms": now_ms + 60_000,
                    }
                },
            }
        ),
        json.dumps(
            {
                "version": 1,
                "panes": {"w1:p1": {"terminal_id": "term-1", "status": "working", "since_ms": "x"}},
            }
        ),
    ):
        path.write_text(content)
        assert _restart(path, clk).stamp([pane()])[0]["status_since_ms"] == now_ms


async def test_tracker_debounces_writes_inside_an_event_loop(tmp_path):
    path = tmp_path / "since.json"
    clk = Clock(T0)
    tr = StatusSinceTracker(str(path), clock=clk, save_delay=60)
    tr.stamp([pane()])
    assert not path.exists()  # scheduled, not written yet
    tr.close()  # shutdown flushes the pending write
    assert json.loads(path.read_text())["panes"]["w1:p1"]["since_ms"] == int(T0 * 1000)


async def test_bridge_list_carries_status_since(tmp_path):
    herdr = StubHerdr(
        [{"pane_id": "w1:p1", "agent": "claude", "agent_status": "blocked", "terminal_id": "t1"}]
    )
    tr = StatusSinceTracker(None, clock=Clock(T0))
    msg = json.loads(await handle_client_message(herdr, "box", '{"type":"list"}', None, tr))
    assert "status_since" in msg["capabilities"]
    assert msg["panes"][0]["status_since_ms"] == int(T0 * 1000)


async def test_bridge_event_stream_carries_status_since():
    herdr = StubHerdr(
        [{"pane_id": "w1:p1", "agent": "claude", "agent_status": "working", "terminal_id": "t1"}]
    )
    tr = StatusSinceTracker(None, clock=Clock(T0))
    stream = HerdrEvents(herdr, poll_interval=0.01, status_since=tr).stream()
    try:
        panes = await anext(stream)
    finally:
        await stream.aclose()
    assert panes[0]["status_since_ms"] == int(T0 * 1000)


# --- wire decode ---


def _snapshot(extra: dict) -> str:
    p = {"pane_id": "p1", "agent_type": "claude", "label": "api", "status": "blocked", **extra}
    return json.dumps({"type": "snapshot", "server_id": "dev", "panes": [p]})


def test_decode_status_since_present_absent_or_invalid():
    assert decode_inbound(_snapshot({"status_since_ms": 1234})).states[0].status_since_ms == 1234
    assert decode_inbound(_snapshot({})).states[0].status_since_ms is None  # old bridge
    for bad in ("1234", -5, 0, 1.5, True, None):
        state = decode_inbound(_snapshot({"status_since_ms": bad})).states[0]
        assert state.status_since_ms is None


# --- orchestrator ---


def make_config():
    return Config(
        servers=[ServerConfig("dev", "wss://x", "t")],
        profiles={"default": AnswerProfile(["enter"], ["esc"], ["ctrl+c"], ["enter"])},
        overview_order=["dev"],
        grid=(5, 3),
    )


def agent(pid, status, since_ms=None):
    return AgentState(
        AgentKey("dev", pid),
        "default",
        pid,
        status,
        terminal_id=f"t-{pid}",
        status_since_ms=since_ms,
    )


def orch(mono, wall):
    return Orchestrator(make_config(), slots=13, clock=mono, wall_clock=wall)


def test_orchestrator_prefers_bridge_time_and_survives_a_runtime_restart():
    wall = Clock(T0 + 600)  # the agent blocked 10 minutes ago (bridge clock)
    snap = [agent("p1", Status.BLOCKED, int(T0 * 1000))]
    first = orch(Clock(100.0), wall)
    first.apply_snapshot("dev", snap)
    assert first._elapsed_text(AgentKey("dev", "p1")) == "10m"
    # Runtime restart: a fresh orchestrator, a different monotonic epoch.
    second = orch(Clock(5.0), wall)
    second.apply_snapshot("dev", snap)
    assert second._elapsed_text(AgentKey("dev", "p1")) == "10m"
    assert second.status_elapsed(AgentKey("dev", "p1")) == 600.0


def test_orchestrator_falls_back_to_first_seen_without_bridge_time():
    mono = Clock(100.0)
    o = orch(mono, Clock(T0))
    o.apply_snapshot("dev", [agent("p1", Status.WORKING)])
    mono.t += 125
    assert o._elapsed_text(AgentKey("dev", "p1")) == "2m"


def test_orchestrator_status_change_resets_time():
    mono, wall = Clock(100.0), Clock(T0 + 600)
    o = orch(mono, wall)
    o.apply_snapshot("dev", [agent("p1", Status.WORKING, int(T0 * 1000))])
    mono.t += 5
    wall.t += 5
    # Same stale stamp with a new status: still restarts at "now".
    o.apply_event("dev", agent("p1", Status.BLOCKED, int(T0 * 1000)))
    assert o.status_elapsed(AgentKey("dev", "p1")) == 0.0
    # A fresh bridge stamp for the new status is honoured.
    o.apply_event("dev", agent("p1", Status.IDLE, int((T0 + 590) * 1000)))
    assert o.status_elapsed(AgentKey("dev", "p1")) == 15.0


def test_orchestrator_clamps_bridge_clock_skew():
    mono, wall = Clock(100.0), Clock(T0)
    o = orch(mono, wall)
    key = AgentKey("dev", "p1")
    # Slightly ahead (within 5 s): clamped to "just now", never negative.
    o.apply_snapshot("dev", [agent("p1", Status.WORKING, int((T0 + 3) * 1000))])
    assert o.status_elapsed(key) == 0.0
    # Far in the future: ignored, local first-seen time is used.
    o.apply_snapshot("dev", [agent("p2", Status.BLOCKED, int((T0 + 3600) * 1000))])
    mono.t += 20
    assert o.status_elapsed(AgentKey("dev", "p2")) == 20.0
    assert o._elapsed_text(AgentKey("dev", "p2")) == "20s"


def test_orchestrator_adopts_bridge_time_arriving_later_for_same_status():
    mono, wall = Clock(100.0), Clock(T0 + 300)
    o = orch(mono, wall)
    key = AgentKey("dev", "p1")
    o.apply_snapshot("dev", [agent("p1", Status.BLOCKED)])  # old bridge
    o.apply_snapshot("dev", [agent("p1", Status.BLOCKED, int(T0 * 1000))])  # upgraded
    assert o.status_elapsed(key) == 300.0


def test_blocked_ordering_uses_bridge_time():
    wall = Clock(T0 + 1000)
    o = orch(Clock(50.0), wall)
    # p1 is seen first locally but has been blocked for less time.
    o.apply_snapshot(
        "dev",
        [
            agent("p1", Status.BLOCKED, int((T0 + 900) * 1000)),
            agent("p2", Status.BLOCKED, int((T0 + 100) * 1000)),
        ],
    )
    assert o._blocked_queue()[0] == AgentKey("dev", "p2")
    ordered = order_agents(o.agents(), ["dev"], blocked_since=o._blocked_since_map())
    assert [a.key.pane_id for a in ordered] == ["p2", "p1"]


def test_swapped_orchestrator_inherits_local_fallback_times():
    mono = Clock(100.0)
    old = orch(mono, Clock(T0))
    old.apply_snapshot("dev", [agent("p1", Status.WORKING)])
    mono.t += 90
    new = Orchestrator(make_config(), slots=13, clock=mono, wall_clock=Clock(T0))
    new.inherit_status_times(old)
    new.apply_snapshot("dev", [agent("p1", Status.WORKING)])
    assert new._elapsed_text(AgentKey("dev", "p1")) == "1m"
    # A different clock basis is never mixed in.
    other = Orchestrator(make_config(), slots=13, clock=Clock(0.0))
    other.inherit_status_times(old)
    assert other._since == {}
