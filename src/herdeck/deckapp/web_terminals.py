"""Browser live-terminal streams for the web cockpit.

``driver.web.WebDeck`` serves ``/term/<index>`` as Server-Sent Events drained
from a per-stream queue: ``{"kind": "meta"}`` first, then ``"frame"`` items,
ending with one ``"closed"`` item. This module fills those queues from the
current source's browser preview pool (``LiveSource.web_term_*``, the same
``observe`` plumbing the desktop agent card uses): one small pump thread per
stream long-polls the pool and forwards frames.
"""

from __future__ import annotations

import contextlib
import queue
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from ..i18n import tr

# Frames a slow browser may lag behind before its stream is ended (it then
# reopens from a fresh full frame).
QUEUE_MAX = 120
# Longest single wait for new frames; the pool's idle reaper (30 s) never
# fires on a pump that keeps polling.
POLL_WAIT_S = 5.0

# Pool close reasons that have a localized browser message.
_REASON_KEYS = {
    "disconnected": "web.term_disconnected",
    "stopped": "web.term_ended",
    "closed": "web.term_ended",
    "": "web.term_ended",
}


@dataclass
class TermSub:
    """One browser terminal subscription, drained by the HTTP thread."""

    req: str
    queue: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=QUEUE_MAX))
    cancelled: threading.Event = field(default_factory=threading.Event)
    session: str | None = None
    source: object | None = None


class WebTerminals:
    """``open``/``close`` callbacks for ``WebDeck.on_terminal``."""

    def __init__(
        self,
        current_source: Callable[[], object],
        *,
        tile_is_current: Callable[[int, int], bool],
        language: Callable[[], str],
        poll_wait_s: float = POLL_WAIT_S,
    ):
        self._current_source = current_source
        self._tile_is_current = tile_is_current
        self._language = language
        self._poll_wait_s = poll_wait_s

    def open(self, index: int, cols: int, rows: int, tile_version: int | None = None) -> TermSub:
        sub = TermSub(req=f"t{uuid.uuid4().hex[:12]}")
        threading.Thread(
            target=self._pump,
            args=(sub, index, cols, rows, tile_version),
            name="herdeck-web-term",
            daemon=True,
        ).start()
        return sub

    def close(self, sub: TermSub) -> None:
        sub.cancelled.set()
        session, source = sub.session, sub.source
        if session is not None and source is not None:
            with contextlib.suppress(Exception):
                source.web_term_close(session)

    # --- pump thread -------------------------------------------------------------
    def _message(self, key: str) -> str:
        return tr(self._language(), key)

    def _finish(self, sub: TermSub, reason: str) -> None:
        closed = {"kind": "closed", "reason": reason}
        try:
            sub.queue.put_nowait(closed)
        except queue.Full:
            # keep memory bounded but always deliver the final close marker
            with contextlib.suppress(queue.Empty):
                sub.queue.get_nowait()
            with contextlib.suppress(queue.Full):
                sub.queue.put_nowait(closed)

    def _pump(self, sub: TermSub, index: int, cols: int, rows: int, tile_version) -> None:
        if tile_version is not None and not self._tile_is_current(index, tile_version):
            self._finish(sub, self._message("web.term_no_agent"))
            return
        source = self._current_source()
        opener = getattr(source, "web_term_open", None)
        if not callable(opener):
            # a source without a bridge (the demo deck) has no terminals
            self._finish(sub, self._message("web.term_disconnected"))
            return
        opened = opener(index, cols, rows)
        if isinstance(opened, str):
            key = "web.term_no_agent" if opened == "no_agent" else "web.term_disconnected"
            self._finish(sub, self._message(key))
            return
        label, session = opened
        sub.source = source
        sub.session = session
        if sub.cancelled.is_set():  # the browser left while the observe started
            with contextlib.suppress(Exception):
                source.web_term_close(session)
            return
        sub.queue.put_nowait({"kind": "meta", "label": label})
        after = 0
        while not sub.cancelled.is_set():
            polled = source.web_term_poll(session, after, self._poll_wait_s)
            if polled is None:  # closed by close() or evicted
                if not sub.cancelled.is_set():
                    self._finish(sub, self._message("web.term_ended"))
                return
            if polled["gap"]:
                # frames were lost: end instead of painting a torn screen
                source.web_term_close(session)
                self._finish(sub, self._message("web.term_ended"))
                return
            for frame in polled["frames"]:
                try:
                    sub.queue.put_nowait({"kind": "frame", **frame})
                except queue.Full:
                    source.web_term_close(session)
                    self._finish(sub, self._message("web.term_ended"))
                    return
            after = polled["next"]
            closed = polled["closed"]
            if closed is not None:
                key = _REASON_KEYS.get(closed)
                self._finish(sub, self._message(key) if key else closed)
                return
