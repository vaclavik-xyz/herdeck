from __future__ import annotations

from .. import layout
from ..config import (
    DEFAULT_PROFILES,
    Config,
    ServerConfig,
)
from ..model import AgentKey, AgentState, Status
from ..orchestrator import Orchestrator
from .source import StateSource

# Two demo servers. Ids are stable and non-secret; the placeholder tokens are
# empty so no real bridge credential ever lives in the mock.
MOCK_SERVERS: tuple[str, str] = ("local", "gpu-box")

# Deterministic cycle used by mock presses for visual feedback (no randomness).
_STATUS_CYCLE: tuple[Status, ...] = (
    Status.WORKING,
    Status.IDLE,
    Status.BLOCKED,
    Status.DONE,
)


def _next_status(status: Status) -> Status:
    try:
        i = _STATUS_CYCLE.index(status)
    except ValueError:
        return _STATUS_CYCLE[0]
    return _STATUS_CYCLE[(i + 1) % len(_STATUS_CYCLE)]


def demo_agents() -> list[AgentState]:
    """A fixed, deterministic demo fleet: 7 agents over 2 servers spanning every
    status (working / idle / blocked / done). No randomness, no time-based seeds —
    two calls return equal data."""
    local, gpu = MOCK_SERVERS
    return [
        AgentState(AgentKey(local, "p0"), "claude", "api", Status.WORKING, repo="api", branch="main"),
        AgentState(
            AgentKey(local, "p1"), "codex", "web", Status.IDLE, repo="web", branch="feat/login"
        ),
        AgentState(
            AgentKey(local, "p2"), "claude", "infra", Status.BLOCKED, repo="infra", branch="fix/dns"
        ),
        AgentState(AgentKey(local, "p3"), "gemini", "docs", Status.DONE, repo="docs", branch="main"),
        AgentState(
            AgentKey(gpu, "p0"), "claude", "train", Status.WORKING, repo="ml", branch="exp/lora"
        ),
        AgentState(AgentKey(gpu, "p1"), "cursor", "data", Status.IDLE, repo="etl", branch="main"),
        AgentState(
            AgentKey(gpu, "p2"), "codex", "eval", Status.BLOCKED, repo="bench", branch="ci/run"
        ),
    ]


def mock_config() -> Config:
    """A minimal Config wired to the two demo servers, with built-in defaults for
    everything else, so the core Orchestrator renders the mock fleet exactly as a
    real deck would. The server tokens are empty (no secret in mock)."""
    servers = [ServerConfig(id=sid, url="", token="") for sid in MOCK_SERVERS]
    return Config(
        servers=servers,
        profiles=dict(DEFAULT_PROFILES),
        overview_order=list(MOCK_SERVERS),
        grid=(5, 3),
    )


class MockSource(StateSource):
    """Deterministic demo state source.

    Holds the fixed demo fleet and feeds it to the orchestrator via
    ``apply_snapshot`` (the same path the live connector uses). A press cycles the
    pressed agent's status for visual feedback only — nothing is sent anywhere.
    """

    source_name = "mock"

    def __init__(self) -> None:
        self._agents = demo_agents()
        self._config = mock_config()

    @property
    def config(self) -> Config:
        return self._config

    @property
    def connected(self) -> bool:
        return True

    def apply_to(self, orch: Orchestrator) -> None:
        for sid in self._config.overview_order:
            states = [a for a in self._agents if a.key.server_id == sid]
            orch.apply_snapshot(sid, states)
            orch.set_connection(sid, True)

    def _ordered(self) -> list[AgentState]:
        return layout.order_agents(self._agents, self._config.overview_order)

    def press(self, index: int) -> None:
        ordered = self._ordered()
        if 0 <= index < len(ordered):  # only real agent tiles; ignore everything else
            agent = ordered[index]
            agent.status = _next_status(agent.status)

    def summary(self) -> dict:
        counts = layout.summary(self._agents)
        return {
            "agents": len(self._agents),
            "blocked": counts.blocked,
            "working": counts.working,
            "idle": counts.idle,
            "done": counts.done,
        }
