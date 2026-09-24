"""Unit tests for the one-runtime pieces that replaced the legacy App:
LiveSource's runtime-services hooks, RuntimeServices (agent control, cockpit
API, interactive Telegram), the browser terminal pump, DriverSink, DeckApp's
held status panel / host ticker, and the host's D200 lock + reload handling.
The end-to-end behaviour is pinned by tests/test_contract_*.py."""

from __future__ import annotations

import io
import queue
import time

import pytest
from PIL import Image

from herdeck.config import (
    DEFAULT_PROFILES,
    Config,
    Notifications,
    ServerConfig,
    TelegramConfig,
)
from herdeck.deckapp import DeckApp
from herdeck.deckapp.live import LiveSource
from herdeck.model import AgentKey, AgentState, Status


class StubIcons:
    def render_tile_bytes(self, tile):
        out = io.BytesIO()
        Image.new("RGB", (4, 4), (1, 2, 3)).save(out, "PNG")
        return out.getvalue()


class FakeRunner:
    def __init__(self):
        self.sent: list[dict] = []

    def send(self, msg):
        self.sent.append(msg)

    def close(self):
        pass


def make_config(**notifications) -> Config:
    config = Config(
        servers=[ServerConfig("local", "ws://bridge", "tok")],
        profiles=dict(DEFAULT_PROFILES),
        overview_order=["local"],
        grid=(5, 3),
    )
    if notifications:
        config.notifications = Notifications(**notifications)
    return config


def agent(pane="p1", status=Status.WORKING, terminal="term-1", **kw) -> AgentState:
    return AgentState(
        AgentKey("local", pane), "claude", f"agent-{pane}", status, terminal_id=terminal, **kw
    )


def live(config=None, **kwargs) -> tuple[LiveSource, FakeRunner]:
    source = LiveSource(config or make_config(), notify_schedule=lambda fn: fn(), **kwargs)
    runner = FakeRunner()
    source.attach_runner(runner, "local")
    source._on_connection("local", True)
    return source, runner


# --- LiveSource: semantic bookkeeping --------------------------------------------


def test_server_is_available_only_after_a_snapshot_on_the_current_connection():
    source, _ = live()
    assert not source.semantic_server_available("local")
    source._on_snapshot("local", [agent()])
    assert source.semantic_server_available("local")
    source._on_connection("local", False)
    assert not source.semantic_server_available("local")
    source._on_connection("local", True)
    assert not source.semantic_server_available("local")  # reconnected, no snapshot yet
    source._on_snapshot("local", [agent()])
    assert source.semantic_server_available("local")


def test_semantic_generation_changes_only_for_the_changed_agent():
    source, _ = live()
    source._on_snapshot("local", [agent("p1"), agent("p2")])
    p1, p2 = source.semantic_generation("local", "p1"), source.semantic_generation("local", "p2")
    source._on_snapshot("local", [agent("p1"), agent("p2", Status.BLOCKED)])
    assert source.semantic_generation("local", "p1") == p1
    assert source.semantic_generation("local", "p2") != p2
    before = source.semantic_generation("local", "p1")
    source._on_event("local", agent("p1", Status.DONE))
    assert source.semantic_generation("local", "p1") != before
    assert [a.key.pane_id for a in source.semantic_agents()] == ["p1", "p2"]
    assert source.semantic_agent(AgentKey("local", "p2")).status is Status.BLOCKED


def test_a_claimed_result_is_not_a_deck_result():
    source, runner = live()
    from herdeck.commands import Command

    claimed = {}

    def tap(server_id, req, data):
        if req.startswith("tg"):
            claimed[req] = data
            kind = "read" if "text" in data else "act_if_blocked"
            return Command(kind, server_id, "p1")
        return None

    source.set_result_tap(tap)
    runner.sent.clear()
    source._on_result("local", "tg1", {"text": "prompt", "pane_id": "p1"})
    assert claimed == {"tg1": {"text": "prompt", "pane_id": "p1"}}
    assert runner.sent == []  # a read result: nothing else happens
    source._on_result("local", "tg2", {"sent": True})
    assert runner.sent == [{"type": "list"}]  # an act ack resyncs, as a deck act does
    runner.sent.clear()
    source._on_result("local", "r9", {"sent": True})
    assert runner.sent == [{"type": "list"}]  # unclaimed results keep the deck path


