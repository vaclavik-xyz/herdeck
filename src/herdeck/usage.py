"""Provider usage snapshots for the deck status panel.

Codex is read through the documented ``codex app-server`` account API. Claude
Code publishes subscription limits to its status-line JSON; ``herdeck-usage
capture-claude`` stores only that small rate-limit snapshot for this poller.
CodexBar remains an optional compatibility fallback for missing providers.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import __version__

log = logging.getLogger(__name__)

_CLI_TIMEOUT_S = 120.0
# Per-request deadline once the app-server session is up.
_APP_SERVER_TIMEOUT_S = 15.0
# Deadline for spawning ``codex app-server`` and answering ``initialize``. A
# cold codex-cli 0.155 needs 13-18 s for the handshake alone, and a thin-client
# ``codex_path`` wrapper that runs codex over SSH adds its connection setup on
# top. The session is kept alive across polls, so only the first start pays it.
_APP_SERVER_START_TIMEOUT_S = 60.0
_STALE_REFRESHES = 4
_CLAUDE_CACHE_MAX_AGE_S = 6 * 60 * 60
_FALLBACK_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")
_PAID_CODEX_PLANS = {
    "business",
    "edu",
    "education",
    "enterprise",
    "go",
    "plus",
    "pro",
    "prolite",
    "team",
}
# Claude subscription tiers that CodexBar reports as ``loginMethod`` (e.g.
# "Claude Max 20x"); matched on the first word after an optional "claude".
_PAID_CLAUDE_PLANS = {"enterprise", "max", "pro", "team"}
# Product prefixes CodexBar may put in front of the tier name.
_LOGIN_METHOD_PREFIXES = {"claude": "claude", "codex": "chatgpt"}


@dataclass
class UsageWindow:
    label: str
    used_percent: int
    resets_at: str | None
    # Pace projection (usage_alerts.UsageTracker): seconds by which the
    # recent burn rate fills this window BEFORE it resets; None = no signal
    # or no early fill.
    full_early_s: int | None = None


@dataclass
class ProviderUsage:
    provider: str
    windows: list[UsageWindow] = field(default_factory=list)
    subscription: str = "unknown"
    plan: str | None = None


def _subscription_from_plan(plan) -> tuple[str, str | None]:
    """Classify a provider-reported subscription tier conservatively."""
    if not isinstance(plan, str) or not plan.strip():
        return "unknown", None
    normalized = plan.strip().lower()
    if normalized == "free":
        return "free", normalized
    if normalized in _PAID_CODEX_PLANS:
        return "paid", normalized
    return "unknown", normalized


def _subscription_from_login_method(provider: str, login_method) -> tuple[str, str | None]:
    """Classify a CodexBar ``loginMethod`` such as "Claude Max 20x" or "pro".

    Only Claude and Codex have a known plan vocabulary; anything else (and any
    unrecognised tier) stays "unknown" so paid-only mode never guesses.
    """
    if not isinstance(login_method, str) or not login_method.strip():
        return "unknown", None
    normalized = " ".join(login_method.strip().lower().split())
    prefix = _LOGIN_METHOD_PREFIXES.get(provider)
    if prefix and normalized.startswith(prefix + " "):
        normalized = normalized[len(prefix) + 1 :]
    if provider == "claude":
        tier = normalized.split(" ", 1)[0]
        if tier == "free":
            return "free", normalized
        if tier in _PAID_CLAUDE_PLANS:
            return "paid", normalized
        return "unknown", normalized
    if provider == "codex":
        return _subscription_from_plan(normalized)
    return "unknown", normalized


def _window_label(minutes) -> str:
    try:
        value = int(minutes)
    except (TypeError, ValueError):
        return "?"
    if value >= 1440:
        return f"{round(value / 1440)}d"
    if value >= 60:
        return f"{round(value / 60)}h"
    return f"{value}m"


def _iso_reset(value, *, allow_epoch: bool = True) -> str | None:
    if isinstance(value, str):
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return value
    if allow_epoch and isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, tz=UTC).isoformat().replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _parse_window(
    raw,
    *,
    minutes_key: str,
    percent_key: str,
    reset_key: str,
    allow_epoch_reset: bool = True,
):
    if not isinstance(raw, dict) or percent_key not in raw:
        return None
    try:
        used = round(float(raw[percent_key]))
    except (TypeError, ValueError):
        return None
    return UsageWindow(
        label=_window_label(raw.get(minutes_key)),
        used_percent=max(0, min(100, used)),
        resets_at=_iso_reset(raw.get(reset_key), allow_epoch=allow_epoch_reset),
    )


def parse_usage(raw: str) -> list[ProviderUsage]:
    """Normalize CodexBar JSON for the compatibility fallback.

    The subscription tier comes from ``usage.loginMethod`` (or
    ``usage.identity.loginMethod``), e.g. "Claude Max 20x" or "pro".
    """
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(entries, list):
        return []
    out: list[ProviderUsage] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        usage = entry.get("usage")
        provider = entry.get("provider")
        if not isinstance(usage, dict) or not isinstance(provider, str):
            continue
        windows = [
            parsed
            for slot in ("primary", "secondary", "tertiary")
            if (
                parsed := _parse_window(
                    usage.get(slot),
                    minutes_key="windowMinutes",
                    percent_key="usedPercent",
                    reset_key="resetsAt",
                    allow_epoch_reset=False,
                )
            )
            is not None
        ]
        if windows:
            identity = usage.get("identity")
            login_method = usage.get("loginMethod")
            if not isinstance(login_method, str) and isinstance(identity, dict):
                login_method = identity.get("loginMethod")
            subscription, plan = _subscription_from_login_method(provider, login_method)
            out.append(ProviderUsage(provider, windows, subscription, plan))
    return out


def parse_codex_account(message: dict) -> tuple[str, str | None]:
    """Read the ChatGPT subscription tier from ``account/read``."""
    result = message.get("result") if isinstance(message, dict) else None
    account = result.get("account") if isinstance(result, dict) else None
    if not isinstance(account, dict) or account.get("type") != "chatgpt":
        return "unknown", None
    return _subscription_from_plan(account.get("planType"))


def parse_codex_rate_limits(
    message: dict,
    *,
    subscription: str = "unknown",
    plan: str | None = None,
) -> ProviderUsage | None:
    """Normalize an ``account/rateLimits/read`` app-server response."""
    result = message.get("result") if isinstance(message, dict) else None
    limits = result.get("rateLimits") if isinstance(result, dict) else None
    if not isinstance(limits, dict):
        return None
    limit_subscription, limit_plan = _subscription_from_plan(limits.get("planType"))
    if limit_subscription != "unknown":
        subscription, plan = limit_subscription, limit_plan
    windows = [
        parsed
        for slot in ("primary", "secondary")
        if (
            parsed := _parse_window(
                limits.get(slot),
                minutes_key="windowDurationMins",
                percent_key="usedPercent",
                reset_key="resetsAt",
            )
        )
        is not None
    ]
    return ProviderUsage("codex", windows, subscription, plan) if windows else None


def parse_claude_statusline(raw: str) -> ProviderUsage | None:
    """Normalize the official Claude Code status-line ``rate_limits`` object."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    limits = payload.get("rate_limits") if isinstance(payload, dict) else None
    if not isinstance(limits, dict):
        return None
    specs = (("five_hour", 300), ("seven_day", 10080))
    windows: list[UsageWindow] = []
    for key, minutes in specs:
        source = limits.get(key)
        if not isinstance(source, dict):
            continue
        source = {**source, "window_minutes": minutes}
        parsed = _parse_window(
            source,
            minutes_key="window_minutes",
            percent_key="used_percentage",
            reset_key="resets_at",
        )
        if parsed is not None:
            windows.append(parsed)
    # Claude documents rate_limits as subscriber-only data. Its absence is
    # inconclusive (the field appears only after the first API response), but
    # its presence is a positive paid-subscription signal.
    return ProviderUsage("claude", windows, "paid") if windows else None


