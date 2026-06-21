from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable

log = logging.getLogger("herdeck.notify")


def escape_applescript(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _macos_sink(title: str, body: str, sound: bool) -> None:
    t, b = escape_applescript(title), escape_applescript(body)
    script = f'display notification "{b}" with title "{t}"'
    if sound:
        script += ' sound name "Glass"'
    subprocess.run(["osascript", "-e", script], timeout=5,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


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
