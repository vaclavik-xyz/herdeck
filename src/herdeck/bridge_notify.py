"""Bridge-side Telegram alerts: the agent control (this part) and the notifier.

``BridgeAgentControl`` implements the control protocol ``TelegramInteractor``
calls. All methods are coroutines (or plain sync for ``current_agent`` /
``reset_confirmation``) running on the bridge's own event loop, exactly as the
interactor awaits them, so no thread bridging is needed. Every answer goes
through the injected ``execute`` (in production a partial of
``bridge_answers.execute_answer``) and therefore through the same episode guard
as a deck's answer.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from types import SimpleNamespace

from .app_control import ActionResult
from .commands import build_action_command, command_to_msg, profile_for
from .config import ConfigError
from .events import EventHub
from .model import AgentKey, AgentState
from .shared_settings import SharedSettings, parse_shared

log = logging.getLogger(__name__)

BY = "telegram"
# Same arming window as RuntimeAgentControl: a Stop tap from long ago can
# never be completed by a later single tap.
CONFIRM_TTL_S = 60.0


class BridgeAgentControl:
    def __init__(
        self,
        *,
        execute: Callable[[dict, str], Awaitable[dict]],
        agents: Callable[[], dict[AgentKey, AgentState]],
        episodes: EventHub,
        settings,
        read_prompt: Callable[[str], Awaitable[str | None]],
        clock: Callable[[], float] = time.monotonic,
    ):
        self._execute = execute
        self._agents = agents
        self._episodes = episodes
        self._settings = settings
        self._read_prompt = read_prompt
        self._clock = clock
        self._pending_confirm: tuple[str, AgentKey] | None = None
        self._pending_confirm_at = 0.0

    # --- state --------------------------------------------------------------
    def _shared(self) -> SharedSettings:
        """The bridge's shared settings; built-in defaults when unset/invalid."""
        raw = self._settings.raw
        if raw is not None:
            try:
                return parse_shared(raw)
            except ConfigError as exc:
                log.warning("shared settings invalid, using defaults: %s", exc)
        return parse_shared({})

    def current_agent(self, key: AgentKey) -> AgentState | None:
        return self._agents().get(key)

    def reset_confirmation(self, key: AgentKey | None = None) -> None:
        if key is None or (self._pending_confirm is not None and self._pending_confirm[1] == key):
            self._pending_confirm = None

    def _episode_fields(self, agent: AgentState) -> dict:
        """episode_id / prompt_revision of the pane's open blocked episode."""
        ep = self._episodes.open_episode(agent.key.pane_id)
        if ep is None or ep.kind != "blocked":
            return {}
        if agent.terminal_id and ep.terminal_id not in ("", agent.terminal_id):
            return {}
        fields = {"episode_id": ep.id}
        if ep.revision:
            fields["prompt_revision"] = ep.revision
        return fields

    # --- reads --------------------------------------------------------------
    async def read_prompt(self, key: AgentKey, *, timeout: float | None = 3.0) -> str:
        agent = self.current_agent(key)
        if agent is None:
            return ""
        ep = self._episodes.open_episode(key.pane_id)
        if ep is not None and ep.kind == "blocked" and ep.prompt:
            return ep.prompt
        return (await self._read_prompt(key.pane_id)) or ""

    # --- answers ------------------------------------------------------------
    async def approve(
        self, key: AgentKey, *, timeout: float | None = 3.0, force: bool = False,
        always: bool = False, confirmed: bool = False,
    ) -> ActionResult:
        return await self._act("approve", key, force=force, always=always, confirmed=confirmed)

    async def deny(
        self, key: AgentKey, *, timeout: float | None = 3.0, force: bool = False,
        confirmed: bool = False,
    ) -> ActionResult:
        return await self._act("deny", key, force=force, always=False, confirmed=confirmed)

    async def stop(
        self, key: AgentKey, *, timeout: float | None = 3.0, confirmed: bool = False
    ) -> ActionResult:
        return await self._act("stop", key, force=True, always=False, confirmed=confirmed)

    async def send_text(
        self, key: AgentKey, text: str, *, timeout: float | None = 3.0
    ) -> ActionResult:
        agent = self.current_agent(key)
        if agent is None:
            return ActionResult(False, message="agent is no longer available")
        if agent.backend != "herdr":
            return ActionResult(False, message="agent is not answerable from the bridge")
        msg: dict = {"type": "send_text", "req": "tg", "pane_id": key.pane_id, "text": text}
        if agent.terminal_id:
            msg["terminal_id"] = agent.terminal_id
        msg.update(self._episode_fields(agent))
        return await self._run(msg)

    async def _act(
        self, action: str, key: AgentKey, *, force: bool, always: bool, confirmed: bool
    ) -> ActionResult:
        agent = self.current_agent(key)
        if agent is None:
            return ActionResult(False, message="agent is no longer available")
        if agent.backend != "herdr":
            return ActionResult(False, message="agent is not answerable from the bridge")
        shared = self._shared()
        action_id = self._action_id(action, force=force, always=always)
        if action_id in shared.safety.require_confirm_for and not confirmed:
            armed = (
                self._pending_confirm == (action_id, key)
                and self._clock() - self._pending_confirm_at <= CONFIRM_TTL_S
            )
            if not armed:
                self._pending_confirm = (action_id, key)
                self._pending_confirm_at = self._clock()
                return ActionResult(False, message="confirmation required")
        self._pending_confirm = None
        # profile_for only reads ``.profiles``.
        profile = profile_for(SimpleNamespace(profiles=shared.answer_profiles), agent.agent_type)
        command = build_action_command(action, agent, profile, force=force, always=always)
        msg = command_to_msg(command, "tg")
        msg.update(self._episode_fields(agent))
        return await self._run(msg)

    @staticmethod
    def _action_id(action: str, *, force: bool, always: bool) -> str:
        if action == "stop" or force:
            return "act_force"
        if action == "approve" and always:
            return "approve_always"
        return action

    async def _run(self, msg: dict) -> ActionResult:
        try:
            data = await self._execute(msg, BY)
        except Exception as exc:  # never let an answer crash the interactor
            log.warning("bridge answer failed: %s", type(exc).__name__)
            return ActionResult(False, message="answer failed")
        if "error" in data:
            return ActionResult(False, message=str(data["error"]))
        return ActionResult(
            data.get("sent") is True,
            skipped=data.get("skipped") is True,
            message=str(data.get("message") or ""),
        )
