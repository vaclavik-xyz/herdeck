import io
import json
import os
import queue
import threading
from datetime import UTC

from herdeck.layout import (
    usage_detail_gauges,
    usage_summary_gauges,
)
from herdeck.usage import (
    CodexAppServerSource,
    ProviderUsage,
    UsagePoller,
    UsageWindow,
    capture_claude_statusline,
    parse_claude_statusline,
    parse_codex_account,
    parse_codex_rate_limits,
    parse_usage,
    poller_from_config,
    read_claude_cache,
    resolve_cli,
)

# Trimmed real `codexbar usage --format json` output (claude + codex).
_CODEXBAR_JSON = json.dumps(
    [
        {
            "provider": "claude",
            "source": "web",
            "usage": {
                "primary": {
                    "resetsAt": "2026-07-02T23:00:00Z",
                    "windowMinutes": 300,
                    "usedPercent": 19,
                },
                "secondary": {
                    "resetsAt": "2026-07-06T13:00:00Z",
                    "windowMinutes": 10080,
                    "usedPercent": 43,
                },
                "tertiary": None,
            },
        },
        {
            "provider": "codex",
            "usage": {
                "primary": {"windowMinutes": 300, "usedPercent": 2},
                "secondary": {"windowMinutes": 10080, "usedPercent": 30},
            },
        },
    ]
)


def test_parse_usage_normalizes_windows():
    data = parse_usage(_CODEXBAR_JSON)
    assert [p.provider for p in data] == ["claude", "codex"]
    claude = data[0]
    assert [(w.label, w.used_percent) for w in claude.windows] == [("5h", 19), ("7d", 43)]
    assert claude.windows[0].resets_at == "2026-07-02T23:00:00Z"
    assert [(w.label, w.used_percent) for w in data[1].windows] == [("5h", 2), ("7d", 30)]


def test_parse_usage_tolerates_garbage():
    assert parse_usage("not json") == []
    assert parse_usage("{}") == []
    assert parse_usage('[{"provider": "x"}]') == []  # no usage dict
    assert parse_usage('[{"provider": "x", "usage": {"primary": {"windowMinutes": 5}}}]') == []


def test_window_labels():
    from herdeck.usage import _window_label

    assert _window_label(300) == "5h"
    assert _window_label(10080) == "7d"
    assert _window_label(45) == "45m"
    assert _window_label(None) == "?"


def test_usage_summary_gauges_keep_provider_identity_and_windows():
    gauges = usage_summary_gauges(parse_usage(_CODEXBAR_JSON))
    assert [(g.label, g.window, g.used_percent, g.color) for g in gauges] == [
        ("Claude", "5H", 19, "orange"),
        ("Claude", "7D", 43, "orange"),
        ("Codex", "5H", 2, "teal"),
        ("Codex", "7D", 30, "teal"),
    ]


def test_usage_detail_gauges_include_reset_hint():
    from datetime import datetime

    data = [ProviderUsage("claude", [UsageWindow("5h", 19, "2026-07-02T23:00:00Z")])]
    now = datetime(2026, 7, 2, 20, 0, tzinfo=UTC)
    (gauge,) = usage_detail_gauges(data, now=now)
    assert (gauge.label, gauge.window, gauge.used_percent) == ("Claude", "5H", 19)
    assert gauge.hint.startswith("reset ")
    (gauge_cs,) = usage_detail_gauges(data, now=now, lang="cs")
    assert gauge_cs.hint.startswith("obnova ")


def test_usage_detail_shows_the_pace_hint_only_with_a_projection():
    data = [
        ProviderUsage(
            "claude",
            [UsageWindow("5h", 60, None, full_early_s=40 * 60), UsageWindow("7d", 20, None)],
        )
    ]
    paced, plain = usage_detail_gauges(data)
    assert (paced.pace, plain.pace) == ("full ~40m early", "")
    assert usage_detail_gauges(data, lang="cs")[0].pace == "plno ~40m dřív"
    # The overview cards stay compact: no pace there.
    assert all(g.pace == "" for g in usage_summary_gauges(data))


