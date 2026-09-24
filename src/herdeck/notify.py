from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .i18n import tr
from .model import AgentState
from .secrets import get_secret

log = logging.getLogger("herdeck.notify")

# Identical delivery failures re-log at WARNING at most this often; the
# repeats stay at DEBUG so a flaky network cannot spam the log.
_WARN_EVERY_S = 300.0
_last_warned: dict[str, tuple[str, float]] = {}
_monotonic = time.monotonic  # test seam


def _warn_failure(context: str, exc: Exception) -> None:
    """Surface a notification delivery failure. The whole point of
    notifications is being away from the deck — a wrong bot token or revoked
    bot must not disappear at DEBUG while blocked agents sit unanswered."""
    reason = str(exc) or type(exc).__name__
    prev = _last_warned.get(context)
    now = _monotonic()
    if prev is not None and prev[0] == reason and now - prev[1] < _WARN_EVERY_S:
        log.debug("notify via %s failed again: %s", context, reason)
        return
    _last_warned[context] = (reason, now)
    log.warning("notify via %s failed: %s", context, reason)


# Per agent+event re-notify floor (seconds). The notification state machine
# re-arms whenever an agent LEAVES a status, so an agent that flaps
# working -> done -> working -> done (or a detection flicker on blocked)
# would otherwise alert on every re-entry. "done" is informational, so a
# minute of quiet per agent is fine; "blocked" needs the user, so it only
# gets a short flap guard.
NOTIFY_COOLDOWN_S: dict[str, float] = {"blocked": 5.0, "done": 60.0}
# A "done" this soon after the user pressed/answered that very agent on the
# deck is the expected result of their own action, not news.
DONE_AFTER_INTERACTION_S = 10.0
_THROTTLE_MAX_KEYS = 1024


def event_title(agent_type: str, event: str, lang: str = "en") -> str:
    """Localized notification title, e.g. ``claude · needs input``."""
    key = "notify.title_blocked" if event == "blocked" else "notify.title_done"
    return tr(lang, key, agent=agent_type or "agent")


