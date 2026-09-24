"""Runtime side of the bridge self-update: ``/maintenance/servers/{id}/update``.

``POST /maintenance/servers/{id}/update`` (body ``{"wait_ms": N}``, optional)
asks that server's bridge to install *this runtime's* herdeck version
(``{"type": "update", "req": "uN", "version": __version__}``, see
``herdeck/self_update.py`` for the bridge half) and waits up to ``wait_ms``
(default ``UPDATE_WAIT_DEFAULT_S``, capped at ``UPDATE_WAIT_MAX_S``) for the
outcome. A pip install can outlast that, so the answer may be ``pending``; the
window then long-polls ``GET /maintenance/servers/{id}/update?after=<next>&
wait_ms=N``, which returns as soon as a new progress line or the outcome lands.
A POST while an update of that server is still running joins it instead of
sending a second one.

Every answer is one JSON shape::

    {"ok": bool, "code": str, "message": str, "server_id": str,
     "target": str | null, "output": str,          # installer tail on failure
     "progress": [{"seq": int, "stage": str, "message": str}, ...],  # after `after`
     "next": int}                                   # the cursor for the next poll

``code`` is one of ``updated`` · ``pending`` · ``not_managed`` · ``readonly`` ·
``failed`` · ``busy`` (another client's update is running on that bridge) ·
``current`` / ``newer`` (the bridge already runs this runtime's version or a
newer one: nothing is sent, the bridge is never pulled back) · ``downgrade``
(the bridge refused an older target) ·
``unsupported`` (the bridge predates self-update, or is not a herdeck bridge)
· ``disconnected`` (the server is not connected, so nothing was sent).
``ok`` is true for ``updated``, ``pending``, ``current`` and ``newer``.

``LiveSource`` mixes in ``BridgeUpdateMixin`` and routes its connector
callbacks (result, error, progress, connection, snapshot) through the
``_bridge_update_on_*`` hooks; an update's request id is claimed here and
never reaches the deck's own result handling.
"""

from __future__ import annotations

import itertools
import re
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import unquote

from .. import __version__
from ..self_update import compare_versions

UPDATE_WAIT_DEFAULT_S = 15.0
UPDATE_WAIT_MAX_S = 25.0
# Longer than the bridge's own worst case (downloads + a 600 s install + the
# verify): past this an unanswered update is reported failed, not pending.
JOB_DEADLINE_S = 900.0
PROGRESS_KEEP = 200
SELF_UPDATE_CAPABILITY = "self_update"

_UNKNOWN_UPDATE = "unknown client message: update"
_ROUTE_RE = re.compile(r"^/maintenance/servers/([^/]+)/update$")


def route_server_id(path: str) -> str | None:
    """The server id of a ``/maintenance/servers/{id}/update`` path, else None."""
    match = _ROUTE_RE.fullmatch(path)
    if match is None:
        return None
    server_id = unquote(match.group(1))
    return server_id or None


def _outcome(code: str, message: str = "", **extra) -> dict:
    ok = code in ("updated", "pending", "current", "newer")
    return {"ok": ok, "code": code, "message": message, **extra}


def result_outcome(data: object) -> dict:
    """Map the bridge's ``update`` result payload onto an outcome."""
    if not isinstance(data, dict):
        return _outcome("failed", "malformed update reply")
    updated = data.get("updated")
    if isinstance(updated, str) and updated:
        if data.get("unchanged"):
            return _outcome("updated", f"already at {updated}")
        return _outcome("updated", f"updated to {updated}; restarting")
    error = data.get("error")
    if not isinstance(error, dict):
        return _outcome("failed", "malformed update reply")
    code = error.get("code")
    message = error.get("message") if isinstance(error.get("message"), str) else ""
    output = error.get("output") if isinstance(error.get("output"), str) else ""
    if code in ("not_managed", "busy", "downgrade"):
        return _outcome(code, message)
    return _outcome("failed", message or str(code or "update failed"), output=output)


def error_outcome(message: str) -> dict:
    """A bridge ``error`` frame naming the update request."""
    if message.startswith("read-only token"):
        return _outcome("readonly", message)
    return _outcome("failed", message or "bridge error")


@dataclass
class _Job:
    server_id: str
    req: str
    target: str
    started: float
    progress: list[dict] = field(default_factory=list)
    seq: int = 0
    outcome: dict | None = None
    disconnected: bool = False