def test_pace_hint_units_stay_short():
    from herdeck.layout import _fmt_early

    assert _fmt_early(30) == "1m"
    assert _fmt_early(59 * 60) == "59m"
    assert _fmt_early(3 * 3600 + 600) == "3h"
    assert _fmt_early(3 * 86400) == "3d"


def test_usage_summary_gauges_include_localized_reset_hint_when_available():
    from datetime import datetime

    data = [ProviderUsage("codex", [UsageWindow("7d", 38, "2026-07-19T18:59:11Z")])]
    now = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)

    (gauge,) = usage_summary_gauges(data, now=now, lang="cs")

    assert gauge.hint.startswith("obnova ")
    data[0].windows[0].resets_at = None
    assert usage_summary_gauges(data, now=now, lang="cs")[0].hint == ""


def test_usage_detail_gauges_page_through_all_windows():
    from herdeck.layout import usage_detail_pages

    data = [
        ProviderUsage("claude", [UsageWindow("5h", 1, None), UsageWindow("7d", 2, None)]),
        ProviderUsage("codex", [UsageWindow("5h", 3, None), UsageWindow("7d", 4, None)]),
    ]
    # 4 windows > the 3-line panel body: page 2 carries the rest — a silent
    # cap made e.g. Codex's weekly reset unobtainable on any surface.
    assert usage_detail_pages(data) == 2
    assert len(usage_detail_gauges(data, page=0)) == 3
    assert [(g.label, g.window) for g in usage_detail_gauges(data, page=1)] == [("Codex", "7D")]
    # clamped, never empty
    assert [(g.label, g.window) for g in usage_detail_gauges(data, page=99)] == [("Codex", "7D")]


class _Proc:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


class _Source:
    def __init__(self, usage=None):
        self.usage = usage
        self.closed = False

    def fetch(self):
        return self.usage

    def close(self):
        self.closed = True


def _poller(runner, **kw):
    kw.setdefault("codex_source", _Source())
    kw.setdefault("claude_reader", lambda _path: None)
    p = UsagePoller(["claude", "codex"], runner=runner, **kw)
    return p


def test_poller_fetches_and_snapshots(monkeypatch):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return _Proc(stdout=_CODEXBAR_JSON)

    monkeypatch.setattr("herdeck.usage.resolve_cli", lambda p: "/fake/codexbar")
    p = _poller(runner)
    p.poll_once()
    snap = p.snapshot()
    assert [x.provider for x in snap] == ["claude", "codex"]
    assert calls[0][1:] == ["usage", "--format", "json", "--provider", "claude,codex"]


def test_poller_prefers_native_sources_and_falls_back_only_for_missing(monkeypatch):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return _Proc(
            stdout=json.dumps(
                [
                    {
                        "provider": "claude",
                        "usage": {"primary": {"windowMinutes": 300, "usedPercent": 20}},
                    }
                ]
            )
        )

    monkeypatch.setattr("herdeck.usage.resolve_cli", lambda p: f"/fake/{p}")
    native = ProviderUsage("codex", [UsageWindow("7d", 31, None)])
    poller = _poller(runner, codex_source=_Source(native))
    poller.poll_once()

    assert calls[0][-1] == "claude"
    assert [(p.provider, p.windows[0].used_percent) for p in poller.snapshot()] == [
        ("claude", 20),
        ("codex", 31),
    ]


def test_poller_failure_keeps_last_snapshot_until_stale(monkeypatch):
    monkeypatch.setattr("herdeck.usage.resolve_cli", lambda p: "/fake/codexbar")
    t = {"now": 0.0}
    good = [_Proc(stdout=_CODEXBAR_JSON)]

    def runner(argv, **kwargs):
        if good:
            return good.pop()
        return _Proc(returncode=1, stderr="boom")

    p = _poller(runner, refresh_secs=60, clock=lambda: t["now"])
    p.poll_once()
    assert p.snapshot()
    p.poll_once()  # fails -> keeps previous data
    t["now"] = 100.0
    assert p.snapshot()
    t["now"] = 60.0 * 4 + 1  # past the staleness horizon
    assert p.snapshot() == []


