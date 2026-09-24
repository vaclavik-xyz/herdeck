"""``herdeck-subagent-hook``: count an agent's subagents for the deck tile.

Claude Code and Codex call this as a command hook (see README "Subagent
tracking"). It reads the hook JSON from stdin, keeps a small per-pane spool of
subagents in ``~/.cache/herdeck/subagents/<pane>.json`` and reports a summary
token ``subagents=<running>/<total>`` on the herdr pane, which the bridge
forwards as pane metadata and the deck draws as a "⑂N" badge.

Rules this module must keep, because it runs inside the agent's own loop:

* stdlib only, no network (only the local ``herdr`` CLI);
* never print to stdout and always exit 0 — for both Claude Code and Codex a
  hook that exits 0 with no output changes nothing about the agent's turn;
* a hard 2 s budget: a watchdog ends the process even if herdr hangs;
* no ``HERDR_PANE_ID`` in the environment (not a herdr pane) -> no-op.

Handled events (anything else is a no-op):

* ``SubagentStart`` / ``SubagentStop`` (Claude and Codex): add / finish an entry.
* Claude ``PostToolUse`` for the ``Agent`` (legacy ``Task``) tool: a
  background launch (``async_launched``) adds a running entry, a completed or
  failed foreground call finishes it — a fallback for a missed start/stop.
* ``PreToolUse`` fired inside a subagent (payload carries ``agent_id``): a
  heartbeat that keeps the entry from going stale.
* ``SessionStart`` with source ``startup``/``clear`` (or a new session id):
  resets the spool.
* OpenCode (``--provider opencode``): the ``herdeck-subagents.js`` plugin
  (``assets/opencode``) reports its child sessions (the ones with a
  ``parentID``) as ``session.created`` (start), ``session.status`` busy
  (heartbeat), ``session.idle`` / ``session.deleted`` (stop) and
  ``session.error`` (fail); see ``parse_opencode_event`` for the payload.

Entries also remember where the provider's transcripts live (``transcript``,
``agent_transcript``, ``sessions_dir``; absolute paths, never sent on the
wire) so the bridge's reconciler (``subagent_reconcile``) can finish a
subagent whose stop hook never arrived.

Stale rule: a running entry without a heartbeat for 10 min is marked stale;
stale entries are dropped 30 min after they were last seen. The spool keeps at
most 20 entries. The spool format is read by the bridge (S2), so keep
``spool_path`` and the entry keys stable.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

try:  # POSIX only; without it the spool is simply not locked.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

SPOOL_VERSION = 1
MAX_ENTRIES = 20
STALE_AFTER_MS = 10 * 60 * 1000
DROP_STALE_AFTER_MS = 30 * 60 * 1000
TOKEN_TTL_MS = 15 * 60 * 1000
# Re-report an unchanged token well inside its TTL so herdr keeps it.
REPORT_REFRESH_MS = 5 * 60 * 1000
# A heartbeat that changes nothing else only rewrites the spool this often.
HEARTBEAT_WRITE_MS = 30 * 1000
BUDGET_S = 2.0
TOKEN_NAME = "subagents"
METADATA_SOURCE = "herdeck:subagents"
STATUSES = ("running", "done", "failed", "stale")
# Claude's subagent tool (renamed from "Task" to "Agent").
_AGENT_TOOLS = {"Agent", "Task"}
_MAX_STDIN = 4 * 2**20
_PANE_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")
_FIELD_MAX = {"id": 128, "type": 64, "description": 160, "model": 64, "provider": 16}
PROVIDERS = ("claude", "codex", "opencode")
# Transcript hints kept per entry for the bridge's reconciler (not on the wire).
PATH_FIELDS = ("transcript", "agent_transcript", "sessions_dir")
_PATH_MAX = 1024

Reporter = Callable[[str, str, float], None]


def spool_dir(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    override = env.get("HERDECK_SUBAGENT_SPOOL_DIR")
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".cache", "herdeck", "subagents")


def spool_path(pane_id: str, env: Mapping[str, str] | None = None) -> str:
    """The spool file for a herdr pane id (sanitised into a file name)."""
    name = _PANE_SAFE_RE.sub("_", pane_id)[:96] or "_"
    if name.startswith("."):
        name = "_" + name[1:]
    return os.path.join(spool_dir(env), f"{name}.json")


def format_token(running: int, total: int) -> str:
    return f"{running}/{total}"


# --- spool -------------------------------------------------------------------


def _empty_spool(pane_id: str, session_id: str = "") -> dict:
    return {"version": SPOOL_VERSION, "pane": pane_id, "session_id": session_id, "entries": []}


def _clean_str(value: object, key: str) -> str:
    return value.strip()[: _FIELD_MAX[key]] if isinstance(value, str) else ""


def _clean_path(value: object) -> str:
    """An absolute, single-line path of sane length; "" for anything else."""
    if not isinstance(value, str) or len(value) > _PATH_MAX or not os.path.isabs(value):
        return ""
    if any(ch in value for ch in "\x00\n\r"):
        return ""
    return value


def _clean_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _clean_entry(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    entry_id = _clean_str(raw.get("id"), "id")
    status = raw.get("status")
    if not entry_id or status not in STATUSES:
        return None
    started = _clean_int(raw.get("started_ms"))
    last_seen = _clean_int(raw.get("last_seen_ms"))
    if started is None or last_seen is None:
        return None
    entry = {
        "id": entry_id,
        "provider": _clean_str(raw.get("provider"), "provider"),
        "type": _clean_str(raw.get("type"), "type"),
        "description": _clean_str(raw.get("description"), "description"),
        "model": _clean_str(raw.get("model"), "model"),
        "depth": _clean_int(raw.get("depth")),
        "started_ms": started,
        "ended_ms": _clean_int(raw.get("ended_ms")),
        "status": status,
        "last_seen_ms": last_seen,
    }
    for key in PATH_FIELDS:
        entry[key] = _clean_path(raw.get(key))
    return entry


def load_spool(path: str, pane_id: str) -> dict:
    """The spool at ``path``; a fresh one when missing or unreadable (a
    corrupt spool is replaced, never fatal)."""
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return _empty_spool(pane_id)
    if not isinstance(raw, dict) or raw.get("version") != SPOOL_VERSION:
        return _empty_spool(pane_id)
    spool = _empty_spool(pane_id, raw.get("session_id") if isinstance(raw.get("session_id"), str) else "")
    entries = raw.get("entries") if isinstance(raw.get("entries"), list) else []
    spool["entries"] = [e for e in (_clean_entry(r) for r in entries) if e is not None]
    reported = raw.get("reported")
    if (
        isinstance(reported, dict)
        and isinstance(reported.get("token"), str)
        and _clean_int(reported.get("at_ms")) is not None
    ):
        spool["reported"] = {"token": reported["token"], "at_ms": reported["at_ms"]}
    return spool


def write_spool(path: str, spool: dict) -> None:
    """Atomically replace the spool (0600 temp file + rename)."""
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(spool, fh, separators=(",", ":"))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class _SpoolLock:
    """An exclusive ``flock`` on ``<spool>.lock``, given up at ``deadline``."""

    def __init__(self, path: str, deadline: float) -> None:
        self._path = path + ".lock"
        self._deadline = deadline
        self._fd: int | None = None

    def __enter__(self) -> bool:
        if fcntl is None:
            return True
        os.makedirs(os.path.dirname(self._path), mode=0o700, exist_ok=True)
        self._fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                if time.monotonic() >= self._deadline:
                    return False
                time.sleep(0.01)

    def __exit__(self, *exc) -> None:
        if self._fd is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None


def summarize(spool: dict) -> tuple[int, int]:
    entries = spool.get("entries", [])
    return sum(1 for e in entries if e["status"] == "running"), len(entries)


def apply_stale_rules(spool: dict, now_ms: int) -> bool:
    """Mark silent running entries stale and drop long-stale ones; True when
    anything changed."""
    changed = False
    kept = []
    for entry in spool["entries"]:
        idle = now_ms - entry["last_seen_ms"]
        if entry["status"] == "running" and idle > STALE_AFTER_MS:
            entry["status"] = "stale"
            changed = True
        if entry["status"] == "stale" and idle > DROP_STALE_AFTER_MS:
            changed = True
            continue
        kept.append(entry)
    spool["entries"] = kept
    return changed


def _bound(spool: dict) -> None:
    """Keep at most MAX_ENTRIES: finished entries go first (oldest first),
    running ones only when nothing else is left to drop."""
    entries = spool["entries"]
    while len(entries) > MAX_ENTRIES:
        finished = [e for e in entries if e["status"] != "running"]
        pool = finished or entries
        victim = min(pool, key=lambda e: (e["ended_ms"] or e["last_seen_ms"], e["started_ms"]))
        entries.remove(victim)


# --- events ------------------------------------------------------------------


@dataclass
class Event:
    """One normalised hook event."""

    kind: str  # start | stop | fail | heartbeat | reset | session | ignore
    agent_id: str = ""
    provider: str = ""
    agent_type: str = ""
    description: str = ""
    model: str = ""
    depth: int | None = None
    # The parent session this event proves the pane is in ("" = unknown).
    session_id: str = ""
    # Transcript hints for the reconciler (see PATH_FIELDS).
    transcript: str = ""
    agent_transcript: str = ""
    sessions_dir: str = ""


def detect_provider(
    payload: Mapping, override: str | None = None, env: Mapping[str, str] | None = None
) -> str:
    """``--provider`` wins (the README snippets pass it); else Codex's
    ``turn_id`` payload extension (subagent/tool events) or the
    ``CODEX_THREAD_ID`` Codex exports to its hooks (its SessionStart has no
    turn_id); else Claude."""
    if override in PROVIDERS:
        return override
    if "turn_id" in payload or (env or {}).get("CODEX_THREAD_ID"):
        return "codex"
    return "claude"


def _str(payload: Mapping, key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def _parent_session(payload: Mapping, provider: str, env: Mapping[str, str]) -> str:
    """The payload's session id when it is known to be the pane's own (parent)
    session. Codex runs hooks for child threads in the same process, so there
    the session only counts when it matches ``CODEX_THREAD_ID``."""
    session = _str(payload, "session_id")
    if provider == "codex":
        thread = env.get("CODEX_THREAD_ID", "")
        return session if thread and session == thread else ""
    return session


def codex_sessions_dir(payload: Mapping, env: Mapping[str, str]) -> str:
    """Codex's rollout tree (``<CODEX_HOME>/sessions``), where the reconciler
    looks for a child thread's rollout: the ``sessions`` ancestor of the
    payload's transcript when there is one, else ``$CODEX_HOME/sessions``,
    else ``~/.codex/sessions``."""
    transcript = _clean_path(payload.get("transcript_path"))
    if transcript:
        head = os.path.dirname(transcript)
        for _ in range(5):
            if os.path.basename(head) == "sessions":
                return head
            head = os.path.dirname(head)
    home = env.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    return _clean_path(os.path.join(home, "sessions"))


def _paths(payload: Mapping, provider: str, env: Mapping[str, str]) -> dict:
    """Transcript hints of a parent-side event (start/stop/Agent tool)."""
    if provider == "codex":
        return {"sessions_dir": codex_sessions_dir(payload, env)}
    return {
        "transcript": _clean_path(payload.get("transcript_path")),
        "agent_transcript": _clean_path(payload.get("agent_transcript_path")),
    }


def parse_event(payload: Mapping, provider: str, env: Mapping[str, str]) -> Event:
    if provider == "opencode":
        return parse_opencode_event(payload)
    name = _str(payload, "hook_event_name")
    agent_id = _str(payload, "agent_id")
    session = _parent_session(payload, provider, env)
    common = {
        "provider": provider,
        "agent_type": _str(payload, "agent_type"),
        "model": _str(payload, "model") if provider == "codex" else "",
    }
    if name in ("SubagentStart", "SubagentStop") and agent_id:
        kind = "start" if name == "SubagentStart" else "stop"
        paths = _paths(payload, provider, env)
        return Event(kind, agent_id, session_id=session, **common, **paths)
    if name == "PreToolUse" and agent_id:
        # Inside a subagent: never a proof of the parent session (a Codex
        # child thread carries its own id).
        return Event("heartbeat", agent_id, **common)
    if name == "PostToolUse" and _str(payload, "tool_name") in _AGENT_TOOLS:
        # agent_id here (a subagent spawning its own) is the caller, not the
        # spawned agent: that one is the response's agentId.
        event = _agent_tool_event(payload, provider, session)
        if event.kind != "ignore":
            for key, value in _paths(payload, provider, env).items():
                setattr(event, key, value)
        return event
    if name == "SessionStart" and not agent_id and session:
        source = _str(payload, "source")
        return Event("reset" if source in ("startup", "clear") else "session", session_id=session)
    return Event("ignore")


# OpenCode's task tool titles a child session "<description> (@<agent> subagent)".
_OPENCODE_TITLE_RE = re.compile(r"^(.*?)\s*\(@([A-Za-z0-9_.-]{1,64}) subagent\)\s*$")
_OPENCODE_BUSY = {"busy", "active", "pending", "retry", "running", "streaming", "working"}


def parse_opencode_event(payload: Mapping) -> Event:
    """One event from the OpenCode plugin (``assets/opencode/herdeck-subagents.js``).

    Payload: ``{"hook_event_name": "session.created" | "session.status" |
    "session.idle" | "session.error" | "session.deleted", "session_id": <child
    session>, "parent_id": <its parent>, "root_session_id": <the pane's root
    session>, "depth": <int>, "title": str, "agent": str, "model": str,
    "status": <session.status type>}``. Only child sessions are reported; one
    without a ``parent_id`` is ignored. The root session is the pane's own
    (parent) session, so a new root resets the spool like Claude's."""
    name = _str(payload, "hook_event_name")
    child = _str(payload, "session_id")
    if not child or not _str(payload, "parent_id"):
        return Event("ignore")
    title = _str(payload, "title").strip()
    agent_type = _str(payload, "agent")
    match = _OPENCODE_TITLE_RE.match(title)
    if match:
        title = match.group(1)
        agent_type = agent_type or match.group(2)
    depth = _clean_int(payload.get("depth"))
    fields = {
        "provider": "opencode",
        "agent_type": agent_type,
        "description": title,
        "model": _str(payload, "model"),
        "depth": depth if depth is not None and depth <= 64 else None,
        "session_id": _str(payload, "root_session_id"),
    }
    if name == "session.created":
        return Event("start", child, **fields)
    if name == "session.status":
        kind = _str(payload, "status").lower()
        if kind == "idle":
            return Event("stop", child, **fields)
        if kind in _OPENCODE_BUSY:
            return Event("heartbeat", child, **fields)
        return Event("ignore")
    if name in ("session.idle", "session.deleted"):
        return Event("stop", child, **fields)
    if name == "session.error":
        return Event("fail", child, **fields)
    return Event("ignore")


