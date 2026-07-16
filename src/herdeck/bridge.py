from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import shutil
import stat
import time
from typing import Protocol

import websockets

from .decisions import decision_choices, decision_revision
from .model import WorkContext
from .protocol import encode

log = logging.getLogger(__name__)

# herdr agent_status values that mark a pane as worth showing on the deck.
_AGENT_STATUSES = {"idle", "working", "blocked", "done"}

# Hard floor: session.snapshot shipped in herdr 0.7.2; there is no fallback path.
_SNAPSHOT_UNSUPPORTED = (
    "herdeck requires herdr >= 0.7.2 (session.snapshot missing); run 'herdr update'"
)

# Startup probe bound: a herdr that accepts but never answers (e.g. mid
# live-handoff) must not block bridge startup; timeout = transient, not fatal.
_PROBE_TIMEOUT = 5.0
_HERDR_RPC_TIMEOUT = 10.0
_HERDR_SUBSCRIBE_TIMEOUT = 10.0
_HERDR_LINE_LIMIT = 1024 * 1024 + 1  # 1 MiB payload plus NDJSON newline
_WIRE_PROTOCOL = 3
_WIRE_CAPABILITIES = ("work_context", "terminal_preview", "metadata_tokens")

# Module-level indirection so tests can fake the clock without touching the
# shared stdlib time module (which asyncio's loop may also consult).
_monotonic = time.monotonic


def _decode_json_object(line: bytes, context: str) -> dict:
    try:
        message = json.loads(line)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{context} returned invalid JSON") from exc
    if not isinstance(message, dict):
        raise RuntimeError(f"{context} returned a non-object response")
    return message


def _validate_subscription_ack(line: bytes) -> None:
    message = _decode_json_object(line, "herdr subscription")
    if "error" in message:
        error = message["error"]
        detail = error.get("message", str(error)) if isinstance(error, dict) else str(error)
        raise RuntimeError(f"herdr subscription failed: {detail}")
    result = message.get("result")
    if not isinstance(result, dict) or result.get("type") != "subscription_started":
        raise RuntimeError("herdr subscription ACK must be subscription_started")


def _decode_event_line(line: bytes) -> dict | None:
    try:
        event = json.loads(line)
    except (TypeError, ValueError):
        return None
    if not isinstance(event, dict) or not isinstance(event.get("event"), str):
        return None
    data = event.get("data")
    if data is not None and not isinstance(data, dict):
        return None
    return event


def _reconnect_backoff(
    current: float,
    *,
    base: float,
    maximum: float,
    connected: bool,
) -> tuple[float, float]:
    if connected:
        return base, base
    return current, min(current * 2, maximum)


def resolve_herdr_socket_path(*, getenv=os.environ.get, fallback: str | None = None) -> str:
    explicit = getenv("HERDR_SOCKET") or getenv("HERDR_SOCKET_PATH")
    if explicit:
        return os.path.expanduser(explicit)
    if fallback:
        return os.path.expanduser(fallback)
    session = getenv("HERDR_SESSION")
    if session:
        return os.path.expanduser(f"~/.config/herdr/sessions/{session}/herdr.sock")
    return os.path.expanduser("~/.config/herdr/herdr.sock")


def _validate_snapshot(snapshot: object) -> dict:
    if not isinstance(snapshot, dict):
        raise RuntimeError("session.snapshot snapshot must be an object")
    for field in ("agents", "panes", "workspaces", "tabs"):
        if field == "agents" or field in snapshot:
            rows = snapshot.get(field)
            if not isinstance(rows, list):
                raise RuntimeError(f"session.snapshot snapshot {field} must be a list")
            for row in rows:
                if not isinstance(row, dict):
                    raise RuntimeError(f"session.snapshot snapshot {field} entries must be objects")
    for record in snapshot["agents"]:
        pane_id = record.get("pane_id")
        if not isinstance(pane_id, str) or not pane_id:
            raise RuntimeError("session.snapshot agent pane_id must be a string")
        terminal_id = record.get("terminal_id")
        if terminal_id is not None and (not isinstance(terminal_id, str) or not terminal_id):
            raise RuntimeError(
                "session.snapshot agent terminal_id must be a nonempty string or null"
            )
        for field in (
            "agent",
            "agent_status",
            "cwd",
            "foreground_cwd",
            "display_agent",
            "title",
            "terminal_title",
            "terminal_title_stripped",
            "workspace_id",
            "tab_id",
        ):
            value = record.get(field)
            if value is not None and not isinstance(value, str):
                raise RuntimeError(f"session.snapshot agent {field} must be a string or null")
        tokens = record.get("tokens")
        if tokens is not None:
            if not isinstance(tokens, dict):
                raise RuntimeError("session.snapshot agent tokens must be an object")
            if len(tokens) > 32:
                raise RuntimeError("session.snapshot agent tokens exceeds 32 entries")
            if not all(isinstance(key, str) for key in tokens):
                raise RuntimeError("session.snapshot agent token keys must be strings")
            if not all(isinstance(value, str) for value in tokens.values()):
                raise RuntimeError("session.snapshot agent token values must be strings")
    for record in snapshot.get("panes", []):
        pane_id = record.get("pane_id")
        if not isinstance(pane_id, str) or not pane_id:
            raise RuntimeError("session.snapshot pane pane_id must be a string")
    for collection, id_field in (("workspaces", "workspace_id"), ("tabs", "tab_id")):
        for record in snapshot.get(collection, []):
            identifier = record.get(id_field)
            if not isinstance(identifier, str) or not identifier:
                raise RuntimeError(f"session.snapshot {collection} {id_field} must be a string")
            label = record.get("label")
            if label is not None and not isinstance(label, str):
                raise RuntimeError(f"session.snapshot {collection} label must be a string or null")
    return snapshot


async def _wait_until(awaitable, deadline: float, message: str):
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        if hasattr(awaitable, "close"):
            awaitable.close()
        raise TimeoutError(message)
    try:
        return await asyncio.wait_for(awaitable, timeout=remaining)
    except TimeoutError as exc:
        raise TimeoutError(message) from exc