def test_poller_missing_cli_is_quiet(monkeypatch, caplog):
    monkeypatch.setattr("herdeck.usage.resolve_cli", lambda p: None)
    p = _poller(lambda *a, **k: _Proc())
    p.poll_once()
    p.poll_once()
    assert p.snapshot() == []


def test_poller_thread_lifecycle(monkeypatch):
    monkeypatch.setattr("herdeck.usage.resolve_cli", lambda p: "/fake/codexbar")
    polled = threading.Event()

    def runner(argv, **kwargs):
        polled.set()
        return _Proc(stdout=_CODEXBAR_JSON)

    p = _poller(runner)
    p.start()
    assert polled.wait(2.0)
    p.close()
    assert p._thread is None


def test_poller_close_closes_codex_source():
    source = _Source()
    p = UsagePoller(["codex"], codex_source=source, codexbar_path="")
    p.close()
    assert source.closed


def test_poller_from_config_gates_on_providers():
    from herdeck.config import UsageConfig

    assert poller_from_config(UsageConfig()) is None
    assert poller_from_config(None) is None
    p = poller_from_config(UsageConfig(providers=["claude"], paid_only=True, refresh_secs=120))
    assert p is not None and p._providers == ["claude"]
    assert p._paid_only is True
    assert p._claude_cache_path == "~/.cache/herdeck/claude-usage.json"


def test_resolve_cli_explicit_path(tmp_path):
    exe = tmp_path / "codexbar"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert resolve_cli(str(exe)) == str(exe)
    assert resolve_cli(str(tmp_path / "missing")) is None


def test_parse_usage_drops_non_string_resets_at():
    raw = json.dumps(
        [
            {
                "provider": "claude",
                "usage": {"primary": {"windowMinutes": 300, "usedPercent": 5, "resetsAt": 123}},
            }
        ]
    )
    (p,) = parse_usage(raw)
    assert p.windows[0].resets_at is None  # malformed reset must not crash rendering


def test_poller_orders_providers_by_config(monkeypatch):
    # The CLI returns entries in its own order (codex first for claude,codex);
    # the panel must follow the user's configured order.
    monkeypatch.setattr("herdeck.usage.resolve_cli", lambda p: "/fake/codexbar")
    reversed_json = json.dumps(
        [
            {"provider": "codex", "usage": {"primary": {"windowMinutes": 300, "usedPercent": 1}}},
            {"provider": "claude", "usage": {"primary": {"windowMinutes": 300, "usedPercent": 2}}},
        ]
    )
    p = UsagePoller(["claude", "codex"], runner=lambda *a, **k: _Proc(stdout=reversed_json))
    p.poll_once()
    assert [x.provider for x in p.snapshot()] == ["claude", "codex"]


def test_parse_codex_app_server_rate_limits():
    usage = parse_codex_rate_limits(
        {
            "id": 7,
            "result": {
                "rateLimits": {
                    "primary": {
                        "usedPercent": 12.6,
                        "windowDurationMins": 300,
                        "resetsAt": 1784487551,
                    },
                    "secondary": {
                        "usedPercent": 29,
                        "windowDurationMins": 10080,
                        "resetsAt": 1784543782,
                    },
                }
            },
        }
    )
    assert usage is not None
    assert [(w.label, w.used_percent) for w in usage.windows] == [("5h", 13), ("7d", 29)]
    assert usage.windows[0].resets_at == "2026-07-19T18:59:11Z"


def test_parse_codex_account_distinguishes_paid_free_and_api_key():
    assert parse_codex_account({"result": {"account": {"type": "chatgpt", "planType": "pro"}}}) == (
        "paid",
        "pro",
    )
    assert parse_codex_account({"result": {"account": {"type": "chatgpt", "planType": "go"}}}) == (
        "paid",
        "go",
    )
    assert parse_codex_account(
        {"result": {"account": {"type": "chatgpt", "planType": "free"}}}
    ) == ("free", "free")
    assert parse_codex_account({"result": {"account": {"type": "apiKey"}}}) == (
        "unknown",
        None,
    )
    assert parse_codex_account(
        {"result": {"account": {"type": "chatgpt", "planType": "future-sentinel"}}}
    ) == ("unknown", "future-sentinel")


