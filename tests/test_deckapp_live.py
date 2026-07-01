"""Tests for the herdeck.deckapp LiveSource (slice 4: live source + mock/live switch).

The live path is exercised with a FAKE connector/runner — no real bridge, no
network, no asyncio loop. We drive the connector callbacks directly (the seam the
real Connector uses) and capture sends through a fake runner. Covers:

* snapshot / event -> orchestrator (the deck reflects live state),
* press -> Command -> connector.send (orchestrator translates the press),
* offline handling (connected:false + reuse of the OFFLINE overview panel),
* the mock/live switch by config + token presence,
* the bridge token never leaking into /state, /health or the source surface.
"""

import io
import json

from PIL import Image

from herdeck.config import DEFAULT_PROFILES, Config, ServerConfig
from herdeck.deckapp import DeckApp
from herdeck.deckapp.live import LiveSource, build_live_source
from herdeck.deckapp.server import create_app, create_live_app, select_live
from herdeck.deckapp.sinks import RenderFrame  # noqa: F401
from herdeck.model import AgentKey, AgentState, Status
from herdeck.orchestrator import Orchestrator

SECRET = "super-secret-bridge-token-xyz"


class StubIcons:
    """Deterministic, content-keyed icon provider (mirrors the slice-1 stub); it
    never touches the network or Pillow's font stack beyond a 4px fill."""

    def render_tile_bytes(self, tile):
        sig = f"{tile.index}|{tile.label}|{tile.color}|{tile.status_text}"
        buf = io.BytesIO()
        c = sum(sig.encode()) % 256
        Image.new("RGB", (4, 4), (c, c, c)).save(buf, "PNG")
        return buf.getvalue()


class RecordingSink:
    def __init__(self):
        self.frames = []
        self.closed = False

    def deliver(self, frame):
        self.frames.append(frame)

    def close(self):
        self.closed = True


class SpinIcons:
    """Like StubIcons but its bytes depend on the spinner phase, so a tick that
    advances a working tile's phase changes that tile's PNG (and only that one)."""

    def render_tile_bytes(self, tile):
        sig = f"{tile.index}|{tile.status_text}|{tile.spinner}"
        buf = io.BytesIO()
        c = sum(sig.encode()) % 256
        Image.new("RGB", (4, 4), (c, c, c)).save(buf, "PNG")
        return buf.getvalue()


def make_live_icons(icons, *, serve=False, tick_interval=0.0):
    config, server = live_config()
    src = LiveSource(config, server)
    src.attach_runner(FakeRunner())
    app = DeckApp(src, serve=serve, icon_provider=icons, tick_interval=tick_interval)
    return app, src, server


class FakeRunner:
    """Captures the wire messages a press would send to the bridge, synchronously
    (no asyncio). Each send is recorded exactly once — there is no retry."""

    def __init__(self, connector=None):
        self.connector = connector
        self.sent: list[dict] = []
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def send(self, msg):
        self.sent.append(msg)

    def close(self):
        self.closed = True
        if self.connector is not None and hasattr(self.connector, "stop"):
            self.connector.stop()


class FakeConnector:
    """Records the callbacks wired by build_live_source so a test can deliver a
    snapshot/event/connection exactly as the real Connector would, with no WS."""

    instances: list["FakeConnector"] = []

    def __init__(self, server, on_snapshot, on_event, on_connection, **kw):
        self.server = server
        self.on_snapshot = on_snapshot
        self.on_event = on_event
        self.on_connection = on_connection
        self.stopped = False
        FakeConnector.instances.append(self)

    async def run(self):
        # Real ConnectorRunner awaits this on its loop; return at once so the
        # background thread exits immediately and never touches the network.
        return

    async def send(self, msg):
        return

    def stop(self):
        self.stopped = True


def live_config(token=SECRET):
    server = ServerConfig(id="prod", url="ws://bridge.local:8765", token=token)
    config = Config(
        servers=[server],
        profiles=dict(DEFAULT_PROFILES),
        overview_order=["prod"],
        grid=(5, 3),
    )
    return config, server


def agent(sid, pane, status, agent_type="claude", label=None):
    return AgentState(
        AgentKey(sid, pane), agent_type, label or pane, status, repo=pane, branch="main"
    )


def make_live(token=SECRET):
    config, server = live_config(token)
    src = LiveSource(config, server)
    runner = FakeRunner()
    src.attach_runner(runner)
    app = DeckApp(src, serve=False, icon_provider=StubIcons())
    return app, src, server, runner


