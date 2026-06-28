from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .commands import Command, build_action_command, profile_for
from .config import Config
from .model import AgentKey, AgentState


@dataclass
class ActionResult:
    sent: bool
    skipped: bool = False
    message: str = ""


class RuntimeAgentControl:
    def __init__(
        self,
        config: Config,
        *,
        send: Callable[[Command, str], Awaitable[None]],
        current_agent: Callable[[AgentKey], AgentState | None],
    ):
        self._config = config
        self._send = send
        self._current_agent = current_agent
        self._req = 0
        self._pending: dict[str, tuple[asyncio.Future, Command]] = {}
        self._pending_confirm: tuple[str, AgentKey] | None = None

    def update_config(self, config: Config) -> None:
        self._config = config
        self._pending_confirm = None

    def reset_confirmation(self, key: AgentKey | None = None) -> None:
        if key is None or (
            self._pending_confirm is not None and self._pending_confirm[1] == key
        ):
            self._pending_confirm = None

    def current_agent(self, key: AgentKey) -> AgentState | None:
        return self._current_agent(key)

    def handle_result(self, req: str, data: dict, *, server_id: str | None = None) -> Command | None:
        pending = self._pending.get(req)
        if pending is None:
            return None
        future, command = pending
        if server_id is not None and command.server_id != server_id:
            return None
        self._pending.pop(req, None)
        if not future.done():
            future.set_result(data)
        return command

    async def read_prompt(self, key: AgentKey, *, timeout: float | None = 3.0) -> str:
        agent = self.current_agent(key)
        if agent is None:
            return ""
        data = await self._request(
            Command("read", agent.key.server_id, agent.key.pane_id, source="detection"),
            timeout=timeout,
        )
        return data.get("text", "")

    async def approve(
        self,
        key: AgentKey,
        *,
        timeout: float | None = 3.0,
        force: bool = False,
        always: bool = False,
    ) -> ActionResult:
        return await self._act("approve", key, timeout=timeout, force=force, always=always)

    async def deny(
        self,
        key: AgentKey,
        *,
        timeout: float | None = 3.0,
        force: bool = False,
    ) -> ActionResult:
        return await self._act("deny", key, timeout=timeout, force=force, always=False)

    async def stop(self, key: AgentKey, *, timeout: float | None = 3.0) -> ActionResult:
        return await self._act("stop", key, timeout=timeout, force=True, always=False)

    async def send_text(
        self, key: AgentKey, text: str, *, timeout: float | None = 3.0
    ) -> ActionResult:
        agent = self.current_agent(key)
        if agent is None:
            return ActionResult(False, message="agent is no longer available")
        data = await self._request(
            Command("send_text", agent.key.server_id, agent.key.pane_id, text=text),
            timeout=timeout,
        )
        return self._action_result(data)

    async def _act(
        self,
        action: str,
        key: AgentKey,
        *,
        timeout: float | None,
        force: bool,
        always: bool,
    ) -> ActionResult:
        agent = self.current_agent(key)
        if agent is None:
            return ActionResult(False, message="agent is no longer available")
        action_id = self._action_id(action, force=force, always=always)
        if action_id in self._config.safety.require_confirm_for:
            if self._pending_confirm != (action_id, key):
                self._pending_confirm = (action_id, key)
                return ActionResult(False, message="confirmation required")
        self._pending_confirm = None
        command = build_action_command(
            action,
            agent,
            profile_for(self._config, agent.agent_type),
            force=force,
            always=always,
        )
        return self._action_result(await self._request(command, timeout=timeout))

    def _action_id(self, action: str, *, force: bool, always: bool) -> str:
        if action == "stop" or force:
            return "act_force"
        if action == "approve" and always:
            return "approve_always"
        return action

    def _action_result(self, data: dict) -> ActionResult:
        return ActionResult(
            data.get("sent") is True,
            skipped=data.get("skipped") is True,
            message=str(data.get("message") or ""),
        )

    async def _request(self, command: Command, *, timeout: float | None) -> dict:
        req = self._next_req()
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[req] = (future, command)
        try:
            await self._send(command, req)
            return await asyncio.wait_for(future, timeout)
        finally:
            pending = self._pending.get(req)
            if pending is not None and pending[0] is future:
                self._pending.pop(req, None)

    def _next_req(self) -> str:
        self._req += 1
        return f"tg{self._req}"
