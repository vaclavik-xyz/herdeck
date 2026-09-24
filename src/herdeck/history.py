"""Bridge-side status history and the ``stats`` query (capability ``history``).

The bridge is the long-running process that sees every status change of every
agent pane (the same vantage point status_since.py uses), so it keeps a small
local record of *episodes*: one row per stretch of time a pane spent in one
effective status (``protocol.effective_status``), written when the episode
ends::

    pane_id, terminal_id, agent_type, repo, project, label,   # snapshot at the end
    from_status, to_status, started_ms, ended_ms, answered

``to_status`` is the status the pane moved to, or ``"gone"`` when the pane
closed (or herdr recycled its id for another terminal). ``answered`` is 1 when
a BLOCKED episode ended after the bridge itself delivered an answer to that
pane (``act`` / ``send_text`` / ``choose_if_blocked``); an answer typed
straight into the terminal is invisible to the bridge and stays 0.

Store: stdlib sqlite3 at ``$XDG_STATE_HOME/herdeck/history.sqlite`` (default
``~/.local/state``), 0600 in a 0700 directory, WAL. All I/O runs on one
worker thread fed by a bounded queue — the event loop only enqueues — and
writes are batched into one transaction. Bounded by ``RETENTION_DAYS`` and
``MAX_ROWS``. A store error is logged and swallowed: history is a nice-to-have
and must never break the bridge. A corrupt database file is moved aside and
recreated.

Query: ``{"type": "stats", "req", "range_days": 1|7|30, "group_by":
"agent"|"repo"|"agent_type", "tz_offset_min"?}`` (read-only token OK) answers
``{"type": "result", "req", "data": <aggregate()>}``. ``range_days`` counts
calendar days in the client's timezone: 1 = today, 7 = today and the six days
before. Compute time is bounded (row limit + deadline, ``truncated`` flags a
cut).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import math
import os
import queue
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .protocol import effective_status

log = logging.getLogger(__name__)

CAPABILITY = "history"
RANGES = (1, 7, 30)
GROUP_BYS = ("agent", "repo", "agent_type")
RETENTION_DAYS = 30
MAX_ROWS = 200_000
# Aggregation bounds: never more rows than the store may hold, never longer
# than this on the worker thread (``truncated`` tells the client).
QUERY_ROW_LIMIT = MAX_ROWS
QUERY_BUDGET_S = 2.0
# How long a stats request waits for the worker (queue + query + aggregation).
QUERY_TIMEOUT_S = 8.0
# Time-to-answer samples returned per group (enough to merge bridges and still
# get a faithful median / p90; evenly thinned beyond this).
SAMPLE_CAP = 1000
MAX_GROUPS = 200
QUEUE_MAX = 10_000
BATCH_MAX = 500
PRUNE_EVERY_S = 3600.0
_MAX_TEXT = 160
_DAY_MS = 86_400_000
_MAX_TZ_OFFSET_MIN = 14 * 60
# Statuses whose time is summed (unknown is never counted).
DURATION_STATUSES = ("working", "blocked", "idle", "waiting", "done")
GONE = "gone"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    pane_id TEXT NOT NULL,
    terminal_id TEXT NOT NULL,
    agent_type TEXT NOT NULL,
    repo TEXT NOT NULL,
    project TEXT NOT NULL,
    label TEXT NOT NULL,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    started_ms INTEGER NOT NULL,
    ended_ms INTEGER NOT NULL,
    answered INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS episodes_ended ON episodes (ended_ms);
"""
_COLUMNS = (
    "pane_id, terminal_id, agent_type, repo, project, label, "
    "from_status, to_status, started_ms, ended_ms, answered"
)


def default_path(name: str = "history.sqlite") -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "herdeck", name)


@dataclass(frozen=True)
class Episode:
    pane_id: str
    terminal_id: str
    agent_type: str
    repo: str
    project: str
    label: str
    from_status: str
    to_status: str
    started_ms: int
    ended_ms: int
    answered: bool = False

    def row(self) -> tuple:
        return (
            self.pane_id,
            self.terminal_id,
            self.agent_type,
            self.repo,
            self.project,
            self.label,
            self.from_status,
            self.to_status,
            self.started_ms,
            self.ended_ms,
            1 if self.answered else 0,
        )


def _text(value: object) -> str:
    return value[:_MAX_TEXT] if isinstance(value, str) else ""


# --- store ---------------------------------------------------------------------