# --- snapshot / event -> orchestrator ---------------------------------------


def test_snapshot_feeds_orchestrator_and_state():
    app, src, server, _ = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(
        server.id,
        [
            agent(server.id, "p0", Status.WORKING),
            agent(server.id, "p1", Status.BLOCKED),
            agent(server.id, "p2", Status.IDLE),
        ],
    )
    app.refresh()
    st = app._state()
    assert st["source"] == "live"
    assert st["connected"] is True
    assert st["summary"]["agents"] == 3
    assert st["summary"]["blocked"] == 1
    assert st["summary"]["working"] == 1
    assert st["summary"]["idle"] == 1


def test_event_updates_a_single_agent():
    app, src, server, _ = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(
        server.id, [agent(server.id, "p0", Status.WORKING), agent(server.id, "p1", Status.IDLE)]
    )
    src._on_event(server.id, agent(server.id, "p0", Status.BLOCKED))
    app.refresh()
    summ = app._state()["summary"]
    assert summ["agents"] == 2
    assert summ["blocked"] == 1
    assert summ["working"] == 0


def test_snapshot_replaces_previous_state():
    app, src, server, _ = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, f"p{i}", Status.WORKING) for i in range(4)])
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.IDLE)])
    app.refresh()
    assert app._state()["summary"]["agents"] == 1


# --- press -> Command -> connector.send -------------------------------------


def test_press_agent_sends_focus_then_read():
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    app.refresh()
    app.press(0)  # drill into the only agent tile
    assert [m["type"] for m in runner.sent] == ["focus", "read"]
    assert all(m["pane_id"] == "p0" for m in runner.sent)


def test_drill_macro_press_sends_text_command():
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    app.refresh()
    app.press(0)  # overview -> drill (focus + read)
    runner.sent.clear()
    app.press(0)  # drill -> first quick-send macro
    assert len(runner.sent) == 1
    msg = runner.sent[0]
    assert msg["type"] == "send_text"
    assert msg["pane_id"] == "p0"
    assert msg["text"]  # the macro body


def test_press_translates_via_orchestrator_on_press():
    # No agents -> pressing the launcher tile opens the launcher; no bridge send.
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    app.refresh()
    app.press(12)  # the reserved launcher tile (slots-1)
    assert runner.sent == []


def test_press_is_noop_without_a_runner():
    # A press before the runner is attached must never raise (defensive wiring).
    config, server = live_config()
    src = LiveSource(config, server)
    orch = Orchestrator(src.config, slots=13)
    src.attach(orch)
    src.press(0)  # no runner -> silently ignored, no crash


# --- live updates re-render the deck (no manual poll) -----------------------


def test_snapshot_callback_refreshes_deck_without_manual_refresh():
    app, src, server, _ = make_live()
    v0 = app._state()["version"]
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    # No manual app.refresh(): the live callback must re-render the DeckApp itself,
    # otherwise /state keeps serving stale tile versions.
    st = app._state()
    assert st["version"] > v0
    assert st["summary"]["agents"] == 1
    assert st["connected"] is True


def test_event_callback_refreshes_deck_without_manual_refresh():
    app, src, server, _ = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    v1 = app._state()["version"]
    src._on_event(server.id, agent(server.id, "p0", Status.BLOCKED))
    st = app._state()
    assert st["version"] > v1
    assert st["summary"]["blocked"] == 1


def test_connection_drop_callback_refreshes_deck():
    app, src, server, _ = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    assert app._state()["connected"] is True
    src._on_connection(server.id, False)  # no manual refresh
    assert app._state()["connected"] is False


# --- read result -> detection enables the blocked-approve drill --------------


def test_read_result_enables_approve_on_blocked_agent():
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])
    app.press(0)  # drill into the blocked agent -> focus + read
    read = [m for m in runner.sent if m["type"] == "read"][-1]
    runner.sent.clear()
    # the bridge answers the read with the prompt text
    src._on_result(read["req"], {"text": "1. Approve\n2. Deny", "pane_id": "p0"})
    app.press(0)  # the first option is now actionable
    assert len(runner.sent) == 1
    msg = runner.sent[0]
    assert msg["type"] == "act"
    assert msg["pane_id"] == "p0"
    assert msg["keys"] == ["1", "enter"]
    assert msg["guard"] is True  # act_if_blocked -> guarded send