def _agent_tool_event(payload: Mapping, provider: str, session: str) -> Event:
    response = payload.get("tool_response")
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    if not isinstance(response, dict):
        return Event("ignore")
    agent_id = _str(response, "agentId")
    status = _str(response, "status").lower()
    if not agent_id or not status:
        return Event("ignore")
    fields = {
        "provider": provider,
        "agent_type": _str(response, "agentType") or _str(tool_input, "subagent_type"),
        "description": _str(response, "description") or _str(tool_input, "description"),
        "session_id": session,
    }
    if status == "async_launched":
        return Event("start", agent_id, **fields)
    if status == "completed":
        return Event("stop", agent_id, **fields)
    if any(word in status for word in ("fail", "error", "kill", "cancel")):
        return Event("fail", agent_id, **fields)
    return Event("ignore")


def claude_meta(payload: Mapping, agent_id: str) -> dict:
    """Description/model/depth/type from Claude's
    ``<session dir>/subagents/agent-<id>.meta.json`` (empty when absent)."""
    transcript = _str(payload, "transcript_path")
    if not transcript.endswith(".jsonl") or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", agent_id):
        return {}
    path = os.path.join(transcript[: -len(".jsonl")], "subagents", f"agent-{agent_id}.meta.json")
    try:
        if os.path.getsize(path) > 64 * 1024:
            return {}
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    meta = {
        "type": _str(raw, "agentType"),
        "description": _str(raw, "description"),
        "model": _str(raw, "model"),
    }
    depth = _clean_int(raw.get("spawnDepth"))
    if depth is not None:
        meta["depth"] = depth
    return meta


