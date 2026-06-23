from __future__ import annotations

from abc import ABC, abstractmethod

from ..orchestrator import Orchestrator


class StateSource(ABC):
    """A source of deck state for the sidecar.

    The sidecar holds one Orchestrator and one StateSource. On each refresh it
    asks the source to ``apply_to`` the orchestrator, then renders. A press is
    routed to ``press``. This keeps the mock and (future) live paths swappable
    behind one interface: ``MockSource`` here, ``LiveSource`` in a later slice.
    """

    #: ``"mock"`` or ``"live"`` — surfaced verbatim in ``/state`` and ``/health``.
    source_name: str = "mock"

    @property
    @abstractmethod
    def config(self):
        """The resolved Config the Orchestrator is built from."""

    @property
    @abstractmethod
    def connected(self) -> bool:
        """Whether the underlying state feed is up (always True for the mock)."""

    @property
    def server_id(self) -> str | None:
        """A non-secret server id for ``/health`` (never a token); None in mock."""
        return None

    def attach(self, orch: Orchestrator) -> None:  # noqa: B027 - optional hook, no-op by default
        """Receive the render orchestrator the DeckApp built (live press path).

        No-op by default; the mock translates presses locally without it.
        """

    def close(self) -> None:  # noqa: B027 - optional hook, no-op by default
        """Release any background resources (live connector). No-op by default."""

    @abstractmethod
    def apply_to(self, orch: Orchestrator) -> None:
        """Push the current agent state into the orchestrator."""

    @abstractmethod
    def press(self, index: int) -> None:
        """Handle a tile press. Out-of-range indices must be ignored."""

    @abstractmethod
    def summary(self) -> dict:
        """``{agents, blocked, working, idle, done}`` for the footer."""