class NotifyThrottle:
    """Cooldown + "you just touched it" suppression for event alerts.

    Only gates delivery: the caller's episode bookkeeping (``newly_entered``)
    still advances, so a suppressed alert is dropped, never queued.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        cooldowns: dict[str, float] | None = None,
        interaction_window: float = DONE_AFTER_INTERACTION_S,
    ):
        self._clock = clock
        self._cooldowns = dict(NOTIFY_COOLDOWN_S if cooldowns is None else cooldowns)
        self._interaction_window = interaction_window
        self._fired: dict[tuple[str, object], float] = {}
        self._touched: dict[object, float] = {}
        self._lock = threading.Lock()

    def note_interaction(self, key) -> None:
        """The user pressed/answered this agent on the deck just now."""
        with self._lock:
            self._touched[key] = self._clock()
            if len(self._touched) > _THROTTLE_MAX_KEYS:
                self._prune(self._touched, self._interaction_window)

    def forget(self, key) -> None:
        """A recycled pane (new terminal identity) is a new agent: drop its history."""
        with self._lock:
            self._touched.pop(key, None)
            for k in [k for k in self._fired if k[1] == key]:
                del self._fired[k]

    def allow(self, event: str, key) -> bool:
        """True when an alert for ``(event, key)`` may fire now (and records it)."""
        with self._lock:
            now = self._clock()
            if event == "done":
                touched = self._touched.get(key)
                if touched is not None and now - touched < self._interaction_window:
                    return False
            last = self._fired.get((event, key))
            if last is not None and now - last < self._cooldowns.get(event, 0.0):
                return False
            self._fired[(event, key)] = now
            if len(self._fired) > _THROTTLE_MAX_KEYS:
                self._prune(self._fired, max(self._cooldowns.values(), default=0.0))
            return True

    def _prune(self, table: dict, horizon: float) -> None:
        now = self._clock()
        for k in [k for k, at in table.items() if now - at >= horizon]:
            del table[k]


def _sink_name(sink) -> str:
    return getattr(sink, "_notify_name", None) or getattr(sink, "__name__", "sink")


def escape_applescript(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _macos_sink(title: str, body: str, sound: bool | str, icon: str | None = None) -> None:
    # ``icon`` is accepted but unused: `display notification` cannot attach images.
    t, b = escape_applescript(title), escape_applescript(body)
    script = f'display notification "{b}" with title "{t}"'
    # `sound` is either a macOS system sound name or a plain on/off switch
    # (True keeps the historical "Glass" default).
    if sound:
        name = sound if isinstance(sound, str) else "Glass"
        script += f' sound name "{escape_applescript(name)}"'
    subprocess.run(
        ["osascript", "-e", script],
        timeout=5,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


# Optional per-item fields beside title/body/sound/icon. ``agent`` is
# ``{"server_id", "pane_id"}``; ``event`` is "blocked"/"done"; a blocked alert
# also carries ``episode`` (the block episode it belongs to — an answer is only
# applied while the agent is still blocked in that episode), and with
# [notifications].banner_actions either ``actions`` (``[{"id", "label"}]`` for a
# binary approve/deny prompt, plus its option signature ``sig``) or ``reply``
# (the inline reply field's placeholder).
FEED_META_KEYS = frozenset({"agent", "event", "episode", "sig", "actions", "reply"})


class NotificationFeed:
    """In-process record of recent event notifications.

    The deck shell (desktop app) long-polls `/notifications` and posts these under its own
    bundle identity, so the banner carries the Herdeck name and icon instead of
    the anonymous "Script Editor" attribution an osascript notification gets.
    ``seq`` is monotonic within a generation; acknowledgements make a shell
    restart safe without clearing pending events.
    """

    # Big enough for a whole-fleet burst (every agent finishing at once) while
    # the shell is mid-delivery; see push() for the overflow policy.
    DEFAULT_MAXLEN = 50

    def __init__(self, maxlen: int = DEFAULT_MAXLEN):
        self._maxlen = max(1, maxlen)
        self._items: deque[dict] = deque()
        self.dropped = 0  # undelivered items evicted by overflow (observability)
        # Lifetime counters for the runtime /health (they survive reset()).
        self.queued = 0
        self.acked = 0  # delivered: shell acknowledgements + runtime fallbacks
        self.fallbacks = 0
        self._generation = uuid.uuid4().hex
        self._seq = 0
        self._acked_seq = 0
        self._fallback_seq: int | None = None
        self._changed = threading.Condition()

    def push(
        self,
        title: str,
        body: str,
        sound: bool | str,
        icon: str | None = None,
        meta: dict | None = None,
    ) -> dict:
        """Queue one banner. ``meta`` (see FEED_META_KEYS) tells the shell which
        agent the banner is about, so a click can open that agent's drill and a
        blocked banner can carry answer buttons bound to its block episode."""
        with self._changed:
            self._seq += 1
            self.queued += 1
            item = {
                "id": f"{self._generation}:{self._seq}",
                "generation": self._generation,
                "seq": self._seq,
                "kind": "alert",
                "title": title,
                "body": body,
                "sound": sound,
                # Absolute path of the project mark PNG (notify_icons), or None.
                "icon": icon,
                "created_at_ms": time.time_ns() // 1_000_000,
            }
            for key, value in (meta or {}).items():
                if key in FEED_META_KEYS and value is not None:
                    item[key] = value
            self._items.append(item)
            dropped = self._trim_locked()
            self._changed.notify_all()
        log.info("notification queued id=%s title=%r", item["id"], title)
        if dropped:
            log.warning(
                "notification feed overflow: dropped %d undelivered item(s) "
                "(capacity %d); the shell is not keeping up",
                dropped,
                self._maxlen,
            )
        return item

    def _trim_locked(self) -> int:
        """Enforce capacity. Already-delivered (acked) items go first; an
        undelivered item is evicted only when the feed is full of undelivered
        work, and that loss is counted + logged instead of vanishing silently.
        Returns how many undelivered items were dropped."""
        if len(self._items) <= self._maxlen:
            return 0
        acked = self._acked_seq
        kept = deque(item for item in self._items if item["seq"] > acked)
        # Keep the newest acked history only if there is room left over.
        room = self._maxlen - len(kept)
        if room > 0:
            history = [item for item in self._items if item["seq"] <= acked][-room:]
            kept = deque([*history, *kept])
        dropped = 0
        while len(kept) > self._maxlen:
            kept.popleft()
            dropped += 1
        self._items = kept
        self.dropped += dropped
        return dropped

    def _advance_acked_locked(self, seq: int) -> None:
        if seq > self._acked_seq:
            self.acked += seq - self._acked_seq
            self._acked_seq = seq

    def stats(self) -> dict:
        """Delivery counters for the runtime /health: a banner that never
        showed up is visible here as pending, dropped or a fallback."""
        with self._changed:
            return {
                "queued": self.queued,
                "acked": self.acked,
                "fallback": self.fallbacks,
                "dropped": self.dropped,
                "pending": max(0, self._seq - self._acked_seq),
            }

    def reset(self) -> None:
        """Drop everything and restart the sequence from zero.

        Called when a NEW shell claims banner duty after a gap: the shell's
        counter starts at 0, so without the reset it would replay every stale
        entry still sitting in the feed as a "new" banner.
        """
        with self._changed:
            self._items.clear()
            self._generation = uuid.uuid4().hex
            self._seq = 0
            self._acked_seq = 0
            self._fallback_seq = None
            self._changed.notify_all()

    def state(self) -> dict:
        with self._changed:
            return {
                "generation": self._generation,
                "seq": self._seq,
                "acked_seq": self._acked_seq,
                "items": list(self._items),
            }

    def wait(self, generation: str | None, after_seq: int, *, timeout: float) -> dict:
        """Wait until this cursor has pending work or the feed generation changes."""
        with self._changed:
            # `after_seq` is the shell's process-local post-delivery cursor. It
            # also repairs a lost explicit ACK without replaying after restart.
            if (
                self._fallback_seq is None
                and generation == self._generation
                and 0 <= after_seq <= self._seq
            ):
                self._advance_acked_locked(after_seq)
            self._changed.wait_for(
                lambda: generation != self._generation
                or (
                    self._fallback_seq is None
                    and any(
                    item["seq"] > max(after_seq, self._acked_seq)
                    for item in self._items
                    )
                ),
                timeout=max(0.0, timeout),
            )
            floor = self._acked_seq
            if generation == self._generation:
                floor = max(floor, after_seq)
            return {
                "generation": self._generation,
                "seq": self._seq,
                "acked_seq": self._acked_seq,
                "items": (
                    []
                    if self._fallback_seq is not None
                    else [item for item in self._items if item["seq"] > floor]
                ),
            }

    def ack(self, generation: str, seq: int) -> bool:
        with self._changed:
            if (
                generation != self._generation
                or seq < 0
                or seq > self._seq
                or self._fallback_seq is not None
            ):
                return False
            item = next((item for item in self._items if item["seq"] == seq), None)
            self._advance_acked_locked(seq)
        latency_ms = (
            max(0, time.time_ns() // 1_000_000 - item["created_at_ms"])
            if item is not None
            else None
        )
        log.info(
            "notification acknowledged id=%s:%s latency_ms=%s",
            generation,
            seq,
            latency_ms if latency_ms is not None else "unknown",
        )
        return True

    def fallback(
        self,
        generation: str,
        seq: int,
        deliver: Callable[[str, str, bool | str], None],
    ) -> bool:
        """Deliver one pending item through the runtime fallback, then ACK it."""
        with self._changed:
            if (
                generation != self._generation
                or seq <= self._acked_seq
                or self._fallback_seq is not None
            ):
                return False
            item = next((item for item in self._items if item["seq"] == seq), None)
            if item is None:
                return False
            payload = (item["title"], item["body"], item["sound"])
            self._fallback_seq = seq
            self._changed.notify_all()
        try:
            deliver(*payload)
        except Exception:
            with self._changed:
                self._fallback_seq = None
                self._changed.notify_all()
            raise
        with self._changed:
            if generation != self._generation or self._fallback_seq != seq:
                return False
            self._advance_acked_locked(seq)
            self.fallbacks += 1
            self._fallback_seq = None
            self._changed.notify_all()
        log.info("notification fallback delivered id=%s:%s", generation, seq)
        return True


def format_claim_age(age: float | None) -> str:
    """``12s`` / ``never`` for the fallback reason (seconds since the last claim)."""
    return "never" if age is None else f"{age:.0f}s"


def runtime_sink(
    feed: NotificationFeed,
    gate: Callable[[], bool],
    *,
    fallback: Callable[[str, str, bool | str], None] = _macos_sink,
    claim_age: Callable[[], float | None] | None = None,
) -> Callable[[str, str, bool | str], None]:
    """Sink for the deckapp runtime path.

    With a live shell, records once into its acknowledged feed; the shell posts
    the native banner with the sound attached. Without a shell, delivers only
    through the osascript fallback, whose ``sound name`` also rides on the
    notification. The sound is never played separately (e.g. via afplay): a
    detached sound would still play while Focus silences the banner. Never
    doing both feed and fallback removes the handoff replay race.

    ``claim_age`` (seconds since the shell last claimed banner duty, ``None``
    = never) makes the fallback line say WHY: with the desktop app installed a
    fallback is abnormal, and the age tells a lapsed claim from a missing app.
    """

    def sink(
        title: str,
        body: str,
        sound: bool | str,
        icon: str | None = None,
        meta: dict | None = None,
    ) -> None:
        if gate():
            feed.push(title, body, sound, icon, meta)
            return
        age = format_claim_age(claim_age()) if claim_age is not None else "unknown"
        log.warning(
            "notification fallback=osascript reason=no_shell_claim last_claim_age=%s title=%r",
            age,
            title,
        )
        fallback(title, body, sound)

    sink._accepts_meta = True
    return sink


def _sink_kwargs(sink, icon: str | None, meta: dict | None) -> dict:
    """Optional keyword arguments for one sink: ``icon`` only when set (plain
    three-argument sinks keep working) and ``meta`` only for sinks that declare
    ``_accepts_meta`` (the shell feed; Telegram and osascript ignore it)."""
    kwargs: dict = {}
    if icon is not None:
        kwargs["icon"] = icon
    if meta is not None and getattr(sink, "_accepts_meta", False):
        kwargs["meta"] = meta
    return kwargs


def _http_post(url: str, fields: dict[str, str]) -> None:
    data = urllib.parse.urlencode(fields).encode()
    with urllib.request.urlopen(url, data=data, timeout=5):
        pass


def make_telegram_sink(
    token: str,
    chat_id: str,
    message_thread_id: int | None = None,
    *,
    post: Callable[[str, dict[str, str]], None] = _http_post,
) -> Callable[[str, str, bool | str], None]:
    """Sink that posts the alert to a Telegram chat via the Bot API."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    def sink(title: str, body: str, sound: bool | str, icon: str | None = None) -> None:
        fields = {
            "chat_id": str(chat_id),
            "text": f"{title}\n{body}",
            "disable_notification": "false" if sound else "true",
        }
        if message_thread_id is not None:
            fields["message_thread_id"] = str(message_thread_id)
        post(url, fields)

    sink._notify_name = "telegram"
    return sink


