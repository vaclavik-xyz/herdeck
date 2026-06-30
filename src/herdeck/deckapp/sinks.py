"""Render sinks: the converged DeckApp renders once per tick and fans the
orchestrator's RenderState out to a list of sinks. The HTTP tile buffer stays
inside DeckApp; additional sinks (the physical D200 USB display) consume the
same frame. Keeping each output behind this small protocol is what lets one
Orchestrator + one bridge connection drive several displays in lockstep."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


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
