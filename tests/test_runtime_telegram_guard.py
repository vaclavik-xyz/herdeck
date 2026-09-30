"""Runtime side of bridge-sent Telegram: the ``telegram`` frame, per-server
telegram state, and the duplicate guard — a runtime never sends Telegram for
agents whose bridge advertises capability ``telegram`` (the bridge does).

Fake Telegram transport only: ``notify.make_telegram_sink`` is replaced by a
recorder and the interactive chain by a list; no token ever reaches the API."""

from __future__ import annotations

import json
import time

import pytest

import herdeck.notify as notify
from herdeck.config import (
    DEFAULT_PROFILES,
    Config,
    Notifications,
    ServerConfig,
    TelegramConfig,
)
from herdeck.connector import Connector
from herdeck.deckapp.live import LiveSource
from herdeck.model import AgentKey, AgentState, Status
from herdeck.protocol import TelegramFrame, decode_inbound

TELEGRAM_CAPS = frozenset({"events", "telegram_config", "telegram"})
PLAIN_CAPS = frozenset({"events"})

STATUS = {
    "token": "file",
    "active": True,
    "inbound": "ok",
    "last_error": None,
    "last_sent_at_ms": 5,
    "recent_chats": [{"chat_id": "-1", "message_thread_id": None, "title": "t", "topic_name": ""}],
}


def _frame(**over) -> dict:
    msg = {
        "type": "telegram",
        "server_id": "bridge-label",
        "revision": 3,
        "updated_at_ms": 1234,
        "updated_by": "mac",
        "settings": {"enabled": True, "chat_id": "-1"},
        "status": dict(STATUS),
    }
    msg.update(over)
    return msg


# --- protocol / connector ------------------------------------------------------------


def test_telegram_frame_decodes():
    msg = decode_inbound(json.dumps(_frame()))
    assert msg == TelegramFrame(
        "bridge-label", 3, 1234, "mac", {"enabled": True, "chat_id": "-1"}, STATUS
    )
    unset = decode_inbound(json.dumps(_frame(settings=None, revision=0)))
    assert unset.settings is None and unset.revision == 0


@pytest.mark.parametrize(
    "bad",
    [
        {"server_id": None},
        {"revision": -1},
        {"revision": "3"},
        {"updated_at_ms": True},
        {"updated_by": 7},
        {"settings": [1]},
        {"status": "active"},
    ],
)
def test_malformed_telegram_frame_is_rejected(bad):
    with pytest.raises(ValueError):
        decode_inbound(json.dumps(_frame(**bad)))


def test_connector_routes_telegram_under_config_id():
    seen = []
    conn = Connector(
        ServerConfig(id="cfg", url="ws://x", token="t"),
        on_snapshot=lambda sid, states: None,
        on_event=lambda sid, state: None,
        on_connection=lambda sid, up: None,
        on_telegram=lambda sid, frame: seen.append((sid, frame)),
    )
    conn._dispatch(json.dumps(_frame()))
    assert [sid for sid, _ in seen] == ["cfg"]
    assert seen[0][1].revision == 3
    # without a consumer the frame is dropped quietly
    Connector(
        ServerConfig(id="cfg", url="ws://x", token="t"),
        on_snapshot=lambda sid, states: None,
        on_event=lambda sid, state: None,
        on_connection=lambda sid, up: None,
    )._dispatch(json.dumps(_frame()))


# --- a two-server runtime with local Telegram (one-way + interactive) ----------------


class FakeConnector:
    def __init__(self, caps):
        self.capabilities = frozenset(caps)


class FakeRunner:
    def __init__(self, caps):
        self.sent: list[dict] = []
        self.connector = FakeConnector(caps)

    def send(self, msg):
        self.sent.append(msg)

    def close(self):
        pass


class FakeIdle:
    def idle_seconds(self):
        return None  # unknown = away: nothing is skipped for presence


def two_server_config(remind_after=0) -> Config:
    config = Config(
        servers=[
            ServerConfig("a", "ws://a", "tok-a"),
            ServerConfig("b", "ws://b", "tok-b"),
        ],
        profiles=dict(DEFAULT_PROFILES),
        overview_order=["a", "b"],
        grid=(5, 3),
    )
    # A 0.14.0-style local setup: macOS banners + Telegram, interactive on.
    config.notifications = Notifications(
        enabled=True,
        on=["blocked", "done"],
        backends=["macos", "telegram"],
        telegram=TelegramConfig(
            token_env="TG_TOKEN", chat_id="-1", interactive=True, allowed_user_ids=[42]
        ),
        skip_focused=False,
        remind_after=remind_after,
    )
    return config


def agent(sid, pane="p1", status=Status.WORKING) -> AgentState:
    return AgentState(AgentKey(sid, pane), "claude", f"{sid}-{pane}", status, terminal_id="t1")


