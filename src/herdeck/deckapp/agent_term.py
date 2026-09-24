"""Live terminal previews for the desktop agent card.

The bridge streams a pane with ``observe`` (herdr ``terminal session observe``)
as ``term_frame`` messages (base64 ANSI) until ``observe_stop`` or its own
``term_closed``. The desktop shell can only relay plain HTTP responses (no
chunked bodies), so each card preview becomes a *session* here: frames land in
a small bounded buffer and the card long-polls it with a cursor.

Observation never outlives its viewer:

* the card closes its session on close / toggle-off / window hide;
* a session nobody polled for ``idle_s`` is stopped by a reaper thread (a
  crashed or frozen WebView cannot leave a herdr observe process running);
* a dropped bridge connection ends every session on that server;
* at most ``max_sessions`` run at once (the bridge allows 3 per connection and
  8 in total) — opening another evicts the least recently polled one.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from ..protocol import TermClosed, TermFrame

# The bridge's own clamps (bridge._OBSERVE_*): asking outside them is pointless.
COLS_MIN, COLS_MAX = 20, 240
ROWS_MIN, ROWS_MAX = 5, 100
# Longest a poll may hold (the desktop proxy allows its wait + 6 s).
POLL_MAX_S = 15.0


@dataclass
class _Session:
    id: str
    req: str
    server_id: str
    pane_id: str
    last_seen: float
    frames: deque = field(default_factory=deque)  # (index, frame dict)
    size: int = 0
    next_index: int = 1
    dropped: bool = False  # frames were evicted since the last full frame
    closed: str | None = None


def _clamp(value, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


class CardTerminals:
    """Card preview sessions. ``send(server_id, msg) -> bool`` hands a wire
    message to that server's connector (False when it cannot)."""

    def __init__(
        self,
        send: Callable[[str, dict], bool],
        *,
        clock: Callable[[], float] = time.monotonic,
        max_sessions: int = 2,
        idle_s: float = 15.0,
        max_frames: int = 512,
        max_bytes: int = 2 * 1024 * 1024,
        reaper: bool = True,
        reap_interval_s: float = 2.0,
    ):
        self._send = send
        self._clock = clock
        self._max_sessions = max_sessions
        self._idle_s = idle_s
        self._max_frames = max_frames
        self._max_bytes = max_bytes
        self._reaper_enabled = reaper
        self._reap_interval_s = reap_interval_s
        self._cond = threading.Condition()
        self._sessions: dict[str, _Session] = {}
        self._by_req: dict[str, str] = {}
        self._reaper: threading.Thread | None = None
        self._stopping = False

    # --- lifecycle ---------------------------------------------------------
    def open(self, server_id: str, pane_id: str, terminal_id: str, *, cols, rows) -> str:
        cols = _clamp(cols, COLS_MIN, COLS_MAX, 100)
        rows = _clamp(rows, ROWS_MIN, ROWS_MAX, 30)
        stops: list[tuple[str, dict]] = []
        with self._cond:
            stops += self._reap_locked()
            while len(self._sessions) >= self._max_sessions:
                oldest = min(self._sessions.values(), key=lambda s: s.last_seen)
                stops += self._drop_locked(oldest, stop_remote=True)
            session_id = secrets.token_hex(8)
            session = _Session(
                session_id, f"c{session_id}", server_id, pane_id, last_seen=self._clock()
            )
            self._sessions[session_id] = session
            self._by_req[session.req] = session_id
            self._ensure_reaper_locked()
        self._flush(stops)
        msg = {"type": "observe", "req": session.req, "pane_id": pane_id, "cols": cols, "rows": rows}
        if terminal_id:
            msg["terminal_id"] = terminal_id
        if not self._safe_send(server_id, msg):
            self._finish(session, "disconnected")
        return session_id

    def close(self, session_id: str) -> bool:
        with self._cond:
            session = self._sessions.get(session_id)
            if session is None:
                return False
            stops = self._drop_locked(session, stop_remote=session.closed is None)
        self._flush(stops)
        return True

    def close_server(self, server_id: str, reason: str) -> None:
        """The connection is gone: end its sessions (no stop to send)."""
        with self._cond:
            for session in self._sessions.values():
                if session.server_id == server_id and session.closed is None:
                    session.closed = reason
            self._cond.notify_all()

    def close_all(self) -> None:
        with self._cond:
            self._stopping = True
            stops: list[tuple[str, dict]] = []
            for session in list(self._sessions.values()):
                stops += self._drop_locked(session, stop_remote=session.closed is None)
            self._cond.notify_all()
        self._flush(stops)

    # --- data --------------------------------------------------------------
    def on_term(self, server_id: str, message: TermFrame | TermClosed) -> None:
        stops: list[tuple[str, dict]] = []
        with self._cond:
            session_id = self._by_req.get(message.req)
            session = self._sessions.get(session_id) if session_id else None
            if session is None or session.server_id != server_id or session.closed is not None:
                return
            if isinstance(message, TermClosed):
                session.closed = message.reason or "closed"
            elif len(message.data) > self._max_bytes:
                session.closed = "terminal frame too large"
                stops.append((server_id, {"type": "observe_stop", "req": session.req}))
            else:
                if message.full:
                    session.frames.clear()
                    session.size = 0
                    session.dropped = False
                session.frames.append((
                    session.next_index,
                    {
                        "seq": message.seq,
                        "full": message.full,
                        "cols": message.cols,
                        "rows": message.rows,
                        "data": message.data,
                    },
                ))
                session.next_index += 1
                session.size += len(message.data)
                while len(session.frames) > self._max_frames or session.size > self._max_bytes:
                    _, old = session.frames.popleft()
                    session.size -= len(old["data"])
                    session.dropped = True
            self._cond.notify_all()
        self._flush(stops)

    def poll(self, session_id: str, after: int, wait_s: float) -> dict | None:
        """Frames after cursor ``after`` (waits up to ``wait_s`` for one).

        ``gap`` says frames the caller never saw were evicted and the first
        returned one is not a full repaint (the caller should reset and wait
        for the next full frame). After a close is delivered with the final
        frames, the session is forgotten (a later poll answers None)."""
        wait_s = max(0.0, min(POLL_MAX_S, wait_s))
        with self._cond:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            session.last_seen = self._clock()

            def ready() -> bool:
                return (
                    self._sessions.get(session_id) is not session
                    or session.closed is not None
                    or session.next_index - 1 > after
                )

            self._cond.wait_for(ready, timeout=wait_s)
            if self._sessions.get(session_id) is not session:
                return None
            session.last_seen = self._clock()
            frames = [f for index, f in session.frames if index > after]
            first = session.frames[0][0] if session.frames else session.next_index
            gap = session.dropped and after < first - 1 and not (frames and frames[0]["full"])
            result = {
                "frames": frames,
                "next": session.next_index - 1,
                "closed": session.closed,
                "gap": gap,
            }
            if session.closed is not None:
                self._drop_locked(session, stop_remote=False)
            return result

    def poll_exists(self, session_id: str) -> bool:
        with self._cond:
            return session_id in self._sessions

    # --- reaping -----------------------------------------------------------
    def reap(self) -> None:
        with self._cond:
            stops = self._reap_locked()
        self._flush(stops)

    def _reap_locked(self) -> list[tuple[str, dict]]:
        now = self._clock()
        stops: list[tuple[str, dict]] = []
        for session in list(self._sessions.values()):
            if now - session.last_seen > self._idle_s:
                stops += self._drop_locked(session, stop_remote=session.closed is None)
        return stops

    def _ensure_reaper_locked(self) -> None:
        if not self._reaper_enabled or (self._reaper is not None and self._reaper.is_alive()):
            return
        self._reaper = threading.Thread(
            target=self._reap_loop, name="herdeck-card-term-reaper", daemon=True
        )
        self._reaper.start()

    def _reap_loop(self) -> None:
        while True:
            with self._cond:
                self._cond.wait(self._reap_interval_s)
                if self._stopping or not self._sessions:
                    self._reaper = None
                    return
                stops = self._reap_locked()
            self._flush(stops)

    # --- helpers -----------------------------------------------------------
    def _drop_locked(self, session: _Session, *, stop_remote: bool) -> list[tuple[str, dict]]:
        self._sessions.pop(session.id, None)
        self._by_req.pop(session.req, None)
        if session.closed is None:
            session.closed = "stopped"
        self._cond.notify_all()
        if stop_remote:
            return [(session.server_id, {"type": "observe_stop", "req": session.req})]
        return []

    def _finish(self, session: _Session, reason: str) -> None:
        with self._cond:
            if session.closed is None:
                session.closed = reason
            self._cond.notify_all()

    def _safe_send(self, server_id: str, msg: dict) -> bool:
        try:
            return bool(self._send(server_id, msg))
        except Exception:
            return False

    def _flush(self, stops: list[tuple[str, dict]]) -> None:
        # Sends happen outside the condition lock (a runner may call back).
        for server_id, msg in stops:
            self._safe_send(server_id, msg)