# Trimmed real `codexbar usage --format json --provider claude` / `codex` output
# (CodexBar 2026-09, email and pace/cost blocks removed).
_CODEXBAR_PAID_JSON = json.dumps(
    [
        {
            "provider": "claude",
            "source": "web",
            "rateWindowLabels": {"secondary": "Weekly", "primary": "Session"},
            "usage": {
                "loginMethod": "Claude Max 20x",
                "primary": {
                    "resetsAt": "2026-09-23T12:10:00Z",
                    "windowMinutes": 300,
                    "usedPercent": 14,
                },
                "secondary": {
                    "resetsAt": "2026-09-28T13:00:00Z",
                    "windowMinutes": 10080,
                    "usedPercent": 17,
                },
                "tertiary": None,
                "identity": {"loginMethod": "Claude Max 20x", "providerID": "claude"},
                "extraRateWindows": [
                    {
                        "title": "Fable only",
                        "id": "claude-weekly-scoped-fable",
                        "window": {
                            "windowMinutes": 10080,
                            "resetsAt": "2026-09-28T13:00:00Z",
                            "usedPercent": 0,
                        },
                    }
                ],
            },
        },
        {
            "provider": "codex",
            "source": "oauth",
            "rateWindowLabels": {"secondary": "Weekly"},
            "usage": {
                "loginMethod": "pro",
                "primary": None,
                "secondary": {
                    "usedPercent": 100,
                    "windowMinutes": 10080,
                    "resetsAt": "2026-09-26T08:45:06Z",
                },
                "tertiary": None,
                "identity": {"loginMethod": "pro", "providerID": "codex"},
            },
        },
    ]
)


def _codexbar_entry(provider, login_method=None, *, identity_only=False):
    usage = {"secondary": {"windowMinutes": 10080, "usedPercent": 5}}
    if login_method is not None:
        usage["identity"] = {"loginMethod": login_method}
        if not identity_only:
            usage["loginMethod"] = login_method
    return {"provider": provider, "usage": usage}


def test_parse_usage_derives_paid_subscription_from_login_method():
    data = {p.provider: p for p in parse_usage(_CODEXBAR_PAID_JSON)}
    assert (data["claude"].subscription, data["claude"].plan) == ("paid", "max 20x")
    assert [(w.label, w.used_percent) for w in data["claude"].windows] == [("5h", 14), ("7d", 17)]
    assert (data["codex"].subscription, data["codex"].plan) == ("paid", "pro")
    assert [(w.label, w.used_percent) for w in data["codex"].windows] == [("7d", 100)]


def test_parse_usage_login_method_classification():
    cases = [
        (_codexbar_entry("claude", "Claude Pro"), "paid"),
        (_codexbar_entry("claude", "Claude Team", identity_only=True), "paid"),
        (_codexbar_entry("claude", "claude enterprise"), "paid"),
        (_codexbar_entry("claude", "Max"), "paid"),
        (_codexbar_entry("claude", "Claude Free"), "free"),
        (_codexbar_entry("claude", "Claude Maximal Sentinel"), "unknown"),
        (_codexbar_entry("claude"), "unknown"),
        (_codexbar_entry("codex", "Plus"), "paid"),
        (_codexbar_entry("codex", "ChatGPT Team"), "paid"),
        (_codexbar_entry("codex", "free"), "free"),
        (_codexbar_entry("codex", "future-sentinel"), "unknown"),
        (_codexbar_entry("gemini", "Pro"), "unknown"),  # no known plan vocabulary
    ]
    for entry, expected in cases:
        (usage,) = parse_usage(json.dumps([entry]))
        assert usage.subscription == expected, entry


def test_paid_only_uses_codexbar_fallback_and_keeps_only_paid(monkeypatch):
    calls = []
    monkeypatch.setattr("herdeck.usage.resolve_cli", lambda p: "/fake/codexbar")
    stdout = json.dumps(
        [_codexbar_entry("claude", "Claude Max 20x"), _codexbar_entry("codex", "mystery")]
    )
    poller = UsagePoller(
        ["claude", "codex"],
        paid_only=True,
        codex_source=_Source(None),
        claude_reader=lambda _path: None,
        runner=lambda argv, **kwargs: calls.append(argv) or _Proc(stdout=stdout),
    )

    poller.poll_once()

    assert calls[0][-1] == "claude,codex"
    assert [usage.provider for usage in poller.snapshot()] == ["claude"]
    # An unconfirmed fallback result is not cached at all in paid-only mode.
    assert "codex" not in poller._data