class Harness:
    """LiveSource on servers "a" (old bridge, no ``telegram``) and "b"
    (bridge actively delivering Telegram), with recording transports."""

    def __init__(self, monkeypatch, *, remind_after=0):
        monkeypatch.setenv("TG_TOKEN", "fake-token")
        self.banners: list[str] = []
        self.telegram: list[str] = []
        self.interactive: list[AgentKey] = []
        monkeypatch.setattr(
            notify,
            "make_telegram_sink",
            lambda token, chat, thread: (
                lambda title, body, sound, icon=None: self.telegram.append(title)
            ),
        )
        self.now = [1000.0]
        config = two_server_config(remind_after)
        src = LiveSource(
            config,
            notify_schedule=lambda fn: fn(),
            notify_clock=lambda: self.now[0],
            idle_probe=FakeIdle(),
            shell_banners=False,
        )
        # the sink LiveSource builds, with a recording local banner
        src._notifier = notify.Notifier(
            sink=notify.deckapp_sink(
                src._notify_feed,
                lambda: False,
                config,
                telegram_factory=src._telegram_sink,
                macos_sink=lambda title, body, sound, icon=None: self.banners.append(title),
                shell=False,
                away=src._user_away,
                local_gate=lambda: not getattr(src._alert_context, "skip_local", False),
            )
        )
        src.set_telegram_interactive(
            lambda: True,
            lambda agent, *, body, sound, multi_server: self.interactive.append(agent.key),
        )
        self.runners = {"a": FakeRunner(PLAIN_CAPS), "b": FakeRunner(TELEGRAM_CAPS)}
        for sid, runner in self.runners.items():
            src.attach_runner(runner, sid)
            src._on_connection(sid, True)
            src._on_snapshot(sid, [agent(sid, "p1"), agent(sid, "p2")])
        self.src = src

    def event(self, sid, pane, status):
        self.src._on_event(sid, agent(sid, pane, status))

    def clear(self):
        self.banners.clear()
        self.telegram.clear()
        self.interactive.clear()


@pytest.fixture
def harness(monkeypatch):
    made = []

    def build(**kw):
        h = Harness(monkeypatch, **kw)
        made.append(h)
        return h

    yield build
    for h in made:
        h.src.close()


def test_bridge_with_active_telegram_owns_telegram_for_its_agents(harness):
    """Review Focus 3: a 0.14.0-configured runtime (local one-way + interactive
    Telegram) connected to a bridge that advertises ``telegram`` sends no
    Telegram for that bridge's agents — only the bridge does — while the macOS
    banner is still delivered."""
    h = harness()
    h.event("b", "p1", Status.BLOCKED)
    h.event("b", "p2", Status.DONE)
    assert h.banners == ["claude · needs input", "claude · done"]
    assert h.telegram == [] and h.interactive == []


def test_old_bridge_agents_keep_the_runtime_telegram(harness):
    h = harness()
    h.event("a", "p1", Status.BLOCKED)
    h.event("a", "p2", Status.DONE)
    assert h.banners == ["claude · needs input", "claude · done"]
    # blocked -> the interactive chain, done -> the one-way sink (as before)
    assert h.interactive == [AgentKey("a", "p1")]
    assert h.telegram == ["claude · done"]


def test_reminders_skip_telegram_for_a_telegram_bridge(harness):
    h = harness(remind_after=10)
    h.event("a", "p1", Status.BLOCKED)
    h.event("b", "p1", Status.BLOCKED)
    h.clear()
    h.now[0] += 10 * 60
    assert h.src.check_reminders() == 2
    # both reminders reach the Mac; only "a"'s goes to Telegram
    assert h.banners == ["claude · still needs input (10 min)"] * 2
    assert h.interactive == [AgentKey("a", "p1")]
    assert h.telegram == []


def test_capability_is_read_at_decision_time_and_dropping_it_resumes(harness):
    h = harness()
    h.event("b", "p1", Status.BLOCKED)
    assert h.interactive == [] and h.telegram == []
    # the bridge disables its bot: the next snapshot lacks ``telegram``
    h.runners["b"].connector.capabilities = frozenset({"events", "telegram_config"})
    h.src._on_snapshot("b", [agent("b", "p1", Status.BLOCKED), agent("b", "p2")])
    h.event("b", "p2", Status.DONE)
    assert h.telegram == ["claude · done"]
    h.now[0] += 3600  # past the per-agent cooldown
    h.event("b", "p1", Status.WORKING)
    h.event("b", "p1", Status.BLOCKED)
    assert h.interactive == [AgentKey("b", "p1")]
    # ... and enabling it again hands Telegram back to the bridge
    h.clear()
    h.runners["b"].connector.capabilities = TELEGRAM_CAPS
    h.now[0] += 3600
    h.event("b", "p1", Status.WORKING)
    h.event("b", "p1", Status.BLOCKED)
    h.event("b", "p2", Status.WORKING)
    h.event("b", "p2", Status.DONE)
    assert h.interactive == [] and h.telegram == []
    assert h.banners == ["claude · needs input", "claude · done"]


def test_skip_focused_with_a_telegram_bridge_drops_the_whole_alert(harness):
    """skip_focused keeps an alert alive only for its remote backends; with the
    bridge owning Telegram there is nothing left for this runtime to send."""
    h = harness()
    h.src._config.notifications.skip_focused = True
    h.src._user_present = lambda: True
    h.src._on_event("b", AgentState(AgentKey("b", "p1"), "claude", "x", Status.DONE, focused=True))
    h.src._on_event("a", AgentState(AgentKey("a", "p1"), "claude", "x", Status.DONE, focused=True))
    assert h.banners == []
    assert h.telegram == ["claude · done"]  # "a" only


