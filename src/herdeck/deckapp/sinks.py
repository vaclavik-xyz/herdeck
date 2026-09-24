"""Render sinks: the converged DeckApp renders once per tick and fans the
orchestrator's RenderState out to a list of sinks. The HTTP tile buffer stays
inside DeckApp; additional sinks (the physical D200 USB display) consume the
same frame. Keeping each output behind this small protocol is what lets one
Orchestrator + one bridge connection drive several displays in lockstep."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


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
    # True only for the periodic animation/elapsed-time ticker. Physical D200
    # full-page uploads visibly blink, so its sink intentionally ignores these
    # volatile frames while browser/HTTP consumers keep animating.
    ticker: bool = False


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

    # Ticker frames are dropped in deliver(); telling DeckApp lets a headless
    # runtime skip rendering them at all.
    wants_ticker_frames = False

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
        # The physical D200 cannot update a spinner/elapsed label without a
        # full-page upload, which visibly blinks. Keep the first rendered view
        # for an otherwise identical tile; the driver then sees byte-identical
        # frames and safely suppresses the USB write. A real status/title/layout
        # change replaces the cached view and still repaints immediately.
        self._stable_tiles: dict[int, tuple[object, object]] = {}
        driver.on_press(on_press)
        self._reader_thread: threading.Thread | None = None
        if start_reader:
            self._reader_thread = threading.Thread(
                target=self._run_reader, name="herdeck-d200-reader", daemon=True
            )
            self._reader_thread.start()

    def deliver(self, frame) -> None:
        if frame.ticker:
            return
        # Always render a FULL frame — every tile plus the panel. The D200 drops the
        # cells missing from a partial (working-only) update, so a working frame would
        # blank the static/idle tiles + panel; re-sending everything keeps the whole
        # deck lit. render() is one combined full-set write in the driver, cheap now
        # that the strmdck retry sleep is neutralized. `frame.working` is ignored (the
        # animating tiles carry their new spinner phase in the full set anyway).
        rs = frame.render
        tiles = self._stabilize_volatile_tiles(
            [t for t in rs.tiles if t.index < self._slots]
        )
        render_frame = getattr(self._driver, "render_frame", None)
        if render_frame is not None:
            # One combined tiles+panel set: atomic (no panel blink), half the
            # zip/USB cost, and byte-identical frames are skipped in the driver.
            render_frame(tiles, rs.panel)
        else:  # injected test doubles may predate render_frame
            self._driver.render(tiles)
            self._driver.render_panel(rs.panel)

    def _stabilize_volatile_tiles(self, tiles: list) -> list:
        stable = []
        current: dict[int, tuple[object, object]] = {}
        for tile in tiles:
            try:
                # Spinner phase is volatile, spinner presence is a real
                # WORKING/non-working transition and must stay in the key.
                semantic = replace(
                    tile,
                    spinner=0 if getattr(tile, "spinner", None) is not None else None,
                    time_text=None,
                )
                display = replace(tile)
            except TypeError:  # lightweight non-dataclass test doubles
                semantic = display = tile
            previous = self._stable_tiles.get(tile.index)
            if previous is not None and previous[0] == semantic:
                display = previous[1]
            current[tile.index] = (semantic, display)
            stable.append(display)
        self._stable_tiles = current
        return stable

    def set_slots(self, slots: int) -> None:
        """Adopt a new profile/grid geometry for subsequent frames."""
        self._slots = slots
        self._stable_tiles.clear()

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

    wants_ticker_frames = False  # same device, same policy as D200Sink

    def __init__(
        self,
        driver_factory: Callable[[], object],
        *,
        on_press: Callable[[int], None],
        slots: int,
        retry_interval: float = 2.0,
        device_lock=None,
        lock_retry_interval: float = 5.0,
    ):
        self._driver_factory = driver_factory
        self._on_press = on_press
        self._slots = slots
        self._retry_interval = max(0.01, retry_interval)
        # Optional DeviceLock (deckapp.device_lock): only its holder may open
        # the D200. None = no arbitration (tests, single-runtime callers).
        self._device_lock = device_lock
        self._lock_retry_interval = max(0.01, lock_retry_interval)
        self._lock_warned = False
        self._was_connected = False
        self._since = _now_ms()
        self._last_frame_at: int | None = None
        self._last_error: str | None = None
        self._lock = threading.Lock()
        self._latest_frame: RenderFrame | None = None
        self._active: D200Sink | None = None
        self._stop = threading.Event()
        self._reconfigure = threading.Event()
        # Set by every event the attached-device wait cares about (disconnect,
        # reconfigure, close), so the supervisor sleeps instead of polling.
        self._wake = threading.Event()
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
            self._last_frame_at = _now_ms()

    def health(self) -> dict:
        """D200 facts for the runtime /health: is the device driven, since
        when, the newest write, why it is not, and who holds the lock."""
        lock = self._device_lock
        owner = None
        if lock is not None and not lock.held:
            owner = lock.owner_pid()
        with self._lock:
            connected = self._active is not None
        return {
            "connected": connected,
            "since": self._since,
            "last_frame_at": self._last_frame_at,
            "last_error": self._last_error,
            "lock_owner": owner,
        }

    def _mark(self, connected: bool, error: str | None) -> None:
        if connected != self._was_connected:
            self._was_connected = connected
            self._since = _now_ms()
        self._last_error = error

    def set_slots(self, slots: int) -> None:
        """Update both the current device and any future reconnected device."""
        with self._lock:
            self._slots = slots
            active = self._active
            if active is not None:
                active.set_slots(slots)

    def reconfigure(self) -> None:
        """Reopen the active driver so it adopts the latest hardware config."""
        self._reconfigure.set()
        self._wake.set()

    def _owns_device(self) -> bool:
        """Hold the D200 lock before touching the device. A runtime that loses
        keeps serving HTTP and is told once; it takes over when the owner exits."""
        lock = self._device_lock
        if lock is None:
            return True
        if lock.acquire():
            if self._lock_warned:
                log.warning("D200 lock %s acquired; this runtime now drives the D200", lock.path)
                self._lock_warned = False
            return True
        if not self._lock_warned:
            owner = lock.owner_pid()
            log.warning(
                "D200 is owned by another herdeck runtime (pid %s, lock %s); "
                "not opening it, retrying every %.0fs",
                owner if owner is not None else "unknown",
                lock.path,
                self._lock_retry_interval,
            )
            self._lock_warned = True
        return False

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self._owns_device():
                self._mark(False, "D200 owned by another herdeck runtime")
                self._stop.wait(self._lock_retry_interval)
                continue
            # If configuration changed while no device was attached, the next
            # factory call already reads the newest app.config.
            self._reconfigure.clear()
            try:
                driver = self._driver_factory()
            except Exception as exc:
                self._mark(False, str(exc) or type(exc).__name__)
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

            def on_disconnect(flag=disconnected) -> None:
                flag.set()
                self._wake.set()

            active = D200Sink(
                driver,
                on_press=self._on_press,
                slots=self._slots,
                on_disconnect=on_disconnect,
            )
            with self._lock:
                # set_slots() may have raced device construction before this
                # active sink was published; adopt the newest value here too.
                active.set_slots(self._slots)
                latest = self._latest_frame
                # Paint the retained frame before publishing the new sink while
                # holding the same lock as deliver(). Otherwise a concurrent new
                # frame could land first and then be overwritten by this older one.
                if latest is not None:
                    active.deliver(latest)
                self._active = active
            self._mark(True, None)
            if latest is not None:
                self._last_frame_at = _now_ms()
            log.info("D200 attached")

            while not (
                self._stop.is_set() or disconnected.is_set() or self._reconfigure.is_set()
            ):
                self._wake.wait()
                self._wake.clear()

            with self._lock:
                if self._active is active:
                    self._active = None
            active.close()
            self._mark(False, "disconnected" if disconnected.is_set() else None)
            if disconnected.is_set() and not self._stop.is_set():
                log.info("D200 disconnected; reopening")
            elif self._reconfigure.is_set() and not self._stop.is_set():
                log.info("D200 configuration changed; reopening")

    def close(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        self._wake.set()
        with self._lock:
            active = self._active
            self._active = None
        if active is not None:
            active.close()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=7.0)
        if self._device_lock is not None:
            # After the device is closed, so the next owner never opens a
            # D200 this runtime is still writing to.
            self._device_lock.release()