class BridgeUpdates:
    """Thread-safe registry of update jobs (one live job per server). Connector
    callbacks arrive on connector threads; HTTP handlers wait on ``_cond``."""

    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._cond = threading.Condition()
        self._jobs: dict[str, _Job] = {}  # latest job per server
        self._by_req: dict[str, _Job] = {}  # unfinished jobs by request id

    def begin(self, server_id: str, req: str, target: str) -> tuple[_Job, bool]:
        """Start a job, or join the unfinished one for ``server_id``."""
        with self._cond:
            current = self._jobs.get(server_id)
            if current is not None and current.outcome is None and not self._expired(current):
                return current, False
            job = _Job(server_id, req, target, self._clock())
            self._jobs[server_id] = job
            self._by_req[req] = job
            return job, True

    def latest(self, server_id: str) -> _Job | None:
        with self._cond:
            return self._jobs.get(server_id)

    def latest_running(self, server_id: str) -> _Job | None:
        with self._cond:
            job = self._jobs.get(server_id)
            return job if job is not None and job.outcome is None and not self._expired(job) else None

    def _finish(self, job: _Job, outcome: dict) -> None:
        if job.outcome is None:
            job.outcome = outcome
        self._by_req.pop(job.req, None)
        self._cond.notify_all()

    def _expired(self, job: _Job) -> bool:
        return job.outcome is None and self._clock() - job.started > JOB_DEADLINE_S

    # --- connector hooks (return True when the frame belonged to an update) ---
    def on_result(self, req: str | None, data: object) -> bool:
        with self._cond:
            job = self._by_req.get(req) if req is not None else None
            if job is None:
                return False
            self._finish(job, result_outcome(data))
            return True

    def on_error(self, server_id: str, req: str | None, message: str) -> bool:
        with self._cond:
            if req is not None:
                job = self._by_req.get(req)
                if job is None:
                    return False
                self._finish(job, error_outcome(message))
                return True
            # An anonymous error is what a bridge that does not know the
            # message raises ("unknown client message: update").
            job = self._jobs.get(server_id)
            if job is None or job.outcome is not None or _UNKNOWN_UPDATE not in (message or ""):
                return False
            self._finish(job, _outcome("unsupported", message))
            return True

    def on_progress(self, req: str | None, stage: str, message: str) -> bool:
        with self._cond:
            job = self._by_req.get(req) if req is not None else None
            if job is None:
                return False
            job.seq += 1
            job.progress.append({"seq": job.seq, "stage": stage, "message": message})
            del job.progress[:-PROGRESS_KEEP]
            self._cond.notify_all()
            return True

    def on_connection(self, server_id: str, up: bool) -> None:
        """A connection drop before the reply: the bridge may be restarting
        into the new version (its reply lost) or may have died. The next
        snapshot's ``herdeck_version`` settles which (``on_bridge_version``)."""
        if up:
            return
        with self._cond:
            job = self._jobs.get(server_id)
            if job is not None and job.outcome is None:
                job.disconnected = True
                self._cond.notify_all()

    def on_bridge_version(self, server_id: str, version: str | None) -> None:
        """Only the target version settles a job whose reply was lost. A
        reconnect at the old version proves nothing (the bridge may still be
        installing, or have restarted mid-install): the job stays pending,
        bounded by ``JOB_DEADLINE_S``."""
        with self._cond:
            job = self._jobs.get(server_id)
            if job is None or job.outcome is not None or not job.disconnected:
                return
            if version == job.target:
                self._finish(job, _outcome("updated", f"bridge reconnected at {version}"))

    # --- HTTP side -----------------------------------------------------------
    def wait(self, job: _Job, after: int, timeout: float) -> dict:
        """Block up to ``timeout`` s for progress past ``after`` or the outcome."""
        deadline = self._clock() + max(0.0, timeout)
        with self._cond:
            while True:
                if self._expired(job):
                    self._finish(
                        job,
                        _outcome(
                            "failed", f"no reply from the bridge within {JOB_DEADLINE_S:.0f}s"
                        ),
                    )
                if job.outcome is not None or job.seq > after:
                    break
                remaining = deadline - self._clock()
                if remaining <= 0:
                    break
                self._cond.wait(min(remaining, 1.0))
            return self._view(job, after)

    def _view(self, job: _Job, after: int) -> dict:
        if job.outcome is not None:
            base = dict(job.outcome)
        else:
            note = (
                "bridge disconnected; waiting for it to come back"
                if job.disconnected
                else "update running"
            )
            base = _outcome("pending", note)
        base.setdefault("output", "")
        base.update(
            server_id=job.server_id,
            target=job.target,
            progress=[p for p in job.progress if p["seq"] > after],
            next=job.seq,
        )
        return base


