"""Runtime usage input selection (usage_hub.UsageHub): bridge frames vs the
local poller per [usage].source, filtering, merging and alerts."""

import time

import pytest

from herdeck.config import ConfigError, UsageConfig
from herdeck.settings import _usage_config
from herdeck.usage import ProviderUsage, UsageWindow
from herdeck.usage_hub import UsageHub

RESET = "2026-09-24T18:00:00Z"


def _usage(provider, used=10, subscription="paid", early=None):
    return ProviderUsage(provider, [UsageWindow("5h", used, RESET, early)], subscription)


class FakePoller:
    def __init__(self, data):
        self.data = data
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def close(self):
        self.closed = True

    def snapshot(self):
        return list(self.data)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make(source="auto", servers=("a",), providers=("codex", "claude"), **cfg):
    clock = Clock()
    pollers: list[FakePoller] = []
    alerts: list = []
    changes: list = []

    def factory():
        poller = FakePoller([_usage("codex", 1, early=None)])
        pollers.append(poller)
        return poller

    hub = UsageHub(
        UsageConfig(providers=list(providers), source=source, **cfg),
        local_factory=factory,
        on_alert=alerts.extend,
        on_change=lambda: changes.append(1),
        server_ids=list(servers),
        clock=clock,
        wall_clock=lambda: 1_700_000_000.0,
        run_thread=False,
    )
    return hub, clock, pollers, alerts, changes


# --- source selection matrix --------------------------------------------------


def test_local_source_polls_locally_and_ignores_bridges():
    hub, _clock, pollers, _alerts, _changes = make("local")
    assert hub.mode == "local" and pollers[0].started
    hub.bridge_update("a", True, [_usage("codex", 77)])
    assert hub.mode == "local"
    assert hub.snapshot() == [_usage("codex", 1)]


def test_bridge_source_never_polls_locally():
    hub, clock, pollers, _alerts, _changes = make("bridge")
    assert hub.mode == "bridge" and hub.snapshot() == []
    clock.now += 3600
    hub.evaluate()
    assert pollers == []
    hub.bridge_update("a", True, [_usage("codex", 77)])
    assert hub.snapshot() == [_usage("codex", 77)]


def test_auto_prefers_a_bridge_that_offers_usage():
    hub, _clock, pollers, _alerts, changes = make("auto")
    assert hub.mode is None and hub.snapshot() == []  # undecided: no local spawn yet
    hub.bridge_update("a", True, None)  # snapshot advertising the capability
    hub.bridge_update("a", True, [_usage("codex", 42)])
    assert hub.mode == "bridge" and pollers == []
    assert hub.snapshot() == [_usage("codex", 42)]
    assert changes  # the deck repaints on bridge data


def test_auto_old_bridge_without_capability_falls_back_to_local():
    hub, _clock, pollers, _alerts, _changes = make("auto")
    hub.bridge_update("a", False, None)  # snapshot without `usage`
    assert hub.mode == "local" and pollers[0].started
    assert hub.snapshot() == [_usage("codex", 1)]


def test_auto_waits_for_every_server_or_the_grace():
    hub, clock, pollers, _alerts, _changes = make("auto", servers=("a", "b"))
    hub.bridge_update("a", False, None)
    assert hub.mode is None  # b has not answered yet
    clock.now += 16
    hub.evaluate()  # the tick
    assert hub.mode == "local" and pollers


def test_auto_without_servers_is_local_at_once():
    hub, _clock, pollers, _alerts, _changes = make("auto", servers=())
    assert hub.mode == "local" and pollers[0].started


def test_auto_switches_to_a_bridge_that_appears_later_and_stops_local():
    hub, _clock, pollers, _alerts, _changes = make("auto")
    hub.bridge_update("a", False, None)
    local = pollers[0]
    hub.bridge_update("a", True, [_usage("codex", 42)])
    assert hub.mode == "bridge"
    for _ in range(100):  # closed off-thread
        if local.closed:
            break
        time.sleep(0.01)
    assert local.closed


