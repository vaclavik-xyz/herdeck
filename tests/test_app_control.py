import asyncio

import pytest

from herdeck.commands import Command
from herdeck.model import AgentKey, AgentState, Status


class FakeSender:
    def __init__(self):
        self.sent = []

    async def send(self, cmd, req):
        self.sent.append((cmd, req))


@pytest.mark.asyncio
async def test_runtime_agent_control_read_prompt_uses_own_request_ids():
    from herdeck.app_control import RuntimeAgentControl
    from herdeck.config import AnswerProfile, Config

    sender = FakeSender()
    key = AgentKey("local", "p1")
    cfg = Config(
        servers=[],
        profiles={
            "default": AnswerProfile(["y"], ["n"], ["ctrl+c"], ["y"]),
            "codex": AnswerProfile(["y", "enter"], ["n", "enter"], ["ctrl+c"], ["y", "enter"]),
        },
        overview_order=["local"],
        grid=(5, 3),
    )
    agent = AgentState(key, "codex", "herdeck", Status.BLOCKED)
    control = RuntimeAgentControl(cfg, send=sender.send, current_agent=lambda k: agent if k == key else None)

    task = asyncio.create_task(control.read_prompt(key, timeout=1))
    await asyncio.sleep(0)

    cmd, req = sender.sent[0]
    assert cmd == Command("read", "local", "p1", source="detection")
    assert req.startswith("tg")

    assert control.handle_result(req, {"text": "Approve?", "pane_id": "p1"}) == cmd
    assert await task == "Approve?"


@pytest.mark.asyncio
async def test_runtime_agent_control_read_prompt_returns_empty_for_missing_agent():
    from herdeck.app_control import RuntimeAgentControl
    from herdeck.config import Config

    sender = FakeSender()
    key = AgentKey("local", "p1")
    cfg = Config(servers=[], profiles={}, overview_order=[], grid=(5, 3))
    control = RuntimeAgentControl(cfg, send=sender.send, current_agent=lambda k: None)

    result = await control.read_prompt(key, timeout=0.01)

    assert result == ""
    assert sender.sent == []


@pytest.mark.asyncio
async def test_runtime_agent_control_approve_uses_profile_and_guard():
    from herdeck.app_control import RuntimeAgentControl
    from herdeck.config import AnswerProfile, Config

    sender = FakeSender()
    key = AgentKey("local", "p1")
    cfg = Config(
        servers=[],
        profiles={
            "default": AnswerProfile(["y"], ["n"], ["ctrl+c"], ["y"]),
            "codex": AnswerProfile(["y", "enter"], ["n", "enter"], ["ctrl+c"], ["y", "enter"]),
        },
        overview_order=["local"],
        grid=(5, 3),
    )
    agent = AgentState(key, "codex", "herdeck", Status.BLOCKED)
    control = RuntimeAgentControl(cfg, send=sender.send, current_agent=lambda k: agent if k == key else None)

    task = asyncio.create_task(control.approve(key, timeout=1))
    await asyncio.sleep(0)

    cmd, req = sender.sent[0]
    assert cmd == Command("act_if_blocked", "local", "p1", keys=["y", "enter"])

    assert control.handle_result(req, {"sent": True}) == cmd
    result = await task
    assert result.sent is True
    assert result.skipped is False


@pytest.mark.asyncio
async def test_runtime_agent_control_ignores_result_from_wrong_server():
    from herdeck.app_control import RuntimeAgentControl
    from herdeck.config import AnswerProfile, Config

    sender = FakeSender()
    key = AgentKey("local", "p1")
    cfg = Config(
        servers=[],
        profiles={
            "default": AnswerProfile(["y"], ["n"], ["ctrl+c"], ["y"]),
            "codex": AnswerProfile(["y", "enter"], ["n", "enter"], ["ctrl+c"], ["y", "enter"]),
        },
        overview_order=["local"],
        grid=(5, 3),
    )
    agent = AgentState(key, "codex", "herdeck", Status.BLOCKED)
    control = RuntimeAgentControl(
        cfg, send=sender.send, current_agent=lambda k: agent if k == key else None
    )

    task = asyncio.create_task(control.approve(key, timeout=1))
    await asyncio.sleep(0)

    cmd, req = sender.sent[0]
    assert control.handle_result(req, {"sent": True}, server_id="other") is None
    assert task.done() is False

    assert control.handle_result(req, {"sent": True}, server_id="local") == cmd
    assert (await task).sent is True


