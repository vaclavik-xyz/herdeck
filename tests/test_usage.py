import json
import threading
from datetime import UTC

from herdeck.layout import usage_detail_lines, usage_summary_lines
from herdeck.usage import (
    ProviderUsage,
    UsagePoller,
    UsageWindow,
    parse_usage,
    poller_from_config,
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


def test_usage_summary_lines_are_compact():
    data = parse_usage(_CODEXBAR_JSON)
    assert usage_summary_lines(data) == [
        "Claude 5h 19% · 7d 43%",
        "Codex 5h 2% · 7d 30%",
    ]


def test_usage_detail_lines_carry_reset_times():
    from datetime import datetime

    data = [
        ProviderUsage(
            "claude",
            [UsageWindow("5h", 19, "2026-07-02T23:00:00Z")],
        )
    ]
    now = datetime(2026, 7, 2, 20, 0, tzinfo=UTC)
    (line,) = usage_detail_lines(data, now=now)
    assert line.startswith("Claude 5h 19% → ")  # reset rendered in local time
    data[0].windows[0].resets_at = None
    (line,) = usage_detail_lines(data, now=now)
    assert line == "Claude 5h 19%"  # no reset -> no dangling arrow


def test_usage_detail_lines_cap_at_panel_size():
    data = [
        ProviderUsage("claude", [UsageWindow("5h", 1, None), UsageWindow("7d", 2, None)]),
        ProviderUsage("codex", [UsageWindow("5h", 3, None), UsageWindow("7d", 4, None)]),
    ]
    assert len(usage_detail_lines(data)) == 3  # panel body holds 3 lines


class _Proc:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _poller(runner, **kw):
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


def test_poller_from_config_gates_on_providers():
    from herdeck.config import UsageConfig

    assert poller_from_config(UsageConfig()) is None
    assert poller_from_config(None) is None
    p = poller_from_config(UsageConfig(providers=["claude"], refresh_secs=120))
    assert p is not None and p._providers == ["claude"]


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
