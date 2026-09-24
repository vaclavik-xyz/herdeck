"""The usage agent: provider usage polled in the user's login session.

A bridge installed as a system LaunchDaemon (``herdeck-service install bridge
--system``) runs outside the user's login (Aqua) session, where ``codex
app-server`` and ``codexbar`` cannot reach the login keychain and time out. The
usage agent is a small LaunchAgent in that session (``herdeck-service install
usage``; ``python -m herdeck.usage_agent``, console script
``herdeck-usage-agent``). It runs the bridge's usage poller
(``usage.poller_from_config(usage.bridge_usage_config())``, so it honours
``HERDECK_USAGE_CONFIG``) and writes the snapshot to :func:`default_path`::

    {"version": 1, "written_at": <epoch s>, "refresh_secs": N,
     "providers": <usage.usage_to_wire(snapshot)>}

atomically (temp file + rename), mode 0600: on every change (checked every
``CHECK_S``) and at least every ``refresh_secs`` (heartbeat).

The bridge reads it through :class:`CompositeUsagePoller` (bridge.py,
``HERDECK_BRIDGE_USAGE=1``): while the file exists the bridge uses it only and
never starts (or stops) its own poller; the providers count only while fresh
(``written_at`` no older than :func:`max_age`), else the frame is empty. With
no file the bridge polls itself, as before the agent existed.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import math
import os
import signal
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from .usage import usage_from_wire, usage_to_wire

log = logging.getLogger(__name__)

FILE_VERSION = 1
FILE_NAME = "bridge-usage.json"
# How often the agent compares the poller's snapshot with the last write.
CHECK_S = 5.0
# The bridge ignores a file older than max(STALE_REFRESHES * refresh_secs,
# MIN_STALE_S): the agent died, hangs, or lost its session.
STALE_REFRESHES = 3
MIN_STALE_S = 300.0
# A file is never trusted for more than this much of its own claim about how
# often it is written (a garbage refresh_secs must not keep data fresh forever).
MAX_REFRESH_S = 24 * 60 * 60
# A written_at this far in the future is garbage, not clock jitter.
FUTURE_SLACK_S = 60.0
# The poller clamps its interval to at least this (usage.UsagePoller).
MIN_REFRESH_S = 30.0


def state_dir(env: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    """``${XDG_STATE_HOME:-~/.local/state}/herdeck`` for ``env`` / ``home``."""
    env = os.environ if env is None else env
    base = env.get("XDG_STATE_HOME")
    if base:
        return Path(base) / "herdeck"
    return (Path.home() if home is None else home) / ".local/state/herdeck"


def default_path(env: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    """Where the agent writes and the bridge reads (both run as the same user)."""
    return state_dir(env, home) / FILE_NAME


def max_age(refresh_secs: float) -> float:
    return max(STALE_REFRESHES * refresh_secs, MIN_STALE_S)


def write_file(path: Path, providers: list[dict], refresh_secs: float, now: float) -> None:
    """Atomically replace ``path`` (0600, parent 0700 when created)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "version": FILE_VERSION,
        "written_at": now,
        "refresh_secs": refresh_secs,
        "providers": providers,
    }
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
            handle.write("\n")
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def read_file(path: Path) -> dict | None:
    """The parsed file, or None when it is missing, unreadable or malformed."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("version") != FILE_VERSION:
        return None
    return doc


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def file_refresh_secs(doc: dict | None) -> float:
    refresh = _number((doc or {}).get("refresh_secs"))
    if refresh is None or refresh <= 0:
        return MIN_STALE_S
    return min(refresh, MAX_REFRESH_S)


def file_age(doc: dict | None, now: float) -> float | None:
    """Seconds since the agent last wrote ``doc``; None when it says nothing usable."""
    written = _number((doc or {}).get("written_at"))
    if written is None or written - now > FUTURE_SLACK_S:
        return None
    return max(0.0, now - written)


def is_fresh(doc: dict | None, now: float) -> bool:
    age = file_age(doc, now)
    return age is not None and age <= max_age(file_refresh_secs(doc))


class UsageAgent:
    """Writes the poller's snapshot to ``path`` on change and as a heartbeat.

    ``poller`` is a usage.UsagePoller (or a fake with ``snapshot()``); starting
    and closing it is the caller's job (:func:`main`)."""

    def __init__(
        self,
        poller,
        path: Path,
        refresh_secs: float,
        *,
        check_s: float = CHECK_S,
        clock: Callable[[], float] = time.time,
    ):
        self._poller = poller
        self._path = Path(path)
        self._refresh = max(MIN_REFRESH_S, float(refresh_secs))
        self._check_s = check_s
        self._clock = clock
        self._last: list[dict] | None = None
        self._written_at: float | None = None

    @property
    def refresh_secs(self) -> float:
        return self._refresh

    def tick(self) -> bool:
        """Write when the snapshot changed or the heartbeat is due. True = wrote."""
        try:
            providers = usage_to_wire(self._poller.snapshot())
        except Exception:
            log.warning("usage snapshot failed", exc_info=True)
            return False
        now = self._clock()
        due = self._written_at is None or now - self._written_at >= self._refresh
        if providers == self._last and not due:
            return False
        try:
            write_file(self._path, providers, self._refresh, now)
        except OSError as exc:
            log.warning("could not write %s: %s", self._path, exc)
            return False
        if providers != self._last:
            log.info(
                "usage: %s", ", ".join(p["provider"] for p in providers) or "no providers yet"
            )
        self._last = providers
        self._written_at = now
        return True

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.tick()
            stop.wait(self._check_s)


