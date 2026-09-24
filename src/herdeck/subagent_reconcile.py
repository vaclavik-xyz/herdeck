"""Bridge-side fallback that finishes subagents whose stop hook never came.

``herdeck-subagent-hook`` marks a subagent done when the agent calls its stop
hook. Hooks get lost (the agent was killed, the hook timed out, a background
subagent finished while its parent was busy), and then the spool shows the
subagent running until the 10-minute stale rule, and the ``⑂N`` badge stays
up. Every ``INTERVAL_S`` the bridge looks at the panes whose spool still has
running or stale entries and checks the provider's own transcripts:

* Claude Code: the parent transcript (``transcript``, the session's
  ``.jsonl``) records a finished subagent as a ``<task-notification>`` with
  ``<task-id>{id}</task-id>`` and ``<status>completed|failed|…</status>``
  (background subagents), or as a ``toolUseResult`` with ``agentId`` and
  ``status`` (foreground ones). The subagent's own transcript
  (``<session>/subagents/agent-<id>.jsonl``) is only used as a sign of life:
  a file written recently keeps the entry from going stale.
* Codex: the child thread's rollout (``rollout-*-<id>.jsonl`` under the
  ``sessions`` tree, whose ``session_meta`` names the ``parent_thread_id``)
  ends a finished turn with an ``event_msg`` ``task_complete`` (or
  ``turn_aborted``).

The latest record wins; anything it does not recognise leaves the entry as it
is, so a format change never finishes a subagent by mistake. Files are only
read from their tail (``TAIL_BYTES``) or head (``HEAD_BYTES``), all file work
runs in a worker thread under the hook's spool lock, and a changed summary is
reported to herdr as the same ``subagents`` token the hook writes. OpenCode
has no transcript to read; its plugin reports every state change itself.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import glob
import json
import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from . import subagent_hook as hook

log = logging.getLogger(__name__)

INTERVAL_S = 60.0
# Newest part of a transcript that is searched (a parent transcript can be
# many MiB; a finished subagent's record is near its end).
TAIL_BYTES = 1024 * 1024
CHILD_TAIL_BYTES = 256 * 1024
HEAD_BYTES = 64 * 1024
LOCK_BUDGET_S = 1.0
# A line longer than this is skipped (it is not the record we look for).
_MAX_LINE = 256 * 1024

_DONE = {"completed", "complete", "done", "success", "succeeded"}
_FAILED = {"failed", "failure", "error", "errored", "killed", "cancelled", "canceled", "aborted"}
_CLAUDE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_CODEX_ID_RE = re.compile(r"[A-Za-z0-9-]{1,64}")
_STATUS_RE = re.compile(r"<status>\s*([A-Za-z_-]{1,32})\s*</status>")


@dataclass(frozen=True)
class Finding:
    """What a transcript says about one subagent. ``status`` is "done",
    "failed", "running" (a later record shows it active again) or None
    (nothing recognisable)."""

    status: str | None = None
    ended_ms: int | None = None
    # Last sign of life (a transcript write), for the stale rule.
    seen_ms: int | None = None
    # The transcript that was found (persisted so it is not searched again).
    agent_transcript: str = ""


def _mtime_ms(path: str) -> int | None:
    try:
        return int(os.stat(path).st_mtime * 1000)
    except OSError:
        return None


def _tail_lines(path: str, nbytes: int) -> list[str]:
    """The complete lines in the last ``nbytes`` of ``path`` (oldest first);
    [] when unreadable."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - nbytes)
            fh.seek(start)
            data = fh.read(nbytes)
    except OSError:
        return []
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]  # the first one is cut
    return [
        line.decode("utf-8", errors="replace")
        for line in lines
        if line.strip() and len(line) <= _MAX_LINE
    ]