class HerdrClient(Protocol):
    async def snapshot(self) -> dict: ...
    async def get_pane(self, pane_id: str) -> dict: ...
    async def read_pane(self, pane_id: str, source: str) -> str: ...
    async def send_keys(self, pane_id: str, keys: list[str]) -> None: ...
    async def focus_agent(self, pane_id: str) -> None: ...
    async def send_text(self, pane_id: str, text: str) -> None: ...
    async def start_agent(self, name: str, argv: list[str]) -> None: ...
    async def worktrees(self, workspace_ids: list[str] | None = None) -> list[dict]: ...


def _is_agent_pane(p: dict) -> bool:
    """A raw herdr pane worth showing on the deck hosts a detected agent."""
    return bool(p.get("agent")) or p.get("agent_status") in _AGENT_STATUSES


def _worktrees_by_workspace(worktrees: list[dict]) -> dict[str, dict]:
    """Index herdr worktrees by the workspace they're open in."""
    return {wt["open_workspace_id"]: wt for wt in (worktrees or []) if wt.get("open_workspace_id")}


def _workspaces_by_id(workspaces: list[dict]) -> dict[str, str]:
    """Index herdr workspaces (session.snapshot's ``workspaces`` list) as {workspace_id: label}."""
    return {
        w["workspace_id"]: w.get("label", "") for w in (workspaces or []) if w.get("workspace_id")
    }


def _tabs_by_id(tabs: list[dict]) -> dict[str, str]:
    """Index herdr tabs (session.snapshot's ``tabs`` list) as {tab_id: label}."""
    return {t["tab_id"]: t.get("label", "") for t in (tabs or []) if t.get("tab_id")}


def _herdr_pane_to_wire(
    p: dict,
    wt_by_ws: dict[str, dict] | None = None,
    ws_by_id: dict[str, str] | None = None,
    tab_by_id: dict[str, str] | None = None,
) -> dict:
    """Map a raw herdr pane to herdeck's wire pane schema.

    herdr uses `agent` / `agent_status` and has no human label. We derive repo +
    branch from the pane's open worktree (herdr `worktree.list`), falling back to
    the working-directory basename when no worktree info is available. The
    workspace/tab labels ride along in `session.snapshot`; a missing lookup
    or empty label stays empty (the raw id is never used as tile text).
    """
    cwd = p.get("foreground_cwd") or p.get("cwd") or ""
    label = os.path.basename(cwd.rstrip("/")) or p.get("workspace_id", "")
    wt = (wt_by_ws or {}).get(p.get("workspace_id", ""), {})
    repo = wt.get("label") or label
    branch = wt.get("branch") or ""
    tokens = p.get("tokens") if isinstance(p.get("tokens"), dict) else {}
    work = WorkContext.from_tokens(tokens)
    return {
        "pane_id": p["pane_id"],
        "agent_type": p.get("agent", "default"),
        "label": label,
        "status": p.get("agent_status", "unknown"),
        "project": label,
        "repo": repo,
        "branch": branch,
        "workspace": (ws_by_id or {}).get(p.get("workspace_id", ""), ""),
        "tab": (tab_by_id or {}).get(p.get("tab_id", ""), ""),
        "waiting_on": tokens.get("waiting_on", ""),
        "progress": tokens.get("progress", ""),
        "metadata": tokens,
        "terminal_id": p.get("terminal_id") or "",
        "title": (p.get("title") or p.get("terminal_title_stripped") or "")[:160],
        "display_agent": (p.get("display_agent") or "")[:160],
        "work": {
            "source": work.source,
            "item": work.item,
            "run": work.run,
            "url": work.url,
        },
    }


def _wire_panes(
    raw: list[dict],
    worktrees: list[dict] | None = None,
    workspaces: list[dict] | None = None,
    tabs: list[dict] | None = None,
) -> list[dict]:
    wt_by_ws = _worktrees_by_workspace(worktrees or [])
    ws_by_id = _workspaces_by_id(workspaces or [])
    tab_by_id = _tabs_by_id(tabs or [])
    return [_herdr_pane_to_wire(p, wt_by_ws, ws_by_id, tab_by_id) for p in raw if _is_agent_pane(p)]


async def _fetch_worktrees(herdr: HerdrClient, workspace_ids: list[str]) -> list:
    """Branch labels come from worktree.list, which herdr scopes to ONE repo —
    each agent workspace is asked about explicitly (device report 2026-07-02:
    every non-focused repo lost its branch line). Failures degrade to no
    labels; they are cosmetic."""
    try:
        return await herdr.worktrees(workspace_ids)
    except Exception:
        return []


def _agent_workspace_ids(raw_panes: list[dict]) -> list[str]:
    """Unique workspace ids of the agent panes (the repos whose worktrees we
    need branch labels for), in a stable order."""
    return sorted(
        {p.get("workspace_id") for p in raw_panes if _is_agent_pane(p) and p.get("workspace_id")}
    )


async def _wired_snapshot(herdr: HerdrClient) -> list[dict]:
    """One session.snapshot carries the agent panes AND the workspace/tab
    labels; only branch labels need the extra per-workspace worktree fan-out
    (worktrees are not part of the snapshot)."""
    snap = await herdr.snapshot()
    raw = snap.get("agents", [])
    worktrees = await _fetch_worktrees(herdr, _agent_workspace_ids(raw))
    return _wire_panes(raw, worktrees, snap.get("workspaces", []), snap.get("tabs", []))


