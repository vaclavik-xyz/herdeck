from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable

from .bootstrap import _discover_config_path, resolve_mode, resolve_runtime_config
from .commands import Command, build_action_command, command_to_msg, profile_for
from .config import Config, ConfigError, load_config
from .connector import Connector
from .model import AgentKey, AgentState, Status


class ConnectionLost(Exception):
    """The bridge connection dropped (or never produced a first snapshot) while waiting."""


class TargetError(Exception):
    """No agent (or more than one) matched a target spec."""

    def __init__(self, message: str, candidates: list[AgentState]):
        super().__init__(message)
        self.candidates = candidates


_GONE = object()  # sentinel: a vanished agent still counts as "left blocked"


class CtlSession:
    """One-shot request/response + status waits over a long-lived Connector.

    Single-loop asyncio: Connector callbacks run synchronously on this loop and
    mutate state directly (no thread bridging like the deck app).
    """

    def __init__(
        self,
        config: Config,
        *,
        server_filter: str | None = None,
        connector_factory: Callable[..., Connector] = Connector,
    ):
        self.config = config
        self.servers = [s for s in config.servers if server_filter in (None, s.id)]
        self._factory = connector_factory
        self.agents: dict[AgentKey, AgentState] = {}
        self._connectors: dict[str, Connector] = {}
        self._tasks: list[asyncio.Task] = []
        self._pending: dict[str, asyncio.Future] = {}
        self._snapshots: dict[str, asyncio.Event] = {}
        self._conn_up: dict[str, bool] = {}
        self._changed = asyncio.Event()
        self._req = 0
        # Servers that produced no first snapshot within open()'s timeout,
        # mapped to the connector's failure reason (None if unknown). A partial
        # fleet proceeds; callers surface this as a warning.
        self.unavailable: dict[str, str | None] = {}

    # --- Connector callbacks (sync, on this loop) ---
    def _mark_available(self, sid: str) -> None:
        self.unavailable.pop(sid, None)  # a late snapshot recovers the server

    def _on_snapshot(self, sid: str, states: list[AgentState]) -> None:
        self._mark_available(sid)
        self.agents = {k: v for k, v in self.agents.items() if k.server_id != sid}
        for s in states:
            self.agents[s.key] = s
        self._snapshots.setdefault(sid, asyncio.Event()).set()
        self._changed.set()

    def _on_event(self, sid: str, state: AgentState) -> None:
        self.agents[state.key] = state
        self._changed.set()

    def _on_connection(self, sid: str, up: bool) -> None:
        self._conn_up[sid] = up
        if not up:
            self._fail_pending(ConnectionLost(f"connection to {sid} lost"))
        self._changed.set()

    def _on_result(self, req: str, data: dict) -> None:
        fut = self._pending.pop(req, None)
        if fut is not None and not fut.done():
            fut.set_result(data)

    def _on_error(self, message: str) -> None:
        self._fail_pending(ConnectionLost(message or "bridge error"))
        self._changed.set()

    def _fail_pending(self, exc: Exception) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    # --- lifecycle ---
    async def open(self, *, timeout: float) -> None:
        for server in self.servers:
            self._snapshots[server.id] = asyncio.Event()
            conn = self._factory(
                server=server,
                on_snapshot=self._on_snapshot,
                on_event=self._on_event,
                on_connection=self._on_connection,
                on_result=self._on_result,
                on_error=self._on_error,
            )
            self._connectors[server.id] = conn
            self._tasks.append(asyncio.create_task(conn.run()))
        try:
            await asyncio.wait_for(
                asyncio.gather(*(self._snapshots[s.id].wait() for s in self.servers)),
                timeout=timeout,
            )
        except TimeoutError as exc:
            # Name the servers that never answered — and why, when the
            # connector knows (e.g. "token rejected", "connection refused").
            missing: dict[str, str | None] = {}
            for server in self.servers:
                if self._snapshots[server.id].is_set():
                    continue
                conn = self._connectors.get(server.id)
                missing[server.id] = getattr(conn, "last_connect_error", None)
            if len(missing) >= len(self.servers):
                parts = [
                    f"'{sid}'" + (f" ({reason})" if reason else "")
                    for sid, reason in missing.items()
                ]
                detail = f" from {', '.join(parts)}" if parts else ""
                raise ConnectionLost(f"timed out waiting for first snapshot{detail}") from exc
            # A PARTIAL fleet proceeds: one dead bridge must not brick a
            # command aimed at a healthy server (`ls`, or approve of an agent
            # that answered). Actions targeting an unavailable server still
            # fail cleanly in request()/resolution.
            self.unavailable = missing

    async def request(self, cmd: Command, *, timeout: float) -> dict:
        if cmd.kind == "list":
            raise ValueError("list has no request/response; read agents from open() snapshot")
        if cmd.server_id in self.unavailable:
            # Connected-but-silent (no first snapshot) is still unavailable — a
            # command would only die by result timeout.
            raise ConnectionLost(f"{cmd.server_id} unavailable (no snapshot)")
        if not self._conn_up.get(cmd.server_id, False):
            raise ConnectionLost(f"{cmd.server_id} not connected")
        self._req += 1
        req = f"c{self._req}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req] = fut
        await self._connectors[cmd.server_id].send(command_to_msg(cmd, req))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError as exc:
            self._pending.pop(req, None)
            raise ConnectionLost("timed out waiting for result") from exc

    async def wait(self, predicate, *, timeout):
        """Level-triggered: check current state first, then block on changes.

        `_changed` is shared across all status changes (foreign agents too), so
        re-check after every wake. arm(clear) -> re-check -> await ensures no
        wakeup is lost: on_event updates `agents` before set(), and in a single
        loop the callback only runs while we are parked at the await.

        Assumes a single concurrent waiter (the one-shot CLI invariant); not safe
        for parallel waiters due to the shared `_changed.clear()`.
        """
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while True:
            match = predicate()
            if match:
                return match
            self._changed.clear()
            match = predicate()
            if match:
                return match
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                return None
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=remaining)
            except TimeoutError:
                return None

    async def settle(self, agent, *, timeout):
        """Wait until `agent` leaves BLOCKED. True if it did, False on timeout."""
        key = agent.key

        def left_blocked():
            a = self.agents.get(key)
            return (a or _GONE) if (a is None or a.status is not Status.BLOCKED) else None

        return await self.wait(left_blocked, timeout=timeout) is not None

    def resolve_target(self, spec: str) -> AgentState:
        """Resolve a spec to one agent: 'server:pane_id' exact, else fuzzy by
        label/repo/branch/pane_id. Raises TargetError on no match or ambiguity."""
        if ":" in spec:
            sid, pid = spec.split(":", 1)
            exact = self.agents.get(AgentKey(sid, pid))
            if exact is not None:
                return exact
        matches = [
            a for a in self.agents.values()
            if spec in (a.label, a.repo, a.branch, a.key.pane_id)
        ]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise TargetError(f"no agent matching {spec!r}", list(self.agents.values()))
        raise TargetError(f"ambiguous agent {spec!r}", matches)

    async def act(self, action, agent, *, force, always, settle_timeout, request_timeout):
        """Send a profile action; for blocked-clearing actions, settle afterwards.

        Returns {"result": "sent"|"skipped", "settled": bool}.
        """
        profile = profile_for(self.config, agent.agent_type)
        cmd = build_action_command(action, agent, profile, force=force, always=always)
        data = await self.request(cmd, timeout=request_timeout)
        if data.get("skipped"):
            return {"result": "skipped", "settled": True}
        settled = True
        if settle_timeout is not None and action in ("approve", "deny", "stop"):
            settled = await self.settle(agent, timeout=settle_timeout)
        return {"result": "sent", "settled": settled}

    async def close(self) -> None:
        for conn in self._connectors.values():
            conn.stop()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass


