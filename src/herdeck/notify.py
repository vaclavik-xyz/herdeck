from __future__ import annotations

import logging
import subprocess
import urllib.parse
import urllib.request
from collections.abc import Callable

log = logging.getLogger("herdeck.notify")


def escape_applescript(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _macos_sink(title: str, body: str, sound: bool) -> None:
    t, b = escape_applescript(title), escape_applescript(body)
    script = f'display notification "{b}" with title "{t}"'
    if sound:
        script += ' sound name "Glass"'
    subprocess.run(
        ["osascript", "-e", script],
        timeout=5,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


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
) -> Callable[[str, str, bool], None]:
    """Sink that posts the alert to a Telegram chat via the Bot API."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    def sink(title: str, body: str, sound: bool) -> None:
        fields = {
            "chat_id": str(chat_id),
            "text": f"{title}\n{body}",
            "disable_notification": "false" if sound else "true",
        }
        if message_thread_id is not None:
            fields["message_thread_id"] = str(message_thread_id)
        post(url, fields)

    return sink


def composite_sink(
    sinks: list[Callable[[str, str, bool], None]],
) -> Callable[[str, str, bool], None]:
    """Fan out to multiple sinks; one failing sink never stops the others."""

    def sink(title: str, body: str, sound: bool) -> None:
        for s in sinks:
            try:
                s(title, body, sound)
            except Exception:
                log.debug("notify sink failed", exc_info=True)

    return sink


class Notifier:
    """Fires notifications via an injectable sink; never raises."""

    def __init__(self, sink: Callable[[str, str, bool], None] = _macos_sink):
        self._sink = sink

    def notify(self, title: str, body: str, sound: bool = False) -> None:
        try:
            self._sink(title, body, sound)
        except Exception:
            log.debug("notify failed", exc_info=True)


class NoopNotifier(Notifier):
    def __init__(self):
        super().__init__(sink=lambda *a: None)