class StubHerdr:
    """In-memory herdr (raw herdr pane shape) for tests."""

    def __init__(
        self,
        panes: list[dict],
        worktrees: list[dict] | None = None,
        workspaces: list[dict] | None = None,
        tabs: list[dict] | None = None,
    ):
        self.panes = panes
        self._worktrees = worktrees or []
        self._workspaces = workspaces or []
        self._tabs = tabs or []
        self.detection: dict[str, str] = {}
        self.sent: list[tuple[str, list[str]]] = []
        self.focused: list[str] = []
        self.started: list[tuple[str, list[str]]] = []

    async def snapshot(self) -> dict:
        return {
            "agents": self.panes,
            "workspaces": self._workspaces,
            "tabs": self._tabs,
        }

    async def worktrees(self, workspace_ids: list[str] | None = None) -> list[dict]:
        self.worktree_queries = getattr(self, "worktree_queries", [])
        self.worktree_queries.append(list(workspace_ids or []))
        return self._worktrees

    async def get_pane(self, pane_id: str) -> dict:
        return next(p for p in self.panes if p["pane_id"] == pane_id)

    async def read_pane(self, pane_id: str, source: str) -> str:
        return self.detection.get(pane_id, "")

    async def send_keys(self, pane_id: str, keys: list[str]) -> None:
        self.sent.append((pane_id, keys))

    async def focus_agent(self, pane_id: str) -> None:
        self.focused.append(pane_id)

    async def send_text(self, pane_id: str, text: str) -> None:
        self.sent.append((pane_id, text))

    async def start_agent(self, name: str, argv: list[str]) -> None:
        self.started.append((name, argv))


async def handle_client_message(herdr: HerdrClient, server_id: str, raw: str) -> str:
    msg = json.loads(raw)
    kind = msg["type"]
    if kind == "list":
        panes = await _wired_snapshot(herdr)
        return encode(_snapshot_message(server_id, panes))
    if kind == "read":
        if await _pane_identity_changed(herdr, msg):
            return _identity_changed_result(msg)
        text = await herdr.read_pane(msg["pane_id"], msg.get("source", "detection"))
        return encode(
            {"type": "result", "req": msg["req"], "data": {"text": text, "pane_id": msg["pane_id"]}}
        )
    if kind == "act":
        guard = msg.get("guard", True)
        pane = None
        if guard or msg.get("terminal_id"):
            pane = await herdr.get_pane(msg["pane_id"])
        if _pane_record_identity_changed(pane, msg.get("terminal_id")):
            return _identity_changed_result(msg)
        if guard:
            assert pane is not None
            if pane.get("agent_status") != "blocked":
                return encode({"type": "result", "req": msg["req"], "data": {"skipped": True}})
        await herdr.send_keys(msg["pane_id"], msg["keys"])
        return encode({"type": "result", "req": msg["req"], "data": {"sent": True}})
    if kind == "focus":
        if await _pane_identity_changed(herdr, msg):
            return _identity_changed_result(msg)
        await herdr.focus_agent(msg["pane_id"])
        return encode({"type": "result", "req": msg["req"], "data": {"focused": True}})
    if kind == "send_text":
        if await _pane_identity_changed(herdr, msg):
            return _identity_changed_result(msg)
        await herdr.send_text(msg["pane_id"], msg["text"])
        return encode({"type": "result", "req": msg["req"], "data": {"sent": True}})
    if kind == "choose_if_blocked":
        pane = await herdr.get_pane(msg["pane_id"])
        if _pane_record_identity_changed(pane, msg.get("terminal_id")):
            return _identity_changed_result(msg)
        if pane.get("agent_status") != "blocked":
            return encode(
                {
                    "type": "result",
                    "req": msg["req"],
                    "data": {"skipped": True, "message": "not_blocked"},
                }
            )
        prompt = await herdr.read_pane(msg["pane_id"], "detection")
        pane = await herdr.get_pane(msg["pane_id"])
        if _pane_record_identity_changed(pane, msg.get("terminal_id")):
            return _identity_changed_result(msg)
        if pane.get("agent_status") != "blocked":
            return encode(
                {
                    "type": "result",
                    "req": msg["req"],
                    "data": {"skipped": True, "message": "not_blocked"},
                }
            )
        current_revision = decision_revision(
            server_id,
            msg["pane_id"],
            str(msg.get("terminal_id") or ""),
            prompt,
        )
        if current_revision != msg.get("decision_revision"):
            return encode(
                {
                    "type": "result",
                    "req": msg["req"],
                    "data": {"skipped": True, "message": "stale_choice"},
                }
            )
        valid_choices = {item["key"] for item in decision_choices(prompt)}
        if msg.get("choice") not in valid_choices:
            return encode(
                {
                    "type": "result",
                    "req": msg["req"],
                    "data": {"skipped": True, "message": "stale_choice"},
                }
            )
        await herdr.send_text(msg["pane_id"], msg["choice"])
        return encode({"type": "result", "req": msg["req"], "data": {"sent": True}})
    if kind == "start":
        await herdr.start_agent(msg["name"], msg["argv"])
        return encode({"type": "result", "req": msg["req"], "data": {"started": True}})
    raise ValueError(f"unknown client message: {kind}")


def _snapshot_message(server_id: str, panes: list[dict]) -> dict:
    return {
        "type": "snapshot",
        "server_id": server_id,
        "protocol": _WIRE_PROTOCOL,
        "capabilities": list(_WIRE_CAPABILITIES),
        "panes": panes,
    }


def _pane_record_identity_changed(pane: dict | None, expected: object) -> bool:
    if not isinstance(expected, str) or not expected:
        return False
    if not isinstance(pane, dict):
        return True
    actual = pane.get("terminal_id")
    return actual != expected


async def _pane_identity_changed(herdr: HerdrClient, msg: dict) -> bool:
    expected = msg.get("terminal_id")
    if not isinstance(expected, str) or not expected:
        return False
    pane = await herdr.get_pane(msg["pane_id"])
    return _pane_record_identity_changed(pane, expected)


def _identity_changed_result(msg: dict) -> str:
    return encode(
        {
            "type": "result",
            "req": msg["req"],
            "data": {"skipped": True, "message": "agent identity changed"},
        }
    )