# --- LiveSource: interactive Telegram routing -------------------------------------


def telegram_config(**telegram) -> Config:
    tg = TelegramConfig(token_env="TG_TOKEN", chat_id="-1", **telegram)
    return make_config(enabled=True, backends=["telegram"], on=["blocked", "done"], telegram=tg,
                       skip_focused=False)


def test_blocked_alerts_go_to_the_interactive_chain_and_done_stays_one_way(monkeypatch):
    import herdeck.notify as notify

    one_way = []
    monkeypatch.setenv("TG_TOKEN", "bot-token")
    monkeypatch.setattr(
        notify,
        "make_telegram_sink",
        lambda token, chat, thread: lambda title, body, sound, icon=None: one_way.append(title),
    )
    source, _ = live(telegram_config())
    interactive = []
    source.set_telegram_interactive(
        lambda: True,
        lambda agent, *, body, sound, multi_server: interactive.append((agent.key, body)),
    )
    source._on_snapshot("local", [agent()])  # baseline
    source._on_event("local", agent(status=Status.BLOCKED))
    assert interactive == [(AgentKey("local", "p1"), "agent-p1")]
    assert one_way == []
    source._on_event("local", agent(status=Status.DONE))
    assert one_way == ["claude · done"]
    assert len(interactive) == 1


def test_without_an_active_interactive_chain_blocked_alerts_are_one_way(monkeypatch):
    import herdeck.notify as notify

    one_way = []
    monkeypatch.setenv("TG_TOKEN", "bot-token")
    monkeypatch.setattr(
        notify,
        "make_telegram_sink",
        lambda token, chat, thread: lambda title, body, sound, icon=None: one_way.append(title),
    )
    source, _ = live(telegram_config())
    interactive = []
    source.set_telegram_interactive(lambda: False, lambda *a, **k: interactive.append(a))
    source._on_snapshot("local", [agent()])
    source._on_event("local", agent(status=Status.BLOCKED))
    assert one_way == ["claude · needs input"]
    assert interactive == []


def test_interactive_alerts_honor_only_when_away(monkeypatch):
    monkeypatch.setenv("TG_TOKEN", "bot-token")
    source, _ = live(telegram_config(only_when_away=5))
    source._user_away = lambda seconds: False
    interactive = []
    source.set_telegram_interactive(lambda: True, lambda *a, **k: interactive.append(a))
    source._on_snapshot("local", [agent()])
    source._on_event("local", agent(status=Status.BLOCKED))
    assert interactive == []


# --- RuntimeServices ----------------------------------------------------------------


class FakeInteractor:
    instances: list = []

    def __init__(self, client, control, *, store=None, offset=None, **kwargs):
        self.control = control
        self.store = store
        self.kwargs = kwargs
        self.alerts = []
        self._offset = offset
        self.polls = 0
        FakeInteractor.instances.append(self)

    @property
    def offset(self):
        return self._offset

    @property
    def inbound_disabled(self):
        return False

    async def notify_blocked(self, agent, *, body, sound, multi_server):
        self.alerts.append((agent.key, body))

    async def poll_once(self, *, timeout, is_current):
        import asyncio

        self.polls += 1
        await asyncio.sleep(0.01)


@pytest.fixture
def services_env(monkeypatch):
    from herdeck.deckapp.services import RuntimeServices

    FakeInteractor.instances = []
    made = []

    def build(config, source):
        services = RuntimeServices(
            config,
            current_source=lambda: source,
            getenv=lambda name: "bot-token" if name == "TG_TOKEN" else None,
            bot_client_factory=lambda token: object(),
            interactor_factory=FakeInteractor,
        )
        services.wire(source)
        made.append(services)
        return services

    yield build
    for services in made:
        services.close()


