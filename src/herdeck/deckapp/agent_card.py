"""The desktop agent card: one agent in full, driven over the runtime's /agent/* routes.

The deck drill shows a blocked prompt in three panel lines; the card shows the
whole prompt, the same parsed options (``Orchestrator.answer_options``), a
free-text reply and Stop/Focus. Its commands take the same guarded wire paths
as the deck (``act`` with the blocked guard + terminal identity, ``send_text``,
``focus``), plus one card-only guard: an answer names the prompt revision the
user was looking at, and is refused when the runtime's current prompt differs.

Unlike a deck press, a card action waits (briefly) for the bridge's reply so the
window can say what happened — including a read-only bridge token refusing the
message, which the deck path would drop silently.

``AgentCardMixin`` is mixed into ``LiveSource`` (it uses that class's buffers
and locks); the route helpers at the bottom are called from ``DeckApp``'s HTTP
handler. The demo mock has no card: every route answers 404 there.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field

from ..commands import Command, command_to_msg, profile_for
from ..decisions import decision_revision
from ..model import AgentKey, Status, Subagent
from .agent_term import CardTerminals

# How long a card action waits for the bridge's reply before answering
# "pending" (sent, but unconfirmed). Well under the desktop proxy's timeout.
CARD_REPLY_TIMEOUT_S = 6.0
# A free-text reply is typed into the agent's terminal: keep it bounded.
CARD_TEXT_MAX = 4000
# The card renders at most this much of a prompt (the tail: the live question).
PROMPT_MAX_CHARS = 64 * 1024
# At most one card-triggered re-read per agent in this window.
_REFRESH_MIN_INTERVAL_S = 2.0

# CSI (colours, cursor moves), OSC (titles, hyperlinks; BEL or ST terminated),
# and every other ESC sequence (intermediates + final byte: ESC c, ESC ( B, …).
_ESCAPE_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|\x1b[ -/]*[0-~]"
)
# C0 controls except TAB and LF, DEL, the C1 range, and the invisible
# formatting characters that can reorder or hide text (bidi embeddings /
# overrides / isolates, zero-width spaces and joiners, marks, BOM).
_CONTROL_RE = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]"
)


def sanitize_prompt(text: str) -> str:
    """Terminal capture -> plain text safe to show verbatim (monospace).

    Strips escape sequences and control characters (CRLF becomes LF) and keeps
    only the last ``PROMPT_MAX_CHARS`` — the live question sits at the bottom.
    """
    text = _ESCAPE_RE.sub("", text or "").replace("\r\n", "\n")
    text = _CONTROL_RE.sub("", text)
    if len(text) > PROMPT_MAX_CHARS:
        text = text[-PROMPT_MAX_CHARS:]
    return text


def subagent_rows(subagents: tuple[Subagent, ...], now_ms: int) -> list[dict]:
    """The card's "Subagents" list: bridge order (most recent first) plus
    ``duration_s`` — so far for a running (or stale) one, total once it
    ended. The runtime's clock stands in for the bridge host's; a small skew
    only shifts a running duration, and it never goes negative."""
    rows = []
    for sub in subagents:
        end = sub.ended_ms if sub.ended_ms is not None else now_ms
        rows.append(
            {
                "id": sub.id,
                "provider": sub.provider,
                "type": sub.type,
                "description": sub.description,
                "model": sub.model,
                "depth": sub.depth,
                "status": sub.status,
                "duration_s": max(0, (end - sub.started_ms) // 1000),
            }
        )
    return rows


def outcome(code: str, message: str = "", *, ok: bool | None = None) -> dict:
    """The JSON a card action answers with. ``code`` is stable for the UI."""
    if ok is None:
        ok = code in ("sent", "focused", "pending")
    return {"ok": ok, "code": code, "message": message}


def result_outcome(data: dict) -> dict:
    """Map a bridge ``result`` payload onto a card outcome."""
    if not isinstance(data, dict):
        return outcome("sent")
    if data.get("skipped"):
        message = data.get("message") or ""
        if message in ("", "not_blocked"):
            return outcome("not_blocked")
        if message == "agent identity changed":
            return outcome("identity_changed")
        if message == "stale_choice":
            return outcome("stale")
        return outcome("rejected", str(message))
    if data.get("focused"):
        return outcome("focused")
    error = data.get("error")
    if error:
        text = error.get("message", error) if isinstance(error, dict) else error
        return outcome("error", str(text))
    return outcome("sent")


def error_outcome(message: str) -> dict:
    """A bridge ``error`` frame as a card outcome (read-only tokens named)."""
    if message.startswith("read-only token"):
        return outcome("readonly", message)
    return outcome("error", message or "bridge error")


@dataclass
class _Reply:
    server_id: str
    event: threading.Event = field(default_factory=threading.Event)
    result: dict | None = None


class PendingReplies:
    """Card requests waiting for their bridge reply, keyed by request id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waits: dict[str, _Reply] = {}

    def register(self, req: str, server_id: str) -> _Reply:
        reply = _Reply(server_id)
        with self._lock:
            self._waits[req] = reply
        return reply

    def discard(self, req: str) -> None:
        with self._lock:
            self._waits.pop(req, None)

    def _finish(self, reply: _Reply | None, result: dict) -> bool:
        if reply is None:
            return False
        if reply.result is None:
            reply.result = result
        reply.event.set()
        return True

    def resolve(self, req: str | None, data: dict) -> bool:
        if req is None:
            return False
        with self._lock:
            reply = self._waits.pop(req, None)
        return self._finish(reply, result_outcome(data))

    def fail(self, req: str, result: dict) -> bool:
        with self._lock:
            reply = self._waits.pop(req, None)
        return self._finish(reply, result)

    def fail_server(self, server_id: str, result: dict) -> None:
        with self._lock:
            victims = [r for r, w in self._waits.items() if w.server_id == server_id]
            replies = [self._waits.pop(r) for r in victims]
        for reply in replies:
            self._finish(reply, result)


