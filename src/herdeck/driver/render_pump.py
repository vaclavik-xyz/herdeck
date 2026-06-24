from __future__ import annotations

import threading
import time
from collections.abc import Callable


class RenderPump:
    """Serialize blocking deck writes on a single worker thread, off the event loop.

    ``submit(channel, payload)`` records the LATEST payload per channel and returns
    immediately. The worker paints whatever channels are pending — newest wins, so a
    burst of submits collapses to the most recent state per channel (coalescing) and
    intermediate frames are dropped. While idle for ``keep_alive_interval`` seconds it
    calls ``keep_alive`` (so the device never blocks the loop and presses can't pile up).
    """

    # Paint order when several channels are pending in one cycle.
    CHANNELS = ("tiles", "panel", "working")

    def __init__(
        self,
        *,
        paint: Callable[[str, object], None],
        keep_alive: Callable[[], None] | None = None,
        keep_alive_interval: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._paint = paint
        self._keep_alive = keep_alive
        self._interval = keep_alive_interval
        self._clock = clock
        self._cv = threading.Condition()
        self._pending: dict[str, object] = {}
        self._stopped = False
        self._thread = threading.Thread(target=self._run, name="herdeck-render", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def submit(self, channel: str, payload: object) -> None:
        with self._cv:
            self._pending[channel] = payload
            self._cv.notify()

    def close(self, timeout: float = 2.0) -> None:
        with self._cv:
            self._stopped = True
            self._cv.notify()
        self._thread.join(timeout)

    def _run(self) -> None:
        next_keep_alive = self._clock() + self._interval
        while True:
            with self._cv:
                if not self._pending and not self._stopped:
                    # Wait only until the next keep-alive is due, so activity can't
                    # starve it; with no keep-alive, sleep until the next submit.
                    timeout = (
                        max(0.0, next_keep_alive - self._clock())
                        if self._keep_alive is not None
                        else None
                    )
                    self._cv.wait(timeout=timeout)
                if self._stopped:
                    return
                batch = self._pending
                self._pending = {}
            if "tiles" in batch:
                # A full render repaints every tile, so a coalesced partial spinner
                # update in the same batch is stale — drop it (would clobber fresh tiles).
                batch.pop("working", None)
            for ch in self.CHANNELS:
                if ch in batch:
                    self._safe(self._paint, ch, batch[ch])
            if self._keep_alive is not None and self._clock() >= next_keep_alive:
                self._safe(self._keep_alive)
                next_keep_alive = self._clock() + self._interval

    @staticmethod
    def _safe(fn, *args) -> None:
        try:
            fn(*args)
        except Exception:
            pass  # a failed paint must never kill the worker