def test_paid_only_skips_codexbar_when_native_sources_cover_everything(monkeypatch):
    calls = []
    monkeypatch.setattr("herdeck.usage.resolve_cli", lambda p: "/fake/codexbar")
    paid_codex = ProviderUsage("codex", [UsageWindow("5h", 12, None)], "paid", "pro")
    paid_claude = ProviderUsage("claude", [UsageWindow("7d", 3, None)], "paid")
    poller = UsagePoller(
        ["claude", "codex"],
        paid_only=True,
        codex_source=_Source(paid_codex),
        claude_reader=lambda _path: paid_claude,
        runner=lambda *args, **kwargs: calls.append(args) or _Proc(stdout=_CODEXBAR_JSON),
    )

    poller.poll_once()

    assert [usage.provider for usage in poller.snapshot()] == ["claude", "codex"]
    assert calls == []


def test_paid_only_falls_back_only_for_missing_provider(monkeypatch):
    calls = []
    monkeypatch.setattr("herdeck.usage.resolve_cli", lambda p: "/fake/codexbar")
    paid = ProviderUsage("codex", [UsageWindow("5h", 12, None)], "paid", "pro")
    poller = UsagePoller(
        ["claude", "codex"],
        paid_only=True,
        codex_source=_Source(paid),
        claude_reader=lambda _path: None,
        # Legacy CodexBar output without loginMethod: no positive paid signal.
        runner=lambda argv, **kwargs: calls.append(argv) or _Proc(stdout=_CODEXBAR_JSON),
    )

    poller.poll_once()

    assert calls[0][-1] == "claude"
    assert [usage.provider for usage in poller.snapshot()] == ["codex"]


def test_paid_only_hides_free_native_subscription():
    free = ProviderUsage("codex", [UsageWindow("5h", 12, None)], "free", "free")
    poller = UsagePoller(["codex"], paid_only=True, codex_source=_Source(free), codexbar_path="")
    poller.poll_once()
    assert poller.snapshot() == []


def _alert_poller(source, alerts, wall, **kw):
    return UsagePoller(
        ["codex"],
        codex_source=source,
        codexbar_path="",
        on_alert=alerts.extend,
        wall_clock=lambda: wall["now"],
        **kw,
    )


def test_poller_sends_threshold_alerts_after_the_baseline():
    source = _Source(ProviderUsage("codex", [UsageWindow("5h", 70, None)]))
    alerts: list = []
    wall = {"now": 1_800_000_000.0}
    poller = _alert_poller(source, alerts, wall, alert_at=[80])
    poller.poll_once()
    assert alerts == []
    source.usage = ProviderUsage("codex", [UsageWindow("5h", 85, None)])
    wall["now"] += 300
    poller.poll_once()
    assert [(a.kind, a.provider, a.percent) for a in alerts] == [("threshold", "codex", 80)]


def test_poller_alert_callback_failure_keeps_polling():
    source = _Source(ProviderUsage("codex", [UsageWindow("5h", 70, None)]))
    wall = {"now": 1_800_000_000.0}

    def boom(_alerts):
        raise RuntimeError("sink down")

    poller = UsagePoller(
        ["codex"],
        codex_source=source,
        codexbar_path="",
        alert_at=[80],
        on_alert=boom,
        wall_clock=lambda: wall["now"],
    )
    poller.poll_once()
    source.usage = ProviderUsage("codex", [UsageWindow("5h", 90, None)])
    poller.poll_once()
    assert poller.snapshot()[0].windows[0].used_percent == 90


