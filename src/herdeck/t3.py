"""T3 Code backend connector on orchestration protocol 2.

Reads poll the HTTP shell every second and fetch a thread's bounded detail only
when it changed; actions are dispatched as V2 commands over the WebSocket RPC.
The callback surface matches Herdeck's bridge connector (``on_snapshot``,
``on_connection``, ``on_result``, ``health()``, ``send()``, ``run()``,
``stop()``). Contract: docs/t3-code-integration.md.

Delivery rules: every press re-reads the thread, must carry the revision the
deck saw and an action that is still offered. Each command gets a uuid4
``commandId`` that is remembered per (thread, revision, action, payload, text):
a transport failure is retried once with the SAME command, and a later identical
press (outcome unknown, or accepted before T3 published the new state) replays
it, so the server's commandId dedupe means it can never execute twice. Only a
rejection forgets the commands. A thread whose delivery outcome is unknown
stays read-only until its revision changes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import UTC, datetime

from .t3_actions import Incompatible, build_commands, capabilities, negotiate, thread_actions
from .t3_client import T3Client, T3Error, T3Http, T3Rejected
from .t3_state import active_plan, lifecycle, thread_state

# T3Error/T3Http are re-exported for settings.py.
__all__ = ["T3Connector", "T3Error", "T3Http", "T3Rejected"]

log = logging.getLogger("herdeck.t3")

READ_STATE_OBSOLETE = ("[[servers]] desktop_read_state is obsolete: "
                       "T3 0.0.46+ keeps read state on the server")
OPERATE_DENIED = "credential lacks orchestration:operate"
OPERATE_NOTICE = "T3 credential lacks orchestration:operate — actions disabled.\n"
UNCERTAIN = "T3 delivery uncertain; inspect the conversation before retrying"
CREDENTIAL_INVALID = "T3 credential invalid or expired"
UNAVAILABLE = "T3 unavailable; check the connection"
_CONNECT_ERRORS = {401: CREDENTIAL_INVALID,
                   403: "T3 credential lacks orchestration:read",
                   400: "T3 orchestration protocol mismatch (Herdeck needs 2)"}
# Raised by the pure state/action mappers on a thread that breaks the contract.
_MALFORMED = (KeyError, TypeError, ValueError, AttributeError)
_DELIVERY_MEMORY = 256
_read_state_warned = False


class _IncompatibleServer(T3Error):
    """The descriptor does not speak orchestration protocol 2."""


def _warn_read_state(server):
    global _read_state_warned
    if _read_state_warned:
        return
    if getattr(server, "desktop_read_state", False) or os.environ.get("HERDECK_T3_DESKTOP_READ_STATE") == "1":
        _read_state_warned = True
        log.warning(READ_STATE_OBSOLETE)


def _connect_error(exc):
    if isinstance(exc, _IncompatibleServer):
        return str(exc)
    if isinstance(exc, T3Error):
        return _CONNECT_ERRORS.get(exc.code, UNAVAILABLE)
    return UNAVAILABLE


class T3Connector:
    protocol = 2
    capabilities = frozenset()

    def __init__(self, server, on_snapshot, on_event, on_connection, on_result=None,
                 on_error=None, *, client=None, **kwargs):
        self.server = server
        self.client = client or T3Client(server.url, server.token)
        self._on_snapshot = on_snapshot
        self._on_connection = on_connection
        self._on_result = on_result or (lambda *args: None)
        self._on_error = on_error or (lambda *args: None)
        self.states = {}
        # tid -> (revision, capabilities, actions, shell, detail) BEFORE any
        # read-only lock; send() validates a press against it.
        self._offers = {}
        self._cache = {}  # tid -> (detail cache key, detail)
        self._epoch = uuid.uuid4().hex
        self._features = None
        self._uncertain = {}  # tid -> revision whose delivery outcome is unknown
        self._deliveries = {}  # delivery key -> commands with their commandIds
        self._operate_denied = False
        self._faulty = set()  # malformed thread ids already logged
        self._lock = asyncio.Lock()
        self._stop = False
        self._clock = time.time
        self._retry_delay = 1.0
        self._interval = 1.0
        self.last_connect_error = None
        # /health diagnostics, same shape as the herdr Connector.health(): a
        # T3 server that never answered (configured but not running) is not
        # "down", so the window stays quiet about it (ever_connected=False).
        # Any answer counts (incompatible descriptor, HTTP 4xx), so those
        # errors stay visible while connected stays False.
        self._connected = False
        self._ever_connected = False
        self._since_ms = int(time.time() * 1000)
        self._attempt = 0
        _warn_read_state(server)

    def stop(self):
        self._stop = True

    def health(self) -> dict:
        """Non-secret connection facts for the runtime's /health and GET
        /maintenance, mirroring connector.Connector.health(). T3 is not a
        herdeck bridge: no bridge version and no self-update."""
        return {
            "connected": self._connected,
            "ever_connected": self._ever_connected,
            "last_error": self.last_connect_error,
            "since": self._since_ms,
            "attempt": self._attempt,
            "bridge_version": None,
            "protocol": self.protocol,
            "protocol_supported": True,
            "self_update": False,
            "managed": None,
        }

    def _set_connected(self, up: bool) -> None:
        if up:
            self._ever_connected = True
            self._attempt = 0
        else:
            self._attempt += 1
        if up != self._connected:
            self._connected = up
            self._since_ms = int(time.time() * 1000)
        self._on_connection(self.server.id, up)

    def _malformed(self, tid, exc):
        if tid not in self._faulty:
            self._faulty.add(tid)
            log.warning("T3 thread %s is malformed (%s); shown read-only or skipped",
                        tid, type(exc).__name__)

    # --- reads ---------------------------------------------------------------

    async def refresh(self, force: str | None = None):
        if self._features is None:
            descriptor = await asyncio.to_thread(self.client.descriptor)
            self._ever_connected = True  # it answered, even if incompatible
            try:
                features = negotiate(descriptor)
            except Incompatible as exc:
                self.last_connect_error = str(exc)
                raise _IncompatibleServer(str(exc)) from None
            self._features = features
        shell = await asyncio.to_thread(self.client.shell)
        projects = {p["id"]: p for p in shell["projects"]
                    if isinstance(p, dict) and isinstance(p.get("id"), str)}
        now = self._clock()
        states, offers = {}, {}
        entries = [(s, False) for s in shell["threads"]] + [(s, True) for s in shell["archivedThreads"]]
        for summary, archived in entries:
            tid = summary.get("id") if isinstance(summary, dict) else None
            if not isinstance(tid, str) or not tid:
                self._malformed("<no id>", KeyError("id"))
                continue
            if tid in states:
                continue
            fetched = await self._detail(tid, summary, archived, now, force)
            if fetched is None:
                continue  # 404: the thread is gone
            detail, stale = fetched
            built = self._build(tid, summary, detail, projects, archived, now, stale)
            if built is not None:
                states[tid], offers[tid] = built
        self._cache = {k: v for k, v in self._cache.items() if k in states}
        self._uncertain = {k: v for k, v in self._uncertain.items() if k in states}
        self.states, self._offers = states, offers
        self._on_snapshot(self.server.id, list(states.values()))

    async def _detail(self, tid, summary, archived, now, force):
        """(detail, stale), or None when T3 says the thread no longer exists."""
        cached = self._cache.get(tid)
        try:
            key = json.dumps([summary.get("updatedAt"), summary.get("pendingRuntimeRequest"),
                              summary.get("hasActionableProposedPlan"), summary.get("status")],
                             sort_keys=True)
            active = lifecycle(summary, now, archived) == "active"
        except _MALFORMED as exc:
            self._malformed(tid, exc)
            return (cached[1] if cached else {}), True
        if not (force == tid or (active and (cached is None or cached[0] != key))):
            return (cached[1] if cached else {}), False
        try:
            detail = await asyncio.to_thread(self.client.thread_detail, tid)
            plan = active_plan(detail)
            if plan and not plan["markdown"] and plan.get("detailInTurnItem"):
                # /bounded left the plan text in a turn item it did not include.
                detail = await asyncio.to_thread(self.client.full_detail, tid)
        except T3Error as exc:
            if exc.code in (401, 403):
                raise
            if exc.code == 404:
                self._cache.pop(tid, None)
                return None
            return (cached[1] if cached else {}), True
        except _MALFORMED as exc:
            self._malformed(tid, exc)
            return (cached[1] if cached else {}), True
        self._cache[tid] = (key, detail)
        return detail, False

    def _build(self, tid, summary, detail, projects, archived, now, stale):
        """(published state, offer) for one thread; None if it cannot be mapped.

        A malformed thread degrades to a read-only tile (or is skipped) and
        never fails the refresh of the other threads.
        """
        features = self._features
        try:
            state = thread_state(self.server.id, summary, detail, projects, self._epoch,
                                 features=features, now=now, archived=archived)
            actions = thread_actions(state, summary, detail, features, now)
            caps = capabilities(state, summary, actions)
            self._faulty.discard(tid)
        except _MALFORMED as exc:
            self._malformed(tid, exc)
            try:
                state = thread_state(self.server.id, summary, {}, projects, self._epoch,
                                     features=features, now=now, archived=archived)
            except _MALFORMED:
                return None
            actions, caps, stale = [], ("read",), True
        offer = (state.backend_revision, caps, actions, summary, detail)
        state.backend_actions, state.capabilities = actions, caps
        uncertain = self._uncertain.get(tid)
        if uncertain is not None and not stale and uncertain != state.backend_revision:
            # The thread changed: the command landed, or the user may act anew.
            # A revision built from a stale detail is no evidence either way.
            self._uncertain.pop(tid)
            uncertain = None
        if stale or uncertain is not None:
            state.capabilities, state.backend_actions = ("read",), []
            state.backend_revision += ":stale" if stale else ":uncertain"
            state.progress = "Stale detail" if stale else "Delivery uncertain — inspect T3"
            state.preview = state.progress + ". Controls disabled. Inspect T3.\n" + state.preview
            if stale:
                state.state_labels = {state.status.value: "STALE"}
        if self._operate_denied:
            state.capabilities, state.backend_actions = ("read",), []
            state.preview = OPERATE_NOTICE + state.preview
        return state, (offer if not stale else None)

    async def run(self):
        online = False
        while not self._stop:
            try:
                async with self._lock:
                    await self.refresh()
                self.last_connect_error = None
                self._set_connected(True)
                online = True
            except (T3Error, *_MALFORMED) as exc:
                if getattr(exc, "code", None) is not None:
                    self._ever_connected = True  # an HTTP answer, not a dead transport
                self.last_connect_error = _connect_error(exc)
                if online:
                    self._epoch = uuid.uuid4().hex
                    self._features = None
                    self._cache.clear()
                online = False
                self._set_connected(False)
            await asyncio.sleep(self._interval)
        self._connected = False
        self._on_connection(self.server.id, False)

    # --- delivery ------------------------------------------------------------

    async def send(self, msg):
        async with self._lock:
            req, tid = msg.get("req"), msg.get("pane_id")
            try:
                await self.refresh(force=tid)
                if msg.get("type") == "list":
                    return
                state = self.states.get(tid)
                if state is None:
                    raise T3Error("T3 conversation is no longer available")
                if msg.get("type") == "read":
                    self._on_result(req, {"pane_id": tid, "text": state.preview})
                    return
                if msg.get("type") != "backend_action":
                    raise T3Error("Unsupported T3 action; use semantic controls")
                commands = self._commands(tid, msg)
            except (T3Error, *_MALFORMED) as exc:
                message = str(exc) if isinstance(exc, T3Error) else "Unsupported T3 contract"
                self._on_result(req, {"ok": False, "skipped": True, "message": message})
                return
            await self._deliver(req, tid, *commands)

    def _commands(self, tid, msg):
        """Validate a press against the fresh offer; (key, revision, commands)."""
        if self._operate_denied:
            raise T3Error("T3 credential lacks orchestration:operate; actions disabled")
        offer = self._offers.get(tid)
        if offer is None:
            raise T3Error("T3 detail is stale; controls disabled")
        revision, caps, actions, shell, detail = offer
        action = msg.get("action")
        if action not in caps:
            raise T3Error("T3 action is not available")
        if msg.get("revision") != revision:
            raise T3Error("Stale or already submitted T3 action")
        payload = {}
        if action not in ("stop", "continue"):
            wanted = json.dumps(msg.get("payload"), sort_keys=True)
            # decline and cancel share the id "deny": match by id AND payload.
            match = next((a for a in actions if a["id"] == action
                          and json.dumps(a["payload"], sort_keys=True) == wanted), None)
            if match is None:
                raise T3Error("T3 request or answer is stale")
            payload = match["payload"]
        text = msg.get("text") if action == "continue" else None
        key = (tid, revision, action, json.dumps(payload, sort_keys=True), text)
        commands = self._deliveries.get(key)
        if tid in self._uncertain and commands is None:
            raise T3Error(UNCERTAIN)
        if commands is None:
            now_iso = datetime.fromtimestamp(self._clock(), UTC).isoformat()
            try:
                built = build_commands(action, payload, shell, detail, self._features,
                                       text=text, now_iso=now_iso)
            except ValueError as exc:
                raise T3Error(str(exc)) from None
            commands = [{**c, "commandId": str(uuid.uuid4())} for c in built]
            self._deliveries[key] = commands
            while len(self._deliveries) > _DELIVERY_MEMORY:
                del self._deliveries[next(iter(self._deliveries))]
        return key, revision, commands

    async def _dispatch(self, commands):
        """("accepted"|"rejected"|"uncertain", reason). Stops at the first failure."""
        for command in commands:
            for attempt in (1, 2):
                try:
                    await self.client.dispatch(command)
                    break
                except T3Rejected as exc:
                    if attempt == 2 and getattr(exc, "handshake", False):
                        # The retry never reached the command, and attempt 1
                        # is unknown: the first may have run.
                        return "uncertain", None
                    return "rejected", exc.reason
                except Exception as exc:  # any other failure: the outcome is unknown
                    if getattr(exc, "code", None) == 401 and attempt == 1:
                        # The ticket was refused: this command was never sent.
                        return "rejected", CREDENTIAL_INVALID
                    if attempt == 2:
                        return "uncertain", None
                    await asyncio.sleep(self._retry_delay)
        return "accepted", None

    async def _deliver(self, req, tid, key, revision, commands):
        outcome, reason = await self._dispatch(commands)
        if outcome == "uncertain":
            self._uncertain[tid] = revision
            result = {"ok": False, "uncertain": True, "message": UNCERTAIN}
        elif outcome == "accepted":
            # Keep the commands: a repeat press on the same revision (T3 has
            # not published the effect yet) replays them instead of minting
            # new ids, so e.g. a double-pressed reply is sent once.
            self._uncertain.pop(tid, None)
            result = {"ok": True, "sent": True, "accepted": True}
        else:
            self._deliveries.pop(key, None)  # rejected ids fail again on replay
            if reason == OPERATE_DENIED:
                self._operate_denied = True
            result = {"ok": False, "rejected": True, "message": reason}
        self._on_result(req, result)
        try:
            # Only called from send(), which holds self._lock (asyncio.Lock is
            # not reentrant), so this refresh cannot race run().
            await self.refresh(force=tid)
        except (T3Error, *_MALFORMED):
            pass  # the poll loop reports connection problems
