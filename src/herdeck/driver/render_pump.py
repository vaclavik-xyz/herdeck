from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable

log = logging.getLogger(__name__)


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
    SLOW_PAINT_MS = 250.0  # a worker-block longer than this gets a WARNING log

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
        self._last_paint_ms: float | None = None
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
        # The worker owns a private asyncio loop and runs each batch inside it, so a
        # device library can schedule its USB write via asyncio.get_running_loop() +
        # create_task (strmdck's D200 path does exactly this). Without a running loop
        # the write raises "no running event loop" and is silently dropped — a blank
        # device. The loop stays OFF the app's main event loop (the pump's whole point),
        # and is the single loop every write binds its asyncio.Lock to.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
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
                do_keep_alive = self._keep_alive is not None and self._clock() >= next_keep_alive
                t0 = self._clock()
                loop.run_until_complete(self._paint_batch(batch, do_keep_alive))
                dt_ms = (self._clock() - t0) * 1000.0
                self._last_paint_ms = dt_ms
                channels = ",".join(ch for ch in self.CHANNELS if ch in batch)
                if dt_ms >= self.SLOW_PAINT_MS:
                    log.warning("render worker blocked %.0fms painting [%s]", dt_ms, channels)
                elif channels:
                    log.debug("render worker painted [%s] in %.1fms", channels, dt_ms)
                if do_keep_alive:
                    next_keep_alive = self._clock() + self._interval
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    async def _paint_batch(self, batch: dict[str, object], do_keep_alive: bool) -> None:
        for ch in self.CHANNELS:
            if ch in batch:
                self._safe(self._paint, ch, batch[ch])
        if do_keep_alive:
            self._safe(self._keep_alive)
        # Drain the fire-and-forget device-write tasks the paints scheduled via
        # create_task, so each frame's USB write completes before the next batch (and
        # before close) instead of being cancelled when the loop next stops.
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    @staticmethod
    def _safe(fn, *args) -> None:
        try:
            fn(*args)
        except Exception:
            pass  # a failed paint must never kill the worker
