from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .app_control import ActionResult, RuntimeAgentControl
from .layout import parse_options
from .model import AgentKey, AgentState, Status

API_VERSION = "v1"
TEXT_MAX_BYTES = 4096
IDEMPOTENCY_TTL_S = 10 * 60.0
IDEMPOTENCY_LIMIT = 1024
STOP_CONFIRM_TTL_S = 60.0
STOP_CONFIRM_LIMIT = 1024
DECISION_MAX_CHOICES = 12
DECISION_LABEL_MAX_CHARS = 240


@dataclass(frozen=True)
class SemanticResponse:
    status: int
    body: dict


@dataclass(frozen=True)
class _StoredResult:
    fingerprint: str
    response: SemanticResponse
    expires_at: float


@dataclass(frozen=True)
class _ActionChallenge:
    caller: str
    action: str
    target: tuple[str, str, str]
    expires_at: float
    generation: object


def _bounded(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value if ch >= " " and ch != "\x7f")[:limit]


def _agent_record(agent: AgentState, *, available: bool) -> dict:
    status = agent.status.value if isinstance(agent.status, Status) else Status.UNKNOWN.value
    return {
        "server_id": _bounded(agent.key.server_id, 128),
        "pane_id": _bounded(agent.key.pane_id, 256),
        "terminal_id": _bounded(agent.terminal_id, 256),
        "status": status,
        "available": available,
        "agent_type": _bounded(agent.agent_type, 64),
        "label": _bounded(agent.label, 160),
        "custom_status": _bounded(agent.custom_status, 160),
        "repository": _bounded(agent.repo, 256),
        "branch": _bounded(agent.branch, 256),
        "project": _bounded(agent.project, 256),
        "workspace": _bounded(agent.workspace, 160),
        "tab": _bounded(agent.tab, 160),
        "work": {
            "source": _bounded(agent.work.source, 64),
            "item": _bounded(agent.work.item, 160),
            "run": _bounded(agent.work.run, 160),
            "url": _bounded(agent.work.url, 2048),
        },
    }