def deckapp_sink(
    feed: NotificationFeed,
    gate: Callable[[], bool],
    config,  # herdeck.config.Config (duck-typed; notify.py stays import-free)
    *,
    getenv=get_secret,
    telegram_factory=make_telegram_sink,
    macos_sink=_macos_sink,
    claim_age: Callable[[], float | None] | None = None,
) -> Callable[[str, str, bool | str], None]:
    """Deckapp runtime sink honoring ``[notifications.backends]``.

    The shell claim pipeline (feed + osascript fallback) serves the
    "macos" backend; telegram fires independently and always. When
    notifications are disabled, nothing fires and the feed stays empty.
    """

    n = config.notifications
    if not n.enabled:

        def noop(title: str, body: str, sound: bool | str, icon: str | None = None) -> None:
            pass

        return noop

    macos_on = "macos" in n.backends
    sinks: list[Callable[[str, str, bool | str], None]] = [
        runtime_sink(
            feed,
            gate,
            fallback=macos_sink if macos_on else (lambda t, b, s: None),
            claim_age=claim_age,
        )
    ]
    if not macos_on:
        # No shell banner should fire either: the runtime's feed drives the
        # shell banners, which are the macOS backend's job.
        sinks[0] = lambda t, b, s, icon=None: None  # noqa: E731
    if "telegram" in n.backends:
        tg = n.telegram
        token = getenv(tg.token_env) if tg else None
        if tg and token and tg.chat_id:
            sinks.append(telegram_factory(token, tg.chat_id, tg.message_thread_id))
        else:
            log.warning(
                "telegram notifications enabled but token/chat_id "
                "missing; skipping telegram backend"
            )
    for backend in n.backends:
        if backend not in ("macos", "telegram"):
            log.warning("unknown notification backend %r; skipping", backend)
    if len(sinks) == 1:
        return sinks[0]
    return composite_sink(sinks)