# herdr events that change fleet membership (need a status re-subscribe after).
_GLOBAL_EVENT_TYPES = (
    "pane.created",
    "pane.closed",
    "pane.exited",
    "pane.moved",
    "workspace.closed",
    "tab.closed",
)
_FLEET_EVENT_NAMES = {
    "pane_created",
    "pane_closed",
    "pane_exited",
    "pane_moved",
    "workspace_closed",
    "tab_closed",
}
# Label-bearing events. Workspace/tab labels ride along in every snapshot, so
# these only need to wake the stream; worktree events additionally invalidate
# the cached worktree list (branch labels are not in the snapshot).
_LABEL_EVENT_TYPES = (
    "pane.updated",
    "tab.renamed",
    "workspace.renamed",
    "workspace.updated",
    "workspace.metadata_updated",
    "worktree.created",
    "worktree.opened",
    "worktree.removed",
)
_WORKTREE_EVENT_NAMES = {"worktree_created", "worktree_opened", "worktree_removed"}

# herdr 0.7.3 exposes terminal observation through the CLI only (there is no
# socket RPC). Each process is bounded independently from the per-client cap.
_OBSERVE_MAX_PER_CLIENT = 3
_OBSERVE_MAX_TOTAL = 8
_OBSERVE_COLS_MIN, _OBSERVE_COLS_MAX = 20, 240
_OBSERVE_ROWS_MIN, _OBSERVE_ROWS_MAX = 5, 100
# Full-frame NDJSON lines can exceed asyncio's 64 KiB StreamReader default.
_OBSERVE_LINE_LIMIT = 8 * 2**20
_observe_total = 0


def _resolve_herdr_bin() -> str | None:
    """Resolve herdr even under launchd's minimal PATH."""
    configured = os.environ.get("HERDECK_HERDR_BIN")
    if configured:
        return configured
    found = shutil.which("herdr")
    if found:
        return found
    fallbacks = (
        os.path.expanduser("~/.local/bin/herdr"),
        "/opt/homebrew/bin/herdr",
        "/usr/local/bin/herdr",
        os.path.expanduser("~/.cargo/bin/herdr"),
        "/home/linuxbrew/.linuxbrew/bin/herdr",
    )
    return next((path for path in fallbacks if os.access(path, os.X_OK)), None)


async def _reap_observe_process(proc) -> None:
    if proc is None or proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()


async def _reap_observe_cancellation_safe(proc) -> None:
    """Finish child cleanup even if the parent task is cancelled again."""
    cleanup = asyncio.create_task(_reap_observe_process(proc))
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            continue
    await cleanup


def _valid_terminal_frame(evt: object) -> bool:
    if not isinstance(evt, dict):
        return False
    return (
        type(evt.get("seq")) is int
        and type(evt.get("full")) is bool
        and type(evt.get("width")) is int
        and type(evt.get("height")) is int
        and isinstance(evt.get("bytes"), str)
    )


def _observe_dimension(msg: dict, name: str, default: int, low: int, high: int) -> int:
    value = msg.get(name, default)
    if value is None:
        value = default
    if isinstance(value, bool):
        raise ValueError(name)
    return max(low, min(high, int(value)))


async def _run_observe(
    send,
    req: str,
    pane_id: str,
    cols: int,
    rows: int,
    state: dict,
    socket_path: str,
) -> None:
    """Forward one herdr observe subprocess and finish it exactly once."""
    proc = None
    reason = "stream ended"
    try:
        binary = _resolve_herdr_bin()
        if binary is None:
            reason = "herdr binary not found on the bridge host"
            return
        try:
            child_env = os.environ.copy()
            child_env["HERDR_SOCKET"] = socket_path
            proc = await asyncio.create_subprocess_exec(
                binary,
                "terminal",
                "session",
                "observe",
                pane_id,
                "--cols",
                str(cols),
                "--rows",
                str(rows),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=_OBSERVE_LINE_LIMIT,
                env=child_env,
            )
        except OSError as exc:
            reason = f"could not start herdr: {exc}"
            return

        assert proc.stdout is not None
        got_output = False
        while True:
            line = await proc.stdout.readline()
            if not line:
                if not got_output:
                    reason = "no stream from herdr (needs herdr >= 0.7.3)"
                break
            try:
                evt = json.loads(line)
            except (TypeError, ValueError):
                reason = "invalid terminal stream from herdr"
                break
            got_output = True
            if not isinstance(evt, dict):
                reason = "invalid terminal stream from herdr"
                break
            kind = evt.get("type")
            if kind == "terminal.closed":
                raw_reason = evt.get("reason")
                reason = raw_reason if isinstance(raw_reason, str) and raw_reason else "closed"
                break
            if kind != "terminal.frame":
                continue
            if evt.get("encoding") != "ansi":
                reason = f"unsupported terminal encoding: {evt.get('encoding')}"
                break
            if not _valid_terminal_frame(evt):
                reason = "invalid terminal frame from herdr"
                break
            delivered = await send(
                encode(
                    {
                        "type": "term_frame",
                        "req": req,
                        "seq": evt["seq"],
                        "full": evt["full"],
                        "cols": evt["width"],
                        "rows": evt["height"],
                        "data": evt["bytes"],
                    }
                )
            )
            if not delivered:
                return
    except asyncio.CancelledError:
        reason = "stopped"
    except Exception as exc:
        log.warning("terminal observe %s failed: %s", req, exc)
        reason = "terminal preview failed"
    finally:
        await _reap_observe_cancellation_safe(proc)
        if not state["closed"]:
            state["closed"] = True
            await send(encode({"type": "term_closed", "req": req, "reason": reason}))