def test_auto_keeps_bridge_numbers_through_a_flap_then_falls_back():
    hub, clock, pollers, _alerts, _changes = make("auto")
    hub.bridge_update("a", True, [_usage("codex", 42)])
    hub.bridge_update("a", False, None)  # disconnect
    clock.now += 5
    hub.evaluate()
    assert hub.mode == "bridge" and hub.snapshot() == [_usage("codex", 42)]
    assert pollers == []  # a flap spawns no local codex
    hub.bridge_update("a", True, None)  # reconnected (snapshot); frame follows
    clock.now += 60
    hub.evaluate()
    assert hub.mode == "bridge" and hub.snapshot() == [_usage("codex", 42)]
    hub.bridge_update("a", False, None)
    clock.now += 16
    hub.evaluate()
    assert hub.mode == "local" and pollers[0].started


# --- filtering + merging ------------------------------------------------------


def test_bridge_data_gets_this_runtimes_providers_order_and_paid_only():
    hub, _clock, _pollers, _alerts, _changes = make(
        "bridge", providers=("claude", "codex"), paid_only=True
    )
    hub.bridge_update(
        "a",
        True,
        [_usage("codex", 5), _usage("claude", 9, "unknown"), _usage("gemini", 3)],
    )
    assert hub.snapshot() == [_usage("codex", 5)]  # claude not paid, gemini not listed
    hub.bridge_update("a", True, [_usage("codex", 5), _usage("claude", 9)])
    assert [u.provider for u in hub.snapshot()] == ["claude", "codex"]


def test_multiple_bridges_merge_per_provider_in_config_order():
    hub, _clock, _pollers, _alerts, _changes = make("auto", servers=("first", "second"))
    hub.bridge_update("second", True, [_usage("codex", 20), _usage("claude", 21)])
    hub.bridge_update("first", True, [_usage("codex", 10)])
    assert hub.snapshot() == [_usage("codex", 10), _usage("claude", 21)]
    assert hub.health() == {"source": "auto", "active": "bridge", "bridges": ["first", "second"]}


def test_paid_only_skips_an_unpaid_first_bridge_for_a_paid_second():
    hub, _clock, _pollers, _alerts, _changes = make(
        "bridge", servers=("first", "second"), providers=("codex",), paid_only=True
    )
    hub.bridge_update("first", True, [_usage("codex", 10, "free")])
    hub.bridge_update("second", True, [_usage("codex", 20)])
    assert hub.snapshot() == [_usage("codex", 20)]


def test_pace_hint_is_the_bridges():
    hub, _clock, _pollers, _alerts, _changes = make("bridge")
    hub.bridge_update("a", True, [_usage("codex", 50, early=1800)])
    assert hub.snapshot()[0].windows[0].full_early_s == 1800


# --- alerts from bridge data --------------------------------------------------


def test_alerts_fire_from_bridge_data_after_a_silent_baseline():
    hub, _clock, _pollers, alerts, _changes = make("bridge", alert_at=[80, 95])
    hub.bridge_update("a", True, [_usage("codex", 85)])
    assert alerts == []  # first observation is a baseline
    hub.bridge_update("a", True, [_usage("codex", 96)])
    assert [(a.kind, a.provider, a.percent) for a in alerts] == [("threshold", "codex", 95)]
    hub.bridge_update("a", True, [_usage("codex", 97)])
    assert len(alerts) == 1  # once per window period


def test_alerts_skip_providers_the_panel_hides():
    hub, _clock, _pollers, alerts, _changes = make(
        "bridge", providers=("codex",), paid_only=True, alert_at=[50]
    )
    hub.bridge_update("a", True, [_usage("codex", 10, "free")])
    hub.bridge_update("a", True, [_usage("codex", 90, "free")])
    assert alerts == []


