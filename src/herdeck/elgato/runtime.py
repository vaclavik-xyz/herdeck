from __future__ import annotations

import asyncio
import os
import stat
from collections.abc import Callable

from ..commands import Command, command_to_msg
from ..config import Config, ConfigError
from ..connector import Connector
from ..icons import DEFAULT_AGENT_SLUGS, IconProvider
from ..model import AgentKey
from .frozen import baked_assets_dir as _baked_assets_dir
from .frozen import is_frozen as _is_frozen
from .frozen import make_png_rasterizer as _make_png_rasterizer
from .ipc import IpcServer
from .session import ElgatoSession


def discover_ipc(getenv=os.environ.get) -> tuple[str, str]:
    sock = getenv("HERDECK_ELGATO_SOCK")
    token = getenv("HERDECK_ELGATO_TOKEN")
    if not sock or not token:
        raise ConfigError("HERDECK_ELGATO_SOCK and HERDECK_ELGATO_TOKEN must both be set")
    return sock, token


class ReadCorrelator:
    """Accepts a prompt read only for the request that issued it and the agent's
    current block generation — rejects a stale read that lands after the agent left
    and re-entered BLOCKED, and keys by AgentKey so two servers' identical pane ids
    never cross-wire."""

    def __init__(self, session: ElgatoSession, *, blank_cooldown: float = 2.0) -> None:
        self._session = session
        self._pending: dict[AgentKey, tuple[str, int]] = {}
        self._cooldown: dict[AgentKey, tuple[int, float]] = {}  # key -> (gen, retry_after_ts)
        self._blank_cooldown = blank_cooldown

    def issued(self, key: AgentKey, req_id: str) -> None:
        self._pending[key] = (req_id, self._session.block_generation(key))
        self._cooldown.pop(key, None)  # a fresh read supersedes any blank-read backoff

    def has_pending(self, key: AgentKey) -> bool:
        p = self._pending.get(key)
        return p is not None and p[1] == self._session.block_generation(key)

    def in_cooldown(self, key: AgentKey) -> bool:
        # True while a blank read's backoff window is open for the current block
        # generation — the proactive reader skips the key until it elapses, so a
        # persistently-blank read polls on a timer instead of spinning.
        c = self._cooldown.get(key)
        return (
            c is not None
            and c[0] == self._session.block_generation(key)
            and self._session.now() < c[1]
        )

    def result(self, key: AgentKey, req_id: str, text: str) -> bool:
        # Match the issuing request AND the agent's current block generation, then store.
        if self._pending.get(key) == (req_id, self._session.block_generation(key)):
            del self._pending[key]
            if self._session.set_detection(key, text):
                self._cooldown.pop(key, None)
                return True
            # A blank read is not a real prompt: open a backoff window instead of
            # re-reading immediately (spin) or pinning the agent forever (stuck). The
            # ticker retries once the window elapses; a re-block (gen change) retries
            # sooner.
            self._cooldown[key] = (
                self._session.block_generation(key),
                self._session.now() + self._blank_cooldown,
            )
        return False

    def clear_server(self, server_id: str) -> None:
        # Drop pending reads + backoff for a server on disconnect so reconnect re-reads.
        self._pending = {k: v for k, v in self._pending.items() if k.server_id != server_id}
        self._cooldown = {k: v for k, v in self._cooldown.items() if k.server_id != server_id}


def build_command_sender(send: Callable[[Command], None]) -> Callable[[list[Command]], None]:
    def run(cmds: list[Command]) -> None:
        for cmd in cmds:
            send(cmd)
    return run


def _default_session(config: Config) -> ElgatoSession:
    import tempfile

    cache = os.path.join(tempfile.gettempdir(), "herdeck-elgato-icons")
    overrides = (
        os.path.abspath(os.path.expanduser(config.hardware.icons_dir))
        if config.hardware.icons_dir
        else None
    )
    icons = IconProvider(cache_dir=cache, slug_map=DEFAULT_AGENT_SLUGS, overrides_dir=overrides)
    return ElgatoSession(config, icons)


def _frozen_session(config: Config, baked_dir: str) -> ElgatoSession:
    """Session for a frozen bundle: Pillow-only PNG rasterizer, bundled assets,
    no network glyph fetch."""
    import tempfile

    cache = os.path.join(tempfile.gettempdir(), "herdeck-elgato-icons")
    overrides = (
        os.path.abspath(os.path.expanduser(config.hardware.icons_dir))
        if config.hardware.icons_dir
        else None
    )
    icons = IconProvider(
        cache_dir=cache,
        slug_map=DEFAULT_AGENT_SLUGS,
        overrides_dir=overrides,
        fetch=lambda slug: None,  # offline-first: no Simple Icons fetch when frozen
        rasterize=_make_png_rasterizer(baked_dir),
        assets_dir=baked_dir,
    )
    return ElgatoSession(config, icons)


