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

    Full frames push every in-range tile plus the panel; working frames push only
    the spinner-advancing tiles (cheap partial USB writes). The driver's own
    per-index write diff and the neutralized strmdck retry-sleep keep those writes
    fast. Physical button presses are read on a private thread+event-loop and
    routed to ``on_press`` (the DeckApp's thread-safe ``press``), so a D200 press
    flows through the SAME Orchestrator + bridge as a window press."""

    def __init__(
        self,
        driver,
        *,
        on_press: Callable[[int], None],
        slots: int,
        start_reader: bool = True,
    ):
        self._driver = driver
        self._slots = slots
        driver.on_press(on_press)
        self._reader_thread: threading.Thread | None = None
        if start_reader:
            self._reader_thread = threading.Thread(
                target=self._run_reader, name="herdeck-d200-reader", daemon=True
            )
            self._reader_thread.start()

    def deliver(self, frame) -> None:
        rs = frame.render
        if frame.full or frame.working is None:
            self._driver.render([t for t in rs.tiles if t.index < self._slots])
            self._driver.render_panel(rs.panel)
            return
        wanted = set(frame.working)
        tiles = [t for t in rs.tiles if t.index in wanted]
        if tiles:
            self._driver.render_working(tiles)

    def _run_reader(self) -> None:
        try:
            asyncio.run(self._driver.run_reader())
        except Exception:
            log.warning("D200 press reader stopped", exc_info=True)

    def close(self) -> None:
        try:
            self._driver.close()  # closes the device, which ends run_reader()
        except Exception:
            log.warning("D200 driver close failed", exc_info=True)