@pytest.mark.asyncio
async def test_runtime_agent_control_action_result_preserves_connector_failure_message():
    from herdeck.app_control import RuntimeAgentControl
    from herdeck.config import AnswerProfile, Config

    sender = FakeSender()
    key = AgentKey("local", "p1")
    cfg = Config(
        servers=[],
        profiles={
            "default": AnswerProfile(["y"], ["n"], ["ctrl+c"], ["y"]),
            "codex": AnswerProfile(["y", "enter"], ["n", "enter"], ["ctrl+c"], ["y", "enter"]),
        },
        overview_order=["local"],
        grid=(5, 3),
    )
    agent = AgentState(key, "codex", "herdeck", Status.BLOCKED)
    control = RuntimeAgentControl(cfg, send=sender.send, current_agent=lambda k: agent if k == key else None)

    task = asyncio.create_task(control.approve(key, timeout=1))
    await asyncio.sleep(0)

    cmd, req = sender.sent[0]
    assert cmd == Command("act_if_blocked", "local", "p1", keys=["y", "enter"])

    assert control.handle_result(req, {"sent": False, "message": "connection lost"}) == cmd
    result = await task
    assert result.sent is False
    assert result.skipped is False
    assert result.message == "connection lost"


@pytest.mark.asyncio
async def test_runtime_agent_control_action_result_normalizes_bridge_fields():
    from herdeck.app_control import RuntimeAgentControl
    from herdeck.config import AnswerProfile, Config

    sender = FakeSender()
    key = AgentKey("local", "p1")
    cfg = Config(
        servers=[],
        profiles={
            "default": AnswerProfile(["y"], ["n"], ["ctrl+c"], ["y"]),
            "codex": AnswerProfile(["y", "enter"], ["n", "enter"], ["ctrl+c"], ["y", "enter"]),
        },
        overview_order=["local"],
        grid=(5, 3),
    )
    agent = AgentState(key, "codex", "herdeck", Status.BLOCKED)
    control = RuntimeAgentControl(cfg, send=sender.send, current_agent=lambda k: agent if k == key else None)

    task = asyncio.create_task(control.approve(key, timeout=1))
    await asyncio.sleep(0)

    cmd, req = sender.sent[0]
    assert control.handle_result(req, {"sent": "yes", "skipped": "yes", "message": 123}) == cmd
    result = await task
    assert result.sent is False
    assert result.skipped is False
    assert result.message == "123"


@pytest.mark.asyncio
async def test_runtime_agent_control_update_config_changes_action_profile():
    from herdeck.app_control import RuntimeAgentControl
    from herdeck.config import AnswerProfile, Config

    sender = FakeSender()
    key = AgentKey("local", "p1")
    cfg = Config(
        servers=[],
        profiles={
            "default": AnswerProfile(["y"], ["n"], ["ctrl+c"], ["y"]),
            "codex": AnswerProfile(["y", "enter"], ["n", "enter"], ["ctrl+c"], ["y", "enter"]),
        },
        overview_order=["local"],
        grid=(5, 3),
    )
    new_cfg = Config(
        servers=[],
        profiles={
            "default": AnswerProfile(["ok"], ["no"], ["ctrl+c"], ["ok"]),
            "codex": AnswerProfile(["ok", "enter"], ["no", "enter"], ["ctrl+c"], ["ok", "enter"]),
        },
        overview_order=["local"],
        grid=(5, 3),
    )
    agent = AgentState(key, "codex", "herdeck", Status.BLOCKED)
    control = RuntimeAgentControl(cfg, send=sender.send, current_agent=lambda k: agent if k == key else None)

    control.update_config(new_cfg)
    task = asyncio.create_task(control.approve(key, timeout=1))
    await asyncio.sleep(0)

    cmd, req = sender.sent[0]
    assert cmd == Command("act_if_blocked", "local", "p1", keys=["ok", "enter"])
    assert control.handle_result(req, {"sent": True}) == cmd
    assert (await task).sent is True


@pytest.mark.asyncio
async def test_runtime_agent_control_approve_returns_missing_agent_as_failure():
    from herdeck.app_control import RuntimeAgentControl
    from herdeck.config import Config

    sender = FakeSender()
    key = AgentKey("local", "p1")
    cfg = Config(servers=[], profiles={}, overview_order=[], grid=(5, 3))
    control = RuntimeAgentControl(cfg, send=sender.send, current_agent=lambda k: None)

    result = await control.approve(key)

    assert result.sent is False
    assert result.skipped is False
    assert result.message == "agent is no longer available"
    assert sender.sent == []