def test_poller_paid_only_mutes_alerts_for_hidden_providers():
    free = lambda used: ProviderUsage("codex", [UsageWindow("5h", used, None)], "free")  # noqa: E731
    source = _Source(free(70))
    alerts: list = []
    wall = {"now": 1_800_000_000.0}
    poller = _alert_poller(source, alerts, wall, alert_at=[80], paid_only=True)
    poller.poll_once()
    source.usage = free(90)
    poller.poll_once()
    assert alerts == []


def test_poller_snapshot_carries_the_pace_projection():
    from datetime import datetime

    t0 = 1_800_000_000.0
    resets = datetime.fromtimestamp(t0 + 3 * 3600, tz=UTC).isoformat()
    source = _Source(ProviderUsage("codex", [UsageWindow("5h", 10, resets)]))
    wall = {"now": t0}
    poller = _alert_poller(source, [], wall)
    poller.poll_once()
    source.usage = ProviderUsage("codex", [UsageWindow("5h", 30, resets)])
    wall["now"] = t0 + 1200
    poller.poll_once()
    assert poller.snapshot()[0].windows[0].full_early_s == 90 * 60


def test_poller_from_config_passes_alert_settings():
    from herdeck.config import UsageConfig

    sent = []
    p = poller_from_config(
        UsageConfig(providers=["codex"], alert_at=[80, 95], alert_reset=True),
        on_alert=sent.append,
    )
    assert p._tracker._levels == [80, 95]
    assert p._tracker._alert_reset is True
    assert p._on_alert == sent.append


def test_codex_app_server_source_handshakes_and_reads_limits(monkeypatch):
    class FakeAppServer:
        def __init__(self):
            self.stdin = io.StringIO()
            self.stdout = io.StringIO(
                '{"id":0,"result":{"codexHome":"/tmp"}}\n'
                '{"method":"account/updated","params":{}}\n'
                '{"id":1,"result":{"account":{"type":"chatgpt","planType":"pro"}}}\n'
                '{"id":2,"result":{"rateLimits":{"primary":'
                '{"usedPercent":9,"windowDurationMins":300,"resetsAt":1784487551}}}}\n'
            )
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    proc = FakeAppServer()
    argv = []

    def popen(command, **kwargs):
        argv.extend(command)
        return proc

    monkeypatch.setattr("herdeck.usage.resolve_cli", lambda p: "/opt/homebrew/bin/codex")
    source = CodexAppServerSource(popen=popen)
    usage = source.fetch()

    assert argv == ["/opt/homebrew/bin/codex", "app-server"]
    assert usage is not None and usage.windows[0].used_percent == 9
    assert (usage.subscription, usage.plan) == ("paid", "pro")
    sent = [json.loads(line) for line in proc.stdin.getvalue().splitlines()]
    assert [message["method"] for message in sent] == [
        "initialize",
        "initialized",
        "account/read",
        "account/rateLimits/read",
    ]
    account_read = next(message for message in sent if message["method"] == "account/read")
    assert account_read["params"] == {"refreshToken": True}
    source.close()


def test_codex_app_server_reader_handles_coalesced_pipe_responses(monkeypatch):
    read_fd, write_fd = os.pipe()

    class PipeAppServer:
        def __init__(self):
            self.stdin = io.StringIO()
            self.stdout = os.fdopen(read_fd, encoding="utf-8")
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0
            self.stdout.close()

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    proc = PipeAppServer()
    # One OS write deliberately coalesces the initialize response, a
    # notification, and the later rate-limit response. A select()+readline()
    # client can strand the final line in TextIOWrapper's private buffer.
    os.write(
        write_fd,
        b'{"id":0,"result":{"codexHome":"/tmp"}}\n'
        b'{"method":"account/updated","params":{}}\n'
        b'{"id":1,"result":{"account":{"type":"chatgpt","planType":"plus"}}}\n'
        b'{"id":2,"result":{"rateLimits":{"primary":'
        b'{"usedPercent":8,"windowDurationMins":300,"resetsAt":1784487551}}}}\n',
    )
    os.close(write_fd)
    monkeypatch.setattr("herdeck.usage.resolve_cli", lambda p: "/opt/homebrew/bin/codex")
    source = CodexAppServerSource(popen=lambda *args, **kwargs: proc)

    usage = source.fetch()

    assert usage is not None and usage.windows[0].used_percent == 8
    assert usage.subscription == "paid"
    source.close()