def _head_line(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            data = fh.read(HEAD_BYTES)
    except OSError:
        return ""
    return data.split(b"\n", 1)[0].decode("utf-8", errors="replace")


def _json(line: str) -> dict | None:
    try:
        value = json.loads(line)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _timestamp_ms(record: Mapping | None) -> int | None:
    stamp = record.get("timestamp") if isinstance(record, Mapping) else None
    if not isinstance(stamp, str):
        return None
    try:
        parsed = _dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return int(parsed.timestamp() * 1000)


def _classify(status: object) -> str | None:
    if not isinstance(status, str):
        return None
    status = status.strip().lower()
    if status in _DONE:
        return "done"
    if status in _FAILED:
        return "failed"
    if status in ("running", "async_launched", "started", "in_progress"):
        return "running"
    return None


# --- Claude Code ---------------------------------------------------------------


def _claude_record(line: str, agent_id: str) -> tuple[str | None, int | None] | None:
    """(status, at_ms) of a line recording ``agent_id``'s outcome; None when
    the line says nothing about it."""
    marker = f"<task-id>{agent_id}</task-id>"
    at = line.find(marker)
    if at >= 0:
        rest = line[at + len(marker):]
        cut = len(rest)
        for stop in ("</task-notification>", "<task-id>"):
            index = rest.find(stop)
            if index >= 0:
                cut = min(cut, index)
        match = _STATUS_RE.search(rest[:cut])
        if match:
            return _classify(match.group(1)), _timestamp_ms(_json(line))
    if '"toolUseResult"' in line:
        record = _json(line)
        result = record.get("toolUseResult") if record else None
        if isinstance(result, dict) and result.get("agentId") == agent_id:
            status = _classify(result.get("status"))
            if status is not None:
                return status, _timestamp_ms(record)
    return None


def claude_finding(entry: Mapping) -> Finding:
    agent_id = entry.get("id", "")
    if not isinstance(agent_id, str) or not _CLAUDE_ID_RE.fullmatch(agent_id):
        return Finding()
    transcript = entry.get("transcript") or ""
    agent_file = entry.get("agent_transcript") or ""
    if not agent_file and transcript.endswith(".jsonl"):
        agent_file = os.path.join(transcript[: -len(".jsonl")], "subagents", f"agent-{agent_id}.jsonl")
    seen = _mtime_ms(agent_file) if agent_file else None
    if not transcript.endswith(".jsonl"):
        return Finding(seen_ms=seen)
    for line in reversed(_tail_lines(transcript, TAIL_BYTES)):
        if agent_id not in line:
            continue
        record = _claude_record(line, agent_id)
        if record is None:
            continue
        status, at_ms = record
        if status is None:
            continue
        ended = at_ms if at_ms is not None else _mtime_ms(transcript)
        return Finding(status, ended if status != "running" else None, seen)
    return Finding(seen_ms=seen)


# --- Codex ------------------------------------------------------------------------


def _codex_candidates(sessions_dir: str, agent_id: str, started_ms: int, now_ms: int) -> list[str]:
    """Rollouts named for ``agent_id`` in the date directories around its
    start (Codex files rollouts under local YYYY/MM/DD)."""
    days = set()
    for ms in (started_ms - 86_400_000, started_ms, started_ms + 86_400_000, now_ms):
        day = _dt.datetime.fromtimestamp(ms / 1000)
        days.add((day.year, day.month, day.day))
    found: list[str] = []
    for year, month, day in sorted(days):
        pattern = os.path.join(
            glob.escape(sessions_dir), f"{year:04d}", f"{month:02d}", f"{day:02d}",
            f"rollout-*-{agent_id}.jsonl",
        )
        found.extend(glob.glob(pattern))
    return sorted(found)


def _codex_parent(path: str) -> str | None:
    meta = _json(_head_line(path))
    payload = meta.get("payload") if meta else None
    source = payload.get("source") if isinstance(payload, dict) else None
    spawn = source.get("subagent") if isinstance(source, dict) else None
    spawn = spawn.get("thread_spawn") if isinstance(spawn, dict) else None
    parent = spawn.get("parent_thread_id") if isinstance(spawn, dict) else None
    return parent if isinstance(parent, str) else None


def codex_finding(entry: Mapping, parent_session: str, sessions_default: str, now_ms: int) -> Finding:
    agent_id = entry.get("id", "")
    if not isinstance(agent_id, str) or not _CODEX_ID_RE.fullmatch(agent_id):
        return Finding()
    path = entry.get("agent_transcript") or ""
    if not path:
        sessions_dir = entry.get("sessions_dir") or sessions_default
        if not sessions_dir:
            return Finding()
        for candidate in _codex_candidates(sessions_dir, agent_id, entry["started_ms"], now_ms):
            parent = _codex_parent(candidate)
            # Another pane's thread with a colliding id is never ours.
            if parent and parent_session and parent != parent_session:
                continue
            path = candidate
            break
    if not path:
        return Finding()
    seen = _mtime_ms(path)
    for line in reversed(_tail_lines(path, CHILD_TAIL_BYTES)):
        if '"event_msg"' not in line:
            continue
        record = _json(line)
        payload = record.get("payload") if record and record.get("type") == "event_msg" else None
        kind = payload.get("type") if isinstance(payload, dict) else None
        if kind == "task_complete":
            return Finding("done", _timestamp_ms(record) or seen, seen, path)
        if kind == "turn_aborted":
            return Finding("failed", _timestamp_ms(record) or seen, seen, path)
        if kind in ("task_started", "user_message"):
            return Finding("running", None, seen, path)
    return Finding(None, None, seen, path)


# --- spool --------------------------------------------------------------------------


def _finding(entry: dict, spool: dict, sessions_default: str, now_ms: int) -> Finding:
    provider = entry.get("provider")
    try:
        if provider == "claude":
            return claude_finding(entry)
        if provider == "codex":
            return codex_finding(entry, spool.get("session_id", ""), sessions_default, now_ms)
    except Exception:  # noqa: BLE001 - format drift must never break the loop
        log.debug("subagent transcript check failed for %s", entry.get("id"), exc_info=True)
    return Finding()


def _apply(entry: dict, finding: Finding, now_ms: int) -> bool:
    changed = False
    if finding.agent_transcript and not entry.get("agent_transcript"):
        entry["agent_transcript"] = hook._clean_path(finding.agent_transcript)
        changed = bool(entry["agent_transcript"])
    if finding.status in ("done", "failed"):
        ended = finding.ended_ms if finding.ended_ms is not None else now_ms
        entry["status"] = finding.status
        entry["ended_ms"] = min(max(ended, entry["started_ms"]), now_ms)
        entry["last_seen_ms"] = max(entry["last_seen_ms"], entry["ended_ms"])
        return True
    seen = finding.seen_ms
    if seen is not None and seen > entry["last_seen_ms"]:
        entry["last_seen_ms"] = min(seen, now_ms)
        if entry["status"] == "stale" and now_ms - entry["last_seen_ms"] <= hook.STALE_AFTER_MS:
            entry["status"] = "running"
        changed = True
    return changed


def has_open_entries(entries: list[dict]) -> bool:
    return any(e.get("status") in ("running", "stale") for e in entries)


def reconcile_spool(
    pane_id: str,
    now_ms: int,
    *,
    env: Mapping[str, str] | None = None,
    sessions_default: str = "",
) -> str | None:
    """Reconcile one pane's spool from transcripts (blocking file IO: call it
    off the event loop). Returns the summary token to report to herdr when it
    differs from the last reported one, else None."""
    path = hook.spool_path(pane_id, env)
    with hook._SpoolLock(path, time.monotonic() + LOCK_BUDGET_S) as locked:
        if not locked or not os.path.exists(path):
            return None
        spool = hook.load_spool(path, pane_id)
        changed = False
        for entry in spool["entries"]:
            if entry["status"] not in ("running", "stale"):
                continue
            changed = _apply(entry, _finding(entry, spool, sessions_default, now_ms), now_ms) or changed
        changed = hook.apply_stale_rules(spool, now_ms) or changed
        if changed:
            hook.write_spool(path, spool)
        token = hook.format_token(*hook.summarize(spool))
        reported = spool.get("reported") or {}
        return token if reported.get("token") != token else None


def mark_reported(pane_id: str, token: str, now_ms: int, env: Mapping[str, str] | None = None) -> None:
    """Record ``token`` as reported (after herdr accepted it), so the hook's
    next event does not report it again — unless the spool moved on since."""
    path = hook.spool_path(pane_id, env)
    with hook._SpoolLock(path, time.monotonic() + LOCK_BUDGET_S) as locked:
        if not locked or not os.path.exists(path):
            return
        spool = hook.load_spool(path, pane_id)
        if hook.format_token(*hook.summarize(spool)) != token:
            return
        spool["reported"] = {"token": token, "at_ms": now_ms}
        hook.write_spool(path, spool)


def default_codex_sessions(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    home = env.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    return os.path.join(home, "sessions")


class SubagentReconciler:
    """Runs ``reconcile_spool`` every ``interval`` for the panes the spool
    reader last saw with running or stale subagents (bridge event loop)."""

    def __init__(
        self,
        reader,
        herdr,
        *,
        interval: float = INTERVAL_S,
        clock: Callable[[], float] = time.time,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._reader = reader
        self._herdr = herdr
        self._interval = interval
        self._clock = clock
        self._env = env
        self._sessions_default = default_codex_sessions(env)

    def _reconcile_all(self, panes: list[str], now_ms: int) -> dict[str, str]:
        tokens = {}
        for pane in panes:
            try:
                token = reconcile_spool(
                    pane, now_ms, env=self._env, sessions_default=self._sessions_default
                )
            except OSError as exc:
                log.debug("subagent reconcile of %s failed: %s", pane, exc)
                continue
            if token is not None:
                tokens[pane] = token
        return tokens

    async def tick(self) -> dict[str, str]:
        """One pass; returns the tokens reported to herdr, by pane."""
        panes = self._reader.open_panes()
        if not panes:
            return {}
        now_ms = int(self._clock() * 1000)
        tokens = await asyncio.to_thread(self._reconcile_all, panes, now_ms)
        reported = {}
        for pane, token in tokens.items():
            try:
                await self._herdr.report_metadata(
                    pane, hook.METADATA_SOURCE, {hook.TOKEN_NAME: token}, hook.TOKEN_TTL_MS
                )
            except Exception as exc:  # noqa: BLE001 - herdr down: the next pass retries
                log.debug("subagent token report for %s failed: %s", pane, exc)
                continue
            await asyncio.to_thread(mark_reported, pane, token, now_ms, self._env)
            reported[pane] = token
        if reported:
            log.info("subagent reconcile updated %d pane(s)", len(reported))
        return reported

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a fallback must never stop the bridge
                log.warning("subagent reconcile failed", exc_info=True)
