from __future__ import annotations

import asyncio
import logging
import subprocess
import time
import urllib.parse
import urllib.request
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

    The deck shell (desktop app) polls `/state` and posts these under its own
    bundle identity, so the banner carries the Herdeck name and icon instead of
    the anonymous "Script Editor" attribution an osascript notification gets.
    ``seq`` is monotonic per feed instance; the shell diffs against its last
    seen value (a smaller seq after a source swap means "reset, nothing new").
    """

    def __init__(self, maxlen: int = 10):
        self._items: deque[dict] = deque(maxlen=maxlen)
        self._seq = 0

    def push(self, title: str, body: str, sound: bool | str) -> None:
        self._seq += 1
        self._items.append(
            {"seq": self._seq, "title": title, "body": body, "sound": sound}
        )

    def reset(self) -> None:
        """Drop everything and restart the sequence from zero.

        Called when a NEW shell claims banner duty after a gap: the shell's
        counter starts at 0, so without the reset it would replay every stale
        entry still sitting in the feed as a "new" banner.
        """
        self._items.clear()
        self._seq = 0

    def state(self) -> dict:
        return {"seq": self._seq, "items": list(self._items)}


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

    Always records into the feed (the shell turns that into the banner) and
    plays the event sound directly — audio carries no app attribution, so the
    per-event system sound works without the osascript banner. When the gate
    says no shell is attached to post banners (or its notification permission
    is missing), the whole alert falls back to a plain osascript notification
    and no separate audio plays (the fallback banner carries the sound).
    """

    def sink(title: str, body: str, sound: bool | str) -> None:
        feed.push(title, body, sound)
        if gate():
            if sound and isinstance(sound, str):
                sound_player(sound)
            elif sound:  # plain on/off switch -> the historical default
                sound_player("Glass")
            return
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