def test_reset_alert_is_driven_by_the_clock_on_bridge_data():
    clock_wall = [1_700_000_000.0]
    hub = UsageHub(
        UsageConfig(providers=["codex"], source="bridge", alert_reset=True),
        local_factory=lambda: None,
        on_alert=(alerts := []).extend,
        server_ids=["a"],
        clock=(mono := Clock()),
        wall_clock=lambda: clock_wall[0],
        run_thread=False,
    )
    soon = "2023-11-14T22:30:00Z"  # after the first wall time below
    hub.bridge_update("a", True, [ProviderUsage("codex", [UsageWindow("5h", 99, soon)], "paid")])
    hub.bridge_update("a", True, [ProviderUsage("codex", [UsageWindow("5h", 100, soon)], "paid")])
    assert alerts == []
    clock_wall[0] = 1_700_010_000.0  # past the reset; the bridge sent nothing new
    mono.now += 301  # one refresh_secs later the tick re-observes
    hub.evaluate()
    assert [(a.kind, a.provider) for a in alerts] == [("reset", "codex")]


def test_switching_to_bridge_starts_a_fresh_baseline():
    hub, _clock, _pollers, alerts, _changes = make("auto", alert_at=[80])
    hub.bridge_update("a", False, None)  # local first
    hub.bridge_update("a", True, [_usage("codex", 90)])  # now bridge, already past 80
    assert alerts == []


# --- lifecycle ----------------------------------------------------------------


def test_set_servers_keeps_numbers_for_the_grace_and_restarts_the_wait():
    hub, clock, pollers, _alerts, _changes = make("auto")
    hub.bridge_update("a", True, [_usage("codex", 42)])
    hub.set_servers(["a"])  # source swap: the new connectors will re-report
    assert hub.mode == "bridge" and hub.snapshot() == [_usage("codex", 42)]
    clock.now += 16
    hub.evaluate()
    assert hub.mode == "local" and pollers


def test_close_stops_the_local_poller_and_ignores_late_reports():
    hub, _clock, pollers, _alerts, _changes = make("local")
    hub.close()
    assert pollers[0].closed
    hub.bridge_update("a", True, [_usage("codex", 1)])
    assert hub.snapshot() == []


def test_hub_thread_ticks_and_stops():
    hub = UsageHub(
        UsageConfig(providers=["codex"], source="bridge"),
        local_factory=lambda: None,
        server_ids=[],
        tick_s=0.01,
    )
    hub.close()
    assert hub._thread is not None and not hub._thread.is_alive()


# --- config -------------------------------------------------------------------


def test_usage_source_config_validation():
    assert _usage_config({}).source == "auto"
    assert _usage_config({"source": "bridge"}).source == "bridge"
    with pytest.raises(ConfigError, match="usage.source"):
        _usage_config({"source": "cloud"})


# --- LiveSource + DeckApp wiring ------------------------------------------------


def _live():
    from herdeck.config import DEFAULT_PROFILES, Config, ServerConfig
    from herdeck.deckapp.live import LiveSource

    server = ServerConfig(id="prod", url="ws://b", token="t")
    config = Config(
        servers=[server], profiles=dict(DEFAULT_PROFILES), overview_order=["prod"], grid=(5, 3)
    )
    config.usage.providers = ["codex"]
    return LiveSource(config, server), config


def test_live_source_buffers_reports_and_replays_them_to_a_new_sink():
    src, _config = _live()
    src._on_usage("prod", True, None)
    src._on_usage("prod", True, [_usage("codex", 5)])
    src._on_usage("prod", True, None)  # a later snapshot keeps the last frame
    got = []
    src.set_usage_sink(lambda *args: got.append(args))
    assert got == [("prod", True, [_usage("codex", 5)])]
    src._on_usage("prod", False, None)
    assert got[-1] == ("prod", False, None)
    src.set_usage_sink(None)
    src._on_usage("prod", True, None)
    assert len(got) == 2


def test_deckapp_renders_bridge_usage():
    from herdeck.deckapp.server import DeckApp
    from tests.test_deckapp_live import StubIcons

    src, _config = _live()
    src._on_usage("prod", True, [_usage("codex", 64)])  # before the app exists
    app = DeckApp(src, serve=False, icon_provider=StubIcons())
    try:
        assert app._usage_poller.mode == "bridge"
        assert app._orch._usage == [_usage("codex", 64)]  # repainted on the replay
        src._on_usage("prod", True, [_usage("codex", 65)])
        assert app._orch._usage == [_usage("codex", 65)]
        assert app._health()["usage"]["active"] == "bridge"
    finally:
        app.close()