@pytest.mark.asyncio
async def test_runtime_agent_control_stop_respects_safety_confirmation():
    from herdeck.app_control import RuntimeAgentControl
    from herdeck.config import AnswerProfile, Config

    sender = FakeSender()
    key = AgentKey("local", "p1")
    cfg = Config(
        servers=[],
        profiles={
            "default": AnswerProfile(["y"], ["n"], ["ctrl+c"], ["y"]),
            "codex": AnswerProfile(["y", "enter"], ["n", "enter"], ["ctrl+c"], ["y", "enter"]),
        },
        overview_order=["local"],
        grid=(5, 3),
    )
    cfg.safety.require_confirm_for = ["act_force"]
    agent = AgentState(key, "codex", "herdeck", Status.BLOCKED)
    control = RuntimeAgentControl(cfg, send=sender.send, current_agent=lambda k: agent if k == key else None)

    first = await control.stop(key)

    assert first.sent is False
    assert first.skipped is False
    assert first.message == "confirmation required"
    assert sender.sent == []

    task = asyncio.create_task(control.stop(key, timeout=1))
    await asyncio.sleep(0)

    cmd, req = sender.sent[0]
    assert cmd == Command("act_force", "local", "p1", keys=["ctrl+c"])
    assert control.handle_result(req, {"sent": True}) == cmd
    assert (await task).sent is True


@pytest.mark.asyncio
async def test_runtime_agent_control_reset_confirmation_requires_new_first_tap():
    from herdeck.app_control import RuntimeAgentControl
    from herdeck.config import AnswerProfile, Config

    sender = FakeSender()
    key = AgentKey("local", "p1")
    cfg = Config(
        servers=[],
        profiles={
            "default": AnswerProfile(["y"], ["n"], ["ctrl+c"], ["y"]),
            "codex": AnswerProfile(["y", "enter"], ["n", "enter"], ["ctrl+c"], ["y", "enter"]),
        },
        overview_order=["local"],
        grid=(5, 3),
    )
    cfg.safety.require_confirm_for = ["act_force"]
    agent = AgentState(key, "codex", "herdeck", Status.BLOCKED)
    control = RuntimeAgentControl(cfg, send=sender.send, current_agent=lambda k: agent if k == key else None)

    first = await control.stop(key)
    control.reset_confirmation(key)
    second = await control.stop(key)

    assert first.message == "confirmation required"
    assert second.message == "confirmation required"
    assert sender.sent == []


@pytest.mark.asyncio
async def test_runtime_agent_control_send_text_returns_missing_agent():
    from herdeck.app_control import RuntimeAgentControl
    from herdeck.config import Config

    sender = FakeSender()
    key = AgentKey("local", "p1")
    cfg = Config(servers=[], profiles={}, overview_order=[], grid=(5, 3))
    control = RuntimeAgentControl(cfg, send=sender.send, current_agent=lambda k: None)

    result = await control.send_text(key, "hello", timeout=1)

    assert result.sent is False
    assert result.skipped is False
    assert result.message == "agent is no longer available"
    assert sender.sent == []


@pytest.mark.asyncio
async def test_runtime_agent_control_uses_bounded_default_timeout(monkeypatch):
    from herdeck import app_control
    from herdeck.app_control import RuntimeAgentControl
    from herdeck.config import AnswerProfile, Config

    sender = FakeSender()
    key = AgentKey("local", "p1")
    cfg = Config(
        servers=[],
        profiles={
            "default": AnswerProfile(["y"], ["n"], ["ctrl+c"], ["y"]),
            "codex": AnswerProfile(["y", "enter"], ["n", "enter"], ["ctrl+c"], ["y", "enter"]),
        },
        overview_order=["local"],
        grid=(5, 3),
    )
    agent = AgentState(key, "codex", "herdeck", Status.BLOCKED)
    control = RuntimeAgentControl(cfg, send=sender.send, current_agent=lambda k: agent if k == key else None)
    timeouts = []

    async def fake_wait_for(future, timeout):
        timeouts.append(timeout)
        raise TimeoutError

    monkeypatch.setattr(app_control.asyncio, "wait_for", fake_wait_for)

    with pytest.raises(TimeoutError):
        await control.approve(key)

    assert timeouts == [3.0]
    assert sender.sent
    assert control._pending == {}


@pytest.mark.asyncio
async def test_runtime_control_confirmation_expires_after_ttl():
    """A stale Telegram 'confirmation required' arm must not complete an
    act_force much later (roborev 058bcca)."""
    from herdeck.app_control import RuntimeAgentControl
    from herdeck.config import AnswerProfile, Config

    sender = FakeSender()
    key = AgentKey("local", "p1")
    cfg = Config(
        servers=[],
        profiles={"default": AnswerProfile(["y"], ["n"], ["ctrl+c"], ["y"])},
        overview_order=["local"],
        grid=(5, 3),
    )
    cfg.safety.require_confirm_for = ["act_force"]
    agent = AgentState(key, "default", "herdeck", Status.BLOCKED)
    clk = [1000.0]
    control = RuntimeAgentControl(
        cfg,
        send=sender.send,
        current_agent=lambda k: agent if k == key else None,
        clock=lambda: clk[0],
    )
    first = await control.stop(key)
    assert first.message == "confirmation required"
    clk[0] += 120  # far past the TTL: the second tap must re-arm, not fire
    second = await control.stop(key)
    assert second.sent is False
    assert second.message == "confirmation required"
    assert sender.sent == []
