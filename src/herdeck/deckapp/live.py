"""LiveSource — the deckapp StateSource backed by a real bridge.

It reuses the core wholesale: a ``herdeck.connector.Connector`` runs the WebSocket
client (connect, resync-on-reconnect, backoff), and ``Orchestrator`` does the
render + press translation. This module only buffers the connector's callbacks,
re-renders the deck when they fire, and turns a press into ``Command`` wire
messages.

Threading: the connector callbacks run on the connector's asyncio loop thread.
They update this source's small buffer under ``self._lock``, then ask the DeckApp
to re-render via the ``refresh`` callback (which takes the DeckApp's lock). The
DeckApp's render/press path runs on HTTP threads, also under the DeckApp's lock.
Locks are always taken DeckApp-then-source, so there is no inversion: the
orchestrator is only ever mutated while the DeckApp lock is held.

Scope (phase 1): a single Connector to a single server (``config.servers[0]``),
mirroring the design spec ("builds a Connector from the resolved ServerConfig").
Multi-server fan-out is deferred — see ``_single_server_config``.

Secret hygiene: the bridge token lives only inside the ``Connector`` (Authorization
header). It is never stored on the source's public surface — only the non-secret
``server_id`` is exposed (for ``/health``).
"""

from __future__ import annotations

import asyncio
import dataclasses
import threading

from ..commands import Command, command_to_msg
from ..config import Config, ServerConfig
from ..connector import Connector
from ..model import AgentKey, AgentState, Status
from ..orchestrator import Orchestrator
from .source import StateSource


def _single_server_config(config: Config, server: ServerConfig) -> Config:
    """Narrow the resolved config to the one server this source connects to.

    Phase 1 runs a single Connector (per the design spec), so the deck shows only
    that server — narrowing keeps the overview order, tiles and command routing
    consistent with it instead of leaving phantom, never-populated servers. A
    multi-server deck (one connector per server) is a later phase.
    """
    return dataclasses.replace(config, servers=[server], overview_order=[server.id])