def test_stale_read_result_is_ignored():
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])
    app.press(0)  # drill -> focus + read
    runner.sent.clear()
    # a result for a request we never issued must not populate detection
    src._on_result("r999", {"text": "1. Approve", "pane_id": "p0"})
    app.press(0)  # no options -> blank tile -> nothing sent
    assert runner.sent == []


# --- result-driven relist + stale-prompt invalidation (mirrors App) ---------


def test_non_read_result_triggers_relist():
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])
    runner.sent.clear()
    src._on_result("r5", {"ok": True})  # no "text" -> an act/send ack
    assert runner.sent == [{"type": "list"}]  # resync this server, like App


def _drill_with_detection(app, src, server, runner):
    app.press(0)  # drill into p0 -> focus + read
    read = [m for m in runner.sent if m["type"] == "read"][-1]
    src._on_result(read["req"], {"text": "1. Approve\n2. Deny", "pane_id": "p0"})
    runner.sent.clear()


def test_event_unblocking_drilled_pane_clears_stale_prompt():
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])
    _drill_with_detection(app, src, server, runner)
    # the agent leaves BLOCKED then re-blocks (possibly a new prompt): the old
    # options must have been dropped on unblock, so nothing is actionable until a
    # fresh read (had the prompt lingered, press 0 would still send an act).
    src._on_event(server.id, agent(server.id, "p0", Status.IDLE))
    src._on_event(server.id, agent(server.id, "p0", Status.BLOCKED))
    runner.sent.clear()  # ignore the reblock's fresh current-episode read; isolate the press
    app.press(0)
    assert runner.sent == []  # no stale act: options were dropped on unblock, none seeded yet


def test_snapshot_unblocking_drilled_pane_clears_stale_prompt():
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])
    _drill_with_detection(app, src, server, runner)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.IDLE)])
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])
    runner.sent.clear()  # ignore the reblock's fresh current-episode read; isolate the press
    app.press(0)
    assert runner.sent == []  # no stale act: options were dropped on unblock, none seeded yet


def test_cosmetic_change_keeps_detection_while_blocked():
    # Regression ("shows then disappears"): a still-blocked drilled pane whose
    # cosmetic fields flap between snapshots — e.g. the branch label appearing and
    # vanishing as worktree.list momentarily fails in the bridge (label stands in
    # for branch here) — must NOT clear the shown prompt.
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])
    _drill_with_detection(app, src, server, runner)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED, label="changed")])
    app.press(0)  # options stay live -> first option still acts
    assert len(runner.sent) == 1
    assert runner.sent[0]["type"] == "act"
    assert runner.sent[0]["keys"] == ["1", "enter"]


def test_snapshot_not_changing_drilled_pane_keeps_detection():
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])
    _drill_with_detection(app, src, server, runner)
    # an unrelated agent appears; the drilled pane is unchanged -> options stay live
    src._on_snapshot(
        server.id,
        [agent(server.id, "p0", Status.BLOCKED), agent(server.id, "p1", Status.WORKING)],
    )
    app.press(0)
    assert len(runner.sent) == 1
    assert runner.sent[0]["type"] == "act"
    assert runner.sent[0]["keys"] == ["1", "enter"]


# --- pre-read: warm the blocked prompt in the background for an instant drill ----


def _reads(runner):
    return [m for m in runner.sent if m["type"] == "read"]


def test_blocked_pane_triggers_background_preread():
    # A pane entering BLOCKED is read in the background so its prompt is cached
    # before the user ever drills it (the drill then paints options in one frame).
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    runner.sent.clear()
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])
    reads = _reads(runner)
    assert len(reads) == 1
    assert reads[0]["pane_id"] == "p0"
    assert reads[0]["source"] == "detection"


def test_preread_issued_once_while_blocked():
    # The background read fires once per block episode, not on every snapshot poll.
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])
    runner.sent.clear()
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])
    assert _reads(runner) == []


def test_blocked_drill_not_double_read_when_press_registered_it():
    # Pressing a BLOCKED pane fires (and registers) the drill's own read, so a later
    # snapshot with the pane still blocked must not issue a second background read.
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])
    app.press(0)  # drill -> the drill read registers the episode
    runner.sent.clear()
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])  # still blocked
    assert _reads(runner) == []  # its current-episode read is already out