def capture_claude_statusline(
    raw: str,
    path: str,
    *,
    wall_clock=time.time,
) -> bool:
    """Atomically store only Claude rate limits from one status-line payload."""
    usage = parse_claude_statusline(raw)
    if usage is None:
        return False
    target = Path(os.path.expanduser(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "captured_at": wall_clock(),
        "rate_limits": json.loads(raw).get("rate_limits"),
    }
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()
    return True


def read_claude_cache(
    path: str,
    *,
    wall_clock=time.time,
    max_age_s: float = _CLAUDE_CACHE_MAX_AGE_S,
) -> ProviderUsage | None:
    """Read a fresh Claude status-line snapshot without retaining other session data."""
    try:
        payload = json.loads(Path(os.path.expanduser(path)).read_text(encoding="utf-8"))
        captured_at = float(payload["captured_at"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if wall_clock() - captured_at > max_age_s:
        return None
    return parse_claude_statusline(json.dumps(payload))


def resolve_cli(path: str) -> str | None:
    """Resolve an executable from an explicit path, PATH, or Homebrew dirs."""
    if os.path.sep in path:
        expanded = os.path.expanduser(path)
        return expanded if os.access(expanded, os.X_OK) else None
    found = shutil.which(path)
    if found:
        return found
    for directory in _FALLBACK_DIRS:
        candidate = os.path.join(directory, path)
        if os.access(candidate, os.X_OK):
            return candidate
    return None


class CodexAppServerSource:
    """Small persistent JSON-RPC client for Codex account rate limits."""

    def __init__(
        self,
        path: str = "codex",
        *,
        popen=subprocess.Popen,
    ):
        self._path = path
        self._popen = popen
        self._proc = None
        self._next_id = 1
        self._messages: queue.Queue[dict | None] = queue.Queue()
        self._reader_thread: threading.Thread | None = None

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=1.0)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=1.0)
            except Exception:
                pass
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=1.0)
            self._reader_thread = None

    def fetch(self) -> ProviderUsage | None:
        try:
            self._ensure_started()
            account_id = self._next_id
            self._next_id += 1
            self._send(
                {"method": "account/read", "id": account_id, "params": {"refreshToken": True}}
            )
            try:
                subscription, plan = parse_codex_account(self._read_response(account_id))
            except Exception:
                # Entitlement discovery is additive. Older or temporarily
                # degraded app-servers must still provide usage in the default
                # (non-paid-only) mode.
                subscription, plan = "unknown", None
                log.debug("Codex account subscription read failed", exc_info=True)
            limits_id = self._next_id
            self._next_id += 1
            self._send({"method": "account/rateLimits/read", "id": limits_id, "params": {}})
            return parse_codex_rate_limits(
                self._read_response(limits_id), subscription=subscription, plan=plan
            )
        except Exception:
            self.close()
            log.warning("Codex app-server usage poll failed", exc_info=True)
            return None

    def _ensure_started(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        self.close()
        cli = resolve_cli(self._path)
        if cli is None:
            raise FileNotFoundError(self._path)
        self._proc = self._popen(
            [cli, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        messages: queue.Queue[dict | None] = queue.Queue()
        self._messages = messages
        self._reader_thread = threading.Thread(
            target=self._read_stdout,
            args=(self._proc, messages),
            name="herdeck-codex-app-server-reader",
            daemon=True,
        )
        self._reader_thread.start()
        self._send(
            {
                "method": "initialize",
                "id": 0,
                "params": {
                    "clientInfo": {
                        "name": "herdeck",
                        "title": "Herdeck",
                        "version": __version__,
                    }
                },
            }
        )
        self._read_response(0, timeout=_APP_SERVER_START_TIMEOUT_S)
        self._send({"method": "initialized", "params": {}})

    def _send(self, message: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("Codex app-server is not running")
        self._proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self._proc.stdin.flush()

    def _read_response(self, request_id: int, timeout: float | None = None) -> dict:
        if self._proc is None:
            raise RuntimeError("Codex app-server is not running")
        deadline = time.monotonic() + (_APP_SERVER_TIMEOUT_S if timeout is None else timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Codex app-server request {request_id} timed out")
            try:
                message = self._messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError(f"Codex app-server request {request_id} timed out") from exc
            if message is None:
                raise RuntimeError("Codex app-server closed its output")
            # Server-initiated requests carry their own ``id`` (and a
            # ``method``); only a response to our request id counts.
            if "method" in message or message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError(str(message["error"]))
            return message

    @staticmethod
    def _read_stdout(proc, messages: queue.Queue[dict | None]) -> None:
        if proc is None or proc.stdout is None:
            messages.put(None)
            return
        try:
            for line in proc.stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Notifications (e.g. ``remoteControl/status/changed``) have no
                # id and are irrelevant to this polling client. Drop them, and
                # any non-object line, so an idle daemon cannot grow the queue.
                if isinstance(message, dict) and "id" in message:
                    messages.put(message)
        finally:
            messages.put(None)


class UsagePoller:
    """Merge native provider sources, with CodexBar as a compatibility fallback."""

    def __init__(
        self,
        providers: list[str],
        paid_only: bool = False,
        refresh_secs: float = 300.0,
        codex_path: str = "codex",
        claude_cache_path: str = "~/.cache/herdeck/claude-usage.json",
        codexbar_path: str = "codexbar",
        *,
        runner=subprocess.run,
        clock=time.monotonic,
        codex_source=None,
        claude_reader=read_claude_cache,
        alert_at=(),
        alert_reset: bool = False,
        on_alert=None,
        wall_clock=time.time,
    ):
        from .usage_alerts import UsageTracker

        self._providers = list(providers)
        self._paid_only = paid_only
        self._refresh = max(30.0, float(refresh_secs))
        self._codexbar_path = codexbar_path
        self._runner = runner
        self._clock = clock
        self._codex_source = codex_source or CodexAppServerSource(codex_path)
        self._claude_cache_path = claude_cache_path
        self._claude_reader = claude_reader
        self._lock = threading.Lock()
        self._data: dict[str, tuple[ProviderUsage, float]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fallback_missing_logged = False
        # Alerts + pace projection over consecutive polls (poller thread only).
        self._tracker = UsageTracker(alert_at, alert_reset)
        self._on_alert = on_alert
        self._wall_clock = wall_clock

    def start(self) -> None:
        if self._thread is not None or not self._providers:
            return
        self._thread = threading.Thread(target=self._run, name="herdeck-usage-poller", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        self._codex_source.close()

    def snapshot(self) -> list[ProviderUsage]:
        cutoff = self._clock() - _STALE_REFRESHES * self._refresh
        with self._lock:
            return [
                self._data[provider][0]
                for provider in self._providers
                if provider in self._data
                and self._data[provider][1] >= cutoff
                and (
                    not self._paid_only or self._data[provider][0].subscription == "paid"
                )
            ]

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self._refresh)

    def poll_once(self) -> None:
        fresh: dict[str, ProviderUsage] = {}
        requested = set(self._providers)
        if "codex" in requested:
            usage = self._codex_source.fetch()
            if usage is not None:
                fresh["codex"] = usage
        if "claude" in requested:
            usage = self._claude_reader(self._claude_cache_path)
            if usage is not None:
                fresh["claude"] = usage

        # CodexBar fills providers the native sources could not read (e.g. a
        # thin-client deck whose AI logins live on another machine). Its
        # ``loginMethod`` is the paid signal; in paid-only mode anything short
        # of a recognised paid tier is dropped rather than shown as payment.
        missing = [provider for provider in self._providers if provider not in fresh]
        for usage in self._fetch_codexbar(missing):
            if usage.provider not in requested or usage.provider in fresh:
                continue
            if self._paid_only and usage.subscription != "paid":
                continue
            fresh[usage.provider] = usage
        # Always run the tracker, even with nothing fresh: a reset alert is
        # also driven by the clock passing a known reset time.
        annotated, alerts = self._tracker.observe(list(fresh.values()), self._wall_clock())
        if fresh:
            fetched_at = self._clock()
            with self._lock:
                for usage in annotated:
                    self._data[usage.provider] = (usage, fetched_at)
        self._deliver(alerts)

    def _deliver(self, alerts) -> None:
        if not alerts or self._on_alert is None:
            return
        if self._paid_only:
            # Same visibility rule as snapshot(): no alerts for a provider
            # the panel hides.
            with self._lock:
                paid = {
                    provider
                    for provider, (usage, _at) in self._data.items()
                    if usage.subscription == "paid"
                }
            alerts = [alert for alert in alerts if alert.provider in paid]
            if not alerts:
                return
        try:
            self._on_alert(alerts)
        except Exception:
            log.warning("usage alert delivery failed", exc_info=True)

    def _fetch_codexbar(self, providers: list[str]) -> list[ProviderUsage]:
        if not providers or not self._codexbar_path:
            return []
        cli = resolve_cli(self._codexbar_path)
        if cli is None:
            if not self._fallback_missing_logged:
                log.warning("CodexBar fallback not found; missing usage for %s", providers)
                self._fallback_missing_logged = True
            return []
        self._fallback_missing_logged = False
        try:
            proc = self._runner(
                [cli, "usage", "--format", "json", "--provider", ",".join(providers)],
                capture_output=True,
                timeout=_CLI_TIMEOUT_S,
                text=True,
            )
        except Exception:
            log.warning("CodexBar fallback poll failed", exc_info=True)
            return []
        # CodexBar also reports every provider enabled in its own settings and
        # exits 1 when ANY of them fails (e.g. an expired Cursor/Kimi login),
        # even with --provider naming only healthy ones — the JSON on stdout
        # still carries the requested providers' numbers. Keep those.
        wanted = set(providers)
        parsed = [u for u in parse_usage(proc.stdout or "") if u.provider in wanted]
        if proc.returncode != 0 and not parsed:
            log.warning(
                "CodexBar fallback exited %s: %s", proc.returncode, (proc.stderr or "")[:200]
            )
        return parsed


def poller_from_config(usage_config, on_alert=None) -> UsagePoller | None:
    """Build the poller for ``[usage]``; ``on_alert`` receives each poll's
    ``usage_alerts.UsageAlert`` list (on the poller thread)."""
    if usage_config is None or not usage_config.providers:
        return None
    return UsagePoller(
        providers=usage_config.providers,
        paid_only=usage_config.paid_only,
        refresh_secs=usage_config.refresh_secs,
        codex_path=usage_config.codex_path,
        claude_cache_path=usage_config.claude_cache_path,
        codexbar_path=usage_config.codexbar_path,
        alert_at=usage_config.alert_at,
        alert_reset=usage_config.alert_reset,
        on_alert=on_alert,
    )


# --- bridge usage frames ------------------------------------------------------
#
# A bridge on the agents' Mac (where Codex/Claude are logged in) can run this
# poller itself and push its snapshot to every runtime as a ``usage`` frame
# (capability ``usage``). The frame carries the same ProviderUsage model the
# panel renders, UNFILTERED by paid_only: each runtime applies its own
# ``[usage].providers`` / ``paid_only`` (usage_hub.py), so one bridge serves
# runtimes with different settings. ``full_early_s`` is the bridge's pace
# projection (its poller sees every poll; a runtime only sees changes).

USAGE_CAPABILITY = "usage"
# Defensive caps for decoding a frame: a malformed frame must never grow
# runtime state or reach the panel as garbage.
_WIRE_MAX_PROVIDERS = 16
_WIRE_MAX_WINDOWS = 6
_WIRE_TEXT_MAX = 64
_WIRE_PROVIDER_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")
_SUBSCRIPTIONS = {"paid", "free", "unknown"}
# Providers the bridge polls when its usage table names none.
DEFAULT_BRIDGE_PROVIDERS = ("codex", "claude")


def usage_to_wire(data: list[ProviderUsage]) -> list[dict]:
    """ProviderUsage list -> the JSON-able ``providers`` payload of a frame."""
    return [
        {
            "provider": usage.provider,
            "subscription": usage.subscription,
            "plan": usage.plan,
            "windows": [
                {
                    "label": window.label,
                    "used_percent": window.used_percent,
                    "resets_at": window.resets_at,
                    "full_early_s": window.full_early_s,
                }
                for window in usage.windows
            ],
        }
        for usage in data
    ]


def _wire_text(value, default: str | None) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()[:_WIRE_TEXT_MAX]
    return default


def _window_from_wire(raw) -> UsageWindow | None:
    if not isinstance(raw, dict):
        return None
    used = raw.get("used_percent")
    if type(used) is not int:
        return None
    early = raw.get("full_early_s")
    return UsageWindow(
        label=_wire_text(raw.get("label"), "?") or "?",
        used_percent=max(0, min(100, used)),
        resets_at=_iso_reset(raw.get("resets_at"), allow_epoch=False),
        full_early_s=early if type(early) is int and early > 0 else None,
    )


def usage_from_wire(raw) -> list[ProviderUsage]:
    """Validate a usage frame's ``providers`` payload. Malformed entries are
    dropped (never coerced into panel text); a repeated provider keeps its
    first entry."""
    if not isinstance(raw, list):
        return []
    out: list[ProviderUsage] = []
    seen: set[str] = set()
    for entry in raw[:_WIRE_MAX_PROVIDERS]:
        if not isinstance(entry, dict):
            continue
        provider = entry.get("provider")
        if not isinstance(provider, str) or not _WIRE_PROVIDER_RE.fullmatch(provider):
            continue
        raw_windows = entry.get("windows")
        if provider in seen or not isinstance(raw_windows, list):
            continue
        windows = [
            window
            for window in (_window_from_wire(item) for item in raw_windows[:_WIRE_MAX_WINDOWS])
            if window is not None
        ]
        if not windows:
            continue
        subscription = entry.get("subscription")
        if subscription not in _SUBSCRIPTIONS:
            subscription = "unknown"
        seen.add(provider)
        plan = _wire_text(entry.get("plan"), None)
        out.append(ProviderUsage(provider, windows, subscription, plan))
    return out


def bridge_usage_enabled(getenv=os.environ.get) -> bool:
    """``HERDECK_BRIDGE_USAGE=1`` (alias ``HERDECK_USAGE=1``) turns the
    bridge's usage poller on."""
    for name in ("HERDECK_BRIDGE_USAGE", "HERDECK_USAGE"):
        if (getenv(name) or "").strip().lower() in ("1", "true", "yes", "on"):
            return True
    return False


def bridge_usage_config(getenv=os.environ.get):
    """The bridge poller's settings: the ``[usage]`` table of the TOML file at
    ``HERDECK_USAGE_CONFIG`` (e.g. this host's herdeck config.toml, validated
    like any config), else the defaults. Only the poll side is used
    (providers, refresh_secs, codex_path, claude_cache_path, codexbar_path);
    paid_only and alerts belong to each runtime, so they are cleared here.
    No providers named -> codex + claude. Raises SystemExit on a bad file."""
    import tomllib
    from dataclasses import replace

    from .config import ConfigError, UsageConfig
    from .settings import _usage_config

    path = getenv("HERDECK_USAGE_CONFIG")
    if path:
        try:
            raw = tomllib.loads(Path(os.path.expanduser(path)).read_text(encoding="utf-8"))
            table = raw.get("usage")
            cfg = _usage_config(table if isinstance(table, dict) else None)
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ConfigError) as exc:
            raise SystemExit(f"HERDECK_USAGE_CONFIG ({path}): {exc}") from None
    else:
        cfg = UsageConfig()
    return replace(
        cfg,
        providers=list(cfg.providers) or list(DEFAULT_BRIDGE_PROVIDERS),
        paid_only=False,
        alert_at=[],
        alert_reset=False,
    )