class LiveSource(StateSource):
    """A StateSource fed by a real bridge through ``Connector``.

    The connector callbacks buffer the latest fleet state and re-render the deck;
    ``apply_to`` replays the buffer into the render orchestrator via
    ``apply_snapshot``/``set_connection`` (the same path the mock uses). A press is
    translated by ``Orchestrator.on_press`` into ``Command``s and handed to the
    runner's fire-and-forget ``send`` — non-idempotent sends are never retried (the
    Connector/bridge own that guarantee). A ``read`` result is matched back to its
    request and fed to ``set_detection`` so the blocked-agent approve/deny options
    appear.
    """

    source_name = "live"

    def __init__(self, config: Config, server: ServerConfig):
        self._config = _single_server_config(config, server)
        self._server = server
        self._lock = threading.Lock()
        self._agents: dict[AgentKey, AgentState] = {}
        self._connected = False
        self._req = 0
        self._bg_req = 0
        self._active_read_req: str | None = None
        # Pre-read cache: the last-read prompt per BLOCKED pane, read in the
        # background so a drill paints its options in one frame (no read round-trip,
        # no empty flash). ``_preread`` holds the cached prompt text; ``_preread_req``
        # holds the request id of the in-flight read for the pane's CURRENT block
        # episode. Both are dropped when the pane leaves BLOCKED, so a result from a
        # prior episode (an old req) can never repopulate the cache after a re-block.
        self._preread: dict[AgentKey, str] = {}
        self._preread_req: dict[AgentKey, str] = {}
        self._orch: Orchestrator | None = None
        self._deck_lock = None
        self._refresh_locked_cb = None
        self._runner = None

    # --- StateSource surface ---
    @property
    def config(self) -> Config:
        return self._config

    @property
    def language(self) -> str:
        """Language of rendered deck text — /state exposes it so the desktop
        window can switch its own UI language in lockstep."""
        return self._config.view.language

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    @property
    def server_id(self) -> str:
        return self._server.id  # non-secret id only; the token never leaves Connector

    def attach(self, orch: Orchestrator, *, lock=None, refresh_locked=None) -> None:
        """Receive the render orchestrator, its lock, and a lock-free render.

        The orchestrator drives a press (``on_press``) and a read result
        (``set_detection``). ``lock`` is the DeckApp's lock — every live transition
        (buffer swap + invalidation + render) is run while holding it, so a press
        (which also holds it) can never observe a half-applied update.
        ``refresh_locked`` is the DeckApp's lock-free render, called inside that held
        lock to bump tile versions.
        """
        self._orch = orch
        self._deck_lock = lock
        self._refresh_locked_cb = refresh_locked

    def attach_runner(self, runner) -> None:
        """Receive the connector runner (provides fire-and-forget ``send``)."""
        self._runner = runner

    def apply_to(self, orch: Orchestrator) -> None:
        self._orch = orch
        with self._lock:
            states = list(self._agents.values())
            connected = self._connected
        orch.apply_snapshot(self._server.id, states)
        orch.set_connection(self._server.id, connected)

    def press(self, index: int) -> None:
        orch, runner = self._orch, self._runner
        if orch is None or runner is None:
            return
        was_drilling = orch.is_drilling()
        cmds = orch.on_press(index)
        # If this press just opened a drill into a blocked pane whose prompt we
        # pre-read, seed the detection so the very first render shows the options —
        # no wait for the read round-trip, no empty-drill flash. The drill's own
        # read (in cmds) still fires as a refresh, correcting any in-place change.
        if not was_drilling:
            self._seed_detection_from_preread(orch)
        for cmd in cmds:
            try:
                msg = command_to_msg(cmd, self._next_req(cmd))
            except ValueError:
                # Local-only commands (e.g. switch_profile) are not bridge messages;
                # phase 1 does not reload config from the deck, so they are ignored.
                continue
            runner.send(msg)

    def _seed_detection_from_preread(self, orch) -> None:
        """Paint a freshly-opened blocked drill from the pre-read cache (caller holds
        the deck lock, via DeckApp.press). No-op unless the pane is blocked and a
        prompt string was cached (a pending ``None`` entry does not seed)."""
        key = orch.drill_key()
        if key is None:
            return
        agent = orch.get_agent(key)
        if agent is None or agent.status is not Status.BLOCKED:
            return
        with self._lock:
            cached = self._preread.get(key)
        if isinstance(cached, str) and cached:
            orch.set_detection(cached)

    def summary(self) -> dict:
        from .. import layout

        with self._lock:
            agents = list(self._agents.values())
        counts = layout.summary(agents)
        return {
            "agents": len(agents),
            "blocked": counts.blocked,
            "working": counts.working,
            "idle": counts.idle,
            "done": counts.done,
        }

    def close(self) -> None:
        runner = self._runner
        if runner is not None:
            runner.close()

    # --- connector callbacks (run on the connector's loop thread) ---
    def _on_snapshot(self, server_id: str, states: list[AgentState]) -> None:
        new_by_key = {s.key: s for s in states}

        def mutate():
            with self._lock:
                self._agents = dict(new_by_key)
            # Drop the drilled prompt only if the pane left BLOCKED
            # (App.handle_snapshot): the prompt + in-flight read stay valid while it
            # is still blocked.
            self._invalidate_if_drill_unblocked(server_id, new_by_key.get(self._drilled_key()))
            self._reconcile_prereads()
            return True

        self._apply(mutate)

    def _on_event(self, server_id: str, state: AgentState) -> None:
        def mutate():
            with self._lock:
                self._agents[state.key] = state
            # Same rule for a single-pane event: only a real unblock clears the
            # drilled prompt (App.handle_event).
            drilled = self._drilled_key()
            if drilled is not None and drilled == state.key:
                self._invalidate_if_drill_unblocked(server_id, state)
            self._reconcile_prereads()
            return True

        self._apply(mutate)

    def _on_connection(self, server_id: str, up: bool) -> None:
        def mutate():
            with self._lock:
                self._connected = up
                if not up:
                    # In-flight background reads died with the connection
                    # (Connector.send is at-most-once), so their req markers
                    # must go too — otherwise _reconcile_prereads keeps
                    # skipping the still-blocked panes after a reconnect and
                    # instant drill stays dark until each pane re-blocks. The
                    # cached prompt TEXT stays: it is a best-effort hint until
                    # the fresh episode read lands.
                    self._preread_req.clear()
            # No reconnect-time reads: the connector's resync `list` snapshot
            # always follows and _on_snapshot reconciles against the FRESH
            # fleet — issuing reads from the stale pre-disconnect agents here
            # would race panes that unblocked or vanished while offline.
            return True

        self._apply(mutate)

    def _on_result(self, req: str, data: dict) -> None:
        # Mirrors App.handle_result.
        text = data.get("text")
        if text is None:
            # An act/send/start ack: resync this server with a fresh list so a
            # skipped guarded action (pane no longer blocked) can't linger as stale.
            runner = self._runner
            if runner is not None:
                runner.send(command_to_msg(Command("list", self._server.id), None))
            return
        # A read result: cache it for an instant future drill (while the pane is
        # blocked), and surface the prompt now only if it still matches a read we
        # issued and the pane is still drilled (re-checked under the deck lock so a
        # concurrent invalidation wins).
        pane_id = data.get("pane_id")

        def mutate():
            self._cache_preread(pane_id, text, req)
            orch = self._orch
            if orch is None or req is None or not orch.is_drill_pane(self._server.id, pane_id):
                return False
            drilled = orch.drill_key()
            agent = orch.get_agent(drilled)
            # For a BLOCKED drill the detection becomes actionable (parse_options ->
            # approve/deny), so accept only the current-episode read (_preread_req,
            # registered while blocked and dropped on unblock): a pre-block or prior-
            # episode capture must never feed the blocked options. A non-blocked drill
            # only shows the read as detail text, so the plain active-read match holds.
            with self._lock:
                if agent is not None and agent.status is Status.BLOCKED:
                    accepted = req == self._preread_req.get(drilled)
                else:
                    accepted = req == self._active_read_req
            if accepted:
                orch.set_detection(text)
                return True
            return False

        self._apply(mutate)

    def _apply(self, mutate) -> None:
        """Run a state transition (and render it) atomically w.r.t. presses.

        ``mutate`` runs while the DeckApp lock is held — the same lock ``press``
        takes — so a press never sees a half-applied bridge update. It returns True
        when a re-render is warranted; the render also happens under that held lock
        (via the DeckApp's lock-free ``_refresh_locked``) so /state bumps tile
        versions for changed cells. Before the DeckApp attaches, just run the
        mutation (no orchestrator/render yet).
        """
        lock = self._deck_lock
        if lock is None:
            mutate()
            return
        with lock:
            changed = mutate()
            if changed and self._refresh_locked_cb is not None:
                self._refresh_locked_cb()

    # --- pre-read cache (callers hold the deck lock) ---
    def _reconcile_prereads(self) -> None:
        """Keep the pre-read cache in step with the fleet: drop entries for panes no
        longer blocked (their prompt is stale), and issue one background read for each
        blocked pane that has no current-episode read yet.

        This includes the drilled pane: pressing a BLOCKED pane registers its own read
        (so no second read is issued there), but a pane drilled while WORKING that then
        blocks has only a rejected pre-block read — it needs a fresh episode read here
        or its blocked drill would stay blank until the user backs out and re-drills.

        Runs inside a mutate() (deck lock held); ``self._lock`` guards the cache +
        buffer. Sends fire after releasing ``self._lock`` — ``runner.send`` is
        fire-and-forget and never blocks."""
        runner = self._runner
        orch = self._orch
        drilled = self._drilled_key()
        reads: list[tuple[str, AgentKey]] = []
        clear_detection = False
        with self._lock:
            blocked = {k for k, s in self._agents.items() if s.status is Status.BLOCKED}
            for key in set(self._preread) | set(self._preread_req):
                if key not in blocked:  # left BLOCKED -> prompt + pending read are stale
                    self._preread.pop(key, None)
                    self._preread_req.pop(key, None)
            for key in blocked:
                if key in self._preread_req:
                    continue  # a current-episode read is already out (pre-read or drill read)
                self._bg_req += 1
                bg_req = f"p{self._bg_req}"
                self._preread_req[key] = bg_req  # register so the poll won't re-issue
                reads.append((bg_req, key))
                if key == drilled:
                    # The drilled pane just entered a block episode with no valid read:
                    # any current detection is a pre-block capture. Drop it so the
                    # blocked drill shows no options until the fresh read lands.
                    clear_detection = True
        if clear_detection and orch is not None:
            orch.set_detection("")
        if runner is None:
            return
        for bg_req, key in reads:
            runner.send(
                command_to_msg(
                    Command("read", key.server_id, key.pane_id, source="detection"), bg_req
                )
            )

    def _cache_preread(self, pane_id: str | None, text: str, req: str | None) -> None:
        """Store a read result as the pane's cached prompt — only while the pane is
        still BLOCKED and only if ``req`` is the read we last issued for the pane's
        CURRENT block episode (``_preread_req[key]``, set by BOTH the background
        pre-read and the drill read, and dropped the moment the pane leaves BLOCKED).
        The drill read thus keeps the cache fresh when a prompt changes in place,
        while a late read from a prior episode carries a since-replaced req and is
        rejected. Caller holds the deck lock."""
        if pane_id is None or req is None:
            return
        key = AgentKey(self._server.id, pane_id)
        with self._lock:
            state = self._agents.get(key)
            if (
                state is not None
                and state.status is Status.BLOCKED
                and req == self._preread_req.get(key)
            ):
                self._preread[key] = text

    # --- read invalidation (callers hold the deck lock) ---
    def _drilled_key(self) -> AgentKey | None:
        orch = self._orch
        return orch.drill_key() if orch is not None else None

    def _invalidate_if_drill_unblocked(self, server_id: str, new_state) -> None:
        """Drop the drilled prompt only when the agent actually leaves BLOCKED
        (mirrors App._invalidate_read_if_unblocked).

        The prompt (and an in-flight read) stay valid as long as the agent stays
        blocked. Wiping on every cosmetic change instead — e.g. a ``branch`` label
        that flaps in the bridge snapshot because ``worktree.list`` was momentarily
        unavailable — rejected the in-flight read (prompt never showed; "click 3×")
        or cleared an already-shown prompt ("shows then disappears").

        ``new_state`` is the drilled pane's state in the update that just arrived
        (``None`` if it dropped out of the fleet); the caller has already confirmed
        the update is authoritative for the drilled server / pane.
        """
        orch = self._orch
        drill = orch.drill_key() if orch is not None else None
        if drill is None or drill.server_id != server_id:
            return
        if new_state is not None and new_state.status is Status.BLOCKED:
            return  # still blocked -> same prompt, keep the options live
        orch.set_detection("")
        with self._lock:
            self._active_read_req = None

    def _next_req(self, cmd) -> str | None:
        # Mirrors App.next_req_for: `list` carries no req; everything else gets a
        # fresh sequential id. A `read` id is remembered so its result can be matched
        # (``_active_read_req`` for the drill display; ``_preread_req`` per pane so the
        # drill read also refreshes the pre-read cache under the same episode scope).
        if cmd.kind == "list":
            return None
        with self._lock:
            self._req += 1
            req = f"r{self._req}"
            if cmd.kind == "read":
                self._active_read_req = req
                # Register the drill read as the episode's read ONLY when the pane is
                # already BLOCKED. A read issued while the pane is WORKING/IDLE belongs
                # to the pre-block state; letting its marker survive into a later block
                # episode would both suppress the fresh pre-read and let a pre-block
                # capture be accepted as the block prompt.
                if cmd.pane_id is not None:
                    key = AgentKey(cmd.server_id, cmd.pane_id)
                    state = self._agents.get(key)
                    if state is not None and state.status is Status.BLOCKED:
                        self._preread_req[key] = req
        return req


