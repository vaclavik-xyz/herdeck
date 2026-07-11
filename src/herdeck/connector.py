from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Callable

import websockets

from .config import ServerConfig
from .model import AgentKey, AgentState
from .protocol import (
    Error,
    Event,
    Result,
    Snapshot,
    TermClosed,
    TermFrame,
    decode_inbound,
    encode,
)

log = logging.getLogger("herdeck.connector")


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
    ):
        self.server = server
        self._on_snapshot = on_snapshot
        self._on_event = on_event
        self._on_connection = on_connection
        self._on_result = on_result or (lambda req, data: None)
        self._on_error = on_error or (lambda message: None)
        self._on_term = on_term or (lambda server_id, message: None)
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

    @property
    def last_connect_error(self) -> str | None:
        """The most recent connect-failure reason (None after a successful
        connect). Surfaced e.g. by ctl's first-snapshot timeout message."""
        return self._last_connect_error

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
        attempt = 0
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
                    self._stopping_terms.clear()
                    attempt = 0
                    connected = True
                    self._on_connection(self.server.id, True)
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
                    self._on_connection(self.server.id, False)
            if self._stop:
                break
            delay = min(self._backoff_base * (2**attempt), self._backoff_max)
            attempt += 1
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
        # Re-stamp ONLY the key; replace() copies every other field so a new
        # AgentState field (e.g. custom_status) is never silently dropped here.
        return dataclasses.replace(state, key=AgentKey(self.server.id, state.key.pane_id))

    def _dispatch(self, raw: str) -> None:
        msg = decode_inbound(raw)
        if isinstance(msg, Snapshot):
            self._on_snapshot(self.server.id, [self._rekey(s) for s in msg.states])
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
        elif isinstance(msg, Error):
            self._on_error(msg.message)
