"""Each bridge's shared settings apply to ITS OWN agents (issue #116, phase 1).

Two servers: "a" has no shared settings (the local config applies), "b"'s
bridge sent a document that differs. An agent on "b" must never be answered or
notified with "a"'s rules, and vice versa.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from herdeck.app_control import RuntimeAgentControl
from herdeck.commands import Command
from herdeck.config import DEFAULT_PROFILES, Config, ServerConfig, UsageConfig
from herdeck.deckapp.live import LiveSource
from herdeck.deckapp.shared_view import SharedSettingsView
from herdeck.model import AgentKey, Status
from herdeck.orchestrator import Orchestrator
from herdeck.protocol import Settings
from herdeck.usage import ProviderUsage, UsageWindow
from herdeck.usage_hub import UsageHub
from tests.test_deckapp_live import FakeRunner, RecordingNotifier, agent
from tests.test_done_quiet import FakeTimers

MIN = 60_000


def _config(order=("a", "b")) -> Config:
    cfg = Config(
        servers=[ServerConfig(id="a", url="ws://a", token="t"), ServerConfig(id="b", url="ws://b", token="t")],
        profiles=dict(DEFAULT_PROFILES),
        overview_order=list(order),
        grid=(5, 3),
    )
    cfg.notifications.enabled = True
    cfg.notifications.on = ["blocked", "done"]
    cfg.notifications.sound = False
    return cfg


def _frame(server_id: str, settings: dict, revision: int = 3) -> Settings:
    return Settings(server_id, revision, 1_700_000_000_000, "mac-b", settings)


def _source(tmp_path, b_settings: dict, *, config=None, clock=None, now_ms=MIN):
    timers = FakeTimers()
    src = LiveSource(
        config or _config(),
        notify_schedule=lambda fn: fn(),
        notify_clock=clock,
        done_timer=timers,
        shared_view=SharedSettingsView(tmp_path),
    )
    src._wall_ms = lambda: now_ms
    notifier = RecordingNotifier()
    src._notifier = notifier
    src._on_settings("b", _frame("b", b_settings))
    return src, notifier, timers


def st(server, pane, status, since):
    return replace(agent(server, pane, status), status_since_ms=since)


def _alerted(notifier) -> list[tuple[str, str]]:
    return [(m["agent"]["server_id"], m["event"]) for m in notifier.metas]


# --- notification rules -------------------------------------------------------


def test_done_min_work_of_b_defers_a_short_run_on_b_only(tmp_path):
    b = {"notifications": {"on": ["blocked", "done"], "done_min_work": 3, "done_short_delay": 10}}
    src, notifier, timers = _source(tmp_path, b)
    for sid in ("a", "b"):
        src._on_snapshot(sid, [st(sid, "p", Status.WORKING, 0)])
        src._on_event(sid, st(sid, "p", Status.DONE, MIN))  # a one-minute run
    assert _alerted(notifier) == [("a", "done")]  # local done_min_work = 0
    assert len(timers.pending) == 1  # b's short run waits
    timers.fire_all()
    assert _alerted(notifier) == [("a", "done"), ("b", "done")]


def test_on_of_b_applies_to_b_agents_only(tmp_path):
    src, notifier, _timers = _source(tmp_path, {"notifications": {"on": ["blocked"]}})
    for sid in ("a", "b"):
        src._on_snapshot(sid, [st(sid, "p", Status.WORKING, 0)])
        src._on_event(sid, st(sid, "p", Status.DONE, 10 * MIN))
    assert _alerted(notifier) == [("a", "done")]
    for sid in ("a", "b"):
        src._on_event(sid, st(sid, "q", Status.BLOCKED, 10 * MIN))
    assert _alerted(notifier)[1:] == [("a", "blocked"), ("b", "blocked")]


def test_on_of_b_can_enable_an_event_local_config_turns_off(tmp_path):
    cfg = _config()
    cfg.notifications.on = ["blocked"]
    src, notifier, _timers = _source(tmp_path, {"notifications": {"on": ["done"]}}, config=cfg)
    for sid in ("a", "b"):
        src._on_snapshot(sid, [st(sid, "p", Status.WORKING, 0)])
        src._on_event(sid, st(sid, "p", Status.DONE, 10 * MIN))
    assert _alerted(notifier) == [("b", "done")]


def test_remind_after_of_b_reminds_b_agents_only(tmp_path):
    now = [1000.0]
    src, notifier, _timers = _source(
        tmp_path,
        {"notifications": {"on": ["blocked", "done"], "remind_after": 5}},
        clock=lambda: now[0],
    )
    try:
        for sid in ("a", "b"):
            src._on_snapshot(sid, [agent(sid, "p", Status.WORKING)])
            src._on_event(sid, agent(sid, "p", Status.BLOCKED))
        assert _alerted(notifier) == [("a", "blocked"), ("b", "blocked")]
        now[0] += 5 * 60
        assert src.check_reminders() == 1
        assert notifier.metas[-1]["agent"]["server_id"] == "b"
    finally:
        src.close()


def test_subagents_done_of_b_applies_to_b_agents_only(tmp_path):
    src, _notifier, _timers = _source(
        tmp_path, {"notifications": {"on": ["blocked", "done"], "subagents_done": True}}
    )
    seen = []
    src._subagent_bursts.observe = lambda state: seen.append(state.key.server_id)
    src._subagents_notify([agent("a", "p", Status.IDLE), agent("b", "p", Status.IDLE)])
    assert seen == ["b"]


# --- answer profiles and safety -----------------------------------------------

CLAUDE = {"approve": ["1", "enter"], "deny": ["esc"], "stop": ["ctrl+c"], "approve_always": ["2", "enter"]}
B_ANSWERS = {"answer_profiles": {"claude": {**CLAUDE, "approve": ["y"]}}}
YES_NO = "Allow this?\n1. Yes\n2. Yes, and don't ask again\n3. No\n"


def _orch(src: LiveSource, config: Config | None = None) -> Orchestrator:
    orch = Orchestrator(config or src.config, clock=lambda: 0.0, config_for=src.config_for)
    for sid in ("a", "b"):
        orch.set_connection(sid, True)
    return orch


def _drill(orch: Orchestrator, key: AgentKey, detection: str = "") -> None:
    assert orch.open_agent(key) is not None
    orch.set_detection(detection)


def test_deck_fallback_approve_uses_the_agents_bridge_profile(tmp_path):
    src, _n, _t = _source(tmp_path, B_ANSWERS)
    orch = _orch(src)
    for sid in ("a", "b"):
        orch.apply_snapshot(sid, [agent(sid, "p", Status.BLOCKED)])
    sent = {}
    for sid in ("a", "b"):
        _drill(orch, AgentKey(sid, "p"), "Proceed? (y/n)")
        cmds = orch.on_press(0)  # the Approve fallback
        sent[sid] = cmds[0].keys
    assert sent == {"a": ["1", "enter"], "b": ["y"]}


def test_banner_answer_buttons_use_the_agents_bridge_profile(tmp_path):
    src, _n, _t = _source(tmp_path, B_ANSWERS)
    src._config.notifications.banner_actions = True
    src._config.notifications.backends = ["macos"]
    metas = {}
    for sid in ("a", "b"):
        meta: dict = {}
        src._enrich_blocked(agent(sid, "p", Status.BLOCKED), "body", YES_NO, meta)
        metas[sid] = meta
    # "1" is claude's local approve key; on "b" approve is "y", so the prompt
    # is not a plain yes/no there and gets a reply field instead of buttons.
    assert "actions" in metas["a"]
    assert "actions" not in metas["b"] and "reply" in metas["b"]


def test_deck_stop_confirm_follows_the_agents_bridge(tmp_path):
    src, _n, _t = _source(tmp_path, {"safety": {"require_confirm_for": []}})
    orch = _orch(src)
    for sid in ("a", "b"):
        orch.apply_snapshot(sid, [agent(sid, "p", Status.WORKING)])
    stop_i = orch.slots - 2
    _drill(orch, AgentKey("a", "p"))
    assert orch.on_press(stop_i) == []  # local default: act_force is armed first
    _drill(orch, AgentKey("b", "p"))
    cmds = orch.on_press(stop_i)
    assert cmds and cmds[0].kind == "act_force" and cmds[0].server_id == "b"


def test_answer_options_confirm_follows_the_agents_bridge(tmp_path):
    src, _n, _t = _source(tmp_path, {"safety": {"require_confirm_for": ["approve"]}})
    orch = _orch(src)
    for sid in ("a", "b"):
        orch.apply_snapshot(sid, [agent(sid, "p", Status.BLOCKED)])
    confirm = {
        sid: {o["id"]: o["confirm"] for o in orch.answer_options(AgentKey(sid, "p"), YES_NO)}
        for sid in ("a", "b")
    }
    assert confirm["a"]["approve"] is False
    assert confirm["b"]["approve"] is True


def _control(src: LiveSource, agents: dict):
    sent = []

    async def send(cmd, req):
        sent.append(cmd)

    control = RuntimeAgentControl(
        src.config, send=send, current_agent=agents.get, config_for=src.config_for
    )
    return control, sent


@pytest.mark.asyncio
async def test_app_control_approve_and_confirm_use_the_agents_bridge(tmp_path):
    src, _n, _t = _source(
        tmp_path, {**B_ANSWERS, "safety": {"require_confirm_for": ["approve"]}}
    )
    agents = {AgentKey(s, "p"): agent(s, "p", Status.BLOCKED) for s in ("a", "b")}
    control, sent = _control(src, agents)
    assert control.requires_confirmation("approve", server_id="a") is False
    assert control.requires_confirmation("approve", server_id="b") is True
    assert control.requires_confirmation("stop", server_id="a") is True
    assert control.requires_confirmation("stop", server_id="b") is False

    task = asyncio.create_task(control.approve(AgentKey("a", "p"), timeout=0.01))
    await asyncio.sleep(0)
    assert sent[-1].keys == ["1", "enter"]
    task.cancel()
    # "b" needs the confirmation first, then answers with its own keys.
    result = await control.approve(AgentKey("b", "p"), timeout=0.01)
    assert result.message == "confirmation required"
    task = asyncio.create_task(control.approve(AgentKey("b", "p"), timeout=0.01, confirmed=True))
    await asyncio.sleep(0)
    assert sent[-1].server_id == "b" and sent[-1].keys == ["y"]
    task.cancel()


def test_agent_card_stop_uses_the_agents_bridge(tmp_path, monkeypatch):
    from herdeck.deckapp import agent_card

    monkeypatch.setattr(agent_card, "CARD_REPLY_TIMEOUT_S", 0.0)
    src, _n, _t = _source(
        tmp_path,
        {
            "answer_profiles": {"claude": {**CLAUDE, "stop": ["esc", "esc"]}},
            "safety": {"require_confirm_for": []},
        },
    )
    runners = {}
    for sid in ("a", "b"):
        runners[sid] = FakeRunner()
        src.attach_runner(runners[sid], sid)
        src._agents[AgentKey(sid, "p")] = agent(sid, "p", Status.WORKING)
        src._connected[sid] = True
    src.card_stop("a", "p")
    src.card_stop("b", "p")
    assert runners["a"].sent[-1]["keys"] == ["ctrl+c"]
    assert runners["b"].sent[-1]["keys"] == ["esc", "esc"]
    orch = _orch(src)
    src.attach(orch)
    for sid in ("a", "b"):
        orch.apply_snapshot(sid, [agent(sid, "p", Status.WORKING)])
    assert src.card_detail("a", "p")["stop_confirm"] is True
    assert src.card_detail("b", "p")["stop_confirm"] is False


# --- macros and start profiles --------------------------------------------------


def test_macro_page_of_a_drilled_agent_lists_its_bridges_macros(tmp_path):
    src, _n, _t = _source(tmp_path, {"macros": [{"label": "ship", "text": "ship it"}]})
    orch = _orch(src)
    for sid in ("a", "b"):
        orch.apply_snapshot(sid, [agent(sid, "p", Status.WORKING)])
    texts = {}
    for sid in ("a", "b"):
        _drill(orch, AgentKey(sid, "p"))
        cmds = orch.on_press(0)
        texts[sid] = cmds[0].text
    assert texts == {"a": "continue", "b": "ship it"}


def test_launcher_uses_the_target_servers_start_profiles(tmp_path):
    b = {"start_profiles": {"pi": ["pi", "--fast"]}}
    src, _n, _t = _source(tmp_path, b, config=_config(order=("b", "a")))
    orch = _orch(src)
    orch._launcher = True
    labels = [t.label for t in orch.render().tiles]
    assert labels[0] == "pi"
    assert orch.on_press(0) == [Command("start", "b", text="pi", keys=["pi", "--fast"])]

    src2, _n, _t = _source(tmp_path / "2", b, config=_config(order=("a", "b")))
    orch2 = _orch(src2)
    orch2._launcher = True
    assert orch2.on_press(0) == [Command("start", "a", text="claude", keys=["claude"])]


# --- usage alerts -------------------------------------------------------------

RESET = "2026-09-24T18:00:00Z"


def _usage(provider, used):
    return ProviderUsage(provider, [UsageWindow("5h", used, RESET, None)], "paid")


def test_bridge_usage_alerts_use_that_bridges_alert_at(tmp_path):
    cfg = _config()
    cfg.usage = UsageConfig(providers=["codex", "claude"], source="bridge", alert_at=[90])
    src, _n, _t = _source(tmp_path, {"usage": {"alert_at": [50]}}, config=cfg)
    alerts: list = []

    def settings(sid):
        u = src.config_for(sid).usage
        return list(u.alert_at), u.alert_reset

    hub = UsageHub(
        cfg.usage,
        local_factory=lambda: None,
        on_alert=alerts.extend,
        server_ids=["a", "b"],
        wall_clock=lambda: 1_700_000_000.0,
        run_thread=False,
        alert_settings=settings,
    )
    hub.bridge_update("a", True, [_usage("codex", 10)])
    hub.bridge_update("b", True, [_usage("claude", 10)])
    assert alerts == []  # silent baseline
    hub.bridge_update("a", True, [_usage("codex", 60)])
    hub.bridge_update("b", True, [_usage("claude", 60)])
    assert [(a.provider, a.percent) for a in alerts] == [("claude", 50)]
    hub.bridge_update("a", True, [_usage("codex", 95)])
    assert [(a.provider, a.percent) for a in alerts] == [("claude", 50), ("codex", 90)]
    hub.close()


def test_deckapp_feeds_the_usage_hub_each_bridges_alert_settings(tmp_path):
    from herdeck.deckapp import DeckApp
    from tests.test_deckapp_live import StubIcons

    cfg = _config()
    cfg.usage = UsageConfig(providers=["codex"], source="bridge", alert_at=[90])
    src, _n, _t = _source(tmp_path, {"usage": {"alert_at": [50], "alert_reset": True}}, config=cfg)
    app = DeckApp(src, serve=False, icon_provider=StubIcons())
    try:
        hub = app._usage_poller
        assert hub._alert_settings("a") == ([90], False)
        assert hub._alert_settings("b") == ([50], True)
    finally:
        app.close()


def test_deckapp_orchestrator_follows_the_current_source(tmp_path):
    from herdeck.deckapp import DeckApp
    from tests.test_deckapp_live import StubIcons

    src, _n, _t = _source(tmp_path, {"macros": [{"label": "ship", "text": "ship it"}]})
    app = DeckApp(src, serve=False, icon_provider=StubIcons())
    try:
        assert app._orch._config_for("b").macros[0].text == "ship it"
        src2, _n, _t = _source(tmp_path / "2", {"macros": [{"label": "zap", "text": "zap"}]})
        app.swap_source(src2)
        assert app._orch._config_for("b").macros[0].text == "zap"
    finally:
        app.close()


def test_orchestrator_without_config_for_uses_its_own_config():
    cfg = _config()
    orch = Orchestrator(cfg, clock=lambda: 0.0)
    assert orch._config_for("b") is cfg
    new = replace(cfg, macros=[])
    orch.update_config(new)
    assert orch._config_for("b") is new


def test_first_reminder_on_a_fresh_server_survives_checks_and_fires(tmp_path):
    # Regression: the server set used to be snapshotted before the locked pass,
    # so a reminder tracked in between (typically the first blocked agent on a
    # server) was judged with interval 0 and deleted.
    now = [1000.0]
    src, notifier, _timers = _source(
        tmp_path,
        {"notifications": {"on": ["blocked", "done"], "remind_after": 5}},
        clock=lambda: now[0],
    )
    try:
        assert src.check_reminders() == 0  # nothing tracked yet
        src._on_snapshot("b", [agent("b", "p", Status.WORKING)])
        src._on_event("b", agent("b", "p", Status.BLOCKED))
        assert AgentKey("b", "p") in src._reminders
        now[0] += 60
        assert src.check_reminders() == 0
        assert AgentKey("b", "p") in src._reminders  # kept, not dropped
        now[0] += 4 * 60
        assert src.check_reminders() == 1
        assert notifier.metas[-1]["agent"]["server_id"] == "b"
    finally:
        src.close()