def _session_for_runtime(config: Config) -> ElgatoSession:
    """Pick the session builder by runtime: frozen bundle vs dev/test."""
    if _is_frozen():
        return _frozen_session(config, _baked_assets_dir())
    return _default_session(config)


async def serve_elgato(config: Config, *, socket_path: str, token: str, make_session=_session_for_runtime) -> None:
    if not config.servers:
        raise ConfigError("no servers configured for elgato-plugin run")
    loop = asyncio.get_running_loop()
    session = make_session(config)

    req = {"n": 0}
    correlator = ReadCorrelator(session)

    def _next_req() -> str:
        req["n"] += 1
        return f"r{req['n']}"

    def send(cmd: Command) -> None:
        conn = connectors.get(cmd.server_id)
        if conn is None:
            return
        req_id = _next_req()
        if cmd.kind == "read" and cmd.pane_id is not None:
            correlator.issued(AgentKey(cmd.server_id, cmd.pane_id), req_id)
        loop.create_task(conn.send(command_to_msg(cmd, req_id)))

    def on_result(server_id: str, req_id: str, data: dict) -> None:
        pane, text = data.get("pane_id"), data.get("text")
        if text is not None and pane is not None:
            correlator.result(AgentKey(server_id, pane), req_id, text)  # read result -> detection
        elif text is None:
            send(Command("list", server_id))  # act/focus result -> re-list (clears pending state)

    sender = build_command_sender(send)
    server = IpcServer(session, token, on_commands=sender)

    def _proactive_reads() -> None:
        for key in session.blocked_without_detection():
            if not correlator.has_pending(key) and not correlator.in_cooldown(key):
                agent = session.get_agent(key)
                send(
                    Command(
                        "read",
                        key.server_id,
                        key.pane_id,
                        source="detection",
                        terminal_id=(agent.terminal_id or None) if agent else None,
                    )
                )

    def _apply(fn, *args) -> None:
        # snapshot / event / read-result: mutate the session, proactively read any
        # blocked agent without a fresh prompt (so an auto-selected agent enables
        # Approve without a slot press), then push a render diff.
        fn(*args)
        _proactive_reads()
        asyncio.create_task(server.push_diff())

    def _on_connection(server_id: str, up: bool) -> None:
        # Connection-up arrives BEFORE the resync snapshot, while self._agents still
        # holds pre-disconnect panes — so do NOT proactive-read here; the snapshot
        # that immediately follows runs _apply against fresh state. Just update+push.
        session.set_connection(server_id, up)
        if not up:
            correlator.clear_server(server_id)  # drop pending so reconnect re-reads
        asyncio.create_task(server.push_diff())

    connectors: dict[str, Connector] = {}
    for sc in config.servers:
        conn = Connector(
            sc,
            on_snapshot=lambda sid, st: loop.call_soon_threadsafe(_apply, session.apply_snapshot, sid, st),
            on_event=lambda sid, s: loop.call_soon_threadsafe(_apply, session.apply_event, sid, s),
            on_connection=lambda sid, up: loop.call_soon_threadsafe(_on_connection, sid, up),
            on_result=lambda req_id, data, sid=sc.id: loop.call_soon_threadsafe(
                _apply, on_result, sid, req_id, data
            ),
        )
        connectors[sc.id] = conn

    async def _guarded(c):
        try:
            await c.run()
        except Exception:
            pass

    async def _ticker() -> None:
        # Enforces the Stop arm timeout and reverts the armed key visual even when
        # no other event arrives, and retries any blocked agent whose proactive read
        # came back blank once its backoff window has elapsed.
        while True:
            await asyncio.sleep(0.5)
            session.tick()
            _proactive_reads()
            await server.push_diff()

    if os.path.lexists(socket_path):
        if not stat.S_ISSOCK(os.lstat(socket_path).st_mode):
            raise ConfigError(f"HERDECK_ELGATO_SOCK {socket_path!r} exists and is not a socket")
        os.unlink(socket_path)  # only ever remove a stale socket, never a real file
    ipc = await asyncio.start_unix_server(server.handle, path=socket_path)
    tasks = [asyncio.create_task(_guarded(c)) for c in connectors.values()]
    tasks.append(asyncio.create_task(_ticker()))
    async with ipc:
        await asyncio.gather(ipc.serve_forever(), *tasks)