def composite_sink(
    sinks: list[Callable[[str, str, bool | str], None]],
) -> Callable[[str, str, bool | str], None]:
    """Fan out to multiple sinks; one failing sink never stops the others."""

    def sink(
        title: str,
        body: str,
        sound: bool | str,
        icon: str | None = None,
        meta: dict | None = None,
    ) -> None:
        for s in sinks:
            try:
                s(title, body, sound, **_sink_kwargs(s, icon, meta))
            except Exception as exc:
                _warn_failure(_sink_name(s), exc)

    sink._accepts_meta = True
    return sink


class Notifier:
    """Fires notifications via an injectable sink; never raises."""

    def __init__(self, sink: Callable[[str, str, bool | str], None] = _macos_sink):
        self._sink = sink

    def notify(
        self,
        title: str,
        body: str,
        sound: bool | str = False,
        icon: str | None = None,
        meta: dict | None = None,
    ) -> None:
        """``icon`` (a PNG path for the banner) is passed on only when set, so
        plain three-argument sinks keep working; ``meta`` (the feed's agent
        fields, FEED_META_KEYS) only reaches sinks that accept it."""
        try:
            self._sink(title, body, sound, **_sink_kwargs(self._sink, icon, meta))
        except Exception as exc:
            _warn_failure(_sink_name(self._sink), exc)