class CompositeUsagePoller:
    """The bridge's usage poller (``HERDECK_BRIDGE_USAGE=1``): the usage
    agent's file while it exists, else the bridge's own poller.

    * file present: only the file counts; the own poller is never started (or
      is closed, off the caller's thread, when it was running). Its providers
      are returned only while fresh (:func:`is_fresh`), else ``[]``.
    * file absent: the own poller (``own_factory()``, a usage.UsagePoller) is
      started lazily, after :meth:`start` — the behaviour of a bridge that is
      not a LaunchDaemon, and of every bridge before the agent existed.

    Checked on every :meth:`snapshot` (a stat; the file is re-read only when
    its mtime/size/inode changed). Same interface as usage.UsagePoller
    (``start`` / ``snapshot`` / ``close``), so BridgeUsageFeed is unchanged."""

    def __init__(
        self,
        own_factory: Callable[[], object | None],
        path: Path,
        *,
        clock: Callable[[], float] = time.time,
        closer: Callable[[object], None] | None = None,
    ):
        self._factory = own_factory
        self._path = Path(path)
        self._clock = clock
        self._closer = closer or _close_in_background
        self._lock = threading.Lock()
        self._started = False
        self._own = None
        self._mode: str | None = None  # "agent" | "own"
        self._fresh: bool | None = None
        self._key: tuple | None = None
        self._doc: dict | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def mode(self) -> str | None:
        return self._mode

    def start(self) -> None:
        with self._lock:
            self._started = True
            self._select()

    def close(self) -> None:
        with self._lock:
            self._started = False
            own, self._own = self._own, None
        if own is not None:
            own.close()

    def snapshot(self) -> list:
        with self._lock:
            doc = self._select()
            if self._mode == "agent":
                return self._agent_snapshot(doc)
            own = self._own
        return own.snapshot() if own is not None else []

    def _select(self) -> dict | None:
        try:
            st = os.stat(self._path)
        except OSError:
            st = None
        if st is None:
            if self._mode != "own":
                if self._mode == "agent":
                    log.info("usage agent file %s is gone; polling usage on the bridge", self._path)
                self._mode, self._fresh, self._key, self._doc = "own", None, None, None
            if self._started and self._own is None:
                self._own = self._factory()
                if self._own is not None:
                    self._own.start()
            return None
        if self._mode != "agent":
            log.info("usage agent file %s found; using it for provider usage", self._path)
            self._mode = "agent"
            own, self._own = self._own, None
            if own is not None:
                self._closer(own)
        key = (st.st_ino, st.st_mtime_ns, st.st_size)
        if key != self._key:
            self._key, self._doc = key, read_file(self._path)
        return self._doc

    def _agent_snapshot(self, doc: dict | None) -> list:
        fresh = is_fresh(doc, self._clock())
        if fresh != self._fresh:
            if fresh:
                log.info("usage agent data is fresh")
            elif self._fresh is not None or doc is not None:
                age = file_age(doc, self._clock())
                log.info(
                    "usage agent data is stale or unreadable (age %s); sending no usage",
                    f"{age:.0f}s" if age is not None else "unknown",
                )
            self._fresh = fresh
        return usage_from_wire(doc.get("providers")) if fresh and doc is not None else []


def _close_in_background(poller) -> None:
    """Closing a UsagePoller joins its thread and stops ``codex app-server``;
    never on the bridge's event loop."""
    threading.Thread(target=poller.close, name="herdeck-usage-close", daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    from .usage import bridge_usage_config, poller_from_config

    parser = argparse.ArgumentParser(
        prog="herdeck-usage-agent",
        description="Poll Codex/Claude usage in the login session and write it for the "
        "herdeck bridge (configured through HERDECK_USAGE_CONFIG).",
    )
    parser.add_argument(
        "--file", type=Path, default=None, help=f"output file (default: {default_path()})"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = bridge_usage_config()
    poller = poller_from_config(cfg)
    if poller is None:  # pragma: no cover - bridge_usage_config always names providers
        log.error("no usage providers configured")
        return 1
    path = args.file or default_path()
    stop = threading.Event()

    def on_signal(_signum, _frame) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    agent = UsageAgent(poller, path, cfg.refresh_secs)
    log.info(
        "usage agent: providers=%s refresh=%ss file=%s",
        ",".join(cfg.providers),
        agent.refresh_secs,
        path,
    )
    poller.start()
    try:
        agent.run(stop)
    finally:
        poller.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
