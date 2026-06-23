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

from ..commands import command_to_msg
from ..config import Config, ServerConfig
from ..connector import Connector
from ..model import AgentKey, AgentState
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
        self._active_read_req: str | None = None
        self._orch: Orchestrator | None = None
        self._deck_lock = None
        self._refresh_cb = None
        self._runner = None

    # --- StateSource surface ---
    @property
    def config(self) -> Config:
        return self._config

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    @property
    def server_id(self) -> str:
        return self._server.id  # non-secret id only; the token never leaves Connector

    def attach(self, orch: Orchestrator, *, lock=None, refresh=None) -> None:
        """Receive the render orchestrator, its lock, and a refresh callback.

        The orchestrator drives a press (``on_press``) and a read result
        (``set_detection``); the lock guards mutating it from the connector thread;
        ``refresh`` re-renders the deck so live updates bump tile versions.
        """
        self._orch = orch
        self._deck_lock = lock
        self._refresh_cb = refresh

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
        for cmd in orch.on_press(index):
            try:
                msg = command_to_msg(cmd, self._next_req(cmd))
            except ValueError:
                # Local-only commands (e.g. switch_profile) are not bridge messages;
                # phase 1 does not reload config from the deck, so they are ignored.
                continue
            runner.send(msg)

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
        with self._lock:
            self._agents = {s.key: s for s in states}
        self._render()

    def _on_event(self, server_id: str, state: AgentState) -> None:
        with self._lock:
            self._agents[state.key] = state
        self._render()

    def _on_connection(self, server_id: str, up: bool) -> None:
        with self._lock:
            self._connected = up
        self._render()

    def _on_result(self, req: str, data: dict) -> None:
        # Mirrors App.handle_result: only an accepted read (matching the request we
        # issued, for the pane still drilled) feeds the prompt text into detection.
        text = data.get("text")
        if text is None:
            return  # non-read results carry no prompt; nothing to surface in phase 1
        with self._lock:
            accepted = req is not None and req == self._active_read_req
        if not accepted:
            return
        orch, lock = self._orch, self._deck_lock
        if orch is None or lock is None:
            return
        applied = False
        with lock:
            if orch.is_drill_pane(self._server.id, data.get("pane_id")):
                orch.set_detection(text)
                applied = True
        if applied:
            self._render()

    def _render(self) -> None:
        """Re-render the deck after a live update so /state bumps tile versions.

        No-op until the DeckApp has attached (the connector may emit before then).
        """
        refresh = self._refresh_cb
        if refresh is not None:
            refresh()

    def _next_req(self, cmd) -> str | None:
        # Mirrors App.next_req_for: `list` carries no req; everything else gets a
        # fresh sequential id. A `read` id is remembered so its result can be matched.
        if cmd.kind == "list":
            return None
        with self._lock:
            self._req += 1
            req = f"r{self._req}"
            if cmd.kind == "read":
                self._active_read_req = req
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