def test_codex_app_server_keeps_limits_when_account_read_fails(monkeypatch):
    class FakeAppServer:
        def __init__(self):
            self.stdin = io.StringIO()
            self.stdout = io.StringIO(
                '{"id":0,"result":{"codexHome":"/tmp"}}\n'
                '{"id":1,"error":{"code":-32601,"message":"unknown method"}}\n'
                '{"id":2,"result":{"rateLimits":{"primary":'
                '{"usedPercent":7,"windowDurationMins":300,"resetsAt":1784487551}}}}\n'
            )
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr("herdeck.usage.resolve_cli", lambda p: "/opt/homebrew/bin/codex")
    source = CodexAppServerSource(popen=lambda *args, **kwargs: FakeAppServer())

    usage = source.fetch()

    assert usage is not None and usage.windows[0].used_percent == 7
    assert usage.subscription == "unknown"
    source.close()


def test_codex_app_server_reader_cannot_write_into_replacement_session():
    old_messages = queue.Queue()
    replacement_messages = queue.Queue()
    source = CodexAppServerSource()
    source._messages = replacement_messages
    proc = type("Proc", (), {"stdout": io.StringIO('{"id":1,"result":{}}\n')})()

    source._read_stdout(proc, old_messages)

    assert old_messages.get_nowait()["id"] == 1
    assert old_messages.get_nowait() is None
    assert replacement_messages.empty()


_CLAUDE_STATUSLINE_JSON = json.dumps(
    {
        "model": {"display_name": "Sonnet"},
        "rate_limits": {
            "five_hour": {"used_percentage": 17.4, "resets_at": 1784487551},
            "seven_day": {"used_percentage": 44, "resets_at": 1784543782},
        },
    }
)


def test_parse_claude_statusline_subscription_windows():
    usage = parse_claude_statusline(_CLAUDE_STATUSLINE_JSON)
    assert usage is not None
    assert [(w.label, w.used_percent) for w in usage.windows] == [("5h", 17), ("7d", 44)]
    assert usage.subscription == "paid"


def test_capture_claude_statusline_writes_minimal_private_cache(tmp_path):
    target = tmp_path / "nested" / "usage.json"
    assert capture_claude_statusline(_CLAUDE_STATUSLINE_JSON, str(target), wall_clock=lambda: 10)
    stored = json.loads(target.read_text())

    assert stored.keys() == {"captured_at", "rate_limits"}
    assert stored["captured_at"] == 10
    assert not target.stat().st_mode & 0o077


def test_read_claude_cache_rejects_stale_or_invalid_snapshot(tmp_path):
    target = tmp_path / "usage.json"
    capture_claude_statusline(_CLAUDE_STATUSLINE_JSON, str(target), wall_clock=lambda: 10)
    assert read_claude_cache(str(target), wall_clock=lambda: 20, max_age_s=15) is not None
    assert read_claude_cache(str(target), wall_clock=lambda: 30, max_age_s=15) is None
    target.write_text("not json")
    assert read_claude_cache(str(target)) is None


class _ScriptedAppServer:
    """Fake ``codex app-server`` that answers each request as it is written.

    ``delays`` maps a method name to seconds to wait before answering, and
    ``preamble`` lines are emitted before every response (notifications and
    server-initiated requests that a newer codex interleaves).
    """

    def __init__(self, delays=None, preamble=()):
        self._out: queue.Queue[str | None] = queue.Queue()
        self._delays = delays or {}
        self._preamble = list(preamble)
        self.returncode = None
        self.terminated = False
        self.stdout = iter(self._out.get, None)
        server = self

        class _Stdin:
            def write(self, line):
                server._handle(json.loads(line))

            def flush(self):
                pass

        self.stdin = _Stdin()

    def _handle(self, message):
        if "id" not in message:
            return
        method = message["method"]
        results = {
            "initialize": {"codexHome": "/tmp"},
            "account/read": {"account": {"type": "chatgpt", "planType": "pro"}},
            "account/rateLimits/read": {
                "rateLimits": {
                    "secondary": {
                        "usedPercent": 100,
                        "windowDurationMins": 10080,
                        "resetsAt": 1784487551,
                    }
                }
            },
        }
        lines = [*self._preamble, json.dumps({"id": message["id"], "result": results[method]})]

        def emit():
            for line in lines:
                self._out.put(line + "\n")

        delay = self._delays.get(method, 0)
        if delay:
            threading.Timer(delay, emit).start()
        else:
            emit()

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0
        self._out.put(None)

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9
        self._out.put(None)