class ConnectorRunner:
    """Owns the Connector's asyncio loop on a daemon thread and exposes a
    thread-safe, fire-and-forget ``send``. Reconnect/backoff lives in the
    Connector — this only schedules sends and shuts the loop down on close.
    """

    def __init__(self, connector: Connector):
        self._conn = connector
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, name="herdeck-live", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _serve(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._conn.run())
        except Exception:
            pass  # the connector swallows network errors; guard the loop regardless
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    def send(self, msg: dict) -> None:
        loop = self._loop
        if loop.is_closed():
            return
        try:
            # One scheduling attempt, no retry — matches the bridge's at-most-once
            # delivery for non-idempotent sends.
            asyncio.run_coroutine_threadsafe(self._conn.send(msg), loop)
        except RuntimeError:
            pass  # loop not running / shutting down

    def close(self) -> None:
        self._conn.stop()
        if self._thread.is_alive():
            self._thread.join(timeout=2)


def build_live_source(
    config: Config,
    server: ServerConfig,
    *,
    connector_factory=Connector,
    runner_factory=ConnectorRunner,
) -> LiveSource:
    """Wire a LiveSource to a Connector + runner and start the connector.

    ``connector_factory``/``runner_factory`` are injectable so tests can drive the
    callbacks and capture sends without a real bridge.
    """
    source = LiveSource(config, server)
    connector = connector_factory(
        server,
        on_snapshot=source._on_snapshot,
        on_event=source._on_event,
        on_connection=source._on_connection,
        on_result=source._on_result,
    )
    runner = runner_factory(connector)
    source.attach_runner(runner)
    runner.start()
    return source
