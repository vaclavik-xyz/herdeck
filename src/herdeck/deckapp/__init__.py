"""herdeck.deckapp — the desktop-app Python sidecar (phase 1, slice 1: mock core).

A token-authed loopback HTTP server that reuses the herdeck core (Orchestrator +
icons) to render the deck and serve it as JSON state + PNG tiles. State comes from
a swappable StateSource; this slice ships the deterministic MockSource only.
"""

from __future__ import annotations

from .mock import MockSource, demo_agents, mock_config
from .server import DeckApp, create_mock_app
from .source import StateSource

__all__ = [
    "DeckApp",
    "MockSource",
    "StateSource",
    "create_mock_app",
    "demo_agents",
    "mock_config",
]