EXIT_OK, EXIT_USAGE, EXIT_SKIPPED = 0, 2, 3
EXIT_TARGET, EXIT_CONN, EXIT_WAIT_TIMEOUT = 4, 5, 124

_STATUSES = ["blocked", "working", "idle", "done"]


def build_parser() -> argparse.ArgumentParser:
    # Common options are defined TWICE on purpose: on the top parser with real
    # defaults, and on a parent parser with default=SUPPRESS. A subcommand
    # occurrence then overrides the top-level value only when actually present
    # (SUPPRESS keeps the subparser from clobbering it with a default), so BOTH
    # `herdeck-ctl --json ls` and `herdeck-ctl ls --json` work — the arg-order
    # trap the README used to document instead of fixing.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS,
        help="machine-readable output",
    )
    common.add_argument(
        "--server", default=argparse.SUPPRESS, help="restrict to one configured server id"
    )
    common.add_argument(
        "--config", default=argparse.SUPPRESS,
        help="config path (default: $HERDECK_CONFIG / discovery)",
    )
    # The connect/request timeout is also accepted after the ACTION subcommands;
    # `wait` keeps its own --timeout (max wait), so it does not get this parent.
    timeout_common = argparse.ArgumentParser(add_help=False)
    timeout_common.add_argument(
        "--timeout", type=float, default=argparse.SUPPRESS,
        help="connect/request timeout (s)",
    )

    p = argparse.ArgumentParser(prog="herdeck-ctl", description="Control herdr agents.")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--server", help="restrict to one configured server id")
    p.add_argument("--config", help="config path (default: $HERDECK_CONFIG / discovery)")
    p.add_argument("--timeout", type=float, default=10.0, help="connect/request timeout (s)")
    sub = p.add_subparsers(dest="cmd", required=True)

    ls = sub.add_parser("ls", parents=[common, timeout_common], help="list agents")
    ls.add_argument("--status", choices=_STATUSES)

    w = sub.add_parser("wait", parents=[common], help="block until an agent reaches a status")
    w.add_argument("agent", nargs="?")
    w.add_argument("--any", dest="any_agent", action="store_true")
    w.add_argument("--until", choices=_STATUSES, required=True)
    # wait has its OWN timeout (default: no limit), independent of the connect
    # --timeout. dest=wait_timeout keeps args.timeout (the connect knob) intact.
    w.add_argument("--timeout", dest="wait_timeout", type=float, default=None,
                   help="max seconds to wait (default: no limit)")

    for name in ("approve", "deny"):
        a = sub.add_parser(name, parents=[common, timeout_common], help=f"{name} a blocked agent")
        a.add_argument("agent")
        a.add_argument("--force", action="store_true", help="ignore the blocked guard")
        a.add_argument("--settle", type=float, default=3.0)
        a.add_argument("--no-settle", dest="settle", action="store_const", const=None)
        if name == "approve":
            a.add_argument("--always", action="store_true", help="approve-always profile")

    st = sub.add_parser("stop", parents=[common, timeout_common], help="stop an agent (unconditional)")
    st.add_argument("agent")
    st.add_argument("--settle", type=float, default=3.0)
    st.add_argument("--no-settle", dest="settle", action="store_const", const=None)

    se = sub.add_parser("send", parents=[common, timeout_common], help="send text to an agent (submits now)")
    se.add_argument("agent")
    se.add_argument("text")

    fo = sub.add_parser("focus", parents=[common, timeout_common], help="bring an agent's pane to the foreground")
    fo.add_argument("agent")
    return p