def test_drilled_working_pane_that_blocks_gets_fresh_read_and_options():
    # A pane drilled while WORKING that then blocks (still drilled) had only a
    # pre-block read, which is rejected. It must get a fresh current-episode read so
    # the block prompt's options appear without the user backing out and re-drilling.
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    app.press(0)  # drill working -> pre-block read (not registered for the episode)
    runner.sent.clear()
    src._on_event(server.id, agent(server.id, "p0", Status.BLOCKED))  # blocks while drilled
    fresh = _reads(runner)
    assert len(fresh) == 1 and fresh[0]["pane_id"] == "p0"
    src._on_result(fresh[0]["req"], {"text": "1. Approve\n2. Deny", "pane_id": "p0"})
    assert app._orch._detection == "1. Approve\n2. Deny"  # options now surface on the drill
    runner.sent.clear()
    app.press(0)  # the first option is actionable
    assert runner.sent and runner.sent[0]["type"] == "act" and runner.sent[0]["keys"] == ["1", "enter"]


def test_preread_makes_drill_instant():
    # With the prompt cached, drilling shows actionable options WITHOUT waiting for
    # a post-drill read round-trip: the first option acts immediately.
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])
    preread = _reads(runner)[-1]
    src._on_result(preread["req"], {"text": "1. Approve\n2. Deny", "pane_id": "p0"})
    app.press(0)  # drill -> options seeded from cache, no wait
    runner.sent.clear()  # discard the focus + refresh-read the drill still fires
    app.press(0)  # press the first option
    assert len(runner.sent) == 1
    assert runner.sent[0]["type"] == "act"
    assert runner.sent[0]["keys"] == ["1", "enter"]


def test_drill_seeds_detection_from_cache():
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])
    preread = _reads(runner)[-1]
    src._on_result(preread["req"], {"text": "1. Approve\n2. Deny", "pane_id": "p0"})
    app.press(0)  # drill
    assert app._orch._detection == "1. Approve\n2. Deny"  # seeded, no empty flash


def test_drill_still_fires_fresh_read_for_refresh():
    # A warm cache paints instantly, but the drill still focuses AND issues a fresh
    # read so a prompt that changed in place corrects within one round-trip.
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])
    preread = _reads(runner)[-1]
    src._on_result(preread["req"], {"text": "1. Approve", "pane_id": "p0"})
    runner.sent.clear()
    app.press(0)
    assert [m["type"] for m in runner.sent] == ["focus", "read"]


def test_preread_result_does_not_render_overview():
    # Caching a background prompt while sitting on the overview must be silent: no
    # detection surfaced, no version churn (we are not drilling that pane).
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])
    preread = _reads(runner)[-1]
    v = app._state()["version"]
    src._on_result(preread["req"], {"text": "1. Approve", "pane_id": "p0"})
    assert app._state()["version"] == v
    assert app._orch._detection == ""


def test_pre_block_drill_read_does_not_surface_on_blocked_drill():
    # Drill a WORKING pane; it blocks while still drilled and the pre-block read then
    # returns. That capture predates the block episode, so it must not feed the
    # blocked drill's detection (which would parse it into approve/deny actions).
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    app.press(0)  # drill the working pane -> focus + read (pre-block)
    pre_block_read = _reads(runner)[-1]
    src._on_event(server.id, agent(server.id, "p0", Status.BLOCKED))  # blocks while drilled
    src._on_result(pre_block_read["req"], {"text": "1. STALE", "pane_id": "p0"})
    assert app._orch._detection == ""  # pre-block capture never surfaces on the blocked drill
    runner.sent.clear()
    app.press(0)  # no options -> blank tile -> no action sent
    assert not [m for m in runner.sent if m["type"] == "act"]


def test_pre_block_detection_cleared_when_drilled_pane_blocks():
    # A pane drilled while WORKING whose pre-block read already populated the detail
    # must not keep that capture as actionable options once it blocks: entering
    # BLOCKED clears the stale detection until the fresh current-episode read lands.
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    app.press(0)  # drill working -> pre-block read
    pre = _reads(runner)[-1]
    src._on_result(pre["req"], {"text": "1. Deploy prod\n2. Cancel", "pane_id": "p0"})
    assert app._orch._detection == "1. Deploy prod\n2. Cancel"  # surfaced as working detail
    src._on_event(server.id, agent(server.id, "p0", Status.BLOCKED))  # blocks while drilled
    assert app._orch._detection == ""  # stale pre-block capture cleared, not actionable
    runner.sent.clear()
    app.press(0)  # press an option tile before the fresh read returns
    assert not [m for m in runner.sent if m["type"] == "act"]  # nothing stale sent