def test_services_control_round_trips_through_the_source(services_env):
    source, runner = live()
    source._on_snapshot("local", [agent(status=Status.BLOCKED)])
    services = services_env(source.config, source)
    runner.sent.clear()
    future = services.semantic_request({"operation": "inventory", "caller": "t"})
    body = future.result(3).body
    assert [a["pane_id"] for a in body["agents"]] == ["p1"]
    assert body["agents"][0]["available"] is True

    import asyncio

    prompt = asyncio.run_coroutine_threadsafe(
        services.control.read_prompt(AgentKey("local", "p1")), services._loop
    )
    msg = None
    deadline = time.monotonic() + 3
    while msg is None and time.monotonic() < deadline:
        msg = next((m for m in runner.sent if m.get("type") == "read"), None)
        time.sleep(0.01)
    assert msg["terminal_id"] == "term-1" and msg["req"].startswith("tg")
    # the connector delivers the result through the source's tap
    source._on_result("local", msg["req"], {"text": "Proceed?", "pane_id": "p1"})
    assert prompt.result(3) == "Proceed?"


def test_services_generation_changes_with_every_wired_source(services_env):
    source, _ = live()
    source._on_snapshot("local", [agent()])
    services = services_env(source.config, source)
    first = services._generation("local", "p1")
    services.wire(source)
    assert services._generation("local", "p1") != first


def test_interactive_telegram_is_built_from_config_and_keeps_its_cursor(services_env):
    config = telegram_config(interactive=True, allowed_user_ids=[42])
    source, _ = live(config)
    services = services_env(config, source)
    assert services.telegram_active()
    first = FakeInteractor.instances[-1]
    assert first.kwargs["allowed_user_ids"] == [42]
    first._offset = 17
    # an unchanged telegram config keeps the interactor (and its alert store)
    services.wire(source)
    assert FakeInteractor.instances[-1] is first
    # a changed one rebuilds it on the same store, continuing from the old cursor
    changed = telegram_config(interactive=True, allowed_user_ids=[42, 7])
    source2, _ = live(changed)
    services.wire(source2)
    second = FakeInteractor.instances[-1]
    assert second is not first and second.store is first.store
    assert second.offset == 17


def test_interactive_telegram_needs_allowed_users(services_env):
    config = telegram_config(interactive=True)
    source, _ = live(config)
    services = services_env(config, source)
    assert not services.telegram_active()


def test_notify_blocked_reaches_the_interactor(services_env):
    config = telegram_config(interactive=True, allowed_user_ids=[42])
    source, _ = live(config)
    services = services_env(config, source)
    services.notify_blocked(agent(status=Status.BLOCKED), body="b", sound=False, multi_server=False)
    interactor = FakeInteractor.instances[-1]
    deadline = time.monotonic() + 3
    while not interactor.alerts and time.monotonic() < deadline:
        time.sleep(0.01)
    assert interactor.alerts == [(AgentKey("local", "p1"), "b")]


def test_demo_source_answers_control_requests_in_process(services_env):
    from herdeck.deckapp.mock import MockSource

    source = MockSource()
    services = services_env(source.config, source)
    body = services.semantic_request({"operation": "inventory", "caller": "t"}).result(3).body
    assert body["agents"] and all(a["available"] for a in body["agents"])
    blocked = next(a for a in body["agents"] if a["status"] == "blocked")
    import asyncio

    prompt = asyncio.run_coroutine_threadsafe(
        services.control.read_prompt(AgentKey(blocked["server_id"], blocked["pane_id"])),
        services._loop,
    ).result(3)
    assert "Do you want to proceed?" in prompt


# --- browser terminal pump ----------------------------------------------------------


class PoolSource:
    """web_term_* double: scripted poll results."""

    def __init__(self, opened, polls):
        self.opened = opened
        self.polls = list(polls)
        self.closed: list[str] = []

    def web_term_open(self, index, cols, rows):
        return self.opened

    def web_term_poll(self, session, after, wait_s):
        if not self.polls:
            time.sleep(0.01)
            return None
        return self.polls.pop(0)

    def web_term_close(self, session):
        self.closed.append(session)
        return True


def drain(sub, count, timeout=3.0):
    items = []
    deadline = time.monotonic() + timeout
    while len(items) < count and time.monotonic() < deadline:
        try:
            items.append(sub.queue.get(timeout=0.05))
        except queue.Empty:
            pass
    return items