# Browser preview pool: WebDeck already caps concurrent streams at 3.
WEB_TERM_SESSIONS = 3
WEB_TERM_IDLE_S = 30.0


class AgentCardMixin:
    """Card operations for ``LiveSource`` (uses its agent buffer, pre-read cache,
    runners and the deck lock; lock order is always deck lock -> source lock)."""

    def _card_init(self) -> None:
        self._card_replies = PendingReplies()
        self._card_reads: dict[AgentKey, float] = {}
        self._card_terms = CardTerminals(self._card_term_send)
        # Browser previews of the web cockpit (driver.web /term SSE): their own
        # session pool, so a card preview never evicts a browser one. The SSE
        # pump polls continuously, so the idle reaper only catches a pump that
        # died without closing.
        self._web_terms = CardTerminals(
            self._card_term_send, max_sessions=WEB_TERM_SESSIONS, idle_s=WEB_TERM_IDLE_S
        )

    # --- connector hooks ---------------------------------------------------
    def _card_on_result(self, req: str | None, data: dict) -> None:
        self._card_replies.resolve(req, data)

    def _on_request_error(self, server_id: str, req: str | None, message: str) -> None:
        """A bridge error frame. A named request fails alone; an anonymous one
        (a handler exception has no req) fails every card request waiting on
        that server — same stance as herdeck-ctl."""
        result = error_outcome(message)
        if req:
            if not self._card_replies.fail(req, result) and not self._card_terms.fail_request(
                server_id, req, message or "bridge error"
            ):
                self._web_terms.fail_request(server_id, req, message or "bridge error")
            return
        self._card_replies.fail_server(server_id, result)
        self._card_terms.fail_fresh(server_id, message or "bridge error")
        self._web_terms.fail_fresh(server_id, message or "bridge error")

    def _card_on_connection(self, server_id: str, up: bool) -> None:
        if not up:
            self._card_replies.fail_server(server_id, outcome("disconnected"))
            self._card_terms.close_server(server_id, "disconnected")
            self._web_terms.close_server(server_id, "disconnected")

    def _on_term(self, server_id: str, message) -> None:
        """Connector callback: a live-terminal frame or close for a card or
        browser preview (each pool ignores requests it does not own)."""
        self._card_terms.on_term(server_id, message)
        self._web_terms.on_term(server_id, message)

    # --- live terminal -----------------------------------------------------
    def _card_term_send(self, server_id: str, msg: dict) -> bool:
        runner = self._runners.get(server_id)
        with self._lock:
            up = self._connected.get(server_id, False)
        if runner is None or not up:
            return False
        runner.send(msg)
        return True

    def card_term_open(self, server_id: str, pane_id: str, cols, rows) -> dict | None:
        """Start observing the agent's pane for the card (see agent_term)."""
        agent, connected = self._card_agent(AgentKey(server_id, pane_id))
        if agent is None:
            return None
        if agent.backend == "t3":
            return outcome("invalid")
        if not connected:
            return outcome("disconnected")
        connector = getattr(self._runners.get(server_id), "connector", None)
        if "terminal_preview" not in getattr(connector, "capabilities", frozenset()):
            # The bridge does not advertise observe: never show a blank "live" view.
            return outcome("unsupported")
        session_id = self._card_terms.open(
            server_id, pane_id, agent.terminal_id, cols=cols, rows=rows
        )
        return {**outcome("open", ok=True), "id": session_id}

    def card_term_poll(self, session_id: str, after: int, wait_ms: int) -> dict | None:
        return self._card_terms.poll(session_id, after, wait_ms / 1000.0)

    def card_term_close(self, session_id: str) -> bool:
        return self._card_terms.close(session_id)

    def _card_close(self) -> None:
        self._card_terms.close_all()
        self._web_terms.close_all()

    # --- browser (web cockpit) previews --------------------------------------
    def web_term_open(self, index: int, cols, rows) -> tuple[str, str] | str:
        """Start observing the agent the deck last rendered on tile ``index``.

        Returns ``(label, session_id)``, or the reason it cannot: "no_agent"
        (empty / just re-occupied tile) or "disconnected". Unlike the card, the
        bridge's ``terminal_preview`` capability is not required — the web
        preview always asked, and a bridge that cannot observe answers with an
        error frame that closes the session."""
        orch = self._orch
        if orch is None:
            return "no_agent"
        with self._card_deck_lock():
            agent = orch.agent_for_preview(index)
        if agent is None:
            return "no_agent"
        key = agent.key
        with self._lock:
            connected = self._connected.get(key.server_id, False)
        if not connected or self._runners.get(key.server_id) is None:
            return "disconnected"
        session_id = self._web_terms.open(
            key.server_id, key.pane_id, agent.terminal_id, cols=cols, rows=rows
        )
        return (agent.label or agent.agent_type, session_id)

    def web_term_poll(self, session_id: str, after: int, wait_s: float) -> dict | None:
        return self._web_terms.poll(session_id, after, wait_s)

    def web_term_close(self, session_id: str) -> bool:
        return self._web_terms.close(session_id)

    # --- queries -----------------------------------------------------------
    def _card_deck_lock(self):
        return self._deck_lock if self._deck_lock is not None else threading.Lock()

    def card_resolve(self, index: int) -> tuple[str, str] | None:
        """The agent the deck last rendered on tile ``index`` (None when empty
        or just re-occupied — the same guard the web preview uses)."""
        orch = self._orch
        if orch is None:
            return None
        with self._card_deck_lock():
            agent = orch.agent_for_preview(index)
        return (agent.key.server_id, agent.key.pane_id) if agent is not None else None

    def card_detail(self, server_id: str, pane_id: str, *, refresh: bool = False) -> dict | None:
        key = AgentKey(server_id, pane_id)
        orch = self._orch
        reread = None
        with self._card_deck_lock():
            with self._lock:
                agent = self._agents.get(key)
                connected = self._connected.get(server_id, False)
                cached = self._preread.get(key)
                pending = key in self._preread_req
            if agent is None:
                return None
            since = orch.status_elapsed(key) if orch is not None else None
            prompt: str | None = None
            prompt_pending = False
            revision: str | None = None
            options: list[dict] = []
            if agent.backend == "t3":
                prompt = agent.preview or None
                revision = agent.backend_revision or None
                options = orch.answer_options(key, "") if orch is not None else []
            elif agent.status is Status.BLOCKED:
                if isinstance(cached, str) and cached:
                    prompt = cached
                    revision = decision_revision(server_id, pane_id, agent.terminal_id, cached)
                    # Labels come from the sanitized text (keys are unchanged).
                    options = (
                        orch.answer_options(key, sanitize_prompt(cached)) if orch is not None else []
                    )
                else:
                    prompt_pending = pending or refresh or connected
                # Re-read on request, and whenever nothing is cached or in
                # flight (e.g. right after an answer dropped the cache).
                if connected and (refresh or not (cached or pending)):
                    reread = self._card_prepare_reread(key, agent)
        if reread is not None:
            # Sent after releasing the deck lock: a runner may deliver its
            # reply synchronously, and that reply takes the deck lock.
            runner, msg = reread
            runner.send(msg)
        options = [{**o, "label": sanitize_prompt(o["label"])} for o in options]
        stop_ok = agent.backend != "t3" or "stop" in agent.capabilities
        text_ok = agent.backend != "t3" or "continue" in agent.capabilities
        return {
            "server_id": server_id,
            "pane_id": pane_id,
            "agent_type": agent.agent_type,
            "display_agent": agent.display_agent,
            "label": agent.label,
            "title": agent.title,
            "repo": agent.repo,
            "branch": agent.branch,
            "workspace": agent.workspace,
            "tab": agent.tab,
            "status": agent.status.value,
            "since_s": int(since) if since is not None else None,
            "backend": agent.backend,
            "connected": connected,
            "prompt": sanitize_prompt(prompt) if prompt is not None else None,
            "prompt_pending": prompt_pending,
            "revision": revision,
            "options": options,
            "can_stop": stop_ok,
            "stop_confirm": "act_force" in self._config.safety.require_confirm_for,
            "can_text": text_ok,
            "can_focus": agent.backend != "t3",
            "subagents": subagent_rows(agent.subagents, int(time.time() * 1000)),
        }

    def _card_prepare_reread(self, key: AgentKey, agent):
        """Register a fresh prompt read for a blocked agent as its episode read
        (so the result lands in the pre-read cache, and in the deck drill when
        that agent is drilled) and return ``(runner, msg)`` for the caller to
        send once it released the deck lock. Rate limited; None when skipped."""
        runner = self._runners.get(key.server_id)
        if runner is None:
            return None
        now = time.monotonic()
        with self._lock:
            last = self._card_reads.get(key)
            if last is not None and now - last < _REFRESH_MIN_INTERVAL_S:
                return None
            self._card_reads[key] = now
            self._bg_req += 1
            req = f"p{self._bg_req}"
            self._preread_req[key] = req
        msg = command_to_msg(
            Command(
                "read",
                key.server_id,
                key.pane_id,
                source="detection",
                terminal_id=agent.terminal_id or None,
            ),
            req,
        )
        return runner, msg

    # --- actions -----------------------------------------------------------
    def _card_agent(self, key: AgentKey):
        with self._lock:
            return self._agents.get(key), self._connected.get(key.server_id, False)

    def card_answer(self, server_id: str, pane_id: str, choice: str, revision: str) -> dict | None:
        """Answer the blocked prompt the user saw (``revision``) with ``choice``
        (an option key, a fallback action id, or a T3 action id)."""
        key = AgentKey(server_id, pane_id)
        orch = self._orch
        with self._card_deck_lock():
            agent, connected = self._card_agent(key)
            if agent is None:
                return None
            if not connected:
                return outcome("disconnected")
            if orch is None:
                return outcome("error", "deck not ready")
            if agent.backend == "t3":
                if not revision or revision != agent.backend_revision:
                    return outcome("stale")
                option = next((o for o in agent.backend_actions if o["id"] == choice), None)
                allowed = {o["key"] for o in orch.answer_options(key, "")}
                if option is None or choice not in allowed:
                    return outcome("invalid")
                cmd = Command(
                    "backend_action",
                    server_id,
                    pane_id,
                    action=choice,
                    payload=option.get("payload", {}),
                    decision_revision=agent.backend_revision,
                )
            else:
                if agent.status is not Status.BLOCKED:
                    return outcome("not_blocked")
                with self._lock:
                    prompt = self._preread.get(key)
                    # The episode this answer is for, captured with the prompt:
                    # the agent may re-block on another prompt while the send
                    # below waits for the bridge.
                    episode = self._block_episode.get(key)
                if not isinstance(prompt, str) or not prompt:
                    return outcome("stale")
                if revision != decision_revision(server_id, pane_id, agent.terminal_id, prompt):
                    return outcome("stale")
                option = next(
                    (
                        o
                        for o in orch.answer_options(key, sanitize_prompt(prompt))
                        if o["key"] == choice
                    ),
                    None,
                )
                if option is None:
                    return outcome("invalid")
                if option["kind"] == "option":
                    cmd = self._blocked_option_command(key, agent, choice, prompt)
                else:
                    profile = profile_for(self._config, agent.agent_type)
                    cmd = Command(
                        "act_if_blocked",
                        server_id,
                        pane_id,
                        keys=list(getattr(profile, option["id"])),
                        terminal_id=agent.terminal_id or None,
                    )
        self._notify_throttle.note_interaction(key)
        result = self._card_send(cmd)
        if agent.backend != "t3":
            # Only an answer that went out spends the episode (so a banner of
            # it can no longer act); a refused / failed one leaves a retry open.
            self._spend_answered_prompt(
                key, episode, answered=result.get("code") in ("sent", "pending")
            )
        return result

    # --- guards shared with banner answers (LiveSource.answer_agent) --------
    def _blocked_option_command(self, key: AgentKey, agent, option_key: str, prompt: str) -> Command:
        """The command that selects numbered option ``option_key`` of the
        blocked ``prompt`` — for the card and for a banner Approve/Deny alike.

        On a protocol-3 bridge it is ``choose_if_blocked`` with the prompt's
        decision revision: the bridge re-reads the prompt and refuses
        ("stale_choice") unless it still hashes to that revision, so an agent
        that moved from prompt A straight to prompt B (never seen leaving
        BLOCKED here) cannot get A's answer typed into B. An older bridge gets
        the deck drill's own path ([digit, enter], blocked + identity guarded).
        """
        if self._card_bridge_protocol(key.server_id) >= 3:
            return Command(
                "choose_if_blocked",
                key.server_id,
                key.pane_id,
                text=option_key,
                terminal_id=agent.terminal_id or None,
                decision_revision=decision_revision(
                    key.server_id, key.pane_id, agent.terminal_id, prompt
                ),
            )
        return Command(
            "act_if_blocked",
            key.server_id,
            key.pane_id,
            keys=[option_key, "enter"],
            terminal_id=agent.terminal_id or None,
        )

    def _spend_answered_prompt(
        self, key: AgentKey, episode: str | None, *, answered: bool = True
    ) -> None:
        """An answer (card or banner) for block ``episode`` was sent: mark that
        episode answered (when ``answered``) so a banner of it can no longer
        act, and drop the cached prompt so the next answer needs a fresh read —
        but only while the agent is still in that episode. An agent that
        already re-blocked on a new prompt keeps the new episode's prompt and
        banner answerable."""
        with self._lock:
            if episode is not None and answered:
                self._note_episode_answered_locked(episode)
            if episode is None or self._block_episode.get(key) == episode:
                self._preread.pop(key, None)
                self._preread_req.pop(key, None)

    def _card_bridge_protocol(self, server_id: str) -> int:
        connector = getattr(self._runners.get(server_id), "connector", None)
        protocol = getattr(connector, "protocol", 1)
        return protocol if isinstance(protocol, int) else 1

    def card_text(self, server_id: str, pane_id: str, text: str) -> dict | None:
        """Type ``text`` into the agent and submit it (herdeck-ctl ``send``)."""
        key = AgentKey(server_id, pane_id)
        agent, connected = self._card_agent(key)
        if agent is None:
            return None
        body = (text or "").rstrip("\r\n")
        if not body.strip() or len(body) > CARD_TEXT_MAX:
            return outcome("invalid")
        if not connected:
            return outcome("disconnected")
        if agent.backend == "t3":
            if "continue" not in agent.capabilities:
                return outcome("invalid")
            cmd = Command(
                "backend_action",
                server_id,
                pane_id,
                action="continue",
                text=body,
                decision_revision=agent.backend_revision,
            )
        else:
            cmd = Command(
                "send_text", server_id, pane_id, text=body, terminal_id=agent.terminal_id or None
            )
        self._notify_throttle.note_interaction(key)
        return self._card_send(cmd)

    def card_stop(self, server_id: str, pane_id: str) -> dict | None:
        """Interrupt the agent — the deck drill's Stop (unguarded, profile keys).
        The two-step confirmation lives in the card UI (``stop_confirm``)."""
        key = AgentKey(server_id, pane_id)
        agent, connected = self._card_agent(key)
        if agent is None:
            return None
        if not connected:
            return outcome("disconnected")
        if agent.backend == "t3":
            if "stop" not in agent.capabilities:
                return outcome("invalid")
            cmd = Command(
                "backend_action",
                server_id,
                pane_id,
                action="stop",
                decision_revision=agent.backend_revision,
            )
        else:
            cmd = Command(
                "act_force",
                server_id,
                pane_id,
                keys=list(profile_for(self._config, agent.agent_type).stop),
                terminal_id=agent.terminal_id or None,
            )
        self._notify_throttle.note_interaction(key)
        return self._card_send(cmd)

    def card_focus(self, server_id: str, pane_id: str) -> dict | None:
        """Focus the pane in herdr (and bring [local].terminal_app forward)."""
        key = AgentKey(server_id, pane_id)
        agent, connected = self._card_agent(key)
        if agent is None:
            return None
        if not connected:
            return outcome("disconnected")
        if agent.backend == "t3":
            return outcome("invalid")
        return self._card_send(
            Command("focus", server_id, pane_id, terminal_id=agent.terminal_id or None)
        )

    def _card_send(self, cmd: Command) -> dict:
        runner = self._runners.get(cmd.server_id)
        if runner is None:
            return outcome("disconnected")
        req = self._next_req(cmd)
        reply = self._card_replies.register(req, cmd.server_id)
        try:
            runner.send(command_to_msg(cmd, req))
            if not reply.event.wait(CARD_REPLY_TIMEOUT_S):
                return outcome("pending")
        finally:
            self._card_replies.discard(req)
        return reply.result or outcome("sent")


