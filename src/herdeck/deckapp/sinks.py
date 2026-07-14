"""Render sinks: the converged DeckApp renders once per tick and fans the
orchestrator's RenderState out to a list of sinks. The HTTP tile buffer stays
inside DeckApp; additional sinks (the physical D200 USB display) consume the
same frame. Keeping each output behind this small protocol is what lets one
Orchestrator + one bridge connection drive several displays in lockstep."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RenderFrame:
    """One rendered deck state handed to a sink.

    ``render`` is the orchestrator's RenderState (``.tiles`` is a list of
    TileView, ``.panel`` is a PanelView). ``working`` lists the tile indices
    that are spinner-advancing on a partial tick (``None`` on a full frame).
    ``full`` is True for a complete repaint (all tiles + panel), False for a
    working-only tick frame."""

    render: object
    working: list[int] | None
    full: bool


@runtime_checkable
class RenderSink(Protocol):
    """A render target. ``deliver`` is called under DeckApp's lock on every
    render; it must not block for long. ``close`` tears the sink down."""

    def deliver(self, frame: RenderFrame) -> None: ...

    def close(self) -> None: ...


class D200Sink:
    """RenderSink that drives a physical Ulanzi D200 via an open ``D200Driver``.

    Every frame (full or working) pushes every in-range tile plus the panel as a
    full set. The D200 firmware drops cells not included in a partial write, so
    always re-sending the complete layout keeps static and idle tiles lit. The
    neutralized strmdck retry-sleep makes a full combined write cheap (~12ms).
    Physical button presses are read on a private thread+event-loop and routed to
    ``on_press`` (the DeckApp's thread-safe ``press``), so a D200 press flows
    through the SAME Orchestrator + bridge as a window press."""

    def __init__(
        self,
        driver,
        *,
        on_press: Callable[[int], None],
        slots: int,
        start_reader: bool = True,
        on_disconnect: Callable[[], None] | None = None,
    ):
        self._driver = driver
        self._slots = slots
        self._on_disconnect = on_disconnect
        self._closing = threading.Event()
        driver.on_press(on_press)
        self._reader_thread: threading.Thread | None = None
        if start_reader:
            self._reader_thread = threading.Thread(
                target=self._run_reader, name="herdeck-d200-reader", daemon=True
            )
            self._reader_thread.start()

    def deliver(self, frame) -> None:
        # Always render a FULL frame — every tile plus the panel. The D200 drops the
        # cells missing from a partial (working-only) update, so a working frame would
        # blank the static/idle tiles + panel; re-sending everything keeps the whole
        # deck lit. render() is one combined full-set write in the driver, cheap now
        # that the strmdck retry sleep is neutralized. `frame.working` is ignored (the
        # animating tiles carry their new spinner phase in the full set anyway).
        rs = frame.render
        tiles = [t for t in rs.tiles if t.index < self._slots]
        render_frame = getattr(self._driver, "render_frame", None)
        if render_frame is not None:
            # One combined tiles+panel set: atomic (no panel blink), half the
            # zip/USB cost, and byte-identical frames are skipped in the driver.
            render_frame(tiles, rs.panel)
        else:  # injected test doubles may predate render_frame
            self._driver.render(tiles)
            self._driver.render_panel(rs.panel)

    def _run_reader(self) -> None:
        try:
            asyncio.run(self._driver.run_reader())
        except Exception:
            if not self._closing.is_set():
                log.warning("D200 press reader stopped", exc_info=True)
        finally:
            if not self._closing.is_set() and self._on_disconnect is not None:
                try:
                    self._on_disconnect()
                except Exception:
                    log.warning("D200 disconnect callback failed", exc_info=True)

    def close(self) -> None:
        if self._closing.is_set():
            return
        self._closing.set()
        try:
            self._driver.close()  # closes the device, which ends run_reader()
        except Exception:
            log.warning("D200 driver close failed", exc_info=True)
        reader = self._reader_thread
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=2.0)
        self._reader_thread = None


class ReconnectingD200Sink:
    """Persistent D200 sink that reopens the USB device after HID disconnects.

    macOS invalidates the existing HID handle while the machine sleeps. The
    runtime process survives, so without a supervisor the reader thread exits
    and the deck stays on its firmware-default page forever. This sink retains
    the newest full render frame, replaces the failed driver, and immediately
    repaints that frame when the device becomes available again.
    """

    def __init__(
        self,
        driver_factory: Callable[[], object],
        *,
        on_press: Callable[[int], None],
        slots: int,
        retry_interval: float = 2.0,
    ):
        self._driver_factory = driver_factory
        self._on_press = on_press
        self._slots = slots
        self._retry_interval = max(0.01, retry_interval)
        self._lock = threading.Lock()
        self._latest_frame: RenderFrame | None = None
        self._active: D200Sink | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="herdeck-d200-reconnect",
            daemon=True,
        )
        self._thread.start()

    def deliver(self, frame: RenderFrame) -> None:
        with self._lock:
            self._latest_frame = frame
            active = self._active
        if active is not None:
            active.deliver(frame)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                driver = self._driver_factory()
            except Exception as exc:
                log.info(
                    "no D200 attached (%s); retrying in %.1fs",
                    exc,
                    self._retry_interval,
                )
                self._stop.wait(self._retry_interval)
                continue

            if self._stop.is_set():
                try:
                    driver.close()
                except Exception:
                    pass
                return

            disconnected = threading.Event()
            active = D200Sink(
                driver,
                on_press=self._on_press,
                slots=self._slots,
                on_disconnect=disconnected.set,
            )
            with self._lock:
                latest = self._latest_frame
                # Paint the retained frame before publishing the new sink while
                # holding the same lock as deliver(). Otherwise a concurrent new
                # frame could land first and then be overwritten by this older one.
                if latest is not None:
                    active.deliver(latest)
                self._active = active
            log.info("D200 attached")

            while not self._stop.wait(0.25):
                if disconnected.is_set():
                    break

            with self._lock:
                if self._active is active:
                    self._active = None
            active.close()
            if disconnected.is_set() and not self._stop.is_set():
                log.info("D200 disconnected; reopening")

    def close(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        with self._lock:
            active = self._active
            self._active = None
        if active is not None:
            active.close()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=7.0)