# --- per-server telegram state (for the editor / GET /config) ------------------------


def test_live_source_keeps_per_server_telegram_state(harness):
    h = harness()
    state = h.src.telegram_state()
    assert set(state) == {"a", "b"}
    assert state["a"] == {
        "offered": False,
        "active": False,
        "connected": True,
        "revision": None,
        "updated_at_ms": None,
        "updated_by": None,
        "settings": None,
        "status": None,
    }
    frame = decode_inbound(json.dumps(_frame()))
    h.src._on_telegram("b", frame)
    b = h.src.telegram_state()["b"]
    assert b["offered"] is True and b["active"] is True and b["connected"] is True
    assert b["revision"] == 3 and b["updated_by"] == "mac"
    assert b["settings"] == {"enabled": True, "chat_id": "-1"}
    assert b["status"] == STATUS
    # a copy: callers cannot mutate the stored frame
    b["settings"]["chat_id"] = "x"
    assert h.src.telegram_state()["b"]["settings"]["chat_id"] == "-1"
    # a disconnect forgets it (a fresh frame follows the next snapshot)
    h.src._on_connection("b", False)
    assert h.src.telegram_state()["b"]["revision"] is None


def test_a_bridge_without_telegram_config_drops_a_stale_frame(harness):
    h = harness()
    h.src._on_telegram("b", decode_inbound(json.dumps(_frame())))
    h.runners["b"].connector.capabilities = PLAIN_CAPS  # re-pointed at an old bridge
    h.src._on_snapshot("b", [agent("b", "p1")])
    assert h.src.telegram_state()["b"]["settings"] is None


def test_bridges_own_telegram_needs_every_connected_server(harness):
    h = harness()
    assert h.src.bridges_own_telegram() is False  # "a" is an old bridge
    h.src._on_connection("a", False)
    assert h.src.bridges_own_telegram() is True  # only "b" connected
    h.src._on_connection("b", False)
    assert h.src.bridges_own_telegram() is False  # nobody connected


# --- the interactive poller (getUpdates) ----------------------------------------------


class FakeInteractor:
    def __init__(self, client, control, *, store=None, offset=None, **kwargs):
        self._offset = offset
        self.polls = 0

    @property
    def offset(self):
        return self._offset

    @property
    def inbound_disabled(self):
        return False

    async def notify_blocked(self, agent, *, body, sound, multi_server):
        pass

    async def poll_once(self, *, timeout, is_current):
        import asyncio

        self.polls += 1
        await asyncio.sleep(0.01)


def _wait_for(pred, timeout=3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_interactive_poller_idles_while_every_bridge_sends_telegram(harness):
    """A runtime whose token moved to the bridge must not long-poll the same
    bot (409 "terminated by other getUpdates", stolen callbacks): while every
    connected server advertises ``telegram`` the poller stays idle; a server
    without it (mixed fleet) keeps it polling."""
    from herdeck.deckapp.services import RuntimeServices

    h = harness()
    src = h.src
    src._on_connection("a", False)  # only "b" (telegram bridge) connected
    made = []

    def interactor(*a, **kw):
        made.append(FakeInteractor(*a, **kw))
        return made[-1]

    services = RuntimeServices(
        src.config,
        current_source=lambda: src,
        getenv=lambda name: "fake-token" if name == "TG_TOKEN" else None,
        bot_client_factory=lambda token: object(),
        interactor_factory=interactor,
    )
    try:
        services.wire(src)
        assert services.telegram_active()
        time.sleep(0.3)
        assert made[-1].polls == 0
        # a server without ``telegram`` connects: polling resumes
        src._on_connection("a", True)
        assert _wait_for(lambda: made[-1].polls > 0)
        # it goes away again: idle again
        src._on_connection("a", False)
        time.sleep(0.1)  # let an in-flight poll finish
        settled = made[-1].polls
        time.sleep(0.3)
        assert made[-1].polls == settled
        # the bridge drops the capability: polling resumes
        h.runners["b"].connector.capabilities = frozenset({"events", "telegram_config"})
        assert _wait_for(lambda: made[-1].polls > settled)
    finally:
        services.close()


def test_poller_survives_a_source_that_is_not_ready_yet():
    """The host's ``current_source`` raises until its app exists; the
    ownership check must not kill the poll loop (it keeps polling)."""
    from herdeck.deckapp.services import RuntimeServices

    config = two_server_config()
    made = []

    def interactor(*a, **kw):
        made.append(FakeInteractor(*a, **kw))
        return made[-1]

    def not_ready():
        raise KeyError("app")

    services = RuntimeServices(
        config,
        current_source=not_ready,
        getenv=lambda name: "fake-token" if name == "TG_TOKEN" else None,
        bot_client_factory=lambda token: object(),
        interactor_factory=interactor,
    )
    try:
        assert _wait_for(lambda: made and made[-1].polls > 0)
    finally:
        services.close()