class HerdrEvents:
    """Yields the full agent list whenever it changes.

    The source of truth is a diff of ``session.snapshot`` (so additions, status changes
    AND removals are all reflected — a closed pane simply drops out). Re-lists are
    triggered immediately by herdr's push events (``events.subscribe``) when a
    socket path is given, with a slow poll as a safety net; without one it falls
    back to pure polling.
    """

    def __init__(
        self,
        herdr: HerdrClient,
        socket_path: str | None = None,
        poll_interval: float = 5.0,
        backoff_base: float = 0.3,
        backoff_max: float = 30.0,
    ):
        self._herdr = herdr
        self._socket_path = socket_path
        self._interval = poll_interval
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._wake = asyncio.Event()
        # Cached worktree list (branch labels — the one thing session.snapshot
        # does not carry). A pane.agent_status_changed wake cannot change it, so
        # status wakes reuse the cache; fleet/worktree events (_listen) mark it
        # stale, and an age check refreshes it at least every poll_interval even
        # when constant status wakes keep the slow-poll timeout from ever firing
        # (a branch switch inside an existing worktree emits no event).
        self._worktrees: list | None = None
        self._worktrees_stale = True
        self._worktrees_at = 0.0  # monotonic time of the last refresh

    async def stream(self):
        listener = asyncio.create_task(self._listen()) if self._socket_path else None
        prev: list[dict] | None = None
        try:
            while True:
                try:
                    snap = await self._herdr.snapshot()
                    raw = snap.get("agents", [])
                    if self._worktrees is None or self._worktrees_stale:
                        # agents first: worktrees are fetched for exactly the
                        # workspaces the agent panes live in (per-repo scoping)
                        self._worktrees = await _fetch_worktrees(
                            self._herdr, _agent_workspace_ids(raw)
                        )
                        self._worktrees_stale = False
                        self._worktrees_at = _monotonic()
                    cur = _wire_panes(
                        raw,
                        self._worktrees,
                        snap.get("workspaces", []),
                        snap.get("tabs", []),
                    )
                except Exception as exc:
                    if str(exc) == _SNAPSHOT_UNSUPPORTED:
                        raise  # herdr too old: never retryable, surface loudly
                    cur = None
                if cur is not None:
                    if cur != prev:
                        yield cur
                        prev = cur
                try:  # wake on a push event, else slow poll
                    await asyncio.wait_for(self._wake.wait(), timeout=self._interval)
                except TimeoutError:
                    pass
                self._wake.clear()
                # Age-based staling (not timeout-based): frequent status wakes can
                # keep the timeout from ever firing, which would let the branch
                # labels (a branch change is not an event) stay stale forever.
                if _monotonic() - self._worktrees_at >= self._interval:
                    self._worktrees_stale = True
        finally:
            if listener is not None:
                listener.cancel()

    def _note_event(
        self, name: str | None, pane_id: str | None = None, subscribed: set[str] | None = None
    ) -> bool:
        """Digest one push event; True asks the listener to verify topology.

        Herdr 0.7.3 replays retained lifecycle events to new subscribers, so a
        lifecycle event alone is never proof that the current pane set changed.
        ``_topology_changed`` compares a fresh authoritative snapshot before a
        subscription is rebuilt."""
        normalized = name.replace(".", "_") if isinstance(name, str) else None
        if normalized in _WORKTREE_EVENT_NAMES:
            self._worktrees_stale = True
        if normalized in _FLEET_EVENT_NAMES:
            self._worktrees_stale = True  # a new pane may sit in an unseen workspace
            return True
        return False

    @staticmethod
    def _snapshot_pane_ids(snapshot: dict) -> set[str]:
        if not isinstance(snapshot, dict):
            raise RuntimeError("session.snapshot must be an object")
        field = "panes" if "panes" in snapshot else "agents"
        rows = snapshot.get(field)
        if not isinstance(rows, list):
            raise RuntimeError(f"session.snapshot {field} must be a list")
        pane_ids: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError(f"session.snapshot {field} entries must be objects")
            pane_id = row.get("pane_id")
            if not isinstance(pane_id, str) or not pane_id:
                raise RuntimeError(f"session.snapshot {field} pane_id must be a string")
            pane_ids.add(pane_id)
        return pane_ids

    @staticmethod
    def _subscriptions_for(pane_ids: set[str]) -> list[dict]:
        subscriptions = [
            {"type": event_type} for event_type in _GLOBAL_EVENT_TYPES + _LABEL_EVENT_TYPES
        ]
        subscriptions.extend(
            {"type": "pane.agent_status_changed", "pane_id": pane_id}
            for pane_id in sorted(pane_ids)
        )
        return subscriptions

    async def _topology_changed(self, name: str | None, subscribed: set[str]) -> bool:
        normalized = name.replace(".", "_") if isinstance(name, str) else None
        if normalized not in _FLEET_EVENT_NAMES:
            return False
        snapshot = await self._herdr.snapshot()
        return self._snapshot_pane_ids(snapshot) != subscribed

    async def _listen(self) -> None:
        """Hold a herdr event subscription; wake the stream on every event."""
        backoff = self._backoff_base
        while True:
            writer = None
            connected = False
            try:
                deadline = asyncio.get_running_loop().time() + _HERDR_SUBSCRIBE_TIMEOUT
                reader, writer = await _wait_until(
                    asyncio.open_unix_connection(
                        self._socket_path,
                        limit=_HERDR_LINE_LIMIT,
                    ),
                    deadline,
                    "herdr subscription handshake timed out",
                )
                snapshot = await self._herdr.snapshot()
                subscribed_panes = self._snapshot_pane_ids(snapshot)
                subs = self._subscriptions_for(subscribed_panes)
                writer.write(
                    (
                        json.dumps(
                            {
                                "id": "e",
                                "method": "events.subscribe",
                                "params": {"subscriptions": subs},
                            }
                        )
                        + "\n"
                    ).encode()
                )
                await _wait_until(
                    writer.drain(),
                    deadline,
                    "herdr subscription handshake timed out",
                )
                ack = await _wait_until(
                    reader.readline(),
                    deadline,
                    "herdr subscription handshake timed out",
                )
                if not ack:
                    raise ConnectionError("herdr subscription closed before ACK")
                _validate_subscription_ack(ack)
                connected = True
                # Close the snapshot->subscribe race: if topology changed while
                # the stream was being installed, rebuild against the new set.
                if self._snapshot_pane_ids(await self._herdr.snapshot()) != subscribed_panes:
                    continue
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    evt = _decode_event_line(line)
                    if evt is None:
                        continue
                    self._wake.set()
                    name = evt["event"]
                    pane_id = (evt.get("data") or {}).get("pane_id")
                    if self._note_event(
                        name, pane_id, subscribed_panes
                    ) and await self._topology_changed(name, subscribed_panes):
                        break  # real topology change -> re-subscribe panes
            except Exception:
                pass
            finally:
                if writer is not None:
                    writer.close()
                    try:
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining > 0:
                            await asyncio.wait_for(writer.wait_closed(), timeout=remaining)
                    except Exception:
                        pass
            delay, backoff = _reconnect_backoff(
                backoff,
                base=self._backoff_base,
                maximum=self._backoff_max,
                connected=connected,
            )
            await asyncio.sleep(delay)