def test_working_drill_read_still_surfaces_while_working():
    # The scoping must not break the normal case: a read for a still-WORKING drilled
    # pane surfaces in the detail panel as before.
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    app.press(0)  # drill working pane
    read = _reads(runner)[-1]
    src._on_result(read["req"], {"text": "building...", "pane_id": "p0"})
    assert app._orch._detection == "building..."  # still-working read surfaces


def test_late_read_from_pre_block_drill_is_rejected():
    # Drill a pane while it is WORKING; before its read returns the pane blocks. The
    # read was issued pre-block, so it must not seed the block episode's prompt — and
    # the newly-blocked pane still gets its own background pre-read.
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    app.press(0)  # drill the working pane -> focus + read
    pre_block_read = _reads(runner)[-1]
    app.press(app._slots - 1)  # Back to overview
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])  # now blocks
    assert _reads(runner)  # a fresh background pre-read is issued for the block episode
    src._on_result(pre_block_read["req"], {"text": "1. STALE", "pane_id": "p0"})  # pre-block, late
    app.press(0)  # drill the now-blocked pane
    assert app._orch._detection == ""  # the pre-block read never seeded


def test_late_drill_read_after_backout_and_reblock_is_rejected():
    # Drill a blocked pane, back out before its read returns, let the pane unblock
    # while on the overview and re-block. The late drill read from the prior episode
    # must not repopulate the cache — its block episode is over.
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])  # episode A
    app.press(0)  # drill -> focus + read
    drill_read = _reads(runner)[-1]
    app.press(app._slots - 1)  # Back before the read returns
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.IDLE)])  # unblock on overview
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])  # episode B
    src._on_result(drill_read["req"], {"text": "1. STALE", "pane_id": "p0"})  # episode A, late
    app.press(0)  # drill episode B
    assert app._orch._detection == ""  # the stale drill read never seeded


def test_drill_read_refreshes_cache_for_reentry():
    # The drill's own fresh read keeps the cache current: if the prompt changed in
    # place, backing out and re-drilling the still-blocked pane seeds the NEW prompt,
    # not the prompt the background pre-read had cached.
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])
    preread = _reads(runner)[-1]
    src._on_result(preread["req"], {"text": "1. OLD", "pane_id": "p0"})  # cache OLD
    app.press(0)  # drill -> seeds OLD, fires a fresh read
    drill_read = _reads(runner)[-1]
    src._on_result(drill_read["req"], {"text": "1. NEW", "pane_id": "p0"})  # prompt changed
    app.press(app._slots - 1)  # Back to overview
    app.press(0)  # re-drill the still-blocked pane
    assert app._orch._detection == "1. NEW"  # cache was refreshed by the drill read


def test_late_preread_from_prior_episode_is_rejected():
    # A pre-read issued for one block episode must not repopulate the cache after the
    # pane unblocked and re-blocked: the prompt it carries is stale. Only a result
    # matching the current episode's read is accepted.
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])  # episode A
    stale = _reads(runner)[-1]
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.IDLE)])  # unblock -> drop
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])  # episode B
    src._on_result(stale["req"], {"text": "1. STALE", "pane_id": "p0"})  # episode A's late read
    app.press(0)  # drill episode B
    assert app._orch._detection == ""  # the stale prompt never seeded the drill


def test_unblock_clears_preread_cache_then_reblock_rereads():
    # The cached prompt is dropped when the pane leaves BLOCKED; a re-block reads
    # afresh so a stale prompt never seeds the next drill.
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])
    preread = _reads(runner)[-1]
    src._on_result(preread["req"], {"text": "1. Approve", "pane_id": "p0"})
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.IDLE)])  # unblock
    runner.sent.clear()
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])  # re-block
    assert len(_reads(runner)) == 1


def test_snapshot_transition_is_atomic_under_deck_lock():
    # A live update must apply its buffer swap, invalidation and render as ONE
    # transition under the DeckApp lock, so a press (which holds the same lock)
    # cannot observe a half-applied update (new state buffered, prompt not yet
    # invalidated) and act on a stale prompt.
    import threading

    app, src, server, _ = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])

    started, done = threading.Event(), threading.Event()

    def deliver():
        started.set()
        src._on_snapshot(server.id, [agent(server.id, "p1", Status.WORKING)])
        done.set()

    with app._lock:  # stand in for a press/state holding the deck lock
        t = threading.Thread(target=deliver)
        t.start()
        started.wait(1)
        assert not done.wait(0.3)  # the whole transition blocks, not just the render
        with src._lock:
            buffered = set(src._agents)
        assert buffered == {AgentKey(server.id, "p0")}  # buffer swap is gated too
    t.join(2)
    assert done.is_set()
    with src._lock:
        assert set(src._agents) == {AgentKey(server.id, "p1")}


