"""LiveSource — the deckapp StateSource backed by a real bridge.

It reuses the core wholesale: a ``herdeck.connector.Connector`` runs the WebSocket
client (connect, resync-on-reconnect, backoff), and ``Orchestrator`` does the
render + press translation. This module only buffers the connector's callbacks,
re-renders the deck when they fire, and turns a press into ``Command`` wire
messages.

Threading: connector callbacks run on their connector's asyncio loop thread.
They update this source's small buffer under ``self._lock``, then ask the DeckApp
to re-render via the ``refresh`` callback (which takes the DeckApp's lock). The
DeckApp's render/press path runs on HTTP threads, also under the DeckApp's lock.
Locks are always taken DeckApp-then-source, so there is no inversion: the
orchestrator is only ever mutated while the DeckApp lock is held.

One connector is created for every resolved server. Agent identity is already
scoped by ``AgentKey(server_id, pane_id)``, so local Herdr sessions and remote
bridges share one orchestrator without losing command-routing identity.

Secret hygiene: bridge tokens live only inside their ``Connector`` instances
(Authorization headers). The source exposes only non-secret server ids.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable

from .. import notify as _notify
from ..commands import Command, command_to_msg, profile_for
from ..config import Config, ServerConfig
from ..connector import Connector, create_connector
from ..i18n import tr
from ..layout import prompt_excerpt
from ..model import AgentKey, AgentState, Status
from ..notify import (
    NotificationFeed,
    Notifier,
    NotifyThrottle,
    _macos_sink,
    deckapp_sink,
    event_title,
)
from ..notify_events import (
    NOTIFY_EVENT_STATUSES,
    SubagentBursts,
    event_notification_body,
    newly_entered,
)
from ..notify_events import interaction_keys as _interaction_keys
from ..notify_icons import NotificationIconCache
from ..orchestrator import Orchestrator, binary_answer
from ..presence import IdleProbe
from ..project_icons import ingest_project_icon
from ..terminal_app import activate_terminal_app
from ..usage_alerts import usage_alert_message, usage_alert_sound
from .agent_card import AgentCardMixin
from .bridge_update import BridgeUpdateMixin
from .event_cursor import EventCursorStore
from .hooks_relay import HooksMixin
from .live_events import BridgeEventsMixin
from .source import StateSource
from .stats import StatsMixin
from .usage_agent_relay import UsageAgentMixin

log = logging.getLogger(__name__)

# A banner reply is typed into the agent's pane: bounded, and stripped of
# control / bidi-override characters (a terminal escape must never ride in on
# notification text). Line breaks collapse to spaces (below); tabs stay.
REPLY_MAX_CHARS = 2000
_REPLY_STRIP_RE = re.compile("[\x00-\x08\x0e-\x1f\x7f\u202a-\u202e\u2066-\u2069]")
# herdr types the text and then presses enter: a line break inside a reply
# would submit early and type the rest into whatever prompt comes next.
_REPLY_BREAK_RE = re.compile("[\r\n\x0b\x0c\x85\u2028\u2029]+")
# Answered block episodes remembered so a second click (or a reminder banner of
# the same episode) never answers twice.
_ANSWERED_EPISODES_MAX = 256
# How long a blocked alert that needs its prompt ([notifications].banner_actions
# / banner_prompt) waits for the background pre-read before it goes out
# without it. The read normally lands in well under a second.
PROMPT_WAIT_S = 2.0
# [notifications].skip_focused: the herdr-focused pane only counts as "you are
# looking at it" while the deck host saw input within this many seconds. An
# unknown idle time (e.g. Linux) counts as away: dropping an alert needs proof
# that the user is at the host. It only ever silences the local banner, never
# a remote backend (Telegram) — that is for when you are NOT at the host.
FOCUS_PRESENT_S = 120.0
# [notifications].remind_after: at most this many reminders per block episode,
# checked by a small background thread every REMIND_POLL_S seconds.
REMIND_MAX = 3
REMIND_POLL_S = 15.0
# Per-agent semantic generations remembered for the cockpit API (bounded).
SEMANTIC_GENERATION_LIMIT = 4096


def sanitize_reply(text: object) -> str:
    """The banner reply text safe to send to a pane ("" = nothing to send)."""
    if not isinstance(text, str):
        return ""
    clean = _REPLY_BREAK_RE.sub(" ", text)
    return _REPLY_STRIP_RE.sub("", clean).strip()[:REPLY_MAX_CHARS]


def _thread_notify_schedule(fn) -> None:
    """Run a notification off the connector loop thread (osascript latency)."""
    threading.Thread(target=fn, daemon=True, name="herdeck-notify").start()


class LiveSource(
    AgentCardMixin,
    BridgeUpdateMixin,
    StatsMixin,
    HooksMixin,
    UsageAgentMixin,
    BridgeEventsMixin,
    StateSource,
):
    """A StateSource fed by one or more real bridges through ``Connector``.

    The connector callbacks buffer the latest fleet state and re-render the deck;
    ``apply_to`` replays the buffer into the render orchestrator via
    ``apply_snapshot``/``set_connection`` (the same path the mock uses). A press is
    translated by ``Orchestrator.on_press`` into ``Command``s and handed to the
    runner's fire-and-forget ``send`` — non-idempotent sends are never retried (the
    Connector/bridge own that guarantee). A ``read`` result is matched back to its
    request and fed to ``set_detection`` so the blocked-agent approve/deny options
    appear.
    """

    source_name = "live"

    def __init__(
        self,
        config: Config,
        server: ServerConfig | None = None,
        *,
        notify_schedule=None,
        notify_sink_factory=None,
        notification_fallback=None,
        notify_clock=None,
        notify_icons: NotificationIconCache | None = None,
        prompt_wait_s: float = PROMPT_WAIT_S,
        idle_probe: IdleProbe | None = None,
        shell_banners: bool = True,
        event_store: EventCursorStore | None = None,
    ):
        # ``server`` remains accepted for source compatibility with callers that
        # built a one-server source explicitly. The resolved config is authoritative:
        # when it carries a fleet, every selected server participates.
        self._config = config
        # Event notifications (newly_entered bookkeeping per event). The sink
        # records every alert into the feed; the deck shell posts both the
        # banner and sound under one acknowledged delivery. A plain osascript
        # notification is only the fallback while no shell is attached.
        # Interactive Telegram (approve buttons) is opt-in per host: the
        # runtime services install it with set_telegram_interactive, and then
        # blocked alerts go to it instead of the one-way Telegram sink.
        self._notify_keys: dict[str, set] = {event: set() for event in NOTIFY_EVENT_STATUSES}
        self._notify_baselined_servers: set[str] = set()
        # Cooldown per agent+event and "done right after you answered it"
        # suppression (see notify.NotifyThrottle).
        self._notify_clock = notify_clock or time.monotonic
        self._notify_throttle = NotifyThrottle(clock=self._notify_clock)
        # [notifications].subagents_done: per-agent subagent bursts
        # (connector thread only, like _notify_keys).
        self._subagent_bursts = SubagentBursts()
        # Blocked episodes that may get reminders: key -> (episode, since, sent).
        self._reminders: dict[AgentKey, tuple[str, float, int]] = {}
        self._reminder_stop = threading.Event()
        self._reminder_thread: threading.Thread | None = None
        self._notify_schedule = notify_schedule or _thread_notify_schedule
        self._prompt_wait_s = prompt_wait_s
        self._idle_probe = idle_probe or IdleProbe()
        # When the deck (a key, the triage hotkey, a banner drill) was last
        # used; None = not since start. [notifications.telegram].only_when_away.
        self._last_deck_press: float | None = None
        self._notify_feed = NotificationFeed()
        self._notification_fallback = notification_fallback or _macos_sink
        self._notify_gate: Callable[[], bool] = lambda: False
        self._notify_claim_age: Callable[[], float | None] = lambda: None
        self._notify_features: Callable[[], frozenset[str]] = lambda: frozenset()
        # Events each agent was alerted for and has not left yet; leaving one
        # withdraws that agent's delivered shell banners.
        self._bannered: dict[AgentKey, set[str]] = {}
        # Banners carry the agent's project mark (favicon or monogram).
        self._notify_icons = notify_icons or NotificationIconCache()
        # Interactive Telegram hooks (set_telegram_interactive) and the event of
        # the alert this notify thread is delivering (so the one-way Telegram
        # sink can leave blocked alerts to the interactive chain).
        self._tg_interactive: Callable[[], bool] = lambda: False
        self._tg_notify_blocked = None
        self._alert_context = threading.local()
        # Cockpit semantic API bookkeeping: a server is "available" only after
        # a snapshot on its current connection; every agent change bumps that
        # agent's generation (a stop confirmation dies with it).
        self._semantic_ready: set[str] = set()
        self._semantic_generations: OrderedDict[AgentKey, int] = OrderedDict()
        self._semantic_serial = 0
        # Results of requests owned by the runtime services (agent control).
        self._result_tap = None
        if config.notifications.enabled:
            factory = notify_sink_factory or (
                lambda feed, gate: deckapp_sink(
                    feed,
                    gate,
                    self._config,
                    claim_age=lambda: self._notify_claim_age(),
                    away=self._user_away,
                    telegram_factory=self._telegram_sink,
                    shell=shell_banners,
                    local_gate=lambda: not getattr(self._alert_context, "skip_local", False),
                )
            )
            self._notifier = Notifier(sink=factory(self._notify_feed, lambda: self._notify_gate()))
        else:
            self._notifier = None
        self._servers = {item.id: item for item in config.servers}
        if not self._servers and server is not None:
            self._servers = {server.id: server}
        self._lock = threading.Lock()
        # Signalled (under self._lock) when a pre-read lands or a block episode
        # changes, so an alert waiting for its prompt wakes at once.
        self._preread_cv = threading.Condition(self._lock)
        # Block episode per BLOCKED pane: a fresh opaque id each time the pane
        # enters BLOCKED (or its terminal is recycled). Banner answers carry it,
        # so a stale banner can never answer a later prompt.
        self._block_episode: dict[AgentKey, str] = {}
        self._answered_episodes: dict[str, None] = {}
        # Outstanding fire-and-forget banner answers (req -> agent, bounded):
        # a bridge refusal is logged, since nobody else waits for the result.
        self._banner_reqs: dict[str, AgentKey] = {}
        self._agents: dict[AgentKey, AgentState] = {}
        self._connected: dict[str, bool] = {sid: False for sid in self._servers}
        self._req = 0
        self._bg_req = 0
        self._active_read_req: str | None = None
        # Outstanding focus requests (insertion-ordered, bounded): a successful
        # result brings [local].terminal_app forward.
        self._focus_reqs: dict[str, bool] = {}
        # Pre-read cache: the last-read prompt per BLOCKED pane, read in the
        # background so a drill paints its options in one frame (no read round-trip,
        # no empty flash). ``_preread`` holds the cached prompt text; ``_preread_req``
        # holds the request id of the in-flight read for the pane's CURRENT block
        # episode. Both are dropped when the pane leaves BLOCKED, so a result from a
        # prior episode (an old req) can never repopulate the cache after a re-block.
        self._preread: dict[AgentKey, str] = {}
        self._preread_req: dict[AgentKey, str] = {}
        self._orch: Orchestrator | None = None
        self._deck_lock = None
        self._refresh_locked_cb = None
        self._runners: dict[str, object] = {}
        # Bridge usage reports per server (offered, last providers) and the
        # DeckApp's usage hub they go to; buffered so a hub wired after the
        # connectors started (or rebuilt by a config swap) gets a replay.
        self._usage_lock = threading.Lock()
        self._bridge_usage: dict[str, tuple[bool, list | None]] = {}
        self._usage_sink = None
        self._card_init()  # desktop agent card (agent_card.AgentCardMixin)
        self._bridge_update_init()  # bridge self-update (bridge_update.BridgeUpdateMixin)
        self._bridge_events_init(event_store)  # bridge lifecycle events (live_events.py)
        self._stats_init()  # GET /stats relay (stats.StatsMixin)
        self._hooks_init()  # subagent hook install relay (hooks_relay.HooksMixin)
        self._usage_agent_init()  # usage agent install relay (usage_agent_relay.py)

    # --- StateSource surface ---
    @property
    def config(self) -> Config:
        return self._config

    @property
    def language(self) -> str:
        """Language of rendered deck text — /state exposes it so the desktop
        window can switch its own UI language in lockstep."""
        return self._config.view.language

    @property
    def connected(self) -> bool:
        with self._lock:
            return any(self._connected.values())

    @property
    def server_id(self) -> str | None:
        """Backward-compatible primary id for ``/health``."""
        return next(iter(self._servers), None)

    @property
    def server_ids(self) -> list[str]:
        return list(self._servers)

    @property
    def connections(self) -> dict[str, bool]:
        with self._lock:
            return dict(self._connected)

    def attach(self, orch: Orchestrator, *, lock=None, refresh_locked=None) -> None:
        """Receive the render orchestrator, its lock, and a lock-free render.

        The orchestrator drives a press (``on_press``) and a read result
        (``set_detection``). ``lock`` is the DeckApp's lock — every live transition
        (buffer swap + invalidation + render) is run while holding it, so a press
        (which also holds it) can never observe a half-applied update.
        ``refresh_locked`` is the DeckApp's lock-free render, called inside that held
        lock to bump tile versions.
        """
        self._orch = orch
        self._deck_lock = lock
        self._refresh_locked_cb = refresh_locked

    def attach_runner(self, runner, server_id: str | None = None) -> None:
        """Receive a connector runner (provides fire-and-forget ``send``).

        ``server_id`` is optional for the historical one-server test seam.
        """
        sid = server_id or self.server_id
        if sid is not None:
            # Answers leave stamped with the bridge episode (live_events.py).
            self._runners[sid] = self._wrap_runner(sid, runner)

    def apply_to(self, orch: Orchestrator) -> None:
        self._orch = orch
        with self._lock:
            states = list(self._agents.values())
            connected = dict(self._connected)
        for sid in self._servers:
            orch.apply_snapshot(sid, [state for state in states if state.key.server_id == sid])
            orch.set_connection(sid, connected.get(sid, False))

    def press(self, index: int) -> list[Command]:
        orch = self._orch
        if orch is None:
            return []
        return self._drive(orch, lambda: orch.on_press(index))

    def triage(self) -> list[Command]:
        """Open the longest-blocked agent's drill (desktop "next blocked" hotkey)."""
        orch = self._orch
        if orch is None:
            return []
        return self._drive(orch, orch.triage)

    def open_agent(self, key: AgentKey) -> bool:
        """Open ``key``'s drill (a banner click). False when it is unknown."""
        orch = self._orch
        if orch is None or orch.get_agent(key) is None:
            return False
        self._drive(orch, lambda: orch.open_agent(key) or [])
        return True

    def answer_agent(
        self,
        key: AgentKey,
        episode: str,
        *,
        choice: str | None = None,
        sig: str | None = None,
        text: str | None = None,
    ) -> str:
        """Answer a blocked agent from a banner (caller holds the deck lock).

        Exactly one of ``choice`` ("approve"/"deny", with the option signature
        ``sig`` the banner was built from) or ``text`` (an inline reply).
        Returns "ok", "invalid" (malformed request), "unknown" (no such agent),
        "stale" (no longer blocked in ``episode``, already answered — from a
        banner or the agent card — the prompt's options changed, or
        banner_actions / the macos backend was turned off since) or
        "unavailable" (its server is offline). A stale banner therefore never
        answers a later prompt. Approve/Deny go out through the same guarded
        command as the agent card (``_blocked_option_command``).
        """
        n = self._config.notifications
        if not n.banner_actions or "macos" not in n.backends:
            # Answering was turned off after the banner went out: never act
            # on it; the shell opens the drill instead (409).
            return "stale"
        if (choice is None) == (text is None) or not episode:
            return "invalid"
        if choice is not None and choice not in ("approve", "deny"):
            return "invalid"
        clean = sanitize_reply(text) if text is not None else ""
        if text is not None and not clean:
            return "invalid"
        with self._lock:
            state = self._agents.get(key)
            current = self._block_episode.get(key)
            prompt = self._preread.get(key)
            connected = self._connected.get(key.server_id, False)
            answered = episode in self._answered_episodes
        if state is None:
            return "unknown"
        if state.backend != "herdr":
            return "invalid"
        if state.status is not Status.BLOCKED or current != episode or answered:
            return "stale"
        terminal_id = state.terminal_id or None
        if choice is not None:
            answer = binary_answer(
                prompt if isinstance(prompt, str) else "",
                profile_for(self._config, state.agent_type),
                self._config.safety,
            )
            if answer is None or answer.sig != sig:
                return "stale"
            option = answer.approve if choice == "approve" else answer.deny
            cmd = self._blocked_option_command(key, state, option, prompt)
        else:
            cmd = Command(
                "send_text", key.server_id, key.pane_id, text=clean, terminal_id=terminal_id
            )
        runner = self._runners.get(key.server_id)
        if runner is None or not connected:
            return "unavailable"
        self._spend_answered_prompt(key, episode)
        self._notify_throttle.note_interaction(key)
        if self._orch is not None:
            self._orch.note_external_answer(key)
        log.info(
            "banner answer agent=%s:%s kind=%s",
            key.server_id,
            key.pane_id,
            choice or "reply",
        )
        req = self._next_req(cmd)
        with self._lock:
            self._banner_reqs[req] = key
            while len(self._banner_reqs) > 32:
                self._banner_reqs.pop(next(iter(self._banner_reqs)))
        runner.send(command_to_msg(cmd, req))
        return "ok"

    def _note_episode_answered_locked(self, episode: str) -> None:
        """Remember an answered block episode (bounded). Caller holds self._lock."""
        self._answered_episodes[episode] = None
        while len(self._answered_episodes) > _ANSWERED_EPISODES_MAX:
            self._answered_episodes.pop(next(iter(self._answered_episodes)))

    def _drive(self, orch, step) -> list[Command]:
        self._last_deck_press = time.monotonic()
        drilled_before = orch.drill_key()
        cmds = step()
        for key in _interaction_keys(orch, cmds):
            self._notify_throttle.note_interaction(key)
        # If this step just opened a drill into a blocked pane whose prompt we
        # pre-read, seed the detection so the very first render shows the options —
        # no wait for the read round-trip, no empty-drill flash. The drill's own
        # read (in cmds) still fires as a refresh, correcting any in-place change.
        # A triage step moves drill-to-drill, so compare keys, not just "drilling".
        if orch.drill_key() != drilled_before:
            self._seed_detection_from_preread(orch)
        local_commands: list[Command] = []
        for cmd in cmds:
            try:
                msg = command_to_msg(cmd, self._next_req(cmd))
            except ValueError:
                # Local-only commands are handed back to DeckApp. It executes
                # them after releasing the render lock, because a profile switch
                # swaps the source and needs to acquire that same lock.
                if cmd.kind in ("switch_profile", "toggle_pin"):
                    local_commands.append(cmd)
                continue
            runner = self._runners.get(cmd.server_id)
            if runner is not None:
                runner.send(msg)
        return local_commands

    def _seed_detection_from_preread(self, orch) -> None:
        """Paint a freshly-opened blocked drill from the pre-read cache (caller holds
        the deck lock, via DeckApp.press). No-op unless the pane is blocked and a
        prompt string was cached (a pending ``None`` entry does not seed)."""
        key = orch.drill_key()
        if key is None:
            return
        agent = orch.get_agent(key)
        if agent is None or agent.status is not Status.BLOCKED:
            return
        with self._lock:
            cached = self._preread.get(key)
        if isinstance(cached, str) and cached:
            orch.set_detection(cached)

    def summary(self) -> dict:
        from .. import layout

        with self._lock:
            agents = list(self._agents.values())
        counts = layout.summary(agents)
        return {
            "agents": sum(a.lifecycle == "active" for a in agents),
            "blocked": counts.blocked,
            "working": counts.working,
            "idle": counts.idle,
            "done": counts.done,
            "waiting": counts.waiting,
        }

    def server_health(self) -> dict[str, dict]:
        """Per-server connector diagnostics for /health (no tokens: only the
        connector's error text, timings and the bridge's announced version)."""
        with self._lock:
            connected = dict(self._connected)
        out: dict[str, dict] = {}
        for sid in self._servers:
            connector = getattr(self._runners.get(sid), "connector", None)
            health = getattr(connector, "health", None)
            facts = health() if callable(health) else {}
            out[sid] = {**facts, "connected": connected.get(sid, False)}
        return out

    def notification_stats(self) -> dict:
        return self._notify_feed.stats()

    def close(self) -> None:
        self._reminder_stop.set()
        self._card_close()  # stop card terminal previews while runners still send
        for runner in list(self._runners.values()):
            runner.close()
        self._runners.clear()

    # --- runtime services hooks (deckapp.services.RuntimeServices) -----------

    def set_result_tap(self, tap) -> None:
        """``tap(server_id, req, data) -> Command | None`` claims results of
        requests the runtime services issued (agent control: Telegram, the
        cockpit API). A claimed result is not processed as a deck result."""
        self._result_tap = tap

    def set_telegram_interactive(self, active: Callable[[], bool], notify_blocked) -> None:
        """Route blocked alerts to an interactive Telegram chain.

        While ``active()`` is true a blocked alert (after the same skip/stale
        checks as every other alert) calls ``notify_blocked(agent, body=,
        sound=, multi_server=)`` and the one-way Telegram sink stays quiet for
        it; done and usage alerts keep using the one-way sink."""
        self._tg_interactive = active
        self._tg_notify_blocked = notify_blocked

    def _telegram_sink(self, token: str, chat_id: str, message_thread_id: int | None):
        sink = _notify.make_telegram_sink(token, chat_id, message_thread_id)

        def one_way(title: str, body: str, sound, icon: str | None = None) -> None:
            if getattr(self._alert_context, "event", None) == "blocked" and self._tg_interactive():
                return  # the interactive chain owns blocked alerts
            sink(title, body, sound, icon)

        one_way._notify_name = "telegram"
        return one_way

    def _notify_blocked_interactive(self, agent: AgentState, body: str, sound) -> None:
        notify_blocked = self._tg_notify_blocked
        if notify_blocked is None:
            return
        tg = self._config.notifications.telegram
        away_min = getattr(tg, "only_when_away", 0) if tg is not None else 0
        if away_min > 0 and not self._user_away(away_min * 60.0):
            log.info(
                "telegram alert skipped (user at the deck host) agent=%s:%s",
                agent.key.server_id,
                agent.key.pane_id,
            )
            return
        try:
            notify_blocked(
                agent,
                body=body,
                sound=sound,
                multi_server=len(self._config.overview_order) > 1,
            )
        except Exception:
            log.warning("interactive telegram alert failed", exc_info=True)

    # Semantic reads take the deck lock too: a bridge update is applied AND
    # rendered under it, so the API never reports an agent state whose tiles
    # the web front has not received yet.
    def semantic_agents(self) -> list[AgentState]:
        with self._card_deck_lock(), self._lock:
            return list(self._agents.values())

    def semantic_agent(self, key: AgentKey) -> AgentState | None:
        with self._card_deck_lock(), self._lock:
            return self._agents.get(key)

    def semantic_server_available(self, server_id: str) -> bool:
        with self._card_deck_lock(), self._lock:
            return server_id in self._semantic_ready

    def semantic_generation(self, server_id: str, pane_id: str) -> int:
        with self._lock:
            return self._semantic_generations.get(
                AgentKey(server_id, pane_id), self._semantic_serial
            )

    def _bump_semantic_locked(self, keys) -> None:
        """Caller holds self._lock."""
        for key in keys:
            self._semantic_serial += 1
            self._semantic_generations[key] = self._semantic_serial
            self._semantic_generations.move_to_end(key)
            while len(self._semantic_generations) > SEMANTIC_GENERATION_LIMIT:
                self._semantic_generations.popitem(last=False)

    # --- notification plumbing (consumed by the deck shell via /state) -------

    def set_notify_gate(
        self,
        gate: Callable[[], bool],
        claim_age: Callable[[], float | None] | None = None,
        features: Callable[[], frozenset[str]] | None = None,
    ) -> None:
        """Set the "a shell can post banners" predicate.

        True -> the runtime leaves both banner and sound to the shell; False ->
        alerts fall back to a plain osascript banner carrying the sound.
        The DeckApp wires this to its shell-claim heartbeat; ``claim_age``
        (seconds since the last claim, None = never) feeds the fallback reason;
        ``features`` names what the claiming shell understands (e.g.
        "withdraw" — an older shell would post a withdraw item as an empty
        banner, so it only gets one when it says so).
        """
        self._notify_gate = gate
        if claim_age is not None:
            self._notify_claim_age = claim_age
        if features is not None:
            self._notify_features = features

    def notifications_feed_state(self) -> dict:
        """Recent event notifications for the shell to post natively."""
        return self._notify_feed.state()

    def notifications_feed_wait(
        self, generation: str | None, after_seq: int, *, timeout: float
    ) -> dict:
        """Long-poll the acknowledged shell feed without polling latency."""
        return self._notify_feed.wait(generation, after_seq, timeout=timeout)

    def notifications_feed_ack(self, generation: str, seq: int) -> bool:
        """Advance delivery only after the shell posted the native banner."""
        return self._notify_feed.ack(generation, seq)

    def notifications_feed_fallback(self, generation: str, seq: int) -> bool:
        """Use the single runtime fallback after native shell delivery fails."""
        return self._notify_feed.fallback(
            generation, seq, self._notification_fallback
        )

    # --- connector callbacks (run on the connector's loop thread) ---
    def _fire_notify(self, event: str, agent: AgentState, *, at_ms: int | None = None) -> None:
        """Schedule one event alert (never raises, never blocks the loop).
        ``at_ms``: when the bridge saw the transition (reminders count from it)."""
        if self._notifier is None:
            return
        n = self._config.notifications
        if event not in n.on:
            return
        if event == "blocked":
            self._track_reminder(agent.key, at_ms)
        sound = False if not n.sound else n.sounds.get(event, True)
        multi = len(self._config.overview_order) > 1
        if not self._notify_throttle.allow(event, agent.key):
            log.info(
                "notification suppressed (cooldown/recent press) event=%s agent=%s:%s",
                event,
                agent.key.server_id,
                agent.key.pane_id,
            )
            return
        if agent.episode_id:
            # This episode is alerted: a replay of the bridge's events (after
            # a restart, or a failed subscription recovering) must not repeat it.
            self._ev_store.note(agent.key.server_id, episodes=(agent.episode_id,))
        title = event_title(agent.agent_type, event, self._config.view.language)
        body = event_notification_body(agent, multi_server=multi)
        meta = self._alert_meta(event, agent)
        log.info(
            "notification transition event=%s agent=%s:%s observed_at_ms=%s",
            event,
            agent.key.server_id,
            agent.key.pane_id,
            time.time_ns() // 1_000_000,
        )
        self._notify_schedule(lambda: self._deliver_alert(event, agent, title, body, sound, meta))

    # --- reminders ([notifications].remind_after) -----------------------------

    def _track_reminder(self, key: AgentKey, at_ms: int | None = None) -> None:
        """A block episode began: remember when, for its reminders. With the
        bridge's ``at_ms`` they count from bridge time, not from when this
        runtime heard of it (a replay after sleep)."""
        if self._config.notifications.remind_after <= 0:
            return
        since, sent = self._reminder_start(at_ms)
        with self._lock:
            episode = self._block_episode.get(key)
            if episode is None:
                return
            self._reminders[key] = (episode, since, sent)
        self._ensure_reminder_thread()

    def _ensure_reminder_thread(self) -> None:
        if self._reminder_thread is not None or self._reminder_stop.is_set():
            return
        self._reminder_thread = threading.Thread(
            target=self._reminder_loop, name="herdeck-remind", daemon=True
        )
        self._reminder_thread.start()

    def _reminder_loop(self) -> None:
        while not self._reminder_stop.wait(REMIND_POLL_S):
            try:
                self.check_reminders()
            except Exception:
                log.warning("reminder check failed", exc_info=True)

    def check_reminders(self) -> int:
        """Alert again for every agent still blocked in the same episode
        ``remind_after`` minutes (x1, x2, x3) after it began. Returns how many
        reminders were scheduled."""
        n = self._config.notifications
        if self._notifier is None or n.remind_after <= 0:
            return 0
        interval = n.remind_after * 60.0
        now = self._notify_clock()
        due: list[tuple[AgentState, str, float]] = []
        with self._lock:
            for key, (episode, since, sent) in list(self._reminders.items()):
                state = self._agents.get(key)
                if (
                    state is None
                    or state.status is not Status.BLOCKED
                    or self._block_episode.get(key) != episode
                    or sent >= REMIND_MAX
                ):
                    del self._reminders[key]
                    continue
                if now - since >= interval * (sent + 1):
                    self._reminders[key] = (episode, since, sent + 1)
                    due.append((state, episode, now - since))
        lang = self._config.view.language
        sound = False if not n.sound else n.sounds.get("blocked", True)
        multi = len(self._config.overview_order) > 1
        for state, episode, elapsed in due:
            title = tr(
                lang,
                "notify.title_reminder",
                agent=state.agent_type or "agent",
                minutes=int(elapsed // 60),
            )
            body = event_notification_body(state, multi_server=multi)
            meta = {
                "agent": {"server_id": state.key.server_id, "pane_id": state.key.pane_id},
                "event": "blocked",
                "episode": episode,
            }
            log.info(
                "notification reminder agent=%s:%s minutes=%d",
                state.key.server_id,
                state.key.pane_id,
                int(elapsed // 60),
            )
            self._notify_schedule(
                lambda s=state, t=title, b=body, m=meta: self._deliver_alert(
                    "blocked", s, t, b, sound, m
                )
            )
        return len(due)

    def _deliver_alert(
        self, event: str, agent: AgentState, title: str, body: str, sound, meta: dict
    ) -> None:
        """Notify thread: enrich a blocked alert with its prompt when asked to
        ([notifications].banner_actions / banner_prompt), then send it."""
        n = self._config.notifications
        plain_body = body
        skip_local = bool(n.skip_focused and agent.focused and self._user_present())
        if skip_local:
            log.info(
                "local banner skipped (herdr-focused pane) event=%s agent=%s:%s",
                event,
                agent.key.server_id,
                agent.key.pane_id,
            )
            remote = "telegram" in n.backends or (
                event == "blocked" and self._tg_interactive()
            )
            if not remote:
                return
        if event == "blocked" and (n.banner_actions or n.banner_prompt):
            prompt = self._await_prompt(agent.key, meta.get("episode"))
            if prompt is None:
                log.info(
                    "notification dropped (agent left the block episode) agent=%s:%s",
                    agent.key.server_id,
                    agent.key.pane_id,
                )
                return
            body = self._enrich_blocked(agent, body, prompt, meta)
        with self._lock:
            if not self._alert_current(event, agent.key, meta.get("episode")):
                log.info(
                    "notification dropped (agent already left %s) agent=%s:%s",
                    event,
                    agent.key.server_id,
                    agent.key.pane_id,
                )
                return
            if event in NOTIFY_EVENT_STATUSES:  # only these are ever withdrawn
                self._bannered.setdefault(agent.key, set()).add(event)
        self._alert_context.event = event
        self._alert_context.skip_local = skip_local
        try:
            self._notifier.notify(title, body, sound, icon=self._banner_icon(agent), meta=meta)
        finally:
            self._alert_context.event = None
            self._alert_context.skip_local = False
        if event == "blocked" and self._tg_interactive():
            self._notify_blocked_interactive(agent, plain_body, sound)

    def _alert_current(self, event: str, key: AgentKey, episode: str | None) -> bool:
        """Is ``key`` still in ``event``'s status (and block episode)? Caller
        holds self._lock. A stale alert must not outlive its withdraw. An
        event with no status of its own (subagents_done) only needs the agent."""
        state = self._agents.get(key)
        if event not in NOTIFY_EVENT_STATUSES:
            return state is not None
        if state is None or state.status is not NOTIFY_EVENT_STATUSES[event]:
            return False
        if event == "blocked" and self._episode_spent(key.server_id, episode):
            return False  # answered (here or on another client) while it waited
        return event != "blocked" or episode is None or self._block_episode.get(key) == episode

    def _withdraw_left(self, event: str, left: set) -> None:
        """Agents that left ``event``'s status (answered anywhere, back to
        work, gone): ask the shell to remove their delivered banners."""
        if not left:
            return
        with self._lock:
            keys = [key for key in left if event in self._bannered.get(key, ())]
            for key in keys:
                events = self._bannered[key]
                events.discard(event)
                if not events:
                    del self._bannered[key]
        if not keys or not self._notify_gate() or "withdraw" not in self._notify_features():
            return
        for key in keys:
            self._notify_feed.withdraw({"server_id": key.server_id, "pane_id": key.pane_id})

    def _user_away(self, seconds: float) -> bool:
        """Idle on the deck host (HIDIdleTime) AND no deck press for ``seconds``
        (notify thread). An unknown idle time (Linux) leaves only the deck
        press to decide."""
        pressed = self._last_deck_press
        if pressed is not None and time.monotonic() - pressed < seconds:
            return False
        idle = self._idle_probe.idle_seconds()
        return idle is None or idle >= seconds

    def _user_present(self) -> bool:
        """The user touched this host recently (notify thread: may run ioreg).
        An unknown idle time is not proof of presence."""
        idle = self._idle_probe.idle_seconds()
        return idle is not None and idle < FOCUS_PRESENT_S

    def _await_prompt(self, key: AgentKey, episode: str | None) -> str | None:
        """The pre-read prompt of ``key``'s block ``episode`` ("" if it did not
        arrive in time), or None when the agent already left that episode — the
        alert is stale then and must not be posted."""
        with self._preread_cv:
            self._preread_cv.wait_for(
                lambda: self._block_episode.get(key) != episode
                or isinstance(self._preread.get(key), str),
                timeout=self._prompt_wait_s,
            )
            if episode is None or self._block_episode.get(key) != episode:
                return None
            prompt = self._preread.get(key)
        return prompt if isinstance(prompt, str) else ""

    def _enrich_blocked(self, agent: AgentState, body: str, prompt: str, meta: dict) -> str:
        """Add answer buttons / a reply field (banner_actions) to ``meta`` and a
        prompt excerpt (banner_prompt) to ``body``."""
        n = self._config.notifications
        lang = self._config.view.language
        # Answers go out as herdr keystrokes / text; other backends (T3) have
        # their own decision API and keep plain banners.
        if n.banner_actions and "macos" in n.backends and agent.backend == "herdr":
            answer = binary_answer(
                prompt, profile_for(self._config, agent.agent_type), self._config.safety
            )
            if answer is not None:
                meta["actions"] = [
                    {"id": "approve", "label": tr(lang, "act.approve")},
                    {"id": "deny", "label": tr(lang, "act.deny")},
                ]
                meta["sig"] = answer.sig
            else:
                meta["reply"] = tr(lang, "notify.reply_placeholder")
        excerpt = prompt_excerpt(prompt) if n.banner_prompt else ""
        return f"{body}\n{excerpt}" if excerpt else body

    def _alert_meta(self, event: str, agent: AgentState) -> dict:
        """Feed fields naming the agent (and, for blocked, its episode) so a
        banner click can open that agent's drill."""
        meta: dict = {
            "agent": {"server_id": agent.key.server_id, "pane_id": agent.key.pane_id},
            "event": event,
        }
        if event == "blocked":
            with self._lock:
                episode = self._block_episode.get(agent.key)
            if episode is not None:
                meta["episode"] = episode
        return meta

    def notify_usage(self, alerts) -> None:
        """Send usage-limit alerts (usage_alerts.UsageAlert, from the DeckApp's
        poller thread) through the same notifier as the agent alerts. Gated by
        [notifications].enabled only (the `on` list names agent events); the
        sound is the "done" sound (usage news is informational)."""
        if self._notifier is None or not alerts:
            return
        lang = self._config.view.language
        sound = usage_alert_sound(self._config.notifications)
        for alert in alerts:
            title, body = usage_alert_message(alert, lang)
            log.info(
                "usage notification kind=%s provider=%s window=%s percent=%s",
                alert.kind,
                alert.provider,
                alert.window,
                alert.percent,
            )
            self._notify_schedule(
                lambda title=title, body=body: self._notifier.notify(title, body, sound)
            )

    def _banner_icon(self, agent: AgentState) -> str | None:
        """PNG path of the agent's project mark for the macOS banner; runs on
        the notify thread (it may render and write the file)."""
        n = self._config.notifications
        if "macos" not in n.backends:
            return None
        return self._notify_icons.path_for(agent, self._config.view.project_icons)

    def _notify_entered(self, event: str, states: list[AgentState], scope: set) -> list[AgentState]:
        """Advance `event`'s episode bookkeeping within `scope`: withdraw the
        banners of keys that left the status and return the states that just
        entered it (to alert). The caller fires alerts only after EVERY event
        withdrew — a done->blocked agent's new blocked banner must never be
        removed by the withdraw of its old done banner."""
        if self._notifier is None or event not in self._config.notifications.on:
            return []
        tracked = self._notify_keys[event]
        to, entered_here = newly_entered(NOTIFY_EVENT_STATUSES[event], tracked & scope, states)
        self._notify_keys[event] = (tracked - scope) | entered_here
        self._withdraw_left(event, (tracked & scope) - entered_here)
        return [x for x in states if x.key in to]

    # --- [notifications].subagents_done ------------------------------------

    def _subagents_notify(self, states: list[AgentState], gone=()) -> None:
        """Advance the subagent bursts of ``states`` and alert the ones whose
        last subagent finished while the agent is not working (once per burst).
        Bridge lifecycle events carry no subagent news, so this always runs
        on the runtime's own view."""
        if self._notifier is None or not self._config.notifications.subagents_done:
            return
        self._subagent_bursts.forget(gone)
        for state in states:
            count = self._subagent_bursts.observe(state)
            if count is not None:
                self._fire_subagents_done(state, count)

    def _fire_subagents_done(self, agent: AgentState, count: int) -> None:
        n = self._config.notifications
        if not self._notify_throttle.allow("subagents_done", agent.key):
            log.info(
                "notification suppressed (cooldown) event=subagents_done agent=%s:%s",
                agent.key.server_id,
                agent.key.pane_id,
            )
            return
        title = tr(
            self._config.view.language,
            "notify.title_subagents_done",
            agent=agent.agent_type or "agent",
            count=count,
        )
        body = event_notification_body(agent, multi_server=len(self._config.overview_order) > 1)
        sound = False if not n.sound else n.sounds.get("done", True)
        meta = {
            "agent": {"server_id": agent.key.server_id, "pane_id": agent.key.pane_id},
            "event": "subagents_done",
        }
        log.info(
            "notification subagents done agent=%s:%s count=%d",
            agent.key.server_id,
            agent.key.pane_id,
            count,
        )
        self._notify_schedule(
            lambda: self._deliver_alert("subagents_done", agent, title, body, sound, meta)
        )

    def _on_snapshot(self, server_id: str, states: list[AgentState]) -> None:
        self._bridge_update_on_snapshot(server_id)
        self._hooks_on_snapshot(server_id)
        self._usage_agent_on_snapshot(server_id)
        new_by_key = {s.key: s for s in states}
        prev_keys = {key for key in self._agents if key.server_id == server_id}

        def mutate():
            with self._lock:
                recycled = {
                    key
                    for key, state in new_by_key.items()
                    if self._terminal_identity_changed(self._agents.get(key), state)
                }
                previous = {
                    key: state
                    for key, state in self._agents.items()
                    if key.server_id == server_id
                }
                self._bump_semantic_locked(
                    key
                    for key in previous.keys() | new_by_key.keys()
                    if previous.get(key) != new_by_key.get(key)
                )
                self._semantic_ready.add(server_id)
                self._agents = {
                    key: state
                    for key, state in self._agents.items()
                    if key.server_id != server_id
                }
                self._agents.update(new_by_key)
                for key in recycled:
                    self._preread.pop(key, None)
                    self._preread_req.pop(key, None)
                    self._block_episode.pop(key, None)
                    self._notify_throttle.forget(key)
                self._subagent_bursts.forget(recycled)
            if self._drilled_key() in recycled:
                self._active_read_req = None
                if self._orch is not None:
                    self._orch.set_detection("")
            # Drop the drilled prompt only if the pane left BLOCKED
            # the prompt + in-flight read stay valid while it
            # is still blocked.
            self._invalidate_if_drill_unblocked(server_id, new_by_key.get(self._drilled_key()))
            self._reconcile_prereads()
            return True

        self._apply(mutate)
        self._subagents_notify(states, gone=prev_keys - new_by_key.keys())
        if server_id not in self._notify_baselined_servers or self._bridge_events(server_id):
            # A process/source restart observes current truth, not lifecycle
            # transitions. Seed the episode sets without replaying stale alerts.
            # A bridge with lifecycle events drives the alerts itself
            # (live_events.py): the sets only follow along, silently, so a
            # later fallback to an older bridge starts from the truth.
            self._notify_seed(states, set(prev_keys) | {s.key for s in states})
            self._notify_baselined_servers.add(server_id)
            return
        # Notifications reconcile AFTER the buffer update: `scope` is every key
        # this snapshot is authoritative for (previous + current), matching
        # a server-scoped reconciliation.
        self._notify_all(server_id, states, prev_keys)

    def _notify_seed(self, states: list[AgentState], scope: set) -> None:
        if self._notifier is None:
            return
        for event, status in NOTIFY_EVENT_STATUSES.items():
            tracked = self._notify_keys[event]
            entered_here = {s.key for s in states if s.status is status}
            self._notify_keys[event] = (tracked - scope) | entered_here

    def _notify_all(self, server_id: str, states: list[AgentState], prev_keys: set) -> None:
        self._notify_all_events(states, set(prev_keys) | {s.key for s in states})

    def _notify_all_events(self, states: list[AgentState], scope: set) -> None:
        entered = [
            (event, state)
            for event in NOTIFY_EVENT_STATUSES
            for state in self._notify_entered(event, states, scope)
        ]
        for event, state in entered:
            self._fire_notify(event, state)

    def _on_event(self, server_id: str, state: AgentState) -> None:
        def mutate():
            with self._lock:
                recycled = self._terminal_identity_changed(
                    self._agents.get(state.key), state
                )
                if self._agents.get(state.key) != state:
                    self._bump_semantic_locked((state.key,))
                self._agents[state.key] = state
                if recycled:
                    self._preread.pop(state.key, None)
                    self._preread_req.pop(state.key, None)
                    self._block_episode.pop(state.key, None)
                    self._notify_throttle.forget(state.key)
                    self._subagent_bursts.forget((state.key,))
            # Same rule for a single-pane event: only a real unblock clears the
            # drilled prompt.
            drilled = self._drilled_key()
            if drilled is not None and drilled == state.key:
                if recycled:
                    self._active_read_req = None
                    if self._orch is not None:
                        self._orch.set_detection("")
                else:
                    self._invalidate_if_drill_unblocked(server_id, state)
            self._reconcile_prereads()
            return True

        self._apply(mutate)
        self._subagents_notify([state])
        if self._bridge_events(server_id):
            self._notify_seed([state], {state.key})
            return
        self._notify_all_events([state], {state.key})

    @staticmethod
    def _terminal_identity_changed(
        previous: AgentState | None,
        current: AgentState,
    ) -> bool:
        return bool(
            previous is not None
            and previous.terminal_id
            and current.terminal_id
            and previous.terminal_id != current.terminal_id
        )

    def _on_connection(self, server_id: str, up: bool) -> None:
        def mutate():
            with self._lock:
                self._connected[server_id] = up
                # Available again only after the fresh snapshot that follows
                # a (re)connect; every agent of the server changes generation.
                self._semantic_ready.discard(server_id)
                self._bump_semantic_locked(
                    key for key in self._agents if key.server_id == server_id
                )
                if not up:
                    # In-flight background reads died with the connection
                    # (Connector.send is at-most-once), so their req markers
                    # must go too — otherwise _reconcile_prereads keeps
                    # skipping the still-blocked panes after a reconnect and
                    # instant drill stays dark until each pane re-blocks. The
                    # cached prompt TEXT stays: it is a best-effort hint until
                    # the fresh episode read lands.
                    for key in [
                        key for key in self._preread_req if key.server_id == server_id
                    ]:
                        self._preread_req.pop(key, None)
            # No reconnect-time reads: the connector's resync `list` snapshot
            # always follows and _on_snapshot reconciles against the FRESH
            # fleet — issuing reads from the stale pre-disconnect agents here
            # would race panes that unblocked or vanished while offline.
            return True

        self._apply(mutate)
        self._events_on_connection(server_id, up)
        self._card_on_connection(server_id, up)
        self._bridge_update_on_connection(server_id, up)
        self._stats_on_connection(server_id, up)
        self._hooks_on_connection(server_id, up)
        self._usage_agent_on_connection(server_id, up)

    def _on_result(self, *args) -> None:
        """Handle a connector result.

        Accept both the new ``(server_id, req, data)`` form and the historical
        one-server ``(req, data)`` test seam.
        """
        if len(args) == 3:
            server_id, req, data = args
        elif len(args) == 2:
            req, data = args
            server_id = self.server_id
        else:
            raise TypeError("_on_result expects (server_id, req, data) or (req, data)")
        if server_id is None:
            return
        if self._bridge_update_on_result(req, data):
            return  # a bridge self-update reply (bridge_update.py), not a deck command
        if self._stats_on_result(req, data):
            return  # a GET /stats reply (stats.py), not a deck command
        if self._hooks_on_result(req, data):
            return  # a hooks reply (hooks_relay.py), not a deck command
        if self._usage_agent_on_result(req, data):
            return  # a usage_agent reply (usage_agent_relay.py), not a deck command
        tap = self._result_tap
        if tap is not None and req is not None:
            claimed = tap(server_id, req, data)
            if claimed is not None:
                if claimed.kind != "read":
                    # an act/send ack: resync so a skipped guarded action
                    # cannot linger as stale state (same as a deck result)
                    runner = self._runners.get(server_id)
                    if runner is not None:
                        runner.send(command_to_msg(Command("list", server_id), None))
                return
        # A desktop agent card may be waiting on this reply (never consumes it).
        self._card_on_result(req, data)
        with self._lock:
            banner_key = self._banner_reqs.pop(req, None) if req is not None else None
        if banner_key is not None and isinstance(data, dict) and (
            data.get("skipped") or data.get("error")
        ):
            log.warning(
                "banner answer refused by the bridge agent=%s:%s reason=%s",
                banner_key.server_id,
                banner_key.pane_id,
                data.get("message") or data.get("error") or "skipped",
            )
        with self._lock:
            focused = req is not None and self._focus_reqs.pop(req, False)
        if focused and data.get("focused"):
            # herdr switched to the pane; bring its terminal window forward too
            # (opt-in [local].terminal_app, off the connector loop).
            activate_terminal_app(self._config.hardware.terminal_app)
        text = data.get("text")
        if text is None:
            # An act/send/start ack: resync this server with a fresh list so a
            # skipped guarded action (pane no longer blocked) can't linger as stale.
            runner = self._runners.get(server_id)
            if runner is not None:
                runner.send(command_to_msg(Command("list", server_id), None))
            return
        # A read result: cache it for an instant future drill (while the pane is
        # blocked), and surface the prompt now only if it still matches a read we
        # issued and the pane is still drilled (re-checked under the deck lock so a
        # concurrent invalidation wins).
        pane_id = data.get("pane_id")

        def mutate():
            self._cache_preread(server_id, pane_id, text, req)
            orch = self._orch
            if orch is None or req is None or not orch.is_drill_pane(server_id, pane_id):
                return False
            drilled = orch.drill_key()
            agent = orch.get_agent(drilled)
            # For a BLOCKED drill the detection becomes actionable (parse_options ->
            # approve/deny), so accept only the current-episode read (_preread_req,
            # registered while blocked and dropped on unblock): a pre-block or prior-
            # episode capture must never feed the blocked options. A non-blocked drill
            # only shows the read as detail text, so the plain active-read match holds.
            with self._lock:
                if agent is not None and agent.status is Status.BLOCKED:
                    accepted = req == self._preread_req.get(drilled)
                else:
                    accepted = req == self._active_read_req
            if accepted:
                orch.set_detection(text)
                return True
            return False

        self._apply(mutate)

    def set_usage_sink(self, sink) -> None:
        """``sink(server_id, offered, providers)`` receives every bridge usage
        report (usage_hub.UsageHub.bridge_update); the current state of each
        server is replayed at once. None detaches (an outgoing source)."""
        # Sink calls happen under _usage_lock so a replay can never overtake
        # a newer live report (lock order: _usage_lock -> the deck lock).
        with self._usage_lock:
            self._usage_sink = sink
            if sink is None:
                return
            for server_id, (offered, providers) in self._bridge_usage.items():
                sink(server_id, offered, providers)

    def _on_usage(self, server_id: str, offered: bool, providers: list | None) -> None:
        """Connector callback (runner thread): a snapshot's ``usage``
        capability, a usage frame, or a disconnect (offered=False)."""
        with self._usage_lock:
            _prev_offered, prev = self._bridge_usage.get(server_id, (False, None))
            if not offered:
                prev = None
            self._bridge_usage[server_id] = (
                offered,
                providers if providers is not None else prev,
            )
            sink = self._usage_sink
            if sink is not None:
                sink(server_id, offered, providers)

    def _on_project_icon(self, server_id: str, icon) -> None:
        """Connector callback (runner thread): store the favicon; when it is new,
        re-render under the deck lock so waiting tiles pick it up."""
        if ingest_project_icon(icon):
            self._apply(lambda: True)

    def _apply(self, mutate) -> None:
        """Run a state transition (and render it) atomically w.r.t. presses.

        ``mutate`` runs while the DeckApp lock is held — the same lock ``press``
        takes — so a press never sees a half-applied bridge update. It returns True
        when a re-render is warranted; the render also happens under that held lock
        (via the DeckApp's lock-free ``_refresh_locked``) so /state bumps tile
        versions for changed cells. Before the DeckApp attaches, just run the
        mutation (no orchestrator/render yet).
        """
        lock = self._deck_lock
        if lock is None:
            mutate()
            return
        with lock:
            changed = mutate()
            if changed and self._refresh_locked_cb is not None:
                self._refresh_locked_cb()

    # --- pre-read cache (callers hold the deck lock) ---
    def _reconcile_prereads(self) -> None:
        """Keep the pre-read cache in step with the fleet: drop entries for panes no
        longer blocked (their prompt is stale), and issue one background read for each
        blocked pane that has no current-episode read yet.

        This includes the drilled pane: pressing a BLOCKED pane registers its own read
        (so no second read is issued there), but a pane drilled while WORKING that then
        blocks has only a rejected pre-block read — it needs a fresh episode read here
        or its blocked drill would stay blank until the user backs out and re-drills.

        Runs inside a mutate() (deck lock held); ``self._lock`` guards the cache +
        buffer. Sends fire after releasing ``self._lock`` — ``runner.send`` is
        fire-and-forget and never blocks."""
        orch = self._orch
        drilled = self._drilled_key()
        reads: list[tuple[str, AgentKey]] = []
        clear_detection = False
        with self._lock:
            blocked = {k for k, s in self._agents.items() if s.status is Status.BLOCKED}
            episodes_changed = False
            for key in [k for k in self._block_episode if k not in blocked]:
                del self._block_episode[key]
                episodes_changed = True
            for key in blocked:
                # The bridge's episode id when it has one (capability events),
                # so banners, answers and lifecycle events name the same one.
                wanted = self._agents[key].episode_id
                have = self._block_episode.get(key)
                if have is None or (wanted and have != wanted):
                    self._block_episode[key] = wanted or uuid.uuid4().hex[:16]
                    episodes_changed = True
            if episodes_changed:
                self._preread_cv.notify_all()
            for key in set(self._preread) | set(self._preread_req):
                if key not in blocked:  # left BLOCKED -> prompt + pending read are stale
                    self._preread.pop(key, None)
                    self._preread_req.pop(key, None)
            for key in blocked:
                if key in self._preread_req:
                    continue  # a current-episode read is already out (pre-read or drill read)
                if self._bridge_events(key.server_id) and key not in self._ev_prompt_missing:
                    # The bridge pre-reads the prompt and sends it with the
                    # blocked event; a drilled pane shows nothing stale meanwhile.
                    if key == drilled and not self._preread.get(key):
                        clear_detection = True
                    continue
                self._bg_req += 1
                bg_req = f"p{self._bg_req}"
                self._preread_req[key] = bg_req  # register so the poll won't re-issue
                reads.append((bg_req, key))
                if key == drilled:
                    # The drilled pane just entered a block episode with no valid read:
                    # any current detection is a pre-block capture. Drop it so the
                    # blocked drill shows no options until the fresh read lands.
                    clear_detection = True
        if clear_detection and orch is not None:
            orch.set_detection("")
        for bg_req, key in reads:
            runner = self._runners.get(key.server_id)
            if runner is None:
                continue
            with self._lock:
                agent = self._agents.get(key)
            runner.send(
                command_to_msg(
                    Command(
                        "read",
                        key.server_id,
                        key.pane_id,
                        source="detection",
                        terminal_id=(agent.terminal_id or None) if agent else None,
                    ),
                    bg_req,
                )
            )

    def _cache_preread(
        self,
        server_id: str,
        pane_id: str | None,
        text: str,
        req: str | None,
    ) -> None:
        """Store a read result as the pane's cached prompt — only while the pane is
        still BLOCKED and only if ``req`` is the read we last issued for the pane's
        CURRENT block episode (``_preread_req[key]``, set by BOTH the background
        pre-read and the drill read, and dropped the moment the pane leaves BLOCKED).
        The drill read thus keeps the cache fresh when a prompt changes in place,
        while a late read from a prior episode carries a since-replaced req and is
        rejected. Caller holds the deck lock."""
        if pane_id is None or req is None:
            return
        key = AgentKey(server_id, pane_id)
        with self._preread_cv:
            state = self._agents.get(key)
            if (
                state is not None
                and state.status is Status.BLOCKED
                and req == self._preread_req.get(key)
            ):
                self._preread[key] = text
                self._preread_cv.notify_all()

    # --- read invalidation (callers hold the deck lock) ---
    def _drilled_key(self) -> AgentKey | None:
        orch = self._orch
        return orch.drill_key() if orch is not None else None

    def _invalidate_if_drill_unblocked(self, server_id: str, new_state) -> None:
        """Drop the drilled prompt only when the agent actually leaves BLOCKED.

        The prompt (and an in-flight read) stay valid as long as the agent stays
        blocked. Wiping on every cosmetic change instead — e.g. a ``branch`` label
        that flaps in the bridge snapshot because ``worktree.list`` was momentarily
        unavailable — rejected the in-flight read (prompt never showed; "click 3×")
        or cleared an already-shown prompt ("shows then disappears").

        ``new_state`` is the drilled pane's state in the update that just arrived
        (``None`` if it dropped out of the fleet); the caller has already confirmed
        the update is authoritative for the drilled server / pane.
        """
        orch = self._orch
        drill = orch.drill_key() if orch is not None else None
        if drill is None or drill.server_id != server_id:
            return
        if new_state is not None and new_state.status is Status.BLOCKED:
            return  # still blocked -> same prompt, keep the options live
        orch.set_detection("")
        with self._lock:
            self._active_read_req = None

    def _next_req(self, cmd) -> str | None:
        # `list` carries no req; everything else gets a
        # fresh sequential id. A `read` id is remembered so its result can be matched
        # (``_active_read_req`` for the drill display; ``_preread_req`` per pane so the
        # drill read also refreshes the pre-read cache under the same episode scope).
        if cmd.kind == "list":
            return None
        with self._lock:
            self._req += 1
            req = f"r{self._req}"
            if cmd.kind == "focus":
                self._focus_reqs[req] = True
                # Results normally arrive; bound the map if a connector drops them.
                while len(self._focus_reqs) > 32:
                    self._focus_reqs.pop(next(iter(self._focus_reqs)))
            if cmd.kind == "read":
                self._active_read_req = req
                # Register the drill read as the episode's read ONLY when the pane is
                # already BLOCKED. A read issued while the pane is WORKING/IDLE belongs
                # to the pre-block state; letting its marker survive into a later block
                # episode would both suppress the fresh pre-read and let a pre-block
                # capture be accepted as the block prompt.
                if cmd.pane_id is not None:
                    key = AgentKey(cmd.server_id, cmd.pane_id)
                    state = self._agents.get(key)
                    if state is not None and state.status is Status.BLOCKED:
                        self._preread_req[key] = req
        return req


class ConnectorRunner:
    """Owns the Connector's asyncio loop on a daemon thread and exposes a
    thread-safe, fire-and-forget ``send``. Reconnect/backoff lives in the
    Connector — this only schedules sends and shuts the loop down on close.
    """

    def __init__(self, connector: Connector):
        self._conn = connector
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, name="herdeck-live", daemon=True)

    @property
    def connector(self) -> Connector:
        return self._conn

    def start(self) -> None:
        self._thread.start()

    def _serve(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._conn.run())
        except Exception:
            pass  # the connector swallows network errors; guard the loop regardless
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    def send(self, msg: dict) -> None:
        loop = self._loop
        if loop.is_closed():
            return
        try:
            # One scheduling attempt, no retry — matches the bridge's at-most-once
            # delivery for non-idempotent sends.
            asyncio.run_coroutine_threadsafe(self._conn.send(msg), loop)
        except RuntimeError:
            pass  # loop not running / shutting down

    def close(self) -> None:
        self._conn.stop()
        if self._thread.is_alive():
            self._thread.join(timeout=2)


def build_live_source(
    config: Config,
    server: ServerConfig | None = None,
    *,
    connector_factory=create_connector,
    runner_factory=ConnectorRunner,
    shell_banners: bool = True,
) -> LiveSource:
    """Wire a LiveSource to one Connector + runner per selected server.

    ``connector_factory``/``runner_factory`` are injectable so tests can drive the
    callbacks and capture sends without a real bridge.
    """
    source = LiveSource(config, server, shell_banners=shell_banners)
    servers = list(config.servers) or ([server] if server is not None else [])
    for selected in servers:
        connector = connector_factory(
            selected,
            on_snapshot=source._on_snapshot,
            on_event=source._on_event,
            on_connection=source._on_connection,
            on_result=lambda req, data, sid=selected.id: source._on_result(sid, req, data),
            on_project_icon=source._on_project_icon,
            on_term=source._on_term,
            on_request_error=lambda req, message, sid=selected.id: source._on_bridge_error(
                sid, req, message
            ),
            on_progress=lambda req, stage, message, sid=selected.id: source._on_progress(
                sid, req, stage, message
            ),
            on_usage=source._on_usage,
            on_lifecycle=source._on_lifecycle,
            events_cursor=source._events_cursor,
        )
        runner = runner_factory(connector)
        source.attach_runner(runner, selected.id)
        runner.start()
    return source