# A client whose TCP buffer is full (e.g. a laptop that closed its lid with the
# desktop window attached) blocks ws.send until the keepalive ping times it out
# (~40s). Broadcasts must never let one such client starve the others.
_BROADCAST_SEND_TIMEOUT = 2.0


async def _send_to_client(ws, msg: str, lock: asyncio.Lock, timeout: float | None = None) -> bool:
    if timeout is None:
        timeout = _BROADCAST_SEND_TIMEOUT  # module-level so tests can shrink it

    try:
        # Waiting behind another whole message isn't backpressure on THIS
        # message. The lock holder has its own bounded ws.send(), so start this
        # timeout only after serialization hands us the connection.
        async with lock:
            await asyncio.wait_for(ws.send(msg), timeout=timeout)
        return True
    except TimeoutError:
        # Backpressured/half-open: drop the connection so the client reconnects
        # cleanly (a reconnect resyncs with a full snapshot anyway).
        try:
            await ws.close(code=1011, reason="send timeout")
        except Exception:
            pass
    except Exception:
        pass
    return False


async def _broadcast(snapshot_stream, clients: dict, server_id: str) -> None:
    """Forward each changed full agent list to all clients as a snapshot.

    Snapshots (not per-pane events) are used so that removed/finished panes
    disappear from the deck instead of lingering until a manual refresh.
    Clients are isolated from each other: sends fan out concurrently with a
    short per-client timeout, so a stalled laptop cannot freeze status
    updates for the physical deck.
    """
    async for panes in snapshot_stream:
        msg = encode(_snapshot_message(server_id, panes))
        if clients:
            await asyncio.gather(
                *(_send_to_client(ws, msg, lock) for ws, lock in list(clients.items()))
            )


class SocketHerdr:
    """Talks to a real herdr instance over its Unix socket (newline JSON)."""

    def __init__(
        self,
        socket_path: str,
        *,
        timeout: float = _HERDR_RPC_TIMEOUT,
        line_limit: int = _HERDR_LINE_LIMIT,
    ):
        self._path = socket_path
        self._timeout = timeout
        self._line_limit = line_limit

    async def _rpc(self, method: str, params: dict, *, retry: bool = True) -> dict:
        # herdr closes the unix socket after each request (one-shot), so we open
        # a fresh connection per RPC instead of reusing one — reuse fails on the
        # second call of a burst (e.g. act = get_pane + send_keys) as the
        # server-side close isn't detected before the next write.
        # RPCs are NOT serialized: each call reads from its own connection, so the
        # fixed request id "b" cannot cross-talk, and herdr accepts concurrent
        # one-shot connections — this is what lets the per-workspace `worktree.list`
        # fan-out (worktrees()) and on-demand client RPCs (act, start_agent, ...)
        # run in parallel (latency = max instead of sum).
        attempts = 2 if retry else 1
        last_exc: Exception | None = None
        deadline = asyncio.get_running_loop().time() + self._timeout
        for _ in range(attempts):
            reader = writer = None
            try:
                timeout_message = f"herdr RPC {method} timed out"
                reader, writer = await _wait_until(
                    asyncio.open_unix_connection(self._path, limit=self._line_limit),
                    deadline,
                    timeout_message,
                )
                writer.write(
                    (json.dumps({"id": "b", "method": method, "params": params}) + "\n").encode()
                )
                await _wait_until(writer.drain(), deadline, timeout_message)
                try:
                    line = await _wait_until(reader.readline(), deadline, timeout_message)
                except ValueError as exc:
                    raise RuntimeError(
                        f"herdr RPC {method} response exceeds {self._line_limit - 1} bytes"
                    ) from exc
                if not line:  # EOF before a response
                    raise ConnectionError("herdr socket closed")
                res = _decode_json_object(line, f"herdr RPC {method}")
                if "error" in res:
                    err = res["error"]
                    if isinstance(err, dict):
                        message = err.get("message", str(err))
                    else:
                        message = str(err)
                    raise RuntimeError(f"herdr RPC {method} failed: {message}")
                if not isinstance(res.get("result"), dict):
                    raise RuntimeError(f"herdr RPC {method} result must be an object")
                return res
            except (OSError, ConnectionError) as exc:
                last_exc = exc
            finally:
                if writer is not None:
                    writer.close()
                    try:
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining > 0:
                            await asyncio.wait_for(writer.wait_closed(), timeout=remaining)
                    except Exception:
                        pass
        assert last_exc is not None
        raise last_exc

    async def list_panes(self) -> list[dict]:
        # Kept for get_pane's act guard only (herdr has no working pane.get);
        # fleet state comes from snapshot().
        res = await self._rpc("pane.list", {})
        panes = res["result"].get("panes")
        if not isinstance(panes, list):
            raise RuntimeError("herdr RPC pane.list panes must be a list")
        for pane in panes:
            if not isinstance(pane, dict):
                raise RuntimeError("herdr RPC pane.list entries must be objects")
            pane_id = pane.get("pane_id")
            if not isinstance(pane_id, str) or not pane_id:
                raise RuntimeError("herdr RPC pane.list pane_id must be a string")
        return panes

    async def snapshot(self) -> dict:
        # session.snapshot (herdr >= 0.7.4) returns agents, metadata tokens, and
        # workspace/tab labels
        # in one response; worktrees are NOT included (see worktrees()).
        try:
            res = await self._rpc("session.snapshot", {})
        except RuntimeError as exc:
            if "unknown variant" in str(exc):
                raise RuntimeError(_SNAPSHOT_UNSUPPORTED) from exc
            raise
        result = res["result"]
        return _validate_snapshot(result.get("snapshot"))

    async def get_pane(self, pane_id: str) -> dict:
        # herdr has no working `pane.get`; derive the pane from the (supported)
        # pane.list so the act guard can check current status.
        for pane in await self.list_panes():
            if pane.get("pane_id") == pane_id:
                return pane
        return {}

    async def read_pane(self, pane_id: str, source: str) -> str:
        res = await self._rpc("pane.read", {"pane_id": pane_id, "source": source})
        return res.get("result", {}).get("read", {}).get("text", "")

    async def send_keys(self, pane_id: str, keys: list[str]) -> None:
        await self._rpc("pane.send_keys", {"pane_id": pane_id, "keys": keys}, retry=False)

    async def focus_agent(self, pane_id: str) -> None:
        # herdr focuses the agent on screen; target is the agent's pane id.
        await self._rpc("agent.focus", {"target": pane_id})

    async def send_text(self, pane_id: str, text: str) -> None:
        # agent.send types the text into the agent's input but does not submit it,
        # so follow with Enter to actually send the message.
        await self._rpc("agent.send", {"target": pane_id, "text": text}, retry=False)
        await self._rpc("pane.send_keys", {"pane_id": pane_id, "keys": ["enter"]}, retry=False)

    async def start_agent(self, name: str, argv: list[str]) -> None:
        # No workspace_id -> herdr starts the agent in the focused workspace.
        await self._rpc("agent.start", {"name": name, "argv": argv}, retry=False)

    async def worktrees(self, workspace_ids: list[str] | None = None) -> list[dict]:
        # herdr scopes worktree.list to ONE repo (focused when unparametrized);
        # a multi-repo fleet needs one query per agent workspace, concurrently,
        # merged and de-duplicated (same repo open in several workspaces).
        ids = [w for w in (workspace_ids or []) if w]
        if not ids:
            res = await self._rpc("worktree.list", {})
            return res.get("result", {}).get("worktrees", [])

        async def one(ws: str) -> list[dict]:
            try:
                res = await self._rpc("worktree.list", {"workspace_id": ws})
                return res.get("result", {}).get("worktrees", [])
            except Exception:
                return []  # labels are cosmetic; one failing repo must not drop the rest

        merged: list[dict] = []
        seen: set[tuple] = set()
        for lst in await asyncio.gather(*(one(w) for w in ids)):
            for wt in lst:
                key = (wt.get("path"), wt.get("open_workspace_id"))
                if key in seen:
                    continue
                seen.add(key)
                merged.append(wt)
        return merged