# --- non-bridge commands (switch_profile) must not crash the press ------------


def test_press_skips_local_only_commands(monkeypatch):
    from herdeck.commands import Command

    app, src, server, runner = make_live()
    monkeypatch.setattr(
        src._orch,
        "on_press",
        lambda i: [Command("focus", server.id, "p0"), Command("switch_profile", "x", text="x")],
    )
    src.press(0)  # switch_profile is local-only -> skipped, not sent, no crash
    assert [m["type"] for m in runner.sent] == ["focus"]


# --- offline handling -------------------------------------------------------


def test_offline_sets_connected_false_in_state():
    app, src, server, _ = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    app.refresh()
    assert app._state()["connected"] is True
    src._on_connection(server.id, False)  # bridge drops
    app.refresh()
    assert app._state()["connected"] is False


def test_offline_renders_offline_overview_panel():
    config, server = live_config()
    src = LiveSource(config, server)
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    src._on_connection(server.id, False)
    orch = Orchestrator(src.config, slots=13)
    src.apply_to(orch)
    panel = orch.render().panel
    assert panel.title == "OFFLINE"  # reuses layout.panel_overview offline path
    assert src.connected is False


def test_live_source_starts_disconnected():
    config, server = live_config()
    src = LiveSource(config, server)
    assert src.connected is False  # nothing live until the connector says so


# --- secret hygiene: the bridge token never leaks ---------------------------


def test_token_absent_from_state_and_health():
    app, src, server, _ = make_live(token=SECRET)
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    app.refresh()
    blob = json.dumps(app._state()) + json.dumps(app._health())
    assert SECRET not in blob
    assert app._health()["server_id"] == "prod"  # only the non-secret id
    assert app.source_name == "live"


def test_server_id_is_id_not_token():
    config, server = live_config(token=SECRET)
    src = LiveSource(config, server)
    assert src.server_id == "prod"
    assert SECRET not in str(src.server_id)


# --- mock/live switch by config + token presence ----------------------------


def _write_config(tmp_path, *, token_env="TEST_BRIDGE_TOKEN"):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[[servers]]\n"
        'id = "prod"\n'
        'url = "ws://bridge.local:8765"\n'
        f'token_env = "{token_env}"\n\n'
        "[profiles.default]\n"
        'servers = ["prod"]\n'
    )
    return cfg


def test_select_live_returns_config_when_server_and_token_present(tmp_path, monkeypatch):
    cfg = _write_config(tmp_path)
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.setenv("TEST_BRIDGE_TOKEN", SECRET)
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    selected = select_live()
    assert selected is not None
    config, server = selected
    assert server.id == "prod"
    assert server.token == SECRET


def test_select_live_falls_back_to_mock_without_token(tmp_path, monkeypatch):
    cfg = _write_config(tmp_path)
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("TEST_BRIDGE_TOKEN", raising=False)  # token env unset
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    assert select_live() is None


def test_select_live_falls_back_to_mock_without_config(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDECK_CONFIG", raising=False)
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    monkeypatch.chdir(tmp_path)  # no config.toml here
    assert select_live() is None


def test_select_live_honours_mock_env(tmp_path, monkeypatch):
    cfg = _write_config(tmp_path)
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.setenv("TEST_BRIDGE_TOKEN", SECRET)
    monkeypatch.setenv("HERDECK_MOCK", "1")  # explicit mock wins
    assert select_live() is None


def test_create_app_falls_back_to_mock(monkeypatch):
    monkeypatch.setenv("HERDECK_MOCK", "1")
    app = create_app(serve=False, icon_provider=StubIcons())
    try:
        assert app.source_name == "mock"
        assert app._state()["source"] == "mock"
    finally:
        app.close()


# --- build_live_source wiring (fake connector, no network) ------------------


def test_build_live_source_wires_callbacks_and_starts_runner():
    FakeConnector.instances.clear()
    config, server = live_config()
    captured = {}

    def runner_factory(conn):
        runner = FakeRunner(conn)
        captured["runner"] = runner
        return runner

    src = build_live_source(
        config, server, connector_factory=FakeConnector, runner_factory=runner_factory
    )
    conn = FakeConnector.instances[-1]
    assert conn.server is server
    assert captured["runner"].started is True
    assert src.source_name == "live"
    assert src.server_id == "prod"

    # delivering a snapshot through the wired callback updates the source
    conn.on_connection(server.id, True)
    conn.on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])
    assert src.connected is True
    assert src.summary()["blocked"] == 1

    src.close()
    assert conn.stopped is True


