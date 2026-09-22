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
from pathlib import Path
from typing import Protocol

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


def _sink_name(sink) -> str:
    return getattr(sink, "_notify_name", None) or getattr(sink, "__name__", "sink")


def escape_applescript(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _macos_sink(title: str, body: str, sound: bool | str) -> None:
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


class NotificationFeed:
    """In-process record of recent event notifications.

    The deck shell (desktop app) long-polls `/notifications` and posts these under its own
    bundle identity, so the banner carries the Herdeck name and icon instead of
    the anonymous "Script Editor" attribution an osascript notification gets.
    ``seq`` is monotonic within a generation; acknowledgements make a shell
    restart safe without clearing pending events.
    """

    def __init__(self, maxlen: int = 10):
        self._items: deque[dict] = deque(maxlen=maxlen)
        self._generation = uuid.uuid4().hex
        self._seq = 0
        self._acked_seq = 0
        self._fallback_seq: int | None = None
        self._changed = threading.Condition()

    def push(self, title: str, body: str, sound: bool | str) -> dict:
        with self._changed:
            self._seq += 1
            item = {
                "id": f"{self._generation}:{self._seq}",
                "generation": self._generation,
                "seq": self._seq,
                "title": title,
                "body": body,
                "sound": sound,
                "created_at_ms": time.time_ns() // 1_000_000,
            }
            self._items.append(item)
            self._changed.notify_all()
        log.info("notification queued id=%s title=%r", item["id"], title)
        return item

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
                self._acked_seq = max(self._acked_seq, after_seq)
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
            self._acked_seq = max(self._acked_seq, seq)
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
            self._acked_seq = max(self._acked_seq, seq)
            self._fallback_seq = None
            self._changed.notify_all()
        log.info("notification fallback delivered id=%s:%s", generation, seq)
        return True


_SOUND_DIR = Path("/System/Library/Sounds")


def play_sound_file(name: str) -> bool:
    """Play a macOS system sound via `afplay` (audio only, no banner).

    Unlike an osascript notification this is not attributed to any app, so the
    shell can own the banner while the runtime still honors the per-event
    system sound (`True` maps to the historical "Glass" default). Returns
    False when the sound name is not a stock system sound.
    """
    path = _SOUND_DIR / f"{name}.aiff"
    if not path.is_file():
        return False
    subprocess.run(
        ["/usr/bin/afplay", str(path)],
        timeout=10,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return True


def runtime_sink(
    feed: NotificationFeed,
    gate: Callable[[], bool],
    *,
    sound_player: Callable[[str], bool] = play_sound_file,
    fallback: Callable[[str, str, bool | str], None] = _macos_sink,
) -> Callable[[str, str, bool | str], None]:
    """Sink for the deckapp runtime path.

    With a live shell, records once into its acknowledged feed; the shell owns
    both the native banner and sound. Without a shell, delivers only through
    the osascript fallback. Never doing both removes the handoff replay race.
    """

    def sink(title: str, body: str, sound: bool | str) -> None:
        if gate():
            feed.push(title, body, sound)
            return
        log.info("notification fallback=osascript title=%r", title)
        fallback(title, body, sound)

    return sink


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

    def sink(title: str, body: str, sound: bool | str) -> None:
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
    sound_player: Callable[[str], bool] = play_sound_file,
    getenv=get_secret,
    telegram_factory=make_telegram_sink,
    macos_sink=_macos_sink,
) -> Callable[[str, str, bool | str], None]:
    """Deckapp runtime sink honoring ``[notifications.backends]``.

    The shell claim pipeline (feed + sound + osascript fallback) serves the
    "macos" backend; telegram fires independently and always. When
    notifications are disabled, nothing fires and the feed stays empty.
    """

    n = config.notifications
    if not n.enabled:

        def noop(title: str, body: str, sound: bool | str) -> None:
            pass

        return noop

    macos_on = "macos" in n.backends
    sinks: list[Callable[[str, str, bool | str], None]] = [
        runtime_sink(
            feed,
            gate,
            sound_player=sound_player,
            fallback=macos_sink if macos_on else (lambda t, b, s: None),
        )
    ]
    if not macos_on:
        # No shell banner should fire either: the runtime's feed drives the
        # shell banners, which are the macOS backend's job.
        sinks[0] = lambda t, b, s: None  # noqa: E731
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

    def sink(title: str, body: str, sound: bool | str) -> None:
        for s in sinks:
            try:
                s(title, body, sound)
            except Exception as exc:
                _warn_failure(_sink_name(s), exc)

    return sink


class Notifier:
    """Fires notifications via an injectable sink; never raises."""

    def __init__(self, sink: Callable[[str, str, bool | str], None] = _macos_sink):
        self._sink = sink

    def notify(self, title: str, body: str, sound: bool | str = False) -> None:
        try:
            self._sink(title, body, sound)
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
    def __init__(self, notifier: Notifier):
        self._notifier = notifier

    async def notify_blocked(
        self,
        agent: AgentState,
        *,
        body: str,
        sound: bool | str,
        multi_server: bool,
    ) -> None:
        await asyncio.to_thread(self._notifier.notify, agent.agent_type, body, sound)


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