def _find(spool: dict, agent_id: str) -> dict | None:
    return next((e for e in spool["entries"] if e["id"] == agent_id), None)


def _enrich(entry: dict, event: Event, meta: Mapping) -> None:
    for key, value in (
        ("type", event.agent_type or meta.get("type", "")),
        ("description", event.description or meta.get("description", "")),
        ("model", event.model or meta.get("model", "")),
    ):
        if value and not entry[key]:
            entry[key] = _clean_str(value, key)
    depth = event.depth if event.depth is not None else meta.get("depth")
    if entry["depth"] is None and depth is not None:
        entry["depth"] = depth
    for key in PATH_FIELDS:
        value = _clean_path(getattr(event, key))
        if value and not entry.get(key):
            entry[key] = value


def _new_entry(event: Event, now_ms: int) -> dict:
    return {
        "id": _clean_str(event.agent_id, "id"),
        "provider": _clean_str(event.provider, "provider"),
        "type": "",
        "description": "",
        "model": "",
        "depth": None,
        "started_ms": now_ms,
        "ended_ms": None,
        "status": "running",
        "last_seen_ms": now_ms,
        **dict.fromkeys(PATH_FIELDS, ""),
    }


def apply_event(spool: dict, event: Event, now_ms: int, meta: Mapping | None = None) -> bool:
    """Fold one event into the spool; True when the spool changed in a way
    worth writing (heartbeats are throttled)."""
    meta = meta or {}
    changed = False
    if event.kind == "reset" or (
        event.session_id and spool.get("session_id") and event.session_id != spool["session_id"]
    ):
        # "reported" stays: it is what herdr still shows, so the next report
        # compares against it (a reset from "2/3" reports "0/0").
        spool["entries"] = []
        changed = True
    if event.session_id and spool.get("session_id") != event.session_id:
        spool["session_id"] = event.session_id
        changed = True
    if event.kind in ("reset", "session", "ignore"):
        return changed
    entry = _find(spool, event.agent_id)
    if event.kind == "heartbeat":
        if entry is None:
            entry = _new_entry(event, now_ms)
            spool["entries"].append(entry)
            _enrich(entry, event, meta)
            changed = True
        elif entry["status"] in ("running", "stale"):
            revived = entry["status"] == "stale"
            entry["status"] = "running"
            quiet = now_ms - entry["last_seen_ms"] < HEARTBEAT_WRITE_MS
            entry["last_seen_ms"] = max(entry["last_seen_ms"], now_ms)
            changed = changed or revived or not quiet
    elif event.kind == "start":
        if entry is None:
            entry = _new_entry(event, now_ms)
            spool["entries"].append(entry)
        elif entry["status"] == "stale":
            entry["status"] = "running"
        entry["last_seen_ms"] = max(entry["last_seen_ms"], now_ms)
        _enrich(entry, event, meta)
        changed = True
    else:  # stop | fail
        if entry is None:
            entry = _new_entry(event, now_ms)
            spool["entries"].append(entry)
        if entry["status"] in ("running", "stale"):
            entry["status"] = "failed" if event.kind == "fail" else "done"
            entry["ended_ms"] = now_ms
        entry["last_seen_ms"] = max(entry["last_seen_ms"], now_ms)
        _enrich(entry, event, meta)
        changed = True
    _bound(spool)
    return changed