def terminals(source, *, current=True, lang="en"):
    from herdeck.deckapp.web_terminals import WebTerminals

    return WebTerminals(
        lambda: source, tile_is_current=lambda i, v: current, language=lambda: lang
    )


FRAME = {"seq": 0, "full": True, "cols": 80, "rows": 24, "data": "aGk="}


def test_pump_streams_meta_frames_and_the_bridge_close_reason():
    source = PoolSource(
        ("alpha", "s1"),
        [{"frames": [FRAME], "next": 1, "closed": None, "gap": False},
         {"frames": [], "next": 1, "closed": "pane exited", "gap": False}],
    )
    sub = terminals(source).open(0, 80, 24, 5)
    assert drain(sub, 3) == [
        {"kind": "meta", "label": "alpha"},
        {"kind": "frame", **FRAME},
        {"kind": "closed", "reason": "pane exited"},
    ]


@pytest.mark.parametrize(
    ("opened", "lang", "reason"),
    [
        ("no_agent", "en", "no agent terminal on this tile"),
        ("disconnected", "en", None),
    ],
)
def test_pump_reports_why_a_preview_cannot_open(opened, lang, reason):
    from herdeck.i18n import tr

    sub = terminals(PoolSource(opened, [])).open(0, 80, 24, 5)
    expected = reason or tr(lang, "web.term_disconnected")
    assert drain(sub, 1) == [{"kind": "closed", "reason": expected}]


def test_pump_rejects_a_stale_tile_and_maps_pool_reasons():
    from herdeck.i18n import tr

    sub = terminals(PoolSource(("a", "s"), []), current=False).open(0, 80, 24, 5)
    assert drain(sub, 1) == [{"kind": "closed", "reason": tr("en", "web.term_no_agent")}]
    source = PoolSource(("a", "s"), [{"frames": [], "next": 0, "closed": "disconnected",
                                      "gap": False}])
    sub = terminals(source, lang="cs").open(0, 80, 24, 5)
    assert drain(sub, 2)[1] == {"kind": "closed", "reason": tr("cs", "web.term_disconnected")}


def test_pump_ends_a_stream_that_lost_frames():
    source = PoolSource(("a", "s9"), [{"frames": [], "next": 5, "closed": None, "gap": True}])
    sub = terminals(source).open(0, 80, 24, 5)
    assert drain(sub, 2)[1]["kind"] == "closed"
    assert source.closed == ["s9"]


def test_closing_a_subscription_stops_the_observe():
    source = PoolSource(("a", "s2"), [])
    web = terminals(source)
    sub = web.open(0, 80, 24, 5)
    assert drain(sub, 1) == [{"kind": "meta", "label": "a"}]
    web.close(sub)
    assert source.closed == ["s2"]


def test_web_preview_pool_is_separate_from_the_card_pool():
    source, runner = live()
    source._on_snapshot("local", [agent()])

    class Orch:
        def agent_for_preview(self, index):
            return source.semantic_agent(AgentKey("local", "p1")) if index == 0 else None

    source._orch = Orch()
    assert source.web_term_open(3, 80, 24) == "no_agent"
    label, session = source.web_term_open(0, 100, 30)
    assert label == "agent-p1"
    observe = runner.sent[-1]
    assert observe["type"] == "observe" and (observe["cols"], observe["rows"]) == (100, 30)
    assert observe["terminal_id"] == "term-1"
    assert source._card_terms.poll_exists(session) is False
    source._on_connection("local", False)
    assert source.web_term_poll(session, 0, 0)["closed"] == "disconnected"


# --- sinks / DeckApp -------------------------------------------------------------------


def test_driver_sink_renders_in_range_tiles_and_routes_presses():
    from herdeck.deckapp.sinks import DriverSink, RenderFrame
    from herdeck.driver.fake import FakeRenderer
    from herdeck.orchestrator import Orchestrator

    driver = FakeRenderer(13)
    presses = []
    sink = DriverSink(driver, on_press=presses.append, slots=13)
    rs = Orchestrator(make_config(), slots=13).render()
    sink.deliver(RenderFrame(render=rs, working=None, full=True))
    assert driver.last_panel is not None and len(driver.last) == 13
    driver.simulate_press(2)
    assert presses == [2]
    sink.close()