def test_codex_app_server_start_uses_longer_handshake_timeout(monkeypatch):
    # A cold codex (plus SSH for a thin-client wrapper) can take far longer to
    # answer `initialize` than later requests take.
    monkeypatch.setattr("herdeck.usage._APP_SERVER_TIMEOUT_S", 0.05)
    monkeypatch.setattr("herdeck.usage._APP_SERVER_START_TIMEOUT_S", 2.0)
    monkeypatch.setattr("herdeck.usage.resolve_cli", lambda p: "/fake/codex")
    proc = _ScriptedAppServer(delays={"initialize": 0.3})
    source = CodexAppServerSource(popen=lambda *a, **k: proc)

    usage = source.fetch()

    assert usage is not None and usage.windows[0].used_percent == 100
    source.close()


def test_codex_app_server_later_requests_keep_short_timeout(monkeypatch):
    monkeypatch.setattr("herdeck.usage._APP_SERVER_TIMEOUT_S", 0.05)
    monkeypatch.setattr("herdeck.usage._APP_SERVER_START_TIMEOUT_S", 2.0)
    monkeypatch.setattr("herdeck.usage.resolve_cli", lambda p: "/fake/codex")
    proc = _ScriptedAppServer(delays={"account/rateLimits/read": 0.3})
    source = CodexAppServerSource(popen=lambda *a, **k: proc)

    assert source.fetch() is None
    assert proc.terminated  # the timed-out session is torn down, not leaked
    assert source._proc is None


def test_codex_app_server_start_timeout_does_not_leak_process(monkeypatch):
    monkeypatch.setattr("herdeck.usage._APP_SERVER_START_TIMEOUT_S", 0.05)
    monkeypatch.setattr("herdeck.usage.resolve_cli", lambda p: "/fake/codex")
    procs = []

    def popen(*args, **kwargs):
        procs.append(_ScriptedAppServer(delays={"initialize": 0.5}))
        return procs[-1]

    source = CodexAppServerSource(popen=popen)
    assert source.fetch() is None
    assert source.fetch() is None

    assert len(procs) == 2
    assert all(proc.terminated for proc in procs)
    assert source._proc is None


def test_codex_app_server_process_is_reused_across_polls(monkeypatch):
    monkeypatch.setattr("herdeck.usage.resolve_cli", lambda p: "/fake/codex")
    procs = []

    def popen(*args, **kwargs):
        procs.append(_ScriptedAppServer())
        return procs[-1]

    source = CodexAppServerSource(popen=popen)
    first = source.fetch()
    second = source.fetch()

    assert first is not None and second is not None
    assert len(procs) == 1 and not procs[0].terminated
    source.close()
    assert procs[0].terminated


def test_codex_app_server_skips_notifications_and_server_requests(monkeypatch):
    monkeypatch.setattr("herdeck.usage.resolve_cli", lambda p: "/fake/codex")
    # Newer codex emits notifications before responses, and a server-initiated
    # request carries its own `id` that can collide with ours.
    preamble = [
        '{"method":"remoteControl/status/changed","params":{"status":"disabled"}}',
        '{"id":1,"method":"item/tool/requestUserInput","params":{}}',
        '{"id":2,"method":"item/tool/requestUserInput","params":{}}',
        '["not", "an", "object"]',
        '"id"',
    ]
    proc = _ScriptedAppServer(preamble=preamble)
    source = CodexAppServerSource(popen=lambda *a, **k: proc)

    usage = source.fetch()

    assert usage is not None and usage.windows[0].used_percent == 100
    assert (usage.subscription, usage.plan) == ("paid", "pro")
    source.close()