# --- reporting ---------------------------------------------------------------


def _needs_report(last: Mapping | None, token: str, total: int, now_ms: int) -> bool:
    """Report a changed token (but never a first "0/0" for a pane that has not
    had a subagent), and refresh a non-empty one well inside its TTL."""
    if not last:
        return total > 0
    if last.get("token") != token:
        return True
    return total > 0 and now_ms - last.get("at_ms", 0) >= REPORT_REFRESH_MS


def herdr_cli_reporter(env: Mapping[str, str]) -> Reporter:
    """Report the token with ``herdr pane report-metadata`` (the CLI finds
    the socket through the pane's inherited HERDR_* environment)."""
    binary = env.get("HERDECK_HERDR_BIN") or "herdr"

    def report(pane_id: str, token: str, timeout_s: float) -> None:
        subprocess.run(
            [
                binary,
                "pane",
                "report-metadata",
                # herdr 0.9.1 requires the pane id right after the subcommand:
                # trailing it (as its --help usage suggests) fails with
                # "unknown option: <source>" and the token never lands.
                pane_id,
                "--source",
                METADATA_SOURCE,
                "--token",
                f"{TOKEN_NAME}={token}",
                "--ttl-ms",
                str(TOKEN_TTL_MS),
                "--seq",
                str(time.time_ns()),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=dict(env),
            timeout=max(0.05, timeout_s),
            check=False,
        )

    return report


def run(
    raw: bytes,
    argv: list[str],
    env: Mapping[str, str],
    *,
    reporter: Reporter | None = None,
    now_ms: Callable[[], int] | None = None,
    budget_s: float = BUDGET_S,
) -> None:
    """Process one hook invocation. Never raises for bad input."""
    deadline = time.monotonic() + budget_s
    pane_id = env.get("HERDR_PANE_ID", "").strip()
    if not pane_id:
        return
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace")) if raw.strip() else {}
    except ValueError:
        return
    if not isinstance(payload, dict):
        return
    override = None
    if "--provider" in argv:
        index = argv.index("--provider")
        override = argv[index + 1] if index + 1 < len(argv) else None
    provider = detect_provider(payload, override, env)
    event = parse_event(payload, provider, env)
    if event.kind == "ignore":
        return
    meta = claude_meta(payload, event.agent_id) if provider == "claude" and event.agent_id else {}
    clock = now_ms or (lambda: int(time.time() * 1000))
    path = spool_path(pane_id, env)
    token = None
    with _SpoolLock(path, deadline) as locked:
        if not locked:
            return
        now = clock()
        spool = load_spool(path, pane_id)
        changed = apply_stale_rules(spool, now)
        changed = apply_event(spool, event, now, meta) or changed
        running, total = summarize(spool)
        current = format_token(running, total)
        needs_report = _needs_report(spool.get("reported"), current, total, now)
        if needs_report:
            spool["reported"] = {"token": current, "at_ms": now}
            token = current
        if changed or needs_report:
            write_spool(path, spool)
    if token is not None:
        remaining = deadline - time.monotonic()
        if remaining > 0.05:
            try:
                (reporter or herdr_cli_reporter(env))(pane_id, token, remaining)
            except (OSError, subprocess.SubprocessError):
                # herdr missing, hung past the budget or gone: the token
                # simply is not updated; the next event retries.
                pass


def _watchdog(budget_s: float) -> None:
    timer = threading.Timer(budget_s, lambda: os._exit(0))
    timer.daemon = True
    timer.start()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ
    if not env.get("HERDR_PANE_ID"):
        return 0
    # Hard budget: whatever hangs (stdin, the lock, herdr), the agent's hook
    # returns within BUDGET_S with exit 0.
    _watchdog(BUDGET_S + 0.2)
    try:
        raw = sys.stdin.buffer.read(_MAX_STDIN)
        run(raw, argv, env)
    except Exception:  # noqa: BLE001 - a hook must never fail the agent
        pass
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
