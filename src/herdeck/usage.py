"""Usage-limit polling via the CodexBar CLI.

CodexBar (github.com/steipete/CodexBar) ships a CLI: `codexbar usage --format
json --provider claude,codex` prints one entry per provider with rate windows
(`usage.primary` / `usage.secondary`, each `usedPercent` + `resetsAt` +
`windowMinutes`). The poller runs it on the machine that renders the deck (the
CLI reads that machine's own provider auth), so no bridge/protocol change is
needed — the parsed snapshot feeds the orchestrator's overview panel directly.

The poller is a daemon thread (works in both the thread-based deckapp host and
the asyncio legacy app); render paths only ever read its latest snapshot.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# A scoped call takes ~2-3s; leave generous headroom (a cold provider fetch
# can take tens of seconds) without letting a hung CLI pin the thread forever.
_CLI_TIMEOUT_S = 120.0
# Data older than this many refresh intervals is dropped (the CLI is likely
# broken); usage changes slowly, so surviving a few failed polls is fine.
_STALE_REFRESHES = 4
# Locations launchd services miss from PATH (Homebrew on Apple silicon / Intel).
_FALLBACK_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


@dataclass
class UsageWindow:
    label: str  # "5h", "7d" — derived from windowMinutes
    used_percent: int
    resets_at: str | None  # ISO-8601 (UTC) or None


@dataclass
class ProviderUsage:
    provider: str  # codexbar provider id, e.g. "claude"
    windows: list[UsageWindow] = field(default_factory=list)


def _window_label(minutes) -> str:
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return "?"
    if m >= 1440:
        return f"{round(m / 1440)}d"
    if m >= 60:
        return f"{round(m / 60)}h"
    return f"{m}m"


def parse_usage(raw: str) -> list[ProviderUsage]:
    """Normalize `codexbar usage --format json` output. Entries or windows that
    don't match the expected shape are skipped, never fatal."""
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
        windows: list[UsageWindow] = []
        for slot in ("primary", "secondary", "tertiary"):
            win = usage.get(slot)
            if not isinstance(win, dict) or "usedPercent" not in win:
                continue
            try:
                used = int(win["usedPercent"])
            except (TypeError, ValueError):
                continue
            resets_at = win.get("resetsAt")
            if not isinstance(resets_at, str):
                resets_at = None  # anything else would crash reset formatting
            windows.append(
                UsageWindow(
                    label=_window_label(win.get("windowMinutes")),
                    used_percent=used,
                    resets_at=resets_at,
                )
            )
        if windows:
            out.append(ProviderUsage(provider=provider, windows=windows))
    return out


def resolve_cli(path: str) -> str | None:
    """Resolve the codexbar executable: explicit path, PATH lookup, then the
    Homebrew dirs launchd services don't have on PATH."""
    if os.path.sep in path:
        expanded = os.path.expanduser(path)
        return expanded if os.access(expanded, os.X_OK) else None
    found = shutil.which(path)
    if found:
        return found
    for d in _FALLBACK_DIRS:
        candidate = os.path.join(d, path)
        if os.access(candidate, os.X_OK):
            return candidate
    return None


class UsagePoller:
    """Background thread polling the CodexBar CLI; render paths read snapshot().

    Never raises out of the thread: a failed poll keeps the previous snapshot
    (usage moves slowly) until it goes stale, then snapshot() returns []."""

    def __init__(
        self,
        providers: list[str],
        refresh_secs: float = 300.0,
        codexbar_path: str = "codexbar",
        *,
        runner=subprocess.run,
        clock=time.monotonic,
    ):
        self._providers = list(providers)
        self._refresh = max(30.0, float(refresh_secs))
        self._path = codexbar_path
        self._runner = runner
        self._clock = clock
        self._lock = threading.Lock()
        self._data: list[ProviderUsage] = []
        self._fetched_at: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cli_missing_logged = False

    def start(self) -> None:
        if self._thread is not None or not self._providers:
            return
        self._thread = threading.Thread(
            target=self._run, name="herdeck-usage-poller", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def snapshot(self) -> list[ProviderUsage]:
        with self._lock:
            if self._fetched_at is None:
                return []
            if self._clock() - self._fetched_at > _STALE_REFRESHES * self._refresh:
                return []
            return list(self._data)

    # --- internals ---

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self._refresh)

    def poll_once(self) -> None:
        """One CLI fetch; also callable directly from tests."""
        cli = resolve_cli(self._path)
        if cli is None:
            if not self._cli_missing_logged:
                log.warning("codexbar CLI not found (looked for %r); usage panel off", self._path)
                self._cli_missing_logged = True
            return
        self._cli_missing_logged = False
        try:
            proc = self._runner(
                [cli, "usage", "--format", "json", "--provider", ",".join(self._providers)],
                capture_output=True,
                timeout=_CLI_TIMEOUT_S,
                text=True,
            )
        except Exception:
            log.warning("codexbar usage poll failed", exc_info=True)
            return
        if proc.returncode != 0:
            log.warning("codexbar exited %s: %s", proc.returncode, (proc.stderr or "")[:200])
            return
        data = parse_usage(proc.stdout or "")
        if not data:
            log.warning("codexbar returned no parseable usage data")
            return
        # The CLI returns providers in its own order; the panel shows them in
        # the order the user configured.
        order = {p: i for i, p in enumerate(self._providers)}
        data.sort(key=lambda pu: order.get(pu.provider, len(order)))
        with self._lock:
            self._data = data
            self._fetched_at = self._clock()


def poller_from_config(usage_config) -> UsagePoller | None:
    """Build (not start) a poller from Config.usage; None when disabled."""
    if usage_config is None or not usage_config.providers:
        return None
    return UsagePoller(
        providers=usage_config.providers,
        refresh_secs=usage_config.refresh_secs,
        codexbar_path=usage_config.codexbar_path,
    )