def _agent_row(a: AgentState) -> dict:
    return {"server": a.key.server_id, "pane_id": a.key.pane_id, "label": a.label,
            "status": a.status.value, "agent_type": a.agent_type,
            "repo": a.repo, "branch": a.branch}


def _emit(args, payload) -> None:
    if args.json:
        print(json.dumps(payload))
    elif isinstance(payload, list):
        for r in payload:
            print(f"{r['server']}:{r['pane_id']}  {r['status']:<8} "
                  f"{r['repo'] or r['label']} {r['branch']}".rstrip())
    else:
        print(payload.get("result") or payload.get("status") or json.dumps(payload))


def _target_error(args, exc: TargetError) -> int:
    print(str(exc), file=sys.stderr)
    for a in exc.candidates:
        print(f"  {a.key.server_id}:{a.key.pane_id}  {a.label} {a.repo}".rstrip(), file=sys.stderr)
    return EXIT_TARGET


async def dispatch(args, session) -> int:
    if args.cmd == "ls":
        rows = [_agent_row(a) for a in session.agents.values()
                if not args.status or a.status.value == args.status]
        _emit(args, rows)
        return EXIT_OK

    if args.cmd == "wait":
        if bool(args.any_agent) == bool(args.agent):  # exactly one selector required (N2)
            print("wait needs exactly one of <agent> or --any", file=sys.stderr)
            return EXIT_USAGE
        target_status = Status(args.until)
        fixed_key = None
        if not args.any_agent:
            try:
                fixed_key = session.resolve_target(args.agent).key
            except TargetError as e:
                return _target_error(args, e)

        def pred():
            if fixed_key is not None:
                a = session.agents.get(fixed_key)
                if a is None:
                    # The pane closed while we waited (snapshots drop absent
                    # keys): with the default no-limit timeout this predicate
                    # could otherwise never match and the command would hang a
                    # lead-agent script forever.
                    return _GONE
                return a if a.status is target_status else None
            return next((a for a in session.agents.values() if a.status is target_status), None)

        match = await session.wait(pred, timeout=args.wait_timeout)
        if match is _GONE:
            print(
                f"agent {fixed_key.server_id}:{fixed_key.pane_id} vanished while waiting",
                file=sys.stderr,
            )
            return EXIT_TARGET
        if match is None:
            print("wait timed out", file=sys.stderr)
            return EXIT_WAIT_TIMEOUT
        _emit(args, {"agent": f"{match.key.server_id}:{match.key.pane_id}",
                     "status": match.status.value})
        return EXIT_OK

    try:
        agent = session.resolve_target(args.agent)
    except TargetError as e:
        return _target_error(args, e)

    if args.cmd == "send":
        await session.request(
            Command("send_text", agent.key.server_id, agent.key.pane_id, text=args.text),
            timeout=args.timeout,
        )
        _emit(args, {"result": "sent"})
        return EXIT_OK
    if args.cmd == "focus":
        await session.request(
            Command("focus", agent.key.server_id, agent.key.pane_id), timeout=args.timeout
        )
        _emit(args, {"result": "focused"})
        return EXIT_OK

    result = await session.act(
        args.cmd, agent,
        force=getattr(args, "force", False),
        always=getattr(args, "always", False),
        settle_timeout=args.settle,
        request_timeout=args.timeout,
    )
    if not result.get("settled", True):
        print(f"warning: {agent.key.pane_id} still blocked after settle", file=sys.stderr)
    _emit(args, {"agent": f"{agent.key.server_id}:{agent.key.pane_id}", **result})
    return EXIT_SKIPPED if result["result"] == "skipped" else EXIT_OK


