import asyncio

import pytest

from herdeck.app import App, _mock_config, _run_mock
from herdeck.config import Config, ServerConfig
from herdeck.driver.fake import FakeRenderer
from herdeck.model import AgentKey, AgentState, Status


def test_semantic_generation_changes_only_for_affected_target():
    config = Config(
        servers=[ServerConfig("local", "ws://local", "token")],
        profiles={},
        overview_order=["local"],
        grid=(5, 3),
    )
    app = App(config, FakeRenderer(), lambda _command: None)
    first = AgentState(AgentKey("local", "p1"), "codex", "first", Status.BLOCKED)
    second = AgentState(AgentKey("local", "p2"), "codex", "second", Status.WORKING)
    app.handle_snapshot("local", [first, second])
    first_generation = app.semantic_generation("local", "p1")

    app.handle_event(
        "local",
        AgentState(AgentKey("local", "p2"), "codex", "second", Status.DONE),
    )
    assert app.semantic_generation("local", "p1") == first_generation

    app.handle_event(
        "local",
        AgentState(AgentKey("local", "p1"), "codex", "first", Status.WORKING),
    )
    assert app.semantic_generation("local", "p1") != first_generation


class SemanticFakeDeck(FakeRenderer):
    def __init__(self):
        super().__init__()
        self.semantic_callback = None

    def on_semantic(self, callback):
        self.semantic_callback = callback


async def semantic_request(deck, request):
    return await asyncio.to_thread(lambda: deck.semantic_callback(request).result(timeout=2))


@pytest.mark.asyncio
async def test_mock_runtime_registers_live_semantic_inventory():
    deck = SemanticFakeDeck()
    task = asyncio.create_task(_run_mock(_mock_config(), deck))
    try:
        for _ in range(20):
            if deck.semantic_callback is not None:
                break
            await asyncio.sleep(0)
        assert deck.semantic_callback is not None

        response = await semantic_request(
            deck,
            {"operation": "inventory", "caller": "server:test", "payload": None},
        )
        assert response.status == 200
        assert len(response.body["agents"]) == 5
        assert all(
            row["terminal_id"].startswith("mock-terminal-") for row in response.body["agents"]
        )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_mock_transition_invalidates_stop_confirmation_for_changed_target():
    deck = SemanticFakeDeck()
    task = asyncio.create_task(_run_mock(_mock_config(), deck, cycle_interval=0.05))
    try:
        while deck.semantic_callback is None:
            await asyncio.sleep(0)
        armed = await semantic_request(
            deck,
            {
                "operation": "action",
                "caller": "browser:test",
                "payload": {
                    "server_id": "mock",
                    "pane_id": "p1",
                    "terminal_id": "mock-terminal-p1",
                    "idempotency_key": "stop-arm",
                    "action": "stop",
                },
            },
        )
        assert armed.body["outcome"] == "confirmation_required"

        await asyncio.sleep(0.08)
        confirmed = await semantic_request(
            deck,
            {
                "operation": "action",
                "caller": "browser:test",
                "payload": {
                    "server_id": "mock",
                    "pane_id": "p1",
                    "terminal_id": "mock-terminal-p1",
                    "idempotency_key": "stop-confirm",
                    "action": "stop",
                    "confirmation": armed.body["confirmation"],
                },
            },
        )
        assert confirmed.body["outcome"] == "confirmation_expired"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