# --- __main__ entry wiring (mock/live switch) -------------------------------


class _MainStub:
    def __init__(self, name):
        self.host = "127.0.0.1"
        self.port = 4321
        self.token = "loopback-token"
        self.source_name = name

    def close(self):
        pass


def test_main_uses_mock_when_select_live_returns_none(monkeypatch, capsys):
    from herdeck.deckapp import __main__ as deckapp_main

    monkeypatch.setattr(deckapp_main, "create_app", lambda *, host, port: _MainStub("mock"))
    monkeypatch.setattr(deckapp_main.threading.Event, "wait", lambda self: None)
    rc = deckapp_main.main()
    assert rc == 0
    data = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert data["source"] == "mock"
    assert data["host"] == "127.0.0.1"
    assert data["token"] == "loopback-token"


def test_main_uses_live_when_select_live_returns_target(monkeypatch, capsys):
    from herdeck.deckapp import __main__ as deckapp_main

    monkeypatch.setattr(deckapp_main, "create_app", lambda *, host, port: _MainStub("live"))
    monkeypatch.setattr(deckapp_main.threading.Event, "wait", lambda self: None)
    rc = deckapp_main.main()
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out.strip().splitlines()[-1])
    assert data["source"] == "live"
    assert SECRET not in out  # the discovery line never carries the bridge token


def test_create_live_app_reports_live_without_network():
    FakeConnector.instances.clear()
    config, server = live_config()
    app = create_live_app(
        config,
        server,
        serve=False,
        icon_provider=StubIcons(),
        connector_factory=FakeConnector,
    )
    try:
        assert app.source_name == "live"
        assert app._health()["server_id"] == "prod"
        assert SECRET not in json.dumps(app._health())
    finally:
        app.close()


# --- background ticker (Task 1: herdeck-runtime-slice-a) --------------------


def test_tick_once_advances_spinner_phase():
    app, src, server = make_live_icons(StubIcons())
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    app.refresh()
    before = app._orch._phase
    app._tick_once()
    assert app._orch._phase == before + 1


def test_tick_once_animates_working_tile():
    app, src, server = make_live_icons(SpinIcons())
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    app.refresh()
    v = app._state()["version"]
    app._tick_once()
    assert app._state()["version"] > v  # the working tile re-keyed (spinner advanced)


def test_tick_once_quiet_when_all_idle():
    app, src, server = make_live_icons(SpinIcons())
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.IDLE)])
    app.refresh()
    v = app._state()["version"]
    app._tick_once()
    assert app._state()["version"] == v  # idle deck does not churn /state


def test_ticker_thread_not_started_without_serve_or_interval():
    app_a, _, _ = make_live_icons(StubIcons(), serve=False, tick_interval=0.0)
    app_b, _, _ = make_live_icons(StubIcons(), serve=False, tick_interval=0.4)
    assert app_a._ticker_thread is None  # tick_interval 0 -> no ticker
    assert app_b._ticker_thread is None  # serve=False -> no ticker (not actually serving)


def test_ticker_thread_runs_and_stops_when_serving():
    app, src, server = make_live_icons(SpinIcons(), serve=True, tick_interval=0.02)
    try:
        assert app._ticker_thread is not None and app._ticker_thread.is_alive()
    finally:
        app.close()
    assert not app._ticker_thread or not app._ticker_thread.is_alive()


def test_create_live_app_enables_ticker_from_config():
    config, server = live_config()
    app = create_live_app(config, server, serve=False, connector_factory=FakeConnector)
    assert app._tick_interval == config.hardware.tick_interval
    assert config.hardware.tick_interval == 0.4  # the live default that drives animation


# --- render-sink fan-out (Task 1: herdeck-runtime-slice-b) -------------------


