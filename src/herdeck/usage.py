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
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

_CLI_TIMEOUT_S = 120.0
_APP_SERVER_TIMEOUT_S = 15.0
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


@dataclass
class UsageWindow:
    label: str
    used_percent: int
    resets_at: str | None


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
    """Normalize CodexBar JSON for the compatibility fallback."""
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
            out.append(ProviderUsage(provider=provider, windows=windows))
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
                {"method": "account/read", "id": account_id, "params": {"refreshToken": False}}
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
                        "version": "0.1.0",
                    }
                },
            }
        )
        self._read_response(0)
        self._send({"method": "initialized", "params": {}})

    def _send(self, message: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("Codex app-server is not running")
        self._proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self._proc.stdin.flush()

    def _read_response(self, request_id: int) -> dict:
        if self._proc is None:
            raise RuntimeError("Codex app-server is not running")
        deadline = time.monotonic() + _APP_SERVER_TIMEOUT_S
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
            if message.get("id") != request_id:
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
                # Notifications have no id and are irrelevant to this polling
                # client. Drop them here so an idle daemon cannot grow the queue.
                if "id" in message:
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
    ):
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

        # CodexBar does not expose a stable paid-subscription entitlement. In
        # paid-only mode an unknown fallback would be hidden anyway, so avoid
        # both the subprocess and the risk of presenting login as payment.
        missing = (
            []
            if self._paid_only
            else [provider for provider in self._providers if provider not in fresh]
        )
        for usage in self._fetch_codexbar(missing):
            if usage.provider in requested and usage.provider not in fresh:
                fresh[usage.provider] = usage
        if not fresh:
            return
        fetched_at = self._clock()
        with self._lock:
            for provider, usage in fresh.items():
                self._data[provider] = (usage, fetched_at)

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
        if proc.returncode != 0:
            log.warning(
                "CodexBar fallback exited %s: %s", proc.returncode, (proc.stderr or "")[:200]
            )
            return []
        return parse_usage(proc.stdout or "")


def poller_from_config(usage_config) -> UsagePoller | None:
    if usage_config is None or not usage_config.providers:
        return None
    return UsagePoller(
        providers=usage_config.providers,
        paid_only=usage_config.paid_only,
        refresh_secs=usage_config.refresh_secs,
        codex_path=usage_config.codex_path,
        claude_cache_path=usage_config.claude_cache_path,
        codexbar_path=usage_config.codexbar_path,
    )