async def _serve_connection(
    ws,
    herdr: HerdrClient,
    server_id: str,
    token: str,
    clients: dict,
    socket_path: str,
):
    global _observe_total
    auth = ws.request.headers.get("Authorization", "")
    if not hmac.compare_digest(auth, f"Bearer {token}"):
        await ws.close(code=4401, reason="unauthorized")
        return
    send_lock = asyncio.Lock()

    async def send(msg: str) -> bool:
        return await _send_to_client(ws, msg, send_lock)

    panes = await _wired_snapshot(herdr)
    if not await send(encode(_snapshot_message(server_id, panes))):
        return
    clients[ws] = send_lock
    observes: dict[str, tuple[asyncio.Task, dict]] = {}

    def observe_done(req: str, task: asyncio.Task) -> None:
        global _observe_total
        current = observes.get(req)
        if current is not None and current[0] is task:
            observes.pop(req, None)
        _observe_total -= 1

    async def stop_observe(req: str, *, notify: bool = True) -> None:
        current = observes.get(req)
        if current is None:
            return
        task, state = current
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if notify and not state["closed"]:
            state["closed"] = True
            await send(encode({"type": "term_closed", "req": req, "reason": "stopped"}))

    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except ValueError:
                msg = None
            kind = msg.get("type") if isinstance(msg, dict) else None
            if kind == "observe":
                req = msg.get("req")
                pane_id = msg.get("pane_id")
                if not isinstance(req, str) or not req.strip():
                    await send(
                        encode(
                            {
                                "type": "term_closed",
                                "req": req if isinstance(req, str) else "",
                                "reason": "invalid observe request",
                            }
                        )
                    )
                    continue
                if req in observes:  # idempotent duplicate; never replace its task
                    continue
                if not isinstance(pane_id, str) or not pane_id.strip():
                    await send(
                        encode(
                            {
                                "type": "term_closed",
                                "req": req,
                                "reason": "invalid terminal target",
                            }
                        )
                    )
                    continue
                if await _pane_identity_changed(herdr, msg):
                    await send(
                        encode(
                            {
                                "type": "term_closed",
                                "req": req,
                                "reason": "agent identity changed",
                            }
                        )
                    )
                    continue
                if len(observes) >= _OBSERVE_MAX_PER_CLIENT or _observe_total >= _OBSERVE_MAX_TOTAL:
                    await send(
                        encode(
                            {
                                "type": "term_closed",
                                "req": req,
                                "reason": "too many live previews",
                            }
                        )
                    )
                    continue

                try:
                    cols = _observe_dimension(
                        msg, "cols", 100, _OBSERVE_COLS_MIN, _OBSERVE_COLS_MAX
                    )
                    rows = _observe_dimension(msg, "rows", 30, _OBSERVE_ROWS_MIN, _OBSERVE_ROWS_MAX)
                except (OverflowError, TypeError, ValueError):
                    await send(
                        encode(
                            {
                                "type": "term_closed",
                                "req": req,
                                "reason": "invalid terminal dimensions",
                            }
                        )
                    )
                    continue
                state = {"closed": False}
                _observe_total += 1
                try:
                    task = asyncio.create_task(
                        _run_observe(send, req, pane_id, cols, rows, state, socket_path)
                    )
                except Exception:
                    _observe_total -= 1
                    raise
                observes[req] = (task, state)
                task.add_done_callback(lambda done, r=req: observe_done(r, done))
                continue
            if kind == "observe_stop":
                req = msg.get("req")
                if isinstance(req, str):
                    await stop_observe(req)
                continue
            try:
                out = await handle_client_message(herdr, server_id, raw)
            except Exception as exc:
                out = encode({"type": "error", "message": str(exc)})
            await send(out)
    finally:
        clients.pop(ws, None)
        pending = list(observes)
        for req in pending:
            current = observes.get(req)
            if current is not None and not current[0].done():
                current[0].cancel()
        if pending:
            await asyncio.gather(
                *(observes[req][0] for req in pending if req in observes),
                return_exceptions=True,
            )


