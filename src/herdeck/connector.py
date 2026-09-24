from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections.abc import Callable

import websockets

from .config import ServerConfig
from .model import AgentKey, AgentState
from .protocol import (
    WIRE_PROTOCOL,
    Error,
    Event,
    ProjectIcon,
    Result,
    Snapshot,
    TermClosed,
    TermFrame,
    Unknown,
    decode_inbound,
    encode,
)

log = logging.getLogger("herdeck.connector")

# Wire capability + opt-in feature name for project favicon frames.
PROJECT_ICON_FEATURE = "project_icon"


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def create_connector(server, **kwargs):
    if server.backend == "t3":
        from .t3 import T3Connector
        return T3Connector(server, **kwargs)
    return Connector(server, **kwargs)


def _describe_connect_error(exc: Exception) -> str:
    """Human-readable reason for a failed connect. An HTTP 401/403 handshake
    rejection means a bad token — that must read differently from a dead
    bridge or a DNS failure."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (401, 403):
        return f"token rejected (HTTP {status}) — check token_env/keychain"
    # The bridge accepts the handshake and then closes an unauthorized client
    # with app code 4401 — surfaced as a ConnectionClosed with rcvd.code.
    close_code = getattr(getattr(exc, "rcvd", None), "code", None)
    if close_code == 4401:
        return "token rejected (close 4401) — check token_env/keychain"
    return str(exc) or type(exc).__name__


class Connector:
    def __init__(
        self,
        server: ServerConfig,
        on_snapshot: Callable[[str, list[AgentState]], None],
        on_event: Callable[[str, AgentState], None],
        on_connection: Callable[[str, bool], None],
        on_result: Callable[[str, dict], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        backoff_base: float = 0.5,
        backoff_max: float = 30.0,
        on_term: Callable[[str, TermFrame | TermClosed], None] | None = None,
        on_project_icon: Callable[[str, ProjectIcon], None] | None = None,
        on_request_error: Callable[[str | None, str], None] | None = None,
    ):
        self.server = server
        self._on_snapshot = on_snapshot
        self._on_event = on_event
        self._on_connection = on_connection
        self._on_result = on_result or (lambda req, data: None)
        self._on_error = on_error or (lambda message: None)
        self._on_term = on_term or (lambda server_id, message: None)
        # (req, message) for every bridge error frame, in addition to
        # on_error: lets a consumer fail exactly the request the bridge refused.
        self._on_request_error = on_request_error
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._stop = False
        self._ws = None
        self._loop = None
        self._wake = None
        self._send_lock = asyncio.Lock()
        self._stopping_terms: set[str] = set()
        self._last_connect_error: str | None = None
        self._last_logged_error: str | None = None
        self._protocol = 1
        self._capabilities: frozenset[str] = frozenset()
        # /health diagnostics: why is this server dark, and since when?
        self._connected = False
        # A server that has never answered (configured but not running, e.g.
        # an unused T3) is not "down": the window stays quiet about it, like
        # the panel (orchestrator._ever_up).
        self._ever_connected = False
        self._since_ms = _now_ms()
        self._attempt = 0
        self._bridge_version: str | None = None
        self._warned_protocol: int | None = None
        # None = this consumer renders no tiles (e.g. ctl): never opt in.
        self._on_project_icon = on_project_icon
        self._icons_requested = False

    @property
    def last_connect_error(self) -> str | None:
        """The most recent connect-failure reason (None after a successful
        connect). Surfaced e.g. by ctl's first-snapshot timeout message."""
        return self._last_connect_error

    @property
    def protocol(self) -> int:
        return self._protocol

    @property
    def capabilities(self) -> frozenset[str]:
        return self._capabilities

    def health(self) -> dict:
        """Non-secret connection facts for the runtime's /health. ``since`` is
        the unix ms of the last connected/disconnected change; ``attempt``
        counts consecutive failed connects (0 while connected)."""
        return {
            "connected": self._connected,
            "ever_connected": self._ever_connected,
            "last_error": self._last_connect_error,
            "since": self._since_ms,
            "attempt": self._attempt,
            "bridge_version": self._bridge_version,
            "protocol": self._protocol,
            "protocol_supported": self._protocol <= WIRE_PROTOCOL,
        }

    def _set_connected(self, up: bool) -> None:
        if up != self._connected:
            self._connected = up
            self._since_ms = _now_ms()
        self._on_connection(self.server.id, up)

    def stop(self) -> None:
        self._stop = True
        loop = self._loop
        ws = self._ws
        wake = self._wake
        if loop is not None:
            if ws is not None:
                loop.call_soon_threadsafe(lambda: asyncio.ensure_future(ws.close()))
            if wake is not None:
                loop.call_soon_threadsafe(wake.set)

    async def send(self, msg: dict) -> None:
        async with self._send_lock:
            ws = self._ws
            if ws is not None:
                try:
                    await ws.send(encode(msg))
                except websockets.WebSocketException:
                    pass

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._attempt = 0
        while not self._stop:
            connected = False
            try:
                async with websockets.connect(
                    self.server.url,
                    additional_headers={"Authorization": f"Bearer {self.server.token}"},
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
                    self._ws = ws
                    if self._stop:
                        await ws.close()
                        break
                    self._stopping_terms.clear()
                    self._icons_requested = False  # opt-in is per connection
                    self._attempt = 0
                    connected = True
                    self._ever_connected = True
                    self._set_connected(True)
                    await ws.send(encode({"type": "list"}))  # resync-on-reconnect
                    first = True
                    async for raw in ws:
                        if first:
                            # A frame arrived, so this connect genuinely authed:
                            # reset the failure memory (a handshake alone is not
                            # proof — the bridge closes bad tokens with 4401
                            # AFTER accepting the connection).
                            first = False
                            self._last_connect_error = None
                            self._last_logged_error = None
                        try:
                            self._dispatch(raw)
                        except Exception as exc:
                            self._on_error(str(exc) or type(exc).__name__)
            except (OSError, websockets.WebSocketException) as exc:
                reason = _describe_connect_error(exc)
                if reason != self._last_logged_error:
                    # log once per DISTINCT failure: a rejected token must be
                    # visible, a flapping network must not spam the log
                    log.warning(
                        "connect to '%s' (%s) failed: %s",
                        self.server.id,
                        self.server.url,
                        reason,
                    )
                    self._last_logged_error = reason
                self._last_connect_error = reason
            finally:
                self._ws = None
                if connected:
                    self._set_connected(False)
            if self._stop:
                break
            delay = min(self._backoff_base * (2**self._attempt), self._backoff_max)
            self._attempt += 1
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except TimeoutError:
                pass

    def _rekey(self, state: AgentState) -> AgentState:
        """Force the agent key to THIS connector's configured server id.

        The bridge sets its own server_id in frames; routing on the Mac is keyed
        by the config id, so we re-stamp inbound state to keep them consistent
        regardless of the bridge's HERDECK_SERVER_ID.
        """
        if state.key.server_id == self.server.id:
            return state
        # Re-stamp ONLY the key; replace() copies every other AgentState field.
        return dataclasses.replace(state, key=AgentKey(self.server.id, state.key.pane_id))

    def _dispatch(self, raw: str) -> None:
        msg = decode_inbound(raw)
        if isinstance(msg, Snapshot):
            self._protocol = msg.protocol
            self._capabilities = frozenset(msg.capabilities)
            self._bridge_version = msg.herdeck_version
            if msg.protocol > WIRE_PROTOCOL and self._warned_protocol != msg.protocol:
                # Rendering continues best-effort, but new fields may be blank;
                # this used to be silent until the runtime was upgraded.
                log.warning(
                    "bridge '%s' speaks unsupported wire protocol %s (this runtime "
                    "knows <= %s, bridge herdeck %s); upgrade this runtime",
                    self.server.id,
                    msg.protocol,
                    WIRE_PROTOCOL,
                    msg.herdeck_version or "unknown",
                )
                self._warned_protocol = msg.protocol
            self._on_snapshot(self.server.id, [self._rekey(s) for s in msg.states])
            self._maybe_request_icons()
        elif isinstance(msg, Event):
            self._on_event(self.server.id, self._rekey(msg.state))
        elif isinstance(msg, Result):
            self._on_result(msg.req, msg.data)
        elif isinstance(msg, TermFrame):
            if msg.req in self._stopping_terms:
                return
            self._on_term(self.server.id, msg)
        elif isinstance(msg, TermClosed):
            if msg.stop_remote:
                if msg.req in self._stopping_terms:
                    return
                self._stopping_terms.add(msg.req)
                asyncio.create_task(self.send({"type": "observe_stop", "req": msg.req}))
                self._on_term(self.server.id, TermClosed(msg.req, msg.reason))
                return
            if msg.req in self._stopping_terms:
                self._stopping_terms.discard(msg.req)
                return
            self._on_term(self.server.id, msg)
        elif isinstance(msg, ProjectIcon):
            if self._on_project_icon is not None:
                self._on_project_icon(self.server.id, msg)
        elif isinstance(msg, Unknown):
            return  # a newer bridge's frame type: ignored by design
        elif isinstance(msg, Error):
            self._on_error(msg.message)
            if self._on_request_error is not None:
                self._on_request_error(msg.req, msg.message)

    def _maybe_request_icons(self) -> None:
        """Opt in to project_icon frames once per connection — only when this
        consumer renders them and the bridge advertises them. The first
        snapshot of a connection is the earliest point the capability is
        known; the bridge answers the extra ``list`` with a snapshot followed
        by every icon it references."""
        if (
            self._on_project_icon is None
            or self._icons_requested
            or PROJECT_ICON_FEATURE not in self._capabilities
        ):
            return
        self._icons_requested = True
        asyncio.create_task(self.send({"type": "list", "features": [PROJECT_ICON_FEATURE]}))