def test_add_sink_delivers_initial_full_frame():
    app, src, server = make_live_icons(StubIcons())
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    app.refresh()
    sink = RecordingSink()
    app.add_sink(sink)
    assert len(sink.frames) == 1
    assert sink.frames[0].full is True
    assert sink.frames[0].render is not None  # the RenderState was handed over


def test_press_delivers_full_frame_to_sink():
    app, src, server = make_live_icons(StubIcons())
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.IDLE)])
    sink = RecordingSink()
    app.add_sink(sink)
    sink.frames.clear()
    app.press(0)
    assert sink.frames and sink.frames[-1].full is True


def test_tick_delivers_working_frame_with_indices():
    app, src, server = make_live_icons(StubIcons())
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    sink = RecordingSink()
    app.add_sink(sink)
    sink.frames.clear()
    app._tick_once()
    frame = sink.frames[-1]
    assert frame.full is False
    assert frame.working == [0]  # the single working agent is tile 0


def test_full_refresh_tick_delivers_exactly_one_full_frame():
    app, src, server = make_live_icons(StubIcons())
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    sink = RecordingSink()
    app.add_sink(sink)
    sink.frames.clear()
    for _ in range(app.FULL_REFRESH_TICKS):
        app._tick_once()
    fulls = [f for f in sink.frames if f.full]
    assert len(fulls) == 1  # one full frame at the Nth tick, working frames otherwise


def test_failing_sink_does_not_break_http_or_other_sinks():
    app, src, server = make_live_icons(StubIcons())
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])

    class BoomSink:
        def deliver(self, frame):
            raise RuntimeError("boom")

        def close(self):
            pass

    good = RecordingSink()
    app.add_sink(BoomSink())
    app.add_sink(good)
    good.frames.clear()
    v = app._state()["version"]
    app._tick_once()  # must NOT raise despite BoomSink
    assert app._state()["version"] >= v  # HTTP buffer still advanced
    assert good.frames  # the healthy sink still got its frame


def test_close_closes_sinks():
    app, src, server = make_live_icons(StubIcons())
    sink = RecordingSink()
    app.add_sink(sink)
    app.close()
    assert sink.closed is True


def test_swap_source_fans_full_frame_to_sinks():
    """swap_source must immediately repaint every registered sink (Finding 1)."""
    app, src, server = make_live_icons(StubIcons())
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    sink = RecordingSink()
    app.add_sink(sink)
    sink.frames.clear()  # discard the initial paint from add_sink

    # Build a second live source the same way make_live_icons does.
    config2, server2 = live_config()
    new_src = LiveSource(config2, server2)
    new_src.attach_runner(FakeRunner())

    app.swap_source(new_src)
    assert sink.frames, "swap_source must fan out at least one frame"
    assert sink.frames[-1].full is True


def test_close_detaches_sinks_before_closing():
    # Prove the ORDERING: when a sink's close() runs, the sink list has already
    # been detached (emptied) under the lock. With the old broken order (close
    # sinks, THEN clear) app._sinks would still hold the sink here.
    app, src, server = make_live_icons(StubIcons())
    seen = {}

    class OrderSink:
        def __init__(self):
            self.closed = False

        def deliver(self, frame):
            pass

        def close(self):
            seen["sinks_at_close"] = list(app._sinks)
            self.closed = True

    sink = OrderSink()
    app.add_sink(sink)
    app.close()
    assert sink.closed is True
    assert seen["sinks_at_close"] == []  # detached before close() ran


def test_reconnect_reissues_prereads_for_still_blocked_panes():
    """In-flight pre-reads die with the connection; after a reconnect the
    still-blocked panes must get a FRESH episode read, or instant drill stays
    dark until each pane re-blocks (audit: preread-reissue)."""
    app, src, server, runner = make_live()
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p1", Status.BLOCKED)])
    reads = [m for m in runner.sent if m.get("type") == "read"]
    assert len(reads) == 1  # the background pre-read went out
    # bridge drops (mac sleep); the in-flight read is lost with it
    src._on_connection(server.id, False)
    src._on_connection(server.id, True)
    # even before the resync snapshot lands, a fresh episode read is issued
    reads = [m for m in runner.sent if m.get("type") == "read"]
    assert len(reads) == 2
    # and the resync snapshot does not double-issue for the same episode
    src._on_snapshot(server.id, [agent(server.id, "p1", Status.BLOCKED)])
    reads = [m for m in runner.sent if m.get("type") == "read"]
    assert len(reads) == 2