class BridgeUpdateMixin:
    """Bridge self-update for ``LiveSource`` (uses its ``_servers``,
    ``_runners`` and ``_connected``)."""

    def _bridge_update_init(self) -> None:
        self._bridge_updates = BridgeUpdates()
        self._bridge_update_reqs = itertools.count(1)

    # --- connector hooks ---------------------------------------------------
    def _bridge_update_on_result(self, req: str | None, data: object) -> bool:
        return self._bridge_updates.on_result(req, data)

    def _bridge_update_on_error(self, server_id: str, req: str | None, message: str) -> bool:
        return self._bridge_updates.on_error(server_id, req, message)

    def _on_bridge_error(self, server_id: str, req: str | None, message: str) -> None:
        """Connector ``on_request_error``: an update's own error stops here; an
        anonymous one still reaches the card requests too (it may be theirs)."""
        if self._bridge_update_on_error(server_id, req, message) and req is not None:
            return
        stats_error = getattr(self, "_stats_on_error", None)
        if callable(stats_error) and stats_error(req, message):
            return  # a GET /stats request's own error (stats.py)
        self._on_request_error(server_id, req, message)

    def _on_progress(self, server_id: str, req: str | None, stage: str, message: str) -> None:
        self._bridge_updates.on_progress(req, stage, message)

    def _bridge_update_on_connection(self, server_id: str, up: bool) -> None:
        self._bridge_updates.on_connection(server_id, up)

    def _bridge_update_on_snapshot(self, server_id: str) -> None:
        connector = getattr(self._runners.get(server_id), "connector", None)
        health = getattr(connector, "health", None)
        if callable(health):
            self._bridge_updates.on_bridge_version(server_id, health().get("bridge_version"))

    # --- operations ----------------------------------------------------------
    def bridge_update(self, server_id: str, wait_s: float) -> dict | None:
        """Send (or join) an update of ``server_id``'s bridge to this runtime's
        version. None = unknown server (404)."""
        if server_id not in self._servers:
            return None
        base = {"server_id": server_id, "target": __version__, "output": "", "progress": [], "next": 0}
        runner = self._runners.get(server_id)
        with self._lock:
            connected = bool(self._connected.get(server_id))
        if runner is None or not connected:
            return {**_outcome("disconnected", "the server is not connected"), **base}
        connector = getattr(runner, "connector", None)
        capabilities = getattr(connector, "capabilities", frozenset())
        if SELF_UPDATE_CAPABILITY not in capabilities:
            return {
                **_outcome(
                    "unsupported",
                    "this bridge cannot update itself (herdeck before self-update, or not "
                    "a herdeck bridge); update it by hand once",
                ),
                **base,
            }
        health = getattr(connector, "health", None)
        bridge_version = health().get("bridge_version") if callable(health) else None
        order = compare_versions(bridge_version, __version__) if bridge_version else None
        if order is not None and order >= 0 and self._bridge_updates.latest_running(server_id) is None:
            # Never pull a bridge back: several runtimes may share it, and an
            # older one must not downgrade it to its own version.
            code = "current" if order == 0 else "newer"
            return {
                **_outcome(code, f"the bridge already runs {bridge_version}"),
                **base,
            }
        req = f"u{next(self._bridge_update_reqs)}"
        job, created = self._bridge_updates.begin(server_id, req, __version__)
        if created:
            runner.send({"type": "update", "req": req, "version": __version__})
        return self._bridge_updates.wait(job, 0, wait_s)

    def bridge_update_status(self, server_id: str, after: int, wait_s: float) -> dict | None:
        """The latest update of ``server_id`` (long-poll). None = none yet."""
        if server_id not in self._servers:
            return None
        job = self._bridge_updates.latest(server_id)
        if job is None:
            return None
        return self._bridge_updates.wait(job, after, wait_s)


# --- HTTP route helpers (called from DeckApp's handler) ----------------------


def _wait_s(raw: object, default: float) -> float | None:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    return min(UPDATE_WAIT_MAX_S, max(0.0, float(raw) / 1000.0))


def handle_post(source, path: str, body: dict) -> tuple[int, dict | None]:
    """POST /maintenance/servers/{id}/update [{"wait_ms": N}]."""
    server_id = route_server_id(path)
    if server_id is None or not callable(getattr(source, "bridge_update", None)):
        return 404, None
    wait_s = _wait_s(body.get("wait_ms"), UPDATE_WAIT_DEFAULT_S)
    if wait_s is None:
        return 400, None
    result = source.bridge_update(server_id, wait_s)
    return (200, result) if result is not None else (404, None)


def handle_get(source, path: str, params: dict) -> tuple[int, dict | None]:
    """GET /maintenance/servers/{id}/update?after=N&wait_ms=M."""
    server_id = route_server_id(path)
    if server_id is None or not callable(getattr(source, "bridge_update_status", None)):
        return 404, None
    try:
        after = max(0, int(params.get("after", ["0"])[0]))
        wait_ms = int(params.get("wait_ms", ["0"])[0])
    except (TypeError, ValueError):
        return 400, None
    wait_s = _wait_s(wait_ms, 0.0)
    result = source.bridge_update_status(server_id, after, wait_s or 0.0)
    return (200, result) if result is not None else (404, None)
