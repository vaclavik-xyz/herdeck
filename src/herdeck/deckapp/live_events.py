"""LiveSource's input from bridge lifecycle events (capability ``events``).

A bridge that advertises ``events`` (events.py) is the single source of truth
for "agent X blocked / finished / was answered": several runtimes, the web
cockpit and Telegram then agree, nothing is lost while this runtime sleeps,
and an answer given anywhere retires the prompt here. For such a server the
notification engine is driven by these events and the local snapshot-diff
detection stays quiet (never both, so no duplicate alerts); older bridges keep
the local path.

* The connect-time ``list`` subscribes with the cursor persisted in
  ``EventCursorStore`` (after a runtime restart the bridge replays only what
  was missed). Events up to ``event_sync`` are a batch: only episodes still
  open at its end and never seen before alert, and a first subscription
  (no cursor) is a silent baseline, like the local path's first snapshot.
* ``blocked`` carries the bridge's pre-read prompt: it fills the pre-read
  cache (drill, card, banner excerpt) so no local read round trip is needed;
  only a blocked event without a prompt falls back to a local read.
* ``answered`` withdraws banners, stops reminders, closes a drill left open on
  it and makes banner / card answers for that episode return stale.
* Outgoing answers are stamped with the pane's bridge ``episode_id`` and
  ``prompt_revision`` so the bridge refuses a second answer to one episode.

Mixed into LiveSource (live.py), whose locks and buffers it uses: ``_on_lifecycle``
runs on a connector thread and never holds ``self._lock`` across the deck lock.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import sys
import threading
import time

from ..connector import EVENTS_CAPABILITY
from ..model import AgentKey, AgentState, Status
from ..protocol import EventSync, LifecycleEvent
from . import event_cursor as _event_cursor
from .event_cursor import EventCursorStore

log = logging.getLogger(__name__)

ANSWER_TYPES = frozenset({"act", "send_text", "choose_if_blocked"})
# A subscription that has not seen its event_sync this long after connecting
# counts as failed: local detection takes over and the subscribe is re-sent
# every RESUBSCRIBE_S until the bridge answers.
SYNC_TIMEOUT_S = 10.0
RESUBSCRIBE_S = 30.0
_TAG_RE = re.compile(r"[^a-z0-9_.-]+")


def runtime_tag(active_profile: str) -> str:
    """Which runtime this is, for its own cursor file: several runtimes on
    one host (desktop, herdeck-web, another profile) each track their own
    place in a bridge's event stream and alert on their own."""
    entry = os.path.basename(sys.argv[0] or "") or "herdeck"
    return _TAG_RE.sub("-", f"{entry}-{active_profile}".lower()).strip("-")[:80] or "herdeck"


class StampingRunner:
    """A connector runner whose outgoing answers carry the bridge episode."""

    def __init__(self, runner, stamp):
        self._runner = runner
        self._stamp = stamp

    def send(self, msg: dict) -> None:
        self._runner.send(self._stamp(msg))

    def __getattr__(self, name):
        return getattr(self._runner, name)


