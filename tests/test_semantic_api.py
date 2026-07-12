import asyncio

import pytest

from herdeck.app_control import ActionResult
from herdeck.model import AgentKey, AgentState, Status, WorkContext
from herdeck.semantic_api import TEXT_MAX_BYTES, SemanticAPI


class FakeControl:
    def __init__(self, agents):
        self.agents = agents
        self.calls = []
        self.result = ActionResult(True)
        self.error = None
        self.release = None

    def current_agent(self, key):
        return next((agent for agent in self.agents if agent.key == key), None)

    async def approve(self, key):
        return await self._call("approve", key)

    async def deny(self, key):
        return await self._call("deny", key)

    async def stop(self, key, *, confirmed=False):
        assert confirmed is True
        return await self._call("stop", key)

    async def send_text(self, key, text):
        return await self._call("text", key, text)

    async def _call(self, *call):
        self.calls.append(call)
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        return self.result


def agent(
    server="local",
    pane="p1",
    terminal="t1",
    *,
    status=Status.BLOCKED,
):
    return AgentState(
        AgentKey(server, pane),
        "codex",
        "Agent\x00 label",
        status,
        project="project",
        repo="herdeck",
        branch="feat/api",
        workspace="workspace",
        tab="tab",
        custom_status="needs user",
        terminal_id=terminal,
        title="SECRET PROMPT",
        work=WorkContext("github", "#28", "run-1", "https://example.test/28"),
    )


def make_api(agents, *, available=True, generation=None, clock=None):
    control = FakeControl(agents)
    current_generation = generation or (lambda: 1)
    api = SemanticAPI(
        control,
        agents=lambda: list(agents),
        server_available=lambda _server: available,
        generation=current_generation,
        clock=clock,
    )
    return api, control


def target(**extra):
    return {
        "server_id": "local",
        "pane_id": "p1",
        "terminal_id": "t1",
        "idempotency_key": "request-1",
        **extra,
    }


def test_inventory_is_versioned_bounded_and_excludes_prompt_state():
    agents = [agent(), agent("remote", "p2", "t2", status=Status.WORKING)]
    api, _ = make_api(agents)

    response = api.inventory()

    assert response.status == 200
    assert response.body["api_version"] == "v1"
    assert [(row["server_id"], row["pane_id"]) for row in response.body["agents"]] == [
        ("local", "p1"),
        ("remote", "p2"),
    ]
    row = response.body["agents"][0]
    assert row["terminal_id"] == "t1"
    assert row["label"] == "Agent label"
    assert row["work"] == {
        "source": "github",
        "item": "#28",
        "run": "run-1",
        "url": "https://example.test/28",
    }
    serialized = str(response.body)
    assert "SECRET PROMPT" not in serialized
    assert "prompt" not in serialized.lower()


def test_inventory_tracks_removal_and_terminal_replacement_without_stale_rows():
    agents = [agent()]
    api, _ = make_api(agents)
    assert api.inventory().body["agents"][0]["terminal_id"] == "t1"

    agents[:] = [agent(terminal="t2")]
    assert api.inventory().body["agents"][0]["terminal_id"] == "t2"

    agents.clear()
    assert api.inventory().body["agents"] == []


@pytest.mark.asyncio
async def test_approve_and_deny_are_semantic_and_idempotent():
    agents = [agent()]
    api, control = make_api(agents)

    approved = await api.action("server:a", target(action="approve"))
    replay = await api.action("server:a", target(action="approve"))
    denied = await api.action("server:a", target(action="deny", idempotency_key="request-2"))

    assert approved.body["outcome"] == "sent"
    assert replay == approved
    assert denied.body["outcome"] == "sent"
    assert control.calls == [
        ("approve", AgentKey("local", "p1")),
        ("deny", AgentKey("local", "p1")),
    ]


@pytest.mark.asyncio
async def test_concurrent_duplicate_action_joins_the_inflight_result():
    agents = [agent()]
    api, control = make_api(agents)
    control.release = asyncio.Event()

    first = asyncio.create_task(api.action("server:a", target(action="approve")))
    await asyncio.sleep(0)
    duplicate = asyncio.create_task(api.action("server:a", target(action="approve")))
    await asyncio.sleep(0)
    control.release.set()

    assert await first == await duplicate
    assert control.calls == [("approve", AgentKey("local", "p1"))]


@pytest.mark.asyncio
async def test_nonblocked_and_stale_targets_send_nothing():
    agents = [agent(status=Status.WORKING)]
    api, control = make_api(agents)

    skipped = await api.action("server:a", target(action="approve"))
    stale = await api.action(
        "server:a",
        target(action="deny", terminal_id="old", idempotency_key="request-2"),
    )

    assert skipped.body["outcome"] == "skipped"
    assert stale.status == 409
    assert stale.body["outcome"] == "stale_identity"
    assert control.calls == []