async def _require_snapshot_support(herdr: HerdrClient) -> None:
    """Fail fast when herdr predates session.snapshot (herdeck needs >= 0.7.2).

    Only the version error is fatal: a herdr that is merely not up yet (or is
    mid live-handoff) must not kill the bridge — the stream retries those
    exactly as before."""
    try:
        await asyncio.wait_for(herdr.snapshot(), timeout=_PROBE_TIMEOUT)
    except RuntimeError as exc:
        if str(exc) == _SNAPSHOT_UNSUPPORTED:
            raise
    except Exception:
        pass


def _log_broadcast_task_failure(task: asyncio.Task) -> None:
    """serve() awaits _broadcast in its own foreground, so a fatal stream
    error (e.g. an old herdr surfacing after a transient-down startup probe)
    propagates to whatever called serve(). start_local_bridge()'s broadcast
    runs as a detached background task with no such supervisor, so without
    this the same failure would die silently — surfaced here as an error log
    instead (asyncio's own 'Task exception was never retrieved' warning is
    easy to miss)."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("local bridge broadcast stopped: %s", exc, exc_info=exc)


async def start_local_bridge(socket_path, host="127.0.0.1", herdr=None):
    """Bind an embedded bridge on a loopback ephemeral port with a random,
    in-memory token. Returns (host, port, token, (server, broadcast_task))."""
    import secrets

    token = secrets.token_urlsafe(32)
    # Separate SocketHerdr instances for the event stream vs client requests —
    # RPCs are unserialized one-shot connections, so this split is organizational
    # only (see serve()). A test may inject a single stub for both.
    events_herdr = herdr or SocketHerdr(socket_path)
    client_herdr = herdr or SocketHerdr(socket_path)
    await _require_snapshot_support(events_herdr)
    events = HerdrEvents(events_herdr, socket_path=socket_path)
    clients: dict = {}

    async def handler(ws):
        await _serve_connection(ws, client_herdr, "local", token, clients, socket_path)

    server = await websockets.serve(handler, host, 0)
    port = server.sockets[0].getsockname()[1]
    btask = asyncio.create_task(_broadcast(events.stream(), clients, "local"))
    btask.add_done_callback(_log_broadcast_task_failure)
    return host, port, token, (server, btask)


async def serve(socket_path: str, host: str, port: int, server_id: str, token: str):
    # SocketHerdr RPCs are unserialized one-shot connections (herdr accepts them
    # concurrently), so an on-demand read/focus/act never queues behind an
    # in-flight fleet snapshot. Separate instances for the event stream vs client
    # requests are kept for organizational clarity.
    events_herdr = SocketHerdr(socket_path)
    client_herdr = SocketHerdr(socket_path)
    await _require_snapshot_support(events_herdr)
    events = HerdrEvents(events_herdr, socket_path=socket_path)  # push events + slow poll
    clients: dict = {}

    async def handler(ws):
        await _serve_connection(ws, client_herdr, server_id, token, clients, socket_path)

    async with websockets.serve(handler, host, port):
        await _broadcast(events.stream(), clients, server_id)  # runs forever


def load_bridge_token(*, getenv=os.environ.get) -> str:
    token_file = getenv("HERDECK_TOKEN_FILE")
    if token_file is not None:
        if not token_file.strip():
            raise SystemExit("HERDECK_TOKEN_FILE must not be empty")
        path = os.path.abspath(os.path.expanduser(token_file))
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except OSError as exc:
            raise SystemExit(f"could not read HERDECK_TOKEN_FILE: {exc}") from exc
        if mode & 0o077:
            raise SystemExit("HERDECK_TOKEN_FILE permissions must be 0600 or stricter")
        try:
            token = open(path, encoding="utf-8").read().strip()
        except OSError as exc:
            raise SystemExit(f"could not read HERDECK_TOKEN_FILE: {exc}") from exc
        if not token:
            raise SystemExit("HERDECK_TOKEN_FILE must not be empty")
        return token
    inline = getenv("HERDECK_TOKEN")
    if inline is None:
        raise SystemExit("set HERDECK_TOKEN_FILE or HERDECK_TOKEN")
    if not inline.strip():
        raise SystemExit("HERDECK_TOKEN must not be empty")
    return inline.strip()


def main() -> None:
    socket_path = resolve_herdr_socket_path()
    host = os.environ.get("HERDECK_BIND", "127.0.0.1")  # set to Tailscale IP
    port = int(os.environ.get("HERDECK_PORT", "8788"))
    server_id = os.environ.get("HERDECK_SERVER_ID", "server")
    token = load_bridge_token()
    asyncio.run(serve(socket_path, host, port, server_id, token))


if __name__ == "__main__":
    main()