# --- HTTP route helpers (called from DeckApp's handler) ----------------------


def _key_from(body: dict) -> tuple[str, str] | None:
    server_id, pane_id = body.get("server_id"), body.get("pane_id")
    if isinstance(server_id, str) and server_id and isinstance(pane_id, str) and pane_id:
        return server_id, pane_id
    return None


def handle_get(source, path: str, params: dict) -> tuple[int, dict | None]:
    """GET /agent/detail?index=N | ?server_id=&pane_id= [&refresh=1]."""
    if not callable(getattr(source, "card_detail", None)):
        return 404, None
    if path == "/agent/term/poll":
        try:
            session_id = params["id"][0]
            after = int(params.get("after", ["0"])[0])
            wait_ms = int(params.get("wait_ms", ["0"])[0])
        except (KeyError, IndexError, TypeError, ValueError):
            return 400, None
        polled = source.card_term_poll(session_id, after, wait_ms)
        return (200, polled) if polled is not None else (404, None)
    if path != "/agent/detail":
        return 404, None
    if "index" in params:
        try:
            index = int(params["index"][0])
        except (TypeError, ValueError):
            return 400, None
        key = source.card_resolve(index)
        if key is None:
            return 404, None
    else:
        key = _key_from({k: v[0] for k, v in params.items() if v})
        if key is None:
            return 400, None
    refresh = params.get("refresh", ["0"])[0] == "1"
    detail = source.card_detail(*key, refresh=refresh)
    return (200, detail) if detail is not None else (404, None)


def handle_post(source, path: str, body: dict) -> tuple[int, dict | None]:
    """POST /agent/{answer,text,stop,focus} with a JSON body naming the agent."""
    if not callable(getattr(source, "card_detail", None)):
        return 404, None
    if path == "/agent/term/close":
        session_id = body.get("id")
        if not isinstance(session_id, str) or not session_id:
            return 400, None
        return 200, outcome("closed", ok=source.card_term_close(session_id))
    key = _key_from(body)
    if key is None:
        return 400, None
    if path == "/agent/answer":
        choice, revision = body.get("key"), body.get("revision")
        if not isinstance(choice, str) or not choice or not isinstance(revision, str):
            return 400, None
        result = source.card_answer(*key, choice, revision)
    elif path == "/agent/text":
        text = body.get("text")
        if not isinstance(text, str):
            return 400, None
        result = source.card_text(*key, text)
    elif path == "/agent/stop":
        result = source.card_stop(*key)
    elif path == "/agent/focus":
        result = source.card_focus(*key)
    elif path == "/agent/term/open":
        result = source.card_term_open(*key, body.get("cols"), body.get("rows"))
    else:
        return 404, None
    return (200, result) if result is not None else (404, None)