@pytest.mark.asyncio
async def test_stop_requires_caller_bound_single_use_confirmation():
    agents = [agent()]
    api, control = make_api(agents)

    armed = await api.action("browser:a", target(action="stop"))
    challenge = armed.body["confirmation"]
    cross_caller = await api.action(
        "browser:b",
        target(action="stop", confirmation=challenge, idempotency_key="request-2"),
    )
    replay = await api.action(
        "browser:a",
        target(action="stop", confirmation=challenge, idempotency_key="request-3"),
    )

    assert armed.status == 409
    assert armed.body["outcome"] == "confirmation_required"
    assert cross_caller.body["outcome"] == "confirmation_expired"
    assert replay.body["outcome"] == "confirmation_expired"
    assert control.calls == []


@pytest.mark.asyncio
async def test_confirmed_stop_executes_once_and_replays_result():
    agents = [agent()]
    api, control = make_api(agents)
    armed = await api.action("browser:a", target(action="stop"))
    payload = target(
        action="stop",
        confirmation=armed.body["confirmation"],
        idempotency_key="request-confirm",
    )

    sent = await api.action("browser:a", payload)
    replay = await api.action("browser:a", payload)

    assert sent.body["outcome"] == "sent"
    assert replay == sent
    assert control.calls == [("stop", AgentKey("local", "p1"))]


@pytest.mark.asyncio
async def test_stop_confirmation_invalidates_on_generation_change():
    agents = [agent()]
    generation = [1]
    api, control = make_api(agents, generation=lambda: generation[0])
    armed = await api.action("browser:a", target(action="stop"))
    generation[0] += 1

    response = await api.action(
        "browser:a",
        target(
            action="stop",
            confirmation=armed.body["confirmation"],
            idempotency_key="request-confirm",
        ),
    )

    assert response.body["outcome"] == "confirmation_expired"
    assert control.calls == []


@pytest.mark.asyncio
async def test_stop_confirmation_expires_and_cannot_cross_targets():
    agents = [agent(), agent(pane="p2", terminal="t2")]
    clock = [100.0]
    api, control = make_api(agents, clock=lambda: clock[0])
    armed = await api.action("browser:a", target(action="stop"))

    cross_target = await api.action(
        "browser:a",
        {
            "server_id": "local",
            "pane_id": "p2",
            "terminal_id": "t2",
            "idempotency_key": "request-2",
            "action": "stop",
            "confirmation": armed.body["confirmation"],
        },
    )
    assert cross_target.body["outcome"] == "confirmation_expired"

    armed = await api.action("browser:a", target(action="stop", idempotency_key="request-3"))
    clock[0] += 61
    expired = await api.action(
        "browser:a",
        target(
            action="stop",
            confirmation=armed.body["confirmation"],
            idempotency_key="request-4",
        ),
    )
    assert expired.body["outcome"] == "confirmation_expired"
    assert control.calls == []


@pytest.mark.asyncio
async def test_unconfirmed_stop_reuses_challenge_for_duplicate_request():
    agents = [agent()]
    api, control = make_api(agents)

    first = await api.action("browser:a", target(action="stop"))
    duplicate = await api.action("browser:a", target(action="stop"))

    assert duplicate == first
    assert len(api._challenges) == 1
    assert control.calls == []


@pytest.mark.asyncio
async def test_surrogate_in_idempotency_key_does_not_raise_internal_error():
    agents = [agent()]
    api, control = make_api(agents)

    response = await api.action("server:a", target(action="approve", idempotency_key="bad-\ud800"))

    assert response.body["outcome"] == "sent"
    assert control.calls == [("approve", AgentKey("local", "p1"))]


@pytest.mark.asyncio
async def test_send_text_enforces_utf8_single_line_policy_and_idempotency():
    agents = [agent()]
    api, control = make_api(agents)

    sent = await api.send_text("server:a", target(text="Příliš žluťoučký kůň"))
    replay = await api.send_text("server:a", target(text="Příliš žluťoučký kůň"))
    multiline = await api.send_text(
        "server:a", target(text="first\nsecond", idempotency_key="request-2")
    )
    oversized = await api.send_text(
        "server:a", target(text="é" * (TEXT_MAX_BYTES // 2 + 1), idempotency_key="request-3")
    )

    assert sent.body["outcome"] == "sent"
    assert replay == sent
    assert multiline.status == 422
    assert oversized.status == 422
    assert control.calls == [("text", AgentKey("local", "p1"), "Příliš žluťoučký kůň")]


@pytest.mark.asyncio
async def test_idempotency_key_cannot_be_reused_for_different_payload():
    agents = [agent()]
    api, control = make_api(agents)
    await api.action("server:a", target(action="approve"))

    conflict = await api.action("server:a", target(action="deny"))

    assert conflict.status == 409
    assert conflict.body["error"]["code"] == "idempotency_conflict"
    assert control.calls == [("approve", AgentKey("local", "p1"))]


@pytest.mark.asyncio
async def test_timeout_and_backend_failure_are_structured_and_redacted():
    agents = [agent()]
    api, control = make_api(agents)
    control.error = TimeoutError("secret-token")

    timeout = await api.action("server:a", target(action="approve"))
    control.error = ConnectionError("secret-token")
    failed = await api.action("server:a", target(action="deny", idempotency_key="request-2"))

    assert timeout.status == 504 and timeout.body["outcome"] == "timeout"
    assert failed.status == 503 and failed.body["outcome"] == "backend_failure"
    assert "secret-token" not in str(timeout.body) + str(failed.body)
