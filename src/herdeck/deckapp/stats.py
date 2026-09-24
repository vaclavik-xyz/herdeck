"""Runtime side of the bridge history: ``GET /stats?range=7&group=repo``.

Relays ``{"type": "stats", "req", "range_days", "group_by", "tz_offset_min"}``
(see ``herdeck/history.py``) to every connected bridge that advertises the
``history`` capability, waits for their answers and merges them by summing:
durations and counts add up, per-day buckets add up by date, groups with the
same key merge (``agent`` groups are per server, so their keys carry the
server id), and the time-to-answer median / p90 are recomputed from the
merged samples. ``tz_offset_min`` is this runtime's local offset, so "today"
means the viewer's today.

Answer::

    {"ok": true, "code": "ok", "servers": [ids that answered],
     "missing": [{"server_id", "reason"}], "range_days", "group_by",
     "from_ms", "to_ms", "truncated", "total": {...}, "groups": [...],
     "days": [...]}

or ``{"ok": false, "code": "unsupported" | "disconnected" | "failed",
"message", "missing": [...]}`` when no bridge answered. Samples are dropped
from the HTTP answer (only the summaries are needed by the window).

``LiveSource`` mixes in ``StatsMixin`` and routes its connector callbacks
through the ``_stats_on_*`` hooks; a stats request id is claimed here and
never reaches the deck's own result handling.
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field

from ..history import CAPABILITY, DURATION_STATUSES, GROUP_BYS, RANGES, percentile

STATS_WAIT_S = 10.0
_COUNT_FIELDS = ("blocked_count", "answered_count", "done_count")
_SUM_FIELDS = tuple(f"{s}_ms" for s in DURATION_STATUSES) + _COUNT_FIELDS


def local_tz_offset_min(now: float | None = None) -> int:
    """This machine's current UTC offset in minutes east (DST-aware)."""
    stamp = time.localtime(now if now is not None else time.time())
    return int(stamp.tm_gmtoff // 60)


@dataclass
class _Wait:
    server_id: str
    event: threading.Event = field(default_factory=threading.Event)
    data: dict | None = None
    error: str | None = None


class PendingStats:
    """Stats requests waiting for their bridge reply (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waits: dict[str, _Wait] = {}

    def register(self, req: str, server_id: str) -> _Wait:
        wait = _Wait(server_id)
        with self._lock:
            self._waits[req] = wait
        return wait

    def discard(self, req: str) -> None:
        with self._lock:
            self._waits.pop(req, None)

    def resolve(self, req: str | None, data: object) -> bool:
        if req is None:
            return False
        with self._lock:
            wait = self._waits.pop(req, None)
        if wait is None:
            return False
        if isinstance(data, dict):
            wait.data = data
        else:
            wait.error = "malformed stats reply"
        wait.event.set()
        return True

    def fail(self, req: str | None, message: str) -> bool:
        if req is None:
            return False
        with self._lock:
            wait = self._waits.pop(req, None)
        if wait is None:
            return False
        wait.error = message or "bridge error"
        wait.event.set()
        return True

    def fail_server(self, server_id: str, message: str) -> None:
        with self._lock:
            victims = [r for r, w in self._waits.items() if w.server_id == server_id]
            waits = [self._waits.pop(r) for r in victims]
        for wait in waits:
            wait.error = message
            wait.event.set()


def _num(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


def _samples(bucket: dict) -> list[int]:
    raw = bucket.get("answer_samples_ms")
    return [v for v in raw if type(v) is int and v >= 0] if isinstance(raw, list) else []


def _finish(bucket: dict, samples: list[int], *, recompute: bool) -> dict:
    if recompute:
        samples.sort()
        bucket["answer_median_ms"] = percentile(samples, 0.5)
        bucket["answer_p90_ms"] = percentile(samples, 0.9)
    bucket.pop("answer_samples_ms", None)
    return bucket


def _add(into: dict, src: dict) -> None:
    for name in _SUM_FIELDS:
        into[name] = into.get(name, 0) + _num(src.get(name))


def merge(results: list[tuple[str, dict]], group_by: str) -> dict:
    """Sum the per-bridge ``stats`` data (see module docstring)."""
    recompute = len(results) > 1
    total: dict = {name: 0 for name in _SUM_FIELDS}
    total_samples: list[int] = []
    groups: dict[str, dict] = {}
    group_samples: dict[str, list[int]] = {}
    days: dict[str, dict] = {}
    truncated = False
    first = results[0][1]
    for server_id, data in results:
        truncated = truncated or bool(data.get("truncated"))
        src_total = data.get("total") if isinstance(data.get("total"), dict) else {}
        _add(total, src_total)
        total_samples.extend(_samples(src_total))
        if not recompute:
            total["answer_median_ms"] = src_total.get("answer_median_ms")
            total["answer_p90_ms"] = src_total.get("answer_p90_ms")
        for group in data.get("groups") or []:
            if not isinstance(group, dict) or not isinstance(group.get("key"), str):
                continue
            key = group["key"]
            label = group.get("label") if isinstance(group.get("label"), str) else key
            if group_by == "agent":
                key = f"{server_id}:{key}"
                if recompute:
                    label = f"{label} ({server_id})"
            merged = groups.get(key)
            if merged is None:
                merged = groups[key] = {"key": key, "label": label} | {n: 0 for n in _SUM_FIELDS}
                group_samples[key] = []
            _add(merged, group)
            group_samples[key].extend(_samples(group))
            if not recompute:
                merged["answer_median_ms"] = group.get("answer_median_ms")
                merged["answer_p90_ms"] = group.get("answer_p90_ms")
        for day in data.get("days") or []:
            if not isinstance(day, dict) or not isinstance(day.get("day"), str):
                continue
            merged_day = days.get(day["day"])
            if merged_day is None:
                merged_day = days[day["day"]] = {
                    "day": day["day"],
                    "start_ms": _num(day.get("start_ms")),
                } | {n: 0 for n in _SUM_FIELDS}
            _add(merged_day, day)
    ordered = sorted(
        groups.values(),
        key=lambda g: (-sum(g[f"{s}_ms"] for s in DURATION_STATUSES), g["label"]),
    )
    return {
        "range_days": first.get("range_days"),
        "group_by": group_by,
        "from_ms": min(_num(d.get("from_ms")) for _, d in results),
        "to_ms": max(_num(d.get("to_ms")) for _, d in results),
        "truncated": truncated,
        "total": _finish(total, total_samples, recompute=recompute),
        "groups": [_finish(g, group_samples[g["key"]], recompute=recompute) for g in ordered],
        "days": [days[k] for k in sorted(days)],
    }


class StatsMixin:
    """Stats relay for ``LiveSource`` (uses its ``_runners``, ``_connected``
    and ``_lock``)."""

    def _stats_init(self) -> None:
        self._stats_pending = PendingStats()
        self._stats_reqs = itertools.count(1)

    # --- connector hooks ---------------------------------------------------
    def _stats_on_result(self, req: str | None, data: object) -> bool:
        return self._stats_pending.resolve(req, data)

    def _stats_on_error(self, req: str | None, message: str) -> bool:
        return self._stats_pending.fail(req, message)

    def _stats_on_connection(self, server_id: str, up: bool) -> None:
        if not up:
            self._stats_pending.fail_server(server_id, "disconnected")

    # --- operation -----------------------------------------------------------
    def stats(self, range_days: int, group_by: str, *, wait_s: float = STATS_WAIT_S) -> dict:
        tz = local_tz_offset_min()
        missing: list[dict] = []
        waits: list[tuple[str, str, _Wait]] = []
        with self._lock:
            connected = dict(self._connected)
        for server_id, runner in list(self._runners.items()):
            if not connected.get(server_id):
                missing.append({"server_id": server_id, "reason": "disconnected"})
                continue
            capabilities = getattr(getattr(runner, "connector", None), "capabilities", frozenset())
            if CAPABILITY not in capabilities:
                missing.append({"server_id": server_id, "reason": "unsupported"})
                continue
            req = f"st{next(self._stats_reqs)}"
            waits.append((server_id, req, self._stats_pending.register(req, server_id)))
            runner.send(
                {
                    "type": "stats",
                    "req": req,
                    "range_days": range_days,
                    "group_by": group_by,
                    "tz_offset_min": tz,
                }
            )
        deadline = time.monotonic() + wait_s
        results: list[tuple[str, dict]] = []
        for server_id, req, wait in waits:
            if not wait.event.wait(max(0.0, deadline - time.monotonic())):
                self._stats_pending.discard(req)
                missing.append({"server_id": server_id, "reason": "timeout"})
            elif wait.data is not None:
                results.append((server_id, wait.data))
            else:
                missing.append({"server_id": server_id, "reason": wait.error or "failed"})
        if not results:
            reasons = {m["reason"] for m in missing}
            if not missing or reasons <= {"unsupported"}:
                code = "unsupported"
            elif reasons <= {"disconnected", "unsupported"}:
                code = "disconnected"
            else:
                code = "failed"
            return {"ok": False, "code": code, "message": "no bridge answered", "missing": missing}
        return {
            "ok": True,
            "code": "ok",
            "servers": [server_id for server_id, _ in results],
            "missing": missing,
            **merge(results, group_by),
        }


# --- HTTP route helper (called from DeckApp's handler) ------------------------


def handle_get(source, params: dict) -> tuple[int, dict | None]:
    """GET /stats?range=1|7|30&group=agent|repo|agent_type."""
    if not callable(getattr(source, "stats", None)):
        return 404, None
    try:
        range_days = int(params.get("range", ["7"])[0])
    except (TypeError, ValueError):
        return 400, None
    group_by = params.get("group", ["agent"])[0]
    if range_days not in RANGES or group_by not in GROUP_BYS:
        return 400, None
    return 200, source.stats(range_days, group_by)