def test_status_panel_is_held_then_dismissed_by_a_press():
    source, _ = live()
    app = DeckApp(source, serve=False, icon_provider=StubIcons())
    try:
        frames = []

        class Sink:
            def deliver(self, frame):
                frames.append(frame)

            def close(self):
                pass

        app.add_sink(Sink())
        app.hold_status_panel("reload failed", ["bad toml"])
        assert frames[-1].render.panel.title == "reload failed"
        app.press(0)
        assert frames[-1].render.panel.title != "reload failed"
    finally:
        app.close()


def test_status_panel_lapses_on_the_ticker(monkeypatch):
    import herdeck.deckapp.server as server

    monkeypatch.setattr(server, "STATUS_PANEL_HOLD_S", 0.0)
    source, _ = live()
    app = DeckApp(source, serve=False, icon_provider=StubIcons())
    try:
        app.hold_status_panel("profile locked", ["default"])
        with app._lock:
            assert app._consume_expired_status_locked() is True
            assert app._consume_expired_status_locked() is False
    finally:
        app.close()


def test_a_non_serving_host_can_still_run_the_ticker():
    source, _ = live()
    app = DeckApp(source, serve=False, run_ticker=True, tick_interval=0.05, icon_provider=StubIcons())
    try:
        assert app._ticker_thread is not None and app._server is None
    finally:
        app.close()
    quiet = DeckApp(live()[0], serve=False, tick_interval=0.05, icon_provider=StubIcons())
    try:
        assert quiet._ticker_thread is None
    finally:
        quiet.close()


def test_a_shell_less_host_posts_macos_banners_directly(caplog):
    from herdeck.notify import NotificationFeed, deckapp_sink

    posted = []
    feed = NotificationFeed()
    sink = deckapp_sink(
        feed,
        lambda: False,
        make_config(enabled=True, backends=["macos"]),
        macos_sink=lambda title, body, sound, icon=None: posted.append((title, icon)),
        shell=False,
    )
    sink("t", "b", True, icon="/tmp/i.png")
    assert posted == [("t", "/tmp/i.png")]
    assert feed.state()["items"] == []
    assert "fallback=osascript" not in caplog.text


def test_newly_entered_detects_transition_and_avoids_dup():
    from herdeck.notify_events import newly_entered

    k = AgentKey("s", "p1")
    s_block = [AgentState(k, "claude", "api", Status.BLOCKED)]
    s_work = [AgentState(k, "claude", "api", Status.WORKING)]
    to, seen = newly_entered(Status.BLOCKED, set(), s_block)  # first time -> notify
    assert k in to and k in seen
    to2, seen2 = newly_entered(Status.BLOCKED, seen, s_block)  # same blocked -> no dup
    assert to2 == set() and seen2 == seen
    to3, seen3 = newly_entered(Status.BLOCKED, seen2, s_work)  # left blocked -> reset
    assert to3 == set() and k not in seen3


def test_shutdown_does_not_wait_for_an_in_flight_telegram_long_poll():
    import asyncio
    import threading

    from herdeck.deckapp.services import RuntimeServices

    started = threading.Event()

    class SlowInteractor(FakeInteractor):
        async def poll_once(self, *, timeout, is_current):
            def long_poll():
                started.set()
                time.sleep(5)  # a getUpdates that nobody can cancel

            await asyncio.to_thread(long_poll)

    config = telegram_config(interactive=True, allowed_user_ids=[42])
    source, _ = live(config)
    services = RuntimeServices(
        config,
        current_source=lambda: source,
        getenv=lambda name: "bot-token",
        bot_client_factory=lambda token: object(),
        interactor_factory=SlowInteractor,
    )
    assert started.wait(3)
    begun = time.monotonic()
    services.close()
    assert time.monotonic() - begun < 2.5
    workers = [t for t in threading.enumerate() if t.name == "herdeck-services-io"]
    assert workers and all(t.daemon for t in workers)  # never joined at exit
