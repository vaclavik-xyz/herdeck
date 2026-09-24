"""Where the runtime's provider usage comes from: a bridge or its own poller.

A bridge started with ``HERDECK_BRIDGE_USAGE=1`` polls Codex/Claude on the
agents' Mac and pushes ``usage`` frames (capability ``usage``, see
bridge.BridgeUsageFeed). ``[usage].source`` picks the input:

* ``auto`` (default): bridge data while a connected bridge offers it, else
  this runtime's own ``usage.UsagePoller``. Until every configured server has
  answered (or ``grace_s`` passed since start/swap) nothing is decided, so a
  thin client never spawns ``codex app-server`` just before its bridge shows
  up. A bridge that drops its offer (disconnect, restart without usage) keeps
  its last numbers for ``grace_s``; only then does ``auto`` fall back to local.
  A bridge that offers usage but has no numbers (its poller failing, e.g. a
  LaunchDaemon bridge whose codex/codexbar cannot reach the login keychain)
  counts for ``empty_grace_s`` — long enough for a first poll — and then
  ``auto`` falls back to local until the bridge sends numbers again.
* ``local``: only the own poller (the pre-bridge behaviour).
* ``bridge``: only bridge data; never a local poll.

Filtering is done HERE, on the runtime, for bridge data: the frame is
unfiltered, and this runtime's ``[usage].providers`` (allow-list + panel
order) and ``paid_only`` apply exactly as they do to the local poller. With
several bridges each provider comes from the first server in config order
that has it (under paid_only: the first with a paid subscription).

Alerts and pace: a local poller keeps doing both itself. For bridge data the
hub runs its own ``usage_alerts.UsageTracker`` (this runtime's ``alert_at`` /
``alert_reset``) over the visible providers on every change and once per
``refresh_secs`` for the clock-driven reset alert; the pace hint
(``full_early_s``) is the bridge's, whose poller sees every poll. The tracker
restarts with a silent baseline whenever the hub switches to bridge data, so a
switch never re-announces a level the local poller already announced.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .usage_alerts import UsageTracker

log = logging.getLogger(__name__)

# Undecided window after start / a source swap, and how long a lost bridge
# offer keeps its numbers (a network flap must not spawn a local poller).
_GRACE_S = 15.0
_TICK_S = 5.0
# How long a bridge that offers usage may send no numbers before ``auto``
# stops waiting for it: covers a first bridge poll (codex app-server start
# <= 60 s, codexbar <= 120 s).
_EMPTY_GRACE_S = 180.0


@dataclass
class _Bridge:
    known: bool = False  # answered at least once on this hub (snapshot/frame/down)
    offered: bool = False
    lost_at: float | None = None
    empty_since: float | None = None  # offered but no numbers since then
    data: dict = field(default_factory=dict)  # provider -> ProviderUsage


class UsageHub:
    """Poller-shaped (``snapshot()`` / ``close()``) usage input for DeckApp.

    Thread-safe: ``bridge_update`` arrives on connector threads, ``snapshot``
    on render threads, the tick on its own daemon thread. Callbacks
    (``on_change``, ``on_alert``) are never called with the hub lock held."""

    def __init__(
        self,
        usage_config,
        *,
        local_factory: Callable[[], object],
        on_alert: Callable[[list], None] | None = None,
        on_change: Callable[[], None] | None = None,
        server_ids=(),
        clock=time.monotonic,
        wall_clock=time.time,
        grace_s: float = _GRACE_S,
        empty_grace_s: float = _EMPTY_GRACE_S,
        tick_s: float = _TICK_S,
        run_thread: bool = True,
    ):
        self._source = getattr(usage_config, "source", "auto")
        self._providers = list(usage_config.providers)
        self._paid_only = bool(usage_config.paid_only)
        self._refresh = max(30.0, float(usage_config.refresh_secs))
        self._alert_at = list(usage_config.alert_at)
        self._alert_reset = bool(usage_config.alert_reset)
        self._local_factory = local_factory
        self._on_alert = on_alert
        self._on_change = on_change
        self._clock = clock
        self._wall_clock = wall_clock
        self._grace = grace_s
        self._empty_grace = empty_grace_s
        self._lock = threading.Lock()
        # Serializes evaluate(): the tracker is single-threaded and views must
        # reach it in order. Never held by a caller of the hub's own lock.
        self._eval_lock = threading.Lock()
        self._order: list[str] = list(server_ids)
        self._bridges: dict[str, _Bridge] = {}
        self._undecided_since = clock()
        self._mode: str | None = None  # "local" | "bridge" | None (undecided)
        self._local = None
        self._tracker = UsageTracker(self._alert_at, self._alert_reset)
        self._last_observe = float("-inf")
        self._last_view: list | None = None
        self._closed = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.evaluate()
        if run_thread:
            self._thread = threading.Thread(
                target=self._run, args=(tick_s,), name="herdeck-usage-hub", daemon=True
            )
            self._thread.start()

    # --- inputs ---------------------------------------------------------------

    def set_servers(self, server_ids) -> None:
        """Adopt a (new) source's servers in config order. Earlier bridge data
        is kept for ``grace_s`` as lost (the new source's connectors replay
        or re-report), and the undecided window restarts."""
        now = self._clock()
        with self._lock:
            self._order = list(server_ids)
            kept: dict[str, _Bridge] = {}
            for sid, bridge in self._bridges.items():
                if sid in self._order and bridge.data:
                    kept[sid] = _Bridge(
                        known=False, offered=False, lost_at=now, data=dict(bridge.data)
                    )
            self._bridges = kept
            self._undecided_since = now
        self.evaluate()

    def bridge_update(self, server_id: str, offered: bool, providers: list | None = None) -> None:
        """Connector callback: ``offered`` = the bridge advertises ``usage``
        (False when it disconnected); ``providers`` = a usage frame's data."""
        now = self._clock()
        with self._lock:
            if self._closed:
                return
            bridge = self._bridges.setdefault(server_id, _Bridge())
            bridge.known = True
            if offered:
                bridge.offered = True
                bridge.lost_at = None
                if providers is not None:
                    bridge.data = {usage.provider: usage for usage in providers}
                if bridge.data:
                    bridge.empty_since = None
                elif bridge.empty_since is None:
                    bridge.empty_since = now
            elif bridge.offered:
                bridge.offered = False
                bridge.lost_at = now
        self.evaluate()

    # --- outputs --------------------------------------------------------------

    def snapshot(self) -> list:
        with self._lock:
            mode, local = self._mode, self._local
            if mode == "bridge":
                return self._bridge_view_locked(self._clock())
        if mode == "local" and local is not None:
            return local.snapshot()
        return []

    @property
    def mode(self) -> str | None:
        with self._lock:
            return self._mode

    def health(self) -> dict:
        """Non-secret facts for /health: configured source, the active input
        and which servers currently offer usage."""
        now = self._clock()
        with self._lock:
            return {
                "source": self._source,
                "active": self._mode,
                "bridges": [sid for sid in self._ordered_ids_locked() if self._live(sid, now)],
                # offering usage but without numbers past the empty grace
                "bridges_empty": [
                    sid
                    for sid in self._ordered_ids_locked()
                    if self._live(sid, now) and not self._useful(sid, now)
                ],
            }

    def close(self) -> None:
        with self._lock:
            self._closed = True
            local, self._local = self._local, None
            self._mode = None
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
        if local is not None:
            _close_quietly(local)

    # --- decision -------------------------------------------------------------

    def evaluate(self) -> None:
        """Re-decide the input; start/stop the local poller; for bridge data,
        feed the alert tracker when the visible numbers changed. Called on
        every input and by the tick thread (time-based transitions)."""
        with self._eval_lock:
            changed = self._evaluate_serialized()
        if changed and self._on_change is not None:
            try:
                self._on_change()
            except Exception:
                log.warning("usage change callback failed", exc_info=True)

    def _evaluate_serialized(self) -> bool:
        now = self._clock()
        start_local = stop_local = None
        observe = None
        changed = False
        with self._lock:
            if self._closed:
                return False
            desired = self._desired_locked(now)
            if desired is not None and desired != self._mode:
                log.info("usage source: %s -> %s", self._mode or "none", desired)
                if self._mode == "local":
                    stop_local, self._local = self._local, None
                self._mode = desired
                changed = True
                if desired == "local":
                    start_local = True
                else:
                    # Silent baseline on every switch to bridge data.
                    self._tracker = UsageTracker(self._alert_at, self._alert_reset)
                    self._last_observe = float("-inf")
                    self._last_view = None
            if self._mode == "bridge":
                view = self._bridge_view_locked(now)
                if view != self._last_view or now - self._last_observe >= self._refresh:
                    changed = changed or view != self._last_view
                    self._last_view = view
                    self._last_observe = now
                    observe = view
        if stop_local is not None:
            # Off-thread: a poller close joins its thread and stops codex.
            threading.Thread(target=_close_quietly, args=(stop_local,), daemon=True).start()
        if start_local:
            self._start_local()
        if observe is not None:
            self._observe(observe)
        return changed

    def _desired_locked(self, now: float) -> str | None:
        if self._source == "local":
            return "local"
        if self._source == "bridge":
            return "bridge"
        if any(self._useful(sid, now) for sid in self._bridges):
            return "bridge"
        resolved = all(self._bridges.get(sid, _Bridge()).known for sid in self._order)
        if resolved or now - self._undecided_since >= self._grace:
            return "local"
        return None

    def _live(self, sid: str, now: float) -> bool:
        bridge = self._bridges.get(sid)
        if bridge is None:
            return False
        if bridge.offered:
            return True
        return bridge.lost_at is not None and now - bridge.lost_at < self._grace

    def _useful(self, sid: str, now: float) -> bool:
        """Live, and with numbers — or still inside its first-poll grace."""
        if not self._live(sid, now):
            return False
        bridge = self._bridges[sid]
        if bridge.data:
            return True
        return bridge.empty_since is not None and now - bridge.empty_since < self._empty_grace

    def _ordered_ids_locked(self) -> list[str]:
        extra = [sid for sid in self._bridges if sid not in self._order]
        return [*self._order, *extra]

    def _bridge_view_locked(self, now: float) -> list:
        live = [sid for sid in self._ordered_ids_locked() if self._live(sid, now)]
        out = []
        for provider in self._providers:
            for sid in live:
                usage = self._bridges[sid].data.get(provider)
                if usage is None:
                    continue
                if self._paid_only and usage.subscription != "paid":
                    continue
                out.append(usage)
                break
        return out

    def _start_local(self) -> None:
        try:
            poller = self._local_factory()
        except Exception:
            log.warning("usage poller could not be built", exc_info=True)
            return
        if poller is None:
            return
        with self._lock:
            if self._closed or self._mode != "local" or self._local is not None:
                keep = False
            else:
                self._local = poller
                keep = True
        if not keep:
            _close_quietly(poller)
            return
        poller.start()

    def _observe(self, view: list) -> None:
        # The tracker's pace annotation is discarded: the bridge's is better.
        _annotated, alerts = self._tracker.observe(view, self._wall_clock())
        if alerts and self._on_alert is not None:
            try:
                self._on_alert(alerts)
            except Exception:
                log.warning("usage alert delivery failed", exc_info=True)

    def _run(self, tick_s: float) -> None:
        while not self._stop.wait(tick_s):
            try:
                self.evaluate()
            except Exception:
                log.warning("usage hub tick failed", exc_info=True)


def _close_quietly(poller) -> None:
    try:
        poller.close()
    except Exception:
        pass