class NoopNotifier(Notifier):
    def __init__(self):
        super().__init__(sink=lambda *a: None)


class BlockedAlertNotifier(Protocol):
    async def notify_blocked(
        self,
        agent: AgentState,
        *,
        body: str,
        sound: bool | str,
        multi_server: bool,
    ) -> None: ...


class InboundNotificationPoller(Protocol):
    async def poll_once(
        self, *, timeout: int = 20, is_current: Callable[[], bool] | None = None
    ) -> None: ...


@dataclass(frozen=True)
class BlockedNotificationRuntime:
    notifier: BlockedAlertNotifier
    poller: InboundNotificationPoller | None = None


class NoopBlockedNotifier:
    async def notify_blocked(
        self,
        agent: AgentState,
        *,
        body: str,
        sound: bool | str,
        multi_server: bool,
    ) -> None:
        return None


class LegacyBlockedNotifier:
    def __init__(self, notifier: Notifier, language: str = "en"):
        self._notifier = notifier
        self._language = language

    async def notify_blocked(
        self,
        agent: AgentState,
        *,
        body: str,
        sound: bool | str,
        multi_server: bool,
    ) -> None:
        title = event_title(agent.agent_type, "blocked", self._language)
        await asyncio.to_thread(self._notifier.notify, title, body, sound)


class CompositeBlockedNotifier:
    def __init__(self, notifiers: list[BlockedAlertNotifier]):
        self._notifiers = notifiers

    async def notify_blocked(
        self,
        agent: AgentState,
        *,
        body: str,
        sound: bool | str,
        multi_server: bool,
    ) -> None:
        for notifier in self._notifiers:
            try:
                await notifier.notify_blocked(
                    agent, body=body, sound=sound, multi_server=multi_server
                )
            except Exception:
                log.debug("blocked alert notifier failed", exc_info=True)