class BridgeEventsMixin:
    def _bridge_events_init(self, store: EventCursorStore | None = None) -> None:
        self._ev_store = store or EventCursorStore(
            _event_cursor.default_path(runtime_tag(self._config.meta.active_profile))
        )
        self._ev_client = f"herdeck@{socket.gethostname()}"[:64]
        self._ev_lock = threading.Lock()
        # Subscription health per server (under _ev_lock): synced once its
        # event_sync arrived; until the deadline a pending one keeps local
        # detection quiet; failed = no sync in time, local detection runs.
        self._ev_synced: set[str] = set()
        self._ev_failed: set[str] = set()
        self._ev_deadline: dict[str, float] = {}
        self._ev_generation: dict[str, int] = {}
        self._ev_sync_timeout = SYNC_TIMEOUT_S
        self._ev_resubscribe_s = RESUBSCRIBE_S
        self._ev_timer = _start_timer
        # server -> events buffered until event_sync, and whether that replay
        # may alert (False for a first, cursor-less subscription).
        self._ev_batch: dict[str, list[LifecycleEvent]] = {}
        self._ev_batch_alert: dict[str, bool] = {}
        # Bridge prompt revision per pane: key -> (episode, revision). Under self._lock.
        self._ev_revision: dict[AgentKey, tuple[str, str | None]] = {}
        # Blocked panes whose bridge event came without a prompt (local read).
        self._ev_prompt_missing: set[AgentKey] = set()

    # --- connector hooks ------------------------------------------------------
    def _events_cursor(self, server_id: str) -> dict:
        """The ``events`` field of the connect-time ``list`` (connector thread).
        Starts the event_sync watchdog of this connection."""
        with self._ev_lock:
            generation = self._ev_generation.get(server_id, 0) + 1
            self._ev_generation[server_id] = generation
            self._ev_synced.discard(server_id)
            self._ev_failed.discard(server_id)
            self._ev_deadline[server_id] = time.monotonic() + self._ev_sync_timeout
        self._ev_timer(
            self._ev_sync_timeout, lambda: self._ev_check_sync(server_id, generation)
        )
        return self._ev_subscription(server_id)

    def _ev_subscription(self, server_id: str) -> dict:
        epoch, seq = self._ev_store.cursor(server_id)
        with self._ev_lock:
            self._ev_batch[server_id] = []
            self._ev_batch_alert[server_id] = seq is not None
        return {"after": seq, "epoch": epoch, "client": self._ev_client}

    def _ev_check_sync(self, server_id: str, generation: int) -> None:
        """Watchdog (timer thread): no event_sync on this connection yet ->
        local detection takes over and the subscribe goes out again."""
        with self._ev_lock:
            if self._ev_generation.get(server_id) != generation or server_id in self._ev_synced:
                return
            self._ev_failed.add(server_id)
        with self._lock:
            connected = self._connected.get(server_id, False)
        runner = self._runners.get(server_id)
        if not connected or runner is None or not self._events_offered(server_id):
            return
        log.warning(
            "bridge '%s' offers events but sent no event_sync; alerting locally, re-subscribing",
            server_id,
        )
        runner.send({"type": "list", "events": self._ev_subscription(server_id)})
        self._ev_timer(
            self._ev_resubscribe_s, lambda: self._ev_check_sync(server_id, generation)
        )

    def _events_offered(self, server_id: str) -> bool:
        connector = getattr(self._runners.get(server_id), "connector", None)
        caps = getattr(connector, "capabilities", None)
        return isinstance(caps, frozenset | set) and EVENTS_CAPABILITY in caps

    def _bridge_events(self, server_id: str) -> bool:
        """Does ``server_id``'s bridge drive notifications right now? It must
        offer ``events`` and this connection's subscription must have synced
        (or still be within its grace period, when local detection only
        follows along silently)."""
        if not self._events_offered(server_id):
            return False
        with self._ev_lock:
            if server_id in self._ev_synced:
                return True
            return (
                server_id not in self._ev_failed
                and time.monotonic() < self._ev_deadline.get(server_id, 0.0)
            )

    def _episode_spent(self, server_id: str, episode: str | None) -> bool:
        """Was block ``episode`` answered (here or on another client)? Only
        for an events bridge: it re-opens an episode whose prompt changes, so
        a multi-step prompt stays answerable. Caller holds self._lock."""
        return (
            episode is not None
            and episode in self._answered_episodes
            and self._bridge_events(server_id)
        )

    def _events_on_connection(self, server_id: str, up: bool) -> None:
        if not up:
            with self._ev_lock:
                self._ev_batch.pop(server_id, None)
                self._ev_batch_alert.pop(server_id, None)
                self._ev_synced.discard(server_id)
                self._ev_deadline.pop(server_id, None)
                # a pending watchdog of the old connection does nothing
                self._ev_generation[server_id] = self._ev_generation.get(server_id, 0) + 1

    def _on_lifecycle(self, server_id: str, msg) -> None:
        if isinstance(msg, EventSync):
            with self._ev_lock:
                batch = self._ev_batch.pop(server_id, None) or []
                alert = self._ev_batch_alert.pop(server_id, False)
                self._ev_synced.add(server_id)
                self._ev_failed.discard(server_id)
            self._ev_replay(server_id, batch, alert=alert and bool(batch))
            self._ev_store.note(server_id, epoch=msg.epoch, seq=msg.seq)
            return
        if not isinstance(msg, LifecycleEvent):
            return
        with self._ev_lock:
            batch = self._ev_batch.get(server_id)
            if batch is not None:
                batch.append(msg)
                return
        opens = msg.kind in ("blocked", "done")
        fresh = opens and not self._ev_store.known(server_id, msg.episode_id)
        self._ev_handle(msg, alert=fresh)
        # Known only once its alert is queued (_fire_notify notes it too): a
        # crash in between replays it rather than losing it.
        self._ev_store.note(
            server_id,
            epoch=msg.epoch,
            seq=msg.seq,
            episodes=(msg.episode_id,) if opens else (),
        )

    # --- event handling -------------------------------------------------------
    def _ev_replay(self, server_id: str, batch: list[LifecycleEvent], *, alert: bool) -> None:
        """Apply a replayed batch; alert once per episode still open at its
        end and never seen before."""
        opened: dict[str, LifecycleEvent] = {}
        for msg in batch:
            self._ev_handle(msg, alert=False)
            if msg.kind in ("blocked", "done"):
                opened[msg.episode_id] = msg
            elif msg.kind in ("unblocked", "cleared"):
                opened.pop(msg.episode_id, None)
        if alert:
            with self._lock:
                answered = set(self._answered_episodes)
            for episode, msg in opened.items():
                if episode not in answered and not self._ev_store.known(server_id, episode):
                    self._ev_alert(msg)
        # A baseline, or what did not alert, is known from now on too.
        self._ev_store.note(server_id, episodes=tuple(opened))

    def _ev_alert(self, msg: LifecycleEvent) -> None:
        key = AgentKey(msg.server_id, msg.pane_id)
        status = Status.BLOCKED if msg.kind == "blocked" else Status.DONE
        with self._lock:
            state = self._agents.get(key)
        if _in_episode(state, status, msg.episode_id):
            self._fire_notify(msg.kind, state, at_ms=msg.at_ms)

    def _ev_handle(self, msg: LifecycleEvent, *, alert: bool) -> None:
        handler = {
            "blocked": self._ev_blocked,
            "done": self._ev_done,
            "unblocked": self._ev_closed,
            "cleared": self._ev_closed,
            "answered": self._ev_answered,
        }.get(msg.kind)
        if handler is not None:
            handler(msg, alert)

    def _ev_blocked(self, msg: LifecycleEvent, alert: bool) -> None:
        key = AgentKey(msg.server_id, msg.pane_id)
        reopened = False
        with self._preread_cv:
            state = self._agents.get(key)
            current = _in_episode(state, Status.BLOCKED, msg.episode_id)
            if current:
                self._block_episode[key] = msg.episode_id
                previous = self._ev_revision.get(key)
                self._ev_revision[key] = (msg.episode_id, msg.prompt_revision)
                if msg.prompt is not None:
                    self._preread[key] = msg.prompt
                    self._ev_prompt_missing.discard(key)
                else:
                    self._ev_prompt_missing.add(key)
                # The same episode asks a new question after it was answered:
                # it needs an answer (and an alert) again.
                if (
                    previous is not None
                    and previous[0] == msg.episode_id
                    and previous[1] != msg.prompt_revision
                    and msg.episode_id in self._answered_episodes
                ):
                    del self._answered_episodes[msg.episode_id]
                    reopened = True
                self._preread_cv.notify_all()
        if not current:
            return

        def mutate():
            orch = self._orch
            if msg.prompt and orch is not None and orch.drill_key() == key:
                orch.set_detection(msg.prompt)
            self._reconcile_prereads()  # a prompt-less event falls back to a read
            return True

        self._apply(mutate)
        if alert or reopened:
            self._fire_notify("blocked", state, at_ms=msg.at_ms)

    def _ev_done(self, msg: LifecycleEvent, alert: bool) -> None:
        if alert:
            self._ev_alert(msg)

    def _ev_closed(self, msg: LifecycleEvent, alert: bool) -> None:
        key = AgentKey(msg.server_id, msg.pane_id)
        event = "blocked" if msg.kind == "unblocked" else "done"
        status = Status.BLOCKED if event == "blocked" else Status.DONE
        with self._lock:
            state = self._agents.get(key)
            if self._ev_revision.get(key, ("",))[0] == msg.episode_id:
                self._ev_revision.pop(key, None)
                self._ev_prompt_missing.discard(key)
            if self._reminders.get(key, ("",))[0] == msg.episode_id:
                self._reminders.pop(key, None)
            # Already in a NEWER episode of the same kind: its banner stays.
            newer = (
                state is not None
                and state.status is status
                and bool(state.episode_id)
                and state.episode_id != msg.episode_id
            )
        if not newer:
            self._withdraw_left(event, {key})

    def _ev_answered(self, msg: LifecycleEvent, alert: bool) -> None:
        key = AgentKey(msg.server_id, msg.pane_id)
        with self._lock:
            elsewhere = msg.episode_id not in self._answered_episodes
            self._note_episode_answered_locked(msg.episode_id)
            if self._reminders.get(key, ("",))[0] == msg.episode_id:
                self._reminders.pop(key, None)
            current = self._block_episode.get(key) == msg.episode_id
            if current:
                self._bump_semantic_locked((key,))
        log.info(
            "episode answered agent=%s:%s by=%s%s",
            key.server_id,
            key.pane_id,
            msg.by or "?",
            " (elsewhere)" if elsewhere else "",
        )
        if not current:
            return
        self._withdraw_left("blocked", {key})

        def mutate():
            if elsewhere and self._orch is not None:
                self._orch.note_external_answer(key)
            return True

        self._apply(mutate)

    # --- outgoing answers -----------------------------------------------------
    def _stamp_answer(self, server_id: str, msg: dict) -> dict:
        """Add the pane's bridge ``episode_id`` / ``prompt_revision`` to an
        answer so the bridge can refuse a second answer to one episode."""
        if not isinstance(msg, dict) or msg.get("type") not in ANSWER_TYPES:
            return msg
        if msg.get("type") == "act" and msg.get("guard") is False:
            return msg  # a forced key (stop) is not an answer
        if "episode_id" in msg or not isinstance(msg.get("pane_id"), str):
            return msg
        key = AgentKey(server_id, msg["pane_id"])
        with self._lock:
            state = self._agents.get(key)
            if state is None or state.status is not Status.BLOCKED or not state.episode_id:
                return msg
            if self._block_episode.get(key) != state.episode_id:
                return msg
            revision = self._ev_revision.get(key)
        out = {**msg, "episode_id": state.episode_id}
        if revision is not None and revision[0] == state.episode_id and revision[1]:
            out["prompt_revision"] = revision[1]
        return out

    def _wrap_runner(self, server_id: str, runner):
        return StampingRunner(runner, lambda msg: self._stamp_answer(server_id, msg))

    # --- reminders --------------------------------------------------------------
    def _reminder_start(self, at_ms: int | None) -> tuple[float, int]:
        """(since on the notify clock, reminders already due) for a block that
        began at bridge time ``at_ms`` (None: now)."""
        now = self._notify_clock()
        if at_ms is None:
            return now, 0
        elapsed = max(0.0, time.time() - at_ms / 1000.0)
        interval = self._config.notifications.remind_after * 60.0
        # Reminders that fell due before we heard of the block are not sent late.
        return now - elapsed, int(elapsed // interval) if interval > 0 else 0


def _start_timer(delay: float, fn) -> None:
    timer = threading.Timer(delay, fn)
    timer.daemon = True
    timer.start()


def _in_episode(state: AgentState | None, status: Status, episode_id: str) -> bool:
    return (
        state is not None
        and state.status is status
        and (not state.episode_id or state.episode_id == episode_id)
    )