class HistoryStore:
    """SQLite episode log owned by one worker thread.

    ``append`` never blocks and never raises (a full queue drops the record
    with a warning). ``query(fn)`` runs ``fn(conn)`` on the worker and
    returns a concurrent future."""

    def __init__(
        self,
        path: str,
        *,
        clock: Callable[[], float] = time.time,
        retention_days: int = RETENTION_DAYS,
        max_rows: int = MAX_ROWS,
        queue_max: int = QUEUE_MAX,
    ):
        self._path = path
        self._clock = clock
        self._retention_ms = retention_days * _DAY_MS
        self._max_rows = max_rows
        self._queue: queue.Queue = queue.Queue(maxsize=queue_max)
        self._conn: sqlite3.Connection | None = None
        self._last_prune = 0.0
        self._dropped = 0
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="herdeck-history", daemon=True)
        self._thread.start()

    # --- event-loop side ---
    def append(self, episode: Episode) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait(("append", episode))
        except queue.Full:
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 1000 == 0:
                log.warning("history queue full, %d record(s) dropped", self._dropped)

    def query(self, fn: Callable[[sqlite3.Connection], object]) -> concurrent.futures.Future:
        future: concurrent.futures.Future = concurrent.futures.Future()
        if self._closed:
            future.set_exception(RuntimeError("history store closed"))
            return future
        try:
            self._queue.put_nowait(("query", (fn, future)))
        except queue.Full:
            future.set_exception(RuntimeError("history store busy"))
        return future

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until everything queued so far is written (tests, shutdown)."""
        try:
            self.query(lambda conn: None).result(timeout)
            return True
        except Exception:
            return False

    def close(self, timeout: float = 5.0) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(queue.Full):
            self._queue.put(("close", None), timeout=timeout)
        self._thread.join(timeout)

    # --- worker thread ---
    def _run(self) -> None:
        while True:
            item = self._queue.get()
            batch: list[Episode] = []
            pending: list[tuple] = []
            stop = False
            while True:
                op, payload = item
                if op == "append":
                    batch.append(payload)
                elif op == "query":
                    pending.append(payload)
                elif op == "close":
                    stop = True
                    break
                if len(batch) >= BATCH_MAX:
                    break
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
            if batch:
                self._write(batch)
            for fn, future in pending:
                self._answer(fn, future)
            if stop:
                self._close_conn()
                self._fail_leftovers()
                return

    def _fail_leftovers(self) -> None:
        while True:
            try:
                op, payload = self._queue.get_nowait()
            except queue.Empty:
                return
            if op == "query":
                payload[1].set_exception(RuntimeError("history store closed"))

    def _connection(self) -> sqlite3.Connection | None:
        if self._conn is not None:
            return self._conn
        try:
            self._conn = self._open()
        except sqlite3.DatabaseError as exc:
            log.warning("history store corrupt (%s): %s; starting a new one", self._path, exc)
            self._set_aside()
            try:
                self._conn = self._open()
            except (OSError, sqlite3.Error) as exc2:
                log.warning("history store unavailable (%s): %s", self._path, exc2)
                self._conn = None
        except (OSError, sqlite3.Error) as exc:
            log.warning("history store unavailable (%s): %s", self._path, exc)
            self._conn = None
        return self._conn

    def _open(self) -> sqlite3.Connection:
        directory = os.path.dirname(self._path) or "."
        os.makedirs(directory, mode=0o700, exist_ok=True)
        # Create the file 0600 ourselves: sqlite would use the umask, and its
        # -wal / -shm companions copy the database file's mode.
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
        with contextlib.suppress(OSError):
            os.chmod(self._path, 0o600)
        conn = sqlite3.connect(self._path, timeout=5.0, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)
            conn.execute("SELECT count(*) FROM episodes").fetchone()
        except Exception:
            conn.close()
            raise
        return conn

    def _set_aside(self) -> None:
        stamp = time.strftime("%Y%m%d%H%M%S")
        for suffix in ("", "-wal", "-shm"):
            src = self._path + suffix
            if os.path.exists(src):
                with contextlib.suppress(OSError):
                    os.replace(src, f"{self._path}.corrupt-{stamp}{suffix}")

    def _close_conn(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(sqlite3.Error):
                self._conn.close()
            self._conn = None

    def _write(self, batch: list[Episode], *, retry: bool = True) -> None:
        conn = self._connection()
        if conn is None:
            return
        try:
            conn.execute("BEGIN")
            conn.executemany(
                f"INSERT INTO episodes ({_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [e.row() for e in batch],
            )
            conn.execute("COMMIT")
        except sqlite3.DatabaseError as exc:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            if retry and _is_corruption(exc):
                log.warning("history store corrupt on write (%s): %s; recreating", self._path, exc)
                self._close_conn()
                self._set_aside()
                self._write(batch, retry=False)
                return
            log.warning("history write failed (%s): %s", self._path, exc)
            return
        except sqlite3.Error as exc:
            log.warning("history write failed (%s): %s", self._path, exc)
            return
        now = self._clock()
        if now - self._last_prune >= PRUNE_EVERY_S:
            self._last_prune = now
            self.prune(conn)

    def prune(self, conn: sqlite3.Connection | None = None) -> None:
        """Drop rows past retention, then the oldest beyond the row cap."""
        conn = conn or self._connection()
        if conn is None:
            return
        cutoff = int(self._clock() * 1000) - self._retention_ms
        try:
            conn.execute("DELETE FROM episodes WHERE ended_ms < ?", (cutoff,))
            conn.execute(
                "DELETE FROM episodes WHERE id <= ("
                " SELECT id FROM episodes ORDER BY id DESC LIMIT 1 OFFSET ?)",
                (self._max_rows,),
            )
        except sqlite3.Error as exc:
            log.warning("history prune failed (%s): %s", self._path, exc)

    def _answer(self, fn, future: concurrent.futures.Future) -> None:
        if not future.set_running_or_notify_cancel():
            return
        conn = self._connection()
        if conn is None:
            future.set_exception(RuntimeError("history store unavailable"))
            return
        try:
            future.set_result(fn(conn))
        except Exception as exc:
            future.set_exception(exc)


def _is_corruption(exc: sqlite3.DatabaseError) -> bool:
    text = str(exc).lower()
    return "malformed" in text or "not a database" in text or "corrupt" in text


# --- recorder (event loop) -------------------------------------------------------


@dataclass
class _Live:
    terminal_id: str
    status: str
    started_ms: int
    agent_type: str
    repo: str
    project: str
    label: str
    answered: bool = False


class HistoryRecorder:
    """Turns stamped fleet snapshots into ended episodes (event loop only).

    Fed by StatusSinceTracker after it stamped ``status_since_ms`` on a FULL
    agent snapshot, so an episode that survived a bridge restart starts at the
    restored time rather than at "now"."""

    def __init__(self, sink: Callable[[Episode], None]):
        self._sink = sink
        self._live: dict[str, _Live] = {}

    def observe(self, panes: list[dict], now_ms: int) -> None:
        seen: set[str] = set()
        for pane in panes:
            pane_id = pane.get("pane_id")
            if not isinstance(pane_id, str) or not pane_id:
                continue
            seen.add(pane_id)
            terminal_id = _text(pane.get("terminal_id"))
            status = effective_status(pane.get("status", "unknown"), pane.get("waiting_on")).value
            since = pane.get("status_since_ms")
            started = since if type(since) is int and 0 < since <= now_ms else now_ms
            prev = self._live.get(pane_id)
            if prev is not None and prev.terminal_id == terminal_id and prev.status == status:
                # same episode: keep the freshest labels (branch/worktree info
                # may arrive a snapshot later)
                prev.agent_type = _text(pane.get("agent_type")) or prev.agent_type
                prev.repo = _text(pane.get("repo")) or prev.repo
                prev.project = _text(pane.get("project")) or prev.project
                prev.label = _text(pane.get("label")) or prev.label
                continue
            if prev is not None:
                to_status = status if prev.terminal_id == terminal_id else GONE
                self._end(pane_id, prev, to_status, now_ms)
                started = now_ms
            self._live[pane_id] = _Live(
                terminal_id=terminal_id,
                status=status,
                started_ms=started,
                agent_type=_text(pane.get("agent_type")),
                repo=_text(pane.get("repo")),
                project=_text(pane.get("project")),
                label=_text(pane.get("label")),
            )
        for pane_id in [p for p in self._live if p not in seen]:
            self._end(pane_id, self._live.pop(pane_id), GONE, now_ms)

    def note_answer(self, pane_id: str) -> None:
        """The bridge delivered an answer to ``pane_id``: a BLOCKED episode
        that ends next counts as answered."""
        live = self._live.get(pane_id)
        if live is not None and live.status == "blocked":
            live.answered = True

    def open_episodes(self, now_ms: int) -> list[tuple]:
        """The episodes still running, as rows ending ``now_ms`` (to_status '')."""
        return [
            Episode(
                pane_id,
                live.terminal_id,
                live.agent_type,
                live.repo,
                live.project,
                live.label,
                live.status,
                "",
                live.started_ms,
                now_ms,
                live.answered,
            ).row()
            for pane_id, live in self._live.items()
            if live.started_ms < now_ms
        ]

    def _end(self, pane_id: str, live: _Live, to_status: str, now_ms: int) -> None:
        if now_ms <= live.started_ms:
            return
        try:
            self._sink(
                Episode(
                    pane_id,
                    live.terminal_id,
                    live.agent_type,
                    live.repo,
                    live.project,
                    live.label,
                    live.status,
                    to_status,
                    live.started_ms,
                    now_ms,
                    live.answered and live.status == "blocked",
                )
            )
        except Exception:
            log.exception("history sink failed")


# --- aggregation -------------------------------------------------------------------


def percentile(sorted_values: list[int], p: float) -> int | None:
    """Nearest-rank percentile of an ascending list (None when empty)."""
    if not sorted_values:
        return None
    rank = max(1, math.ceil(p * len(sorted_values)))
    return sorted_values[min(rank, len(sorted_values)) - 1]


def thin(sorted_values: list[int], cap: int = SAMPLE_CAP) -> list[int]:
    """Evenly spaced subset of an ascending list (keeps its shape)."""
    n = len(sorted_values)
    if n <= cap:
        return list(sorted_values)
    return [sorted_values[(i * (n - 1)) // (cap - 1)] for i in range(cap)]


def range_start_ms(now_ms: int, range_days: int, tz_offset_min: int = 0) -> int:
    """Local midnight ``range_days - 1`` days before today (``tz_offset_min``
    = minutes east of UTC)."""
    offset = tz_offset_min * 60_000
    today = ((now_ms + offset) // _DAY_MS) * _DAY_MS - offset
    return today - (range_days - 1) * _DAY_MS


def _empty_bucket() -> dict:
    return {
        **{f"{status}_ms": 0 for status in DURATION_STATUSES},
        "blocked_count": 0,
        "answered_count": 0,
        "done_count": 0,
    }


def _group_key(group_by: str, row: tuple) -> tuple[str, str]:
    pane_id, terminal_id, agent_type, repo, project, label = row[:6]
    if group_by == "agent_type":
        key = agent_type or "?"
        return key, key
    if group_by == "repo":
        key = repo or project or "?"
        return key, key
    name = label or repo or project or pane_id
    return f"{pane_id}/{terminal_id}", f"{name} · {agent_type}" if agent_type else name


def aggregate(
    rows,
    *,
    now_ms: int,
    range_days: int,
    group_by: str,
    tz_offset_min: int = 0,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    """Aggregate episode rows (``Episode.row()`` tuples; an open episode has
    to_status '') over the calendar-day range ending ``now_ms``.

    Durations are clipped to the range and split across local days. Counts
    (blocked / answered / done) belong to the day the episode ended, and only
    closed episodes count. Time to answer = the full duration of each BLOCKED
    episode that ended in the range by a status change (not by the pane
    closing)."""
    start = range_start_ms(now_ms, range_days, tz_offset_min)
    offset = tz_offset_min * 60_000
    days = [_empty_bucket() | {"start_ms": start + i * _DAY_MS} for i in range(range_days)]
    total = _empty_bucket()
    groups: dict[str, dict] = {}
    samples: dict[str, list[int]] = {}
    total_samples: list[int] = []
    truncated = False

    for n, row in enumerate(rows):
        if deadline is not None and n % 4096 == 0 and n and clock() > deadline:
            truncated = True
            break
        from_status, to_status, started, ended, answered = row[6:11]
        if ended <= start or started >= now_ms:
            continue
        key, label = _group_key(group_by, row)
        group = groups.get(key)
        if group is None:
            group = groups[key] = _empty_bucket() | {"key": key, "label": label}
            samples[key] = []
        else:
            group["label"] = label  # rows come oldest first: the newest label wins
        closed = to_status != ""
        if from_status in DURATION_STATUSES:
            field = f"{from_status}_ms"
            lo, hi = max(started, start), min(ended, now_ms)
            if hi > lo:
                group[field] += hi - lo
                total[field] += hi - lo
                day = (lo - start) // _DAY_MS
                while lo < hi and 0 <= day < range_days:
                    edge = min(hi, days[day]["start_ms"] + _DAY_MS)
                    days[day][field] += edge - lo
                    lo = edge
                    day += 1
        if not closed or ended > now_ms:
            continue
        end_day = (ended - start) // _DAY_MS
        bucket = days[end_day] if 0 <= end_day < range_days else None
        if from_status == "blocked":
            for target in (group, total, bucket):
                if target is not None:
                    target["blocked_count"] += 1
                    if answered:
                        target["answered_count"] += 1
            if to_status != GONE:
                samples[key].append(ended - started)
                total_samples.append(ended - started)
        if to_status == "done":
            for target in (group, total, bucket):
                if target is not None:
                    target["done_count"] += 1

    def finish(bucket: dict, values: list[int]) -> dict:
        values.sort()
        bucket["answer_median_ms"] = percentile(values, 0.5)
        bucket["answer_p90_ms"] = percentile(values, 0.9)
        bucket["answer_samples_ms"] = thin(values)
        return bucket

    ordered = sorted(
        groups.values(),
        key=lambda g: (-sum(g[f"{s}_ms"] for s in DURATION_STATUSES), g["label"]),
    )
    if len(ordered) > MAX_GROUPS:
        ordered = ordered[:MAX_GROUPS]
        truncated = True
    for day in days:
        day["day"] = time.strftime("%Y-%m-%d", time.gmtime((day["start_ms"] + offset) / 1000))
    return {
        "range_days": range_days,
        "group_by": group_by,
        "tz_offset_min": tz_offset_min,
        "from_ms": start,
        "to_ms": now_ms,
        "truncated": truncated,
        "total": finish(total, total_samples),
        "groups": [finish(g, samples[g["key"]]) for g in ordered],
        "days": days,
    }


# --- bridge facade -------------------------------------------------------------------


def parse_stats_request(msg: dict) -> tuple[int, str, int] | str:
    """(range_days, group_by, tz_offset_min), or an error message."""
    range_days = msg.get("range_days", 7)
    group_by = msg.get("group_by", "agent")
    tz = msg.get("tz_offset_min", 0)
    if type(range_days) is not int or range_days not in RANGES:
        return f"stats: range_days must be one of {list(RANGES)}"
    if group_by not in GROUP_BYS:
        return f"stats: group_by must be one of {list(GROUP_BYS)}"
    if type(tz) is not int or abs(tz) > _MAX_TZ_OFFSET_MIN:
        return "stats: tz_offset_min must be an integer within ±840"
    return range_days, group_by, tz


class BridgeHistory:
    """What the bridge holds: the recorder (fed by StatusSinceTracker) and the
    store it writes to. Constructed per bridge; ``close`` on shutdown."""

    def __init__(
        self,
        path: str | None = None,
        *,
        clock: Callable[[], float] = time.time,
        store: HistoryStore | None = None,
    ):
        self._clock = clock
        self.store = store or HistoryStore(path or default_path(), clock=clock)
        self.recorder = HistoryRecorder(self.store.append)

    # StatusSinceTracker observer protocol
    def observe(self, panes: list[dict], now_ms: int) -> None:
        self.recorder.observe(panes, now_ms)

    def note_answer(self, pane_id: str) -> None:
        self.recorder.note_answer(pane_id)

    async def stats_reply(self, msg: dict) -> dict:
        req = msg.get("req")
        req = req if isinstance(req, str) else ""
        parsed = parse_stats_request(msg)
        if isinstance(parsed, str):
            return {"type": "error", "req": req, "message": parsed}
        range_days, group_by, tz = parsed
        now_ms = int(self._clock() * 1000)
        open_rows = self.recorder.open_episodes(now_ms)
        start = range_start_ms(now_ms, range_days, tz)

        def run(conn: sqlite3.Connection) -> dict:
            deadline = time.monotonic() + QUERY_BUDGET_S
            conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
            try:
                rows = conn.execute(
                    f"SELECT {_COLUMNS} FROM episodes WHERE ended_ms > ? ORDER BY ended_ms LIMIT ?",
                    (start, QUERY_ROW_LIMIT),
                ).fetchall()
            except sqlite3.OperationalError as exc:
                if "interrupt" not in str(exc).lower():
                    raise
                rows, cut = [], True
            else:
                cut = len(rows) >= QUERY_ROW_LIMIT
            finally:
                conn.set_progress_handler(None, 0)
            result = aggregate(
                [*rows, *open_rows],
                now_ms=now_ms,
                range_days=range_days,
                group_by=group_by,
                tz_offset_min=tz,
                deadline=deadline,
            )
            result["truncated"] = result["truncated"] or cut
            return result

        try:
            data = await asyncio.wait_for(
                asyncio.wrap_future(self.store.query(run)), timeout=QUERY_TIMEOUT_S
            )
        except Exception as exc:
            return {"type": "error", "req": req, "message": f"stats unavailable: {exc}"}
        return {"type": "result", "req": req, "data": data}

    def close(self) -> None:
        self.store.close()