async def _amain(args) -> int:
    config_path = args.config or _discover_config_path()
    file_config = load_config(config_path) if config_path else None
    # The shared resolver honours HERDR_SOCKET > [hardware].herdr_socket > XDG —
    # the deck already did; ctl hardcoding env-or-default made a config-set
    # socket work in `herdeck` but fail in `herdeck-ctl`.
    from .bootstrap import resolve_socket_path

    socket_path = resolve_socket_path(file_config)
    mode = resolve_mode(mock=False, config_path=config_path,
                        config_has_servers=bool(file_config and file_config.servers),
                        socket_path=socket_path, socket_exists=os.path.exists(socket_path))
    if mode[0] == "error":
        print(mode[1], file=sys.stderr)
        return EXIT_CONN
    config, aclose = await resolve_runtime_config(mode, file_config)
    session = CtlSession(config, server_filter=args.server)
    try:
        await session.open(timeout=args.timeout)
        for sid, reason in session.unavailable.items():
            suffix = f": {reason}" if reason else ""
            print(f"warning: server '{sid}' unavailable{suffix}", file=sys.stderr)
        return await dispatch(args, session)
    except ConnectionLost as e:
        print(f"connection error: {e}", file=sys.stderr)
        return EXIT_CONN
    finally:
        await session.close()
        await aclose()


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_amain(args))
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        return EXIT_CONN