class SemanticAPI:
    """Versioned cockpit contract running on the app's asyncio loop."""

    def __init__(
        self,
        control: RuntimeAgentControl,
        *,
        agents: Callable[[], list[AgentState]],
        server_available: Callable[[str], bool],
        generation: Callable[[str, str], object],
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._control = control
        self._agents = agents
        self._server_available = server_available
        self._generation = generation
        self._clock = clock or time.monotonic
        self._results: OrderedDict[tuple[str, str], _StoredResult] = OrderedDict()
        self._inflight: dict[tuple[str, str], tuple[str, asyncio.Future[SemanticResponse]]] = {}
        self._executions: set[asyncio.Task[None]] = set()
        self._challenges: OrderedDict[str, _ActionChallenge] = OrderedDict()

    async def handle(self, request: dict) -> SemanticResponse:
        operation = request.get("operation")
        if operation == "inventory":
            return self.inventory()
        if operation == "action":
            return await self.action(
                request.get("caller", ""),
                request.get("payload"),
            )
        if operation == "text":
            return await self.send_text(
                request.get("caller", ""),
                request.get("payload"),
            )
        if operation == "decisions":
            return await self.decisions(request.get("payload"))
        if operation == "choice":
            return await self.choose(
                request.get("caller", ""),
                request.get("payload"),
            )
        return self._error(404, "not_found", "unknown semantic API operation")

    def inventory(self) -> SemanticResponse:
        records = [
            _agent_record(agent, available=self._server_available(agent.key.server_id))
            for agent in self._agents()
        ]
        records.sort(key=lambda item: (item["server_id"], item["pane_id"]))
        return SemanticResponse(
            200,
            {
                "api_version": API_VERSION,
                "agents": records,
            },
        )

    async def action(self, caller: str, payload: object) -> SemanticResponse:
        if not isinstance(payload, dict):
            return self._validation("request body must be a JSON object")
        allowed = {
            "server_id",
            "pane_id",
            "terminal_id",
            "idempotency_key",
            "action",
            "confirmation",
        }
        if set(payload) - allowed:
            return self._validation("unknown fields are not allowed")
        action = payload.get("action")
        if action not in {"approve", "deny", "stop"}:
            return self._validation("action must be approve, deny, or stop", field="action")
        target = self._target(payload)
        if isinstance(target, SemanticResponse):
            return target
        idempotency_key = self._idempotency_key(payload)
        if isinstance(idempotency_key, SemanticResponse):
            return idempotency_key
        fingerprint = self._fingerprint("action", payload)
        replay = self._replay(caller, idempotency_key, fingerprint)
        if replay is not None:
            return replay
        capacity_error = self._capacity_error(caller, idempotency_key)
        if capacity_error is not None:
            return capacity_error

        requires_confirmation = action == "stop" or self._control.requires_confirmation(action)
        if requires_confirmation:
            confirmation = payload.get("confirmation")
            if confirmation is None:
                agent = self._resolve_target(target)
                if isinstance(agent, SemanticResponse):
                    return agent
                if not self._server_available(agent.key.server_id):
                    return self._outcome(503, "unavailable_target", "target server is offline")
                if action in {"approve", "deny"} and agent.status is not Status.BLOCKED:
                    response = self._outcome(200, "skipped", "agent is not blocked")
                    self._remember(caller, idempotency_key, fingerprint, response)
                    return response
                challenge = secrets.token_urlsafe(24)
                self._prune()
                while len(self._challenges) >= STOP_CONFIRM_LIMIT:
                    self._challenges.popitem(last=False)
                self._challenges[challenge] = _ActionChallenge(
                    caller=caller,
                    action=action,
                    target=target,
                    expires_at=self._clock() + STOP_CONFIRM_TTL_S,
                    generation=self._generation(target[0], target[1]),
                )
                response = SemanticResponse(
                    409,
                    {
                        "api_version": API_VERSION,
                        "outcome": "confirmation_required",
                        "confirmation": challenge,
                        "expires_in": int(STOP_CONFIRM_TTL_S),
                    },
                )
                self._remember(
                    caller,
                    idempotency_key,
                    fingerprint,
                    response,
                    ttl=STOP_CONFIRM_TTL_S,
                )
                return response
        elif payload.get("confirmation") is not None:
            return self._validation("confirmation is not valid for this action")

        owner, pending = self._claim(caller, idempotency_key, fingerprint)
        if isinstance(pending, SemanticResponse):
            return pending
        assert pending is not None
        if owner:
            self._start_execution(
                caller,
                idempotency_key,
                fingerprint,
                self._execute_action(caller, action, target, payload, requires_confirmation),
            )
        return await asyncio.shield(pending)

    async def send_text(self, caller: str, payload: object) -> SemanticResponse:
        if not isinstance(payload, dict):
            return self._validation("request body must be a JSON object")
        allowed = {"server_id", "pane_id", "terminal_id", "idempotency_key", "text"}
        if set(payload) - allowed:
            return self._validation("unknown fields are not allowed")
        target = self._target(payload)
        if isinstance(target, SemanticResponse):
            return target
        idempotency_key = self._idempotency_key(payload)
        if isinstance(idempotency_key, SemanticResponse):
            return idempotency_key
        text = payload.get("text")
        if not isinstance(text, str) or not text:
            return self._validation("text must be a non-empty string", field="text")
        try:
            encoded = text.encode("utf-8")
        except UnicodeEncodeError:
            return self._validation("text must be valid UTF-8", field="text")
        if len(encoded) > TEXT_MAX_BYTES:
            return self._validation(
                f"text exceeds the {TEXT_MAX_BYTES}-byte UTF-8 limit", field="text"
            )
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
            return self._validation(
                "control characters and multiline text are not allowed", field="text"
            )

        fingerprint = self._fingerprint("text", payload)
        replay = self._replay(caller, idempotency_key, fingerprint)
        if replay is not None:
            return replay
        capacity_error = self._capacity_error(caller, idempotency_key)
        if capacity_error is not None:
            return capacity_error
        owner, pending = self._claim(caller, idempotency_key, fingerprint)
        if isinstance(pending, SemanticResponse):
            return pending
        assert pending is not None
        if owner:
            self._start_execution(
                caller,
                idempotency_key,
                fingerprint,
                self._execute_text(target, text),
            )
        return await asyncio.shield(pending)

    async def decisions(self, payload: object) -> SemanticResponse:
        target = self._decision_target(payload)
        if isinstance(target, SemanticResponse):
            return target
        agent = self._resolve_target(target)
        if isinstance(agent, SemanticResponse):
            return agent
        if not self._server_available(agent.key.server_id):
            return self._outcome(503, "unavailable_target", "target server is offline")
        if agent.status is not Status.BLOCKED:
            return SemanticResponse(
                200,
                {"api_version": API_VERSION, "outcome": "not_blocked", "choices": []},
            )
        try:
            prompt = await self._control.read_prompt(agent.key)
        except TimeoutError:
            return self._outcome(504, "timeout", "backend request timed out")
        except (ConnectionError, OSError):
            return self._outcome(503, "backend_failure", "backend request failed")
        except Exception:
            return self._outcome(502, "backend_failure", "backend request failed")
        choices = self._parse_choices(prompt)
        return SemanticResponse(
            200,
            {
                "api_version": API_VERSION,
                "outcome": "ready" if choices else "no_choices",
                "choices": choices,
            },
        )

    async def choose(self, caller: str, payload: object) -> SemanticResponse:
        if not isinstance(payload, dict):
            return self._validation("request body must be a JSON object")
        allowed = {
            "server_id",
            "pane_id",
            "terminal_id",
            "idempotency_key",
            "choice",
        }
        if set(payload) - allowed:
            return self._validation("unknown fields are not allowed")
        target = self._target(payload)
        if isinstance(target, SemanticResponse):
            return target
        idempotency_key = self._idempotency_key(payload)
        if isinstance(idempotency_key, SemanticResponse):
            return idempotency_key
        choice = payload.get("choice")
        if not isinstance(choice, str) or not choice.isdecimal() or len(choice) > 16:
            return self._validation("choice must be a numeric option key", field="choice")

        fingerprint = self._fingerprint("choice", payload)
        replay = self._replay(caller, idempotency_key, fingerprint)
        if replay is not None:
            return replay
        capacity_error = self._capacity_error(caller, idempotency_key)
        if capacity_error is not None:
            return capacity_error
        owner, pending = self._claim(caller, idempotency_key, fingerprint)
        if isinstance(pending, SemanticResponse):
            return pending
        assert pending is not None
        if owner:
            self._start_execution(
                caller,
                idempotency_key,
                fingerprint,
                self._execute_choice(target, choice),
            )
        return await asyncio.shield(pending)

    async def _execute_action(
        self,
        caller: str,
        action: str,
        target: tuple[str, str, str],
        payload: dict,
        requires_confirmation: bool,
    ) -> SemanticResponse:
        agent = self._resolve_target(target)
        if isinstance(agent, SemanticResponse):
            return agent
        if not self._server_available(agent.key.server_id):
            return self._outcome(503, "unavailable_target", "target server is offline")
        if action in {"approve", "deny"} and agent.status is not Status.BLOCKED:
            return self._outcome(200, "skipped", "agent is not blocked")
        if requires_confirmation:
            challenge_error = self._consume_challenge(
                payload.get("confirmation"), caller, action, target
            )
            if challenge_error is not None:
                return challenge_error
        try:
            if action == "approve":
                result = await self._control.approve(agent.key, confirmed=requires_confirmation)
            elif action == "deny":
                result = await self._control.deny(agent.key, confirmed=requires_confirmation)
            else:
                result = await self._control.stop(agent.key, confirmed=True)
        except TimeoutError:
            return self._outcome(504, "timeout", "backend request timed out")
        except (ConnectionError, OSError):
            return self._outcome(503, "backend_failure", "backend request failed")
        except Exception:
            return self._outcome(502, "backend_failure", "backend request failed")
        return self._action_response(result)

    async def _execute_text(self, target: tuple[str, str, str], text: str) -> SemanticResponse:
        agent = self._resolve_target(target)
        if isinstance(agent, SemanticResponse):
            return agent
        if not self._server_available(agent.key.server_id):
            return self._outcome(503, "unavailable_target", "target server is offline")
        try:
            result = await self._control.send_text(agent.key, text)
        except TimeoutError:
            return self._outcome(504, "timeout", "backend request timed out")
        except (ConnectionError, OSError):
            return self._outcome(503, "backend_failure", "backend request failed")
        except Exception:
            return self._outcome(502, "backend_failure", "backend request failed")
        return self._action_response(result)

    async def _execute_choice(
        self, target: tuple[str, str, str], choice: str
    ) -> SemanticResponse:
        agent = self._resolve_target(target)
        if isinstance(agent, SemanticResponse):
            return agent
        if not self._server_available(agent.key.server_id):
            return self._outcome(503, "unavailable_target", "target server is offline")
        if agent.status is not Status.BLOCKED:
            return self._outcome(409, "not_blocked", "agent is no longer blocked")
        try:
            prompt = await self._control.read_prompt(agent.key)
            valid_choices = {item["key"] for item in self._parse_choices(prompt)}
            if choice not in valid_choices:
                return self._outcome(409, "stale_choice", "choice is no longer available")
            result = await self._control.send_text(agent.key, choice)
        except TimeoutError:
            return self._outcome(504, "timeout", "backend request timed out")
        except (ConnectionError, OSError):
            return self._outcome(503, "backend_failure", "backend request failed")
        except Exception:
            return self._outcome(502, "backend_failure", "backend request failed")
        return self._action_response(result)

    def invalidate_challenges(self) -> None:
        self._challenges.clear()

    def _resolve_target(self, target: tuple[str, str, str]) -> AgentState | SemanticResponse:
        server_id, pane_id, terminal_id = target
        agent = self._control.current_agent(AgentKey(server_id, pane_id))
        if agent is None:
            return self._outcome(404, "unavailable_target", "target agent was not found")
        if not agent.terminal_id or agent.terminal_id != terminal_id:
            return self._outcome(409, "stale_identity", "terminal identity is stale")
        return agent

    def _consume_challenge(
        self,
        confirmation: object,
        caller: str,
        action: str,
        target: tuple[str, str, str],
    ) -> SemanticResponse | None:
        if not isinstance(confirmation, str) or not confirmation:
            return self._validation("confirmation must be a non-empty string")
        self._prune()
        challenge = self._challenges.pop(confirmation, None)
        if challenge is None:
            return self._outcome(409, "confirmation_expired", "confirmation is invalid")
        if (
            challenge.caller != caller
            or challenge.action != action
            or challenge.target != target
            or challenge.generation != self._generation(target[0], target[1])
        ):
            return self._outcome(409, "confirmation_expired", "confirmation is invalid")
        return None

    def _target(self, payload: dict) -> tuple[str, str, str] | SemanticResponse:
        values = []
        for field, limit in (("server_id", 128), ("pane_id", 256), ("terminal_id", 256)):
            value = payload.get(field)
            if not isinstance(value, str) or not value or len(value) > limit:
                return self._validation(f"{field} must be a non-empty string", field=field)
            values.append(value)
        return values[0], values[1], values[2]

    def _decision_target(self, payload: object) -> tuple[str, str, str] | SemanticResponse:
        if not isinstance(payload, dict):
            return self._validation("request body must be a JSON object")
        allowed = {"server_id", "pane_id", "terminal_id"}
        if set(payload) - allowed:
            return self._validation("unknown fields are not allowed")
        return self._target(payload)

    @staticmethod
    def _parse_choices(prompt: str) -> list[dict[str, str]]:
        return [
            {
                "key": _bounded(option.key, 16),
                "label": _bounded(option.label, DECISION_LABEL_MAX_CHARS),
            }
            for option in parse_options(prompt)[:DECISION_MAX_CHOICES]
            if option.key and option.label
        ]

    def _idempotency_key(self, payload: dict) -> str | SemanticResponse:
        value = payload.get("idempotency_key")
        if not isinstance(value, str) or not 1 <= len(value) <= 160:
            return self._validation(
                "idempotency_key must be a string of 1 to 160 characters",
                field="idempotency_key",
            )
        return value

    def _replay(self, caller: str, key: str, fingerprint: str) -> SemanticResponse | None:
        self._prune()
        stored = self._results.get((caller, key))
        if stored is None:
            return None
        if stored.fingerprint != fingerprint:
            return self._error(
                409,
                "idempotency_conflict",
                "idempotency key was already used for a different request",
            )
        self._results.move_to_end((caller, key))
        return stored.response

    def _remember(
        self,
        caller: str,
        key: str,
        fingerprint: str,
        response: SemanticResponse,
        *,
        ttl: float = IDEMPOTENCY_TTL_S,
    ) -> None:
        self._prune()
        self._results[(caller, key)] = _StoredResult(fingerprint, response, self._clock() + ttl)

    def _claim(
        self, caller: str, key: str, fingerprint: str
    ) -> tuple[bool, asyncio.Future[SemanticResponse] | SemanticResponse | None]:
        inflight = self._inflight.get((caller, key))
        if inflight is not None:
            existing_fingerprint, future = inflight
            if existing_fingerprint != fingerprint:
                return False, self._error(
                    409,
                    "idempotency_conflict",
                    "idempotency key is in use by a different request",
                )
            return False, future
        capacity_error = self._capacity_error(caller, key)
        if capacity_error is not None:
            return False, capacity_error
        future = asyncio.get_running_loop().create_future()
        self._inflight[(caller, key)] = (fingerprint, future)
        return True, future

    def _start_execution(
        self,
        caller: str,
        key: str,
        fingerprint: str,
        operation: Awaitable[SemanticResponse],
    ) -> None:
        task = asyncio.create_task(self._finish_execution(caller, key, fingerprint, operation))
        self._executions.add(task)
        task.add_done_callback(self._executions.discard)

    async def _finish_execution(
        self,
        caller: str,
        key: str,
        fingerprint: str,
        operation: Awaitable[SemanticResponse],
    ) -> None:
        try:
            response = await operation
        except BaseException:
            self._abandon(caller, key)
            raise
        self._complete(caller, key, fingerprint, response)

    def _capacity_error(self, caller: str, key: str) -> SemanticResponse | None:
        self._prune()
        request_key = (caller, key)
        if request_key in self._results or request_key in self._inflight:
            return None
        if len(self._results) + len(self._inflight) < IDEMPOTENCY_LIMIT:
            return None
        return self._error(
            429,
            "idempotency_capacity",
            "idempotency capacity is temporarily exhausted",
        )

    def _complete(
        self, caller: str, key: str, fingerprint: str, response: SemanticResponse
    ) -> None:
        inflight = self._inflight.pop((caller, key), None)
        self._remember(caller, key, fingerprint, response)
        if inflight is not None and not inflight[1].done():
            inflight[1].set_result(response)

    def _abandon(self, caller: str, key: str) -> None:
        inflight = self._inflight.pop((caller, key), None)
        if inflight is not None and not inflight[1].done():
            inflight[1].cancel()

    def _prune(self) -> None:
        now = self._clock()
        self._results = OrderedDict(
            (key, value) for key, value in self._results.items() if value.expires_at > now
        )
        self._challenges = OrderedDict(
            (key, value) for key, value in self._challenges.items() if value.expires_at > now
        )

    @staticmethod
    def _fingerprint(operation: str, payload: dict) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(f"{operation}\0{canonical}".encode()).hexdigest()

    @staticmethod
    def _action_response(result: ActionResult) -> SemanticResponse:
        message = _bounded(result.message, 256)
        lowered = message.lower()
        if result.sent:
            return SemanticAPI._outcome(200, "sent", "action sent")
        if "identity changed" in lowered or ("terminal" in lowered and "stale" in lowered):
            return SemanticAPI._outcome(409, "stale_identity", "terminal identity is stale")
        if result.skipped:
            return SemanticAPI._outcome(200, "skipped", "action skipped")
        if "confirmation required" in lowered:
            return SemanticAPI._outcome(409, "confirmation_required", "confirmation required")
        if "available" in lowered or "not found" in lowered:
            return SemanticAPI._outcome(404, "unavailable_target", "target is unavailable")
        return SemanticAPI._outcome(502, "backend_failure", "backend action failed")

    @staticmethod
    def _validation(message: str, *, field: str | None = None) -> SemanticResponse:
        body = {
            "api_version": API_VERSION,
            "error": {"code": "validation_error", "message": message},
        }
        if field is not None:
            body["error"]["field"] = field
        return SemanticResponse(422, body)

    @staticmethod
    def _outcome(status: int, outcome: str, message: str) -> SemanticResponse:
        return SemanticResponse(
            status,
            {
                "api_version": API_VERSION,
                "outcome": outcome,
                "message": _bounded(message, 256),
            },
        )

    @staticmethod
    def _error(status: int, code: str, message: str) -> SemanticResponse:
        return SemanticResponse(
            status,
            {
                "api_version": API_VERSION,
                "error": {"code": code, "message": message},
            },
        )
