from __future__ import annotations

import contextlib
import hashlib
import hmac
import io
import ipaddress
import json
import os
import queue
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import files
from urllib.parse import parse_qs, urlsplit, urlunsplit

from ..i18n import tr
from .base import DeckDriver, PanelView, TileView

# Panel press maps to this button index (the orchestrator pages on PANEL_INDICES).
_PANEL_PRESS_INDEX = 13
_TERMINAL_CANCEL = object()
_TERMINAL_STREAM_RE = re.compile(r"[A-Za-z0-9_-]{8,80}")
_WEB_ASSET_TYPES = {
    "xterm.js": "text/javascript; charset=utf-8",
    "addon-fit.js": "text/javascript; charset=utf-8",
    "xterm.css": "text/css; charset=utf-8",
}


def normalize_web_base_path(value: str) -> str:
    value = value.strip()
    if value in {"", "/"}:
        return ""
    if not value.startswith("/") or value.endswith("/") or "//" in value:
        raise ValueError("web base path must look like /herdeck (without a trailing slash)")
    segments = value[1:].split("/")
    if any(
        segment in {"", ".", ".."} or re.fullmatch(r"[A-Za-z0-9._~-]+", segment) is None
        for segment in segments
    ):
        raise ValueError("web base path contains an unsafe segment")
    return value


def normalize_web_origin(value: str, *, https_only: bool = False) -> str:
    value = value.strip()
    if not value:
        if https_only:
            raise ValueError("frame ancestor origin must not be empty")
        return ""
    parsed = urlsplit(value)
    allowed_schemes = {"https"} if https_only else {"http", "https"}
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"invalid web origin: {value}") from exc
    if (
        parsed.scheme.lower() not in allowed_schemes
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or "*" in parsed.netloc
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        scheme = "HTTPS" if https_only else "HTTP(S)"
        raise ValueError(f"web origin must be an exact {scheme} origin without a path")
    raw_host = parsed.hostname.lower()
    try:
        address = ipaddress.ip_address(raw_host)
    except ValueError:
        try:
            host = raw_host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError(f"invalid web origin: {value}") from exc
        labels = host.split(".")
        if len(host) > 253 or any(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) is None
            for label in labels
        ):
            raise ValueError(f"invalid web origin: {value}") from None
    else:
        host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    netloc = host if port in {None, default_port} else f"{host}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, "", "", ""))


def _service_info() -> dict:
    try:
        release = version("herdeck")
    except PackageNotFoundError:
        release = "unknown"
    return {
        "ok": True,
        "service": "herdeck-web",
        "version": release,
        "build": os.environ.get("HERDECK_BUILD_SHA", "unknown"),
    }


@dataclass
class _TerminalStream:
    cancel: threading.Event
    subscription: object | None = None


def _default_token_path() -> str:
    import os

    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "herdeck", "web-token")


def _load_or_create_token(path: str) -> str:
    """A press token that SURVIVES restarts (0600 state file), so the phone's
    bookmarked simulator URL keeps working across the constant restarts of a
    dev loop instead of dead-ending on 403 after every restart."""
    import os

    try:
        if os.stat(path).st_mode & 0o077:
            # repair a leaky pre-existing file before trusting its token
            os.chmod(path, 0o600)
        with open(path, encoding="utf-8") as fh:
            token = fh.read().strip()
        if token:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(24)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token)
    except OSError:
        pass  # an in-memory token still works for this run
    return token


def _env_flag(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"", "0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean flag")


class WebDeck(DeckDriver):
    """A browser-based D200 simulator.

    Renders tiles and the status panel with the SAME code as the real device, so
    the simulator is pixel-faithful, and turns browser clicks into presses. Lets
    you develop the whole app without the physical deck. Bind to a Tailscale IP to
    use it remotely.
    """

    def __init__(
        self,
        slots: int = 13,
        host: str = "127.0.0.1",
        port: int = 8800,
        icon_provider=None,
        icons_dir: str | None = None,
        serve: bool = True,
        press_token: str | None = None,
        token_path: str | None = None,
        cols: int = 5,
        language: str = "en",
        session_ttl: float = 8 * 60 * 60,
        session_clock: Callable[[], float] | None = None,
        base_path: str = "",
        public_origin: str = "",
        frame_ancestors: tuple[str, ...] = (),
        allow_query_token: bool | None = None,
    ):
        self._language = language
        self._bind_host = host
        self._slots = slots
        self._cols = max(1, cols)  # grid width; the page lays cells out with it
        self._callback: Callable[[int], None] | None = None
        self._semantic_request: Callable[[dict], object] | None = None
        self._terminal_open: Callable[[int, int, int, int], object] | None = None
        self._terminal_close: Callable[[object], None] | None = None
        self._terminal_lock = threading.Lock()
        self._terminal_streams: dict[str, _TerminalStream] = {}
        self._terminal_cancelled: dict[str, float] = {}
        self._session_lock = threading.Lock()
        self._sessions: dict[str, float] = {}
        self._session_ttl = session_ttl
        self._session_clock = session_clock or time.monotonic
        self._base_path = normalize_web_base_path(base_path)
        self._public_origin = normalize_web_origin(public_origin)
        self._frame_ancestors = tuple(
            normalize_web_origin(origin, https_only=True) for origin in frame_ancestors
        )
        if self._frame_ancestors and not self._public_origin.startswith("https://"):
            raise ValueError("frame ancestors require an explicit HTTPS public origin")
        self._cross_origin_embed = any(
            origin != self._public_origin for origin in self._frame_ancestors
        )
        self._allow_query_token = (
            _env_flag("HERDECK_WEB_ALLOW_QUERY_TOKEN", default=False)
            if allow_query_token is None
            else allow_query_token
        )
        self._lock = threading.Lock()
        # Long-poll support: /state?since=<version> HOLDS until _bump() fires.
        self._changed = threading.Condition(self._lock)
        self._tiles: dict[int, bytes] = {}  # index -> PNG bytes
        self._tile_ver: dict[int, int] = {}  # index -> last-changed version
        self._panel: bytes | None = None
        self._panel_ver = 0
        self._version = 0
        # Serving decks persist the token across restarts (bookmarkable URL);
        # an embedded/non-serving deck (tests) keeps an ephemeral one.
        if press_token is not None:
            self._press_token = press_token
        elif token_path is not None:
            self._press_token = _load_or_create_token(token_path)
        elif serve:
            self._press_token = _load_or_create_token(_default_token_path())
        else:
            self._press_token = secrets.token_urlsafe(24)
        if icon_provider is None:
            import os
            import tempfile

            from ..icons import DEFAULT_AGENT_SLUGS, IconProvider

            cache = os.path.join(tempfile.gettempdir(), "herdeck-web-icons")
            overrides = os.path.abspath(os.path.expanduser(icons_dir)) if icons_dir else None
            icon_provider = IconProvider(
                cache_dir=cache,
                slug_map=DEFAULT_AGENT_SLUGS,
                overrides_dir=overrides,
            )
        self._icons = icon_provider
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        if serve:
            self._server = ThreadingHTTPServer((host, port), self._handler_class())
            self.host, self.port = self._server.server_address[0], self._server.server_address[1]
            origin_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
            self._browser_origin = self._public_origin or normalize_web_origin(
                f"http://{origin_host}:{self.port}"
            )
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()
        else:
            self._browser_origin = ""

    # --- DeckDriver interface ---
    def slot_count(self) -> int:
        return self._slots

    @property
    def press_token(self) -> str:
        return self._press_token

    # How long a since-matched /state request is held before answering with
    # the unchanged state (the client just re-polls).
    LONG_POLL_TIMEOUT = 25.0
    TERMINAL_PING_INTERVAL = 15.0
    TERMINAL_WRITE_TIMEOUT = 5.0
    TERMINAL_STREAM_LIMIT = 3
    TERMINAL_CANCEL_TTL = 30.0
    TERMINAL_CANCEL_LIMIT = 256
    BROWSER_SESSION_LIMIT = 256
    SEMANTIC_TIMEOUT = 8.0
    JSON_BODY_LIMIT = 16 * 1024

    def _mint_browser_session(self) -> str:
        now = self._session_clock()
        token = secrets.token_urlsafe(32)
        with self._session_lock:
            self._sessions = {
                value: expiry for value, expiry in self._sessions.items() if expiry > now
            }
            while len(self._sessions) >= self.BROWSER_SESSION_LIMIT:
                self._sessions.pop(next(iter(self._sessions)))
            self._sessions[token] = now + self._session_ttl
        return token

    def _browser_session_expiry(self, token: str) -> float | None:
        with self._session_lock:
            return self._sessions.get(token)

    def _revoke_browser_session(self, token: str) -> bool:
        with self._session_lock:
            return self._sessions.pop(token, None) is not None

    def _valid_browser_session(self, token: str) -> bool:
        if not token:
            return False
        now = self._session_clock()
        with self._session_lock:
            expiry = self._sessions.get(token)
            if expiry is None:
                return False
            if expiry <= now:
                self._sessions.pop(token, None)
                return False
            return True

    def _bump(self) -> int:
        """Assign the next monotonic version. Call while holding self._lock."""
        self._version += 1
        self._changed.notify_all()  # release any held long-polls
        return self._version

    def render(self, tiles: list[TileView]) -> None:
        new: dict[int, bytes] = {}
        for t in tiles:
            if t.index >= self._slots:
                continue
            new[t.index] = self._icons.render_tile_bytes(t)
        with self._lock:
            for i, png in new.items():
                if self._tiles.get(i) != png:  # bump only changed/new tiles
                    self._tile_ver[i] = self._bump()
            removed = set(self._tile_ver) - set(new)
            for i in removed:  # drop versions of gone tiles
                del self._tile_ver[i]
            if removed:  # a pure removal must still
                self._bump()  # trip the client's version gate
            self._tiles = new

    def render_working(self, tiles: list[TileView]) -> None:
        """Partial re-render of just the given (working) tiles: bumps only their
        versions and leaves every other tile and the panel untouched, so the
        browser refetches just the animating tiles instead of the whole deck."""
        rendered: dict[int, bytes] = {}
        for t in tiles:
            if t.index >= self._slots:
                continue
            rendered[t.index] = self._icons.render_tile_bytes(t)
        with self._lock:
            for i, png in rendered.items():
                if self._tiles.get(i) != png:
                    self._tiles[i] = png
                    self._tile_ver[i] = self._bump()

    def render_panel(self, panel: PanelView) -> None:
        from ..icons import PANEL_W_TWO_CELL, compose_panel

        buf = io.BytesIO()
        # The page shows the panel in a 2-cells-wide box (width:100%/height:100%),
        # so compose at the two-cell width — the native 458px would be squeezed.
        compose_panel(panel, width=PANEL_W_TWO_CELL).convert("RGB").save(buf, "PNG")
        png = buf.getvalue()
        with self._lock:
            if self._panel != png:  # bump only when it changes
                self._panel = png
                self._panel_ver = self._bump()

    def on_press(self, callback: Callable[[int], None]) -> None:
        self._callback = callback

    def on_semantic(self, callback: Callable[[dict], object]) -> None:
        """Register the app-loop adapter used by the versioned cockpit API."""
        self._semantic_request = callback

    def on_terminal(
        self,
        open_callback: Callable[[int, int, int, int], object],
        close_callback: Callable[[object], None],
    ) -> None:
        """Register the app-side provider for read-only terminal streams."""
        self._terminal_open = open_callback
        self._terminal_close = close_callback

    def terminal_tile_is_current(self, index: int, version: int) -> bool:
        """Return whether *version* still names the image served for *index*."""
        with self._lock:
            return self._tile_ver.get(index) == version

    def press(self, index: int) -> None:
        """Inject a press (called by the HTTP handler thread; the app marshals).

        Only buttons (0..slots-1) and the two panel cells are valid; anything
        else (e.g. a negative index from a crafted request) is ignored.
        """
        if self._callback is not None and 0 <= index < self._slots + 2:
            self._callback(index)

    def close(self) -> None:
        with self._terminal_lock:
            terminal_streams = list(self._terminal_streams.values())
        for stream in terminal_streams:
            stream.cancel.set()
            self._wake_terminal_stream(stream)
        with self._lock:
            self._changed.notify_all()  # release held long-polls before shutdown
        server = self._server
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
            self._server = None
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)
        self._thread = None

    def _reserve_terminal_stream(self, stream_id: str) -> tuple[_TerminalStream | None, str]:
        with self._terminal_lock:
            self._prune_terminal_cancellations_locked()
            if self._terminal_cancelled.pop(stream_id, None) is not None:
                return None, "cancelled"
            if stream_id in self._terminal_streams:
                return None, "duplicate"
            if len(self._terminal_streams) >= self.TERMINAL_STREAM_LIMIT:
                return None, "busy"
            stream = _TerminalStream(threading.Event())
            self._terminal_streams[stream_id] = stream
            return stream, ""

    def _prune_terminal_cancellations_locked(self) -> None:
        now = time.monotonic()
        self._terminal_cancelled = {
            stream_id: expires
            for stream_id, expires in self._terminal_cancelled.items()
            if expires > now
        }

    def _bind_terminal_stream(self, stream_id: str, stream: _TerminalStream, sub: object) -> bool:
        with self._terminal_lock:
            if self._terminal_streams.get(stream_id) is not stream:
                return False
            stream.subscription = sub
            return not stream.cancel.is_set()

    def _release_terminal_stream(self, stream_id: str, stream: _TerminalStream) -> None:
        with self._terminal_lock:
            if self._terminal_streams.get(stream_id) is stream:
                del self._terminal_streams[stream_id]

    @staticmethod
    def _wake_terminal_stream(stream: _TerminalStream) -> None:
        sub_queue = getattr(stream.subscription, "queue", None)
        if sub_queue is None:
            return
        # Explicit close supersedes buffered frames; make cancellation the next
        # item observed instead of waiting behind a full slow-client backlog.
        with contextlib.suppress(queue.Empty):
            while True:
                sub_queue.get_nowait()
        with contextlib.suppress(queue.Full):
            sub_queue.put_nowait(_TERMINAL_CANCEL)

    def _cancel_terminal_stream(self, stream_id: str) -> None:
        with self._terminal_lock:
            stream = self._terminal_streams.get(stream_id)
            if stream is None:
                self._prune_terminal_cancellations_locked()
                if len(self._terminal_cancelled) >= self.TERMINAL_CANCEL_LIMIT:
                    oldest = min(self._terminal_cancelled, key=self._terminal_cancelled.get)
                    del self._terminal_cancelled[oldest]
                self._terminal_cancelled[stream_id] = time.monotonic() + self.TERMINAL_CANCEL_TTL
                return
            stream.cancel.set()
        self._wake_terminal_stream(stream)

    # --- state snapshot for the browser ---
    def _state_locked(self) -> dict:
        return {
            "version": self._version,
            "slots": self._slots,
            "cols": self._cols,
            "has_panel": self._panel is not None,
            "panel": self._panel_ver,
            "tiles": dict(self._tile_ver),
        }

    def _state(self, since=None) -> dict:
        """The state snapshot; with `since` equal to the current version the
        request is HELD until something changes (long poll) or the timeout
        lapses. Idle phone traffic drops from 3.3 req/s to ~2 req/min and a
        change arrives at network latency instead of poll latency."""
        try:
            since_v = int(since)
        except (TypeError, ValueError):
            since_v = None
        with self._lock:
            if since_v is not None and self._version == since_v:
                self._changed.wait(timeout=self.LONG_POLL_TIMEOUT)
            return self._state_locked()

    def _tile_png(self, index: int, version: int | None = None) -> bytes | None:
        with self._lock:
            if version is not None and self._tile_ver.get(index) != version:
                return None
            return self._tiles.get(index)

    def _panel_png(self) -> bytes | None:
        with self._lock:
            return self._panel

    # --- HTTP ---
    def _handler_class(self):
        deck = self
        forbidden = b"herdeck simulator: a valid browser session is required.\n"
        if deck._allow_query_token:
            forbidden += (
                b"Open the full URL including the ?token=... part printed by herdeck-web url.\n"
            )
        forbidden_page = (
            b"<!doctype html><meta charset=utf-8><title>herdeck simulator</title>"
            b'<body style="background:#0b0b0d;color:#e7ecf3;'
            b'font:14px/1.5 system-ui;padding:2em;max-width:32em">'
            b"<h2>Browser session required</h2>"
            + b"<p>"
            + tr(
                deck._language,
                "web.forbidden" if deck._allow_query_token else "web.session_required",
            ).encode()
            + b"</p>"
        )
        page_headers = {
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "connect-src 'self'; object-src 'none'; base-uri 'none'; "
                "frame-ancestors "
                + (" ".join(deck._frame_ancestors) if deck._frame_ancestors else "'none'")
            ),
        }

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # silence default request logging
                pass

            def _send(
                self,
                code,
                body=b"",
                ctype="text/plain; charset=utf-8",
                headers=None,
            ):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _send_json(self, code, body, headers=None):
                merged = {"Cache-Control": "no-store", **(headers or {})}
                self._send(
                    code,
                    json.dumps(body, ensure_ascii=True, separators=(",", ":")).encode(),
                    "application/json; charset=utf-8",
                    merged,
                )

            def _api_error(self, code, error_code, message):
                self._send_json(
                    code,
                    {
                        "api_version": "v1",
                        "error": {"code": error_code, "message": message},
                    },
                )

            def _read_json(self):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self.close_connection = True
                    return None
                if length <= 0 or length > deck.JSON_BODY_LIMIT:
                    self.close_connection = True
                    return None
                try:
                    return json.loads(self.rfile.read(length))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self.close_connection = True
                    return None

            def _session_cookie(self, session, *, clear=False):
                path = f"{deck._base_path}/" if deck._base_path else "/"
                parts = [f"herdeck_session={'' if clear else session}", f"Path={path}", "HttpOnly"]
                parts.append("SameSite=" + ("None" if deck._cross_origin_embed else "Strict"))
                if deck._public_origin.startswith("https://"):
                    parts.append("Secure")
                if clear:
                    parts.append("Max-Age=0")
                else:
                    parts.append(f"Max-Age={max(1, int(deck._session_ttl))}")
                return "; ".join(parts)

            def _api_caller(self, *, write=False, header_only=False):
                token = self.headers.get("X-Herdeck-Token", "")
                if self._valid_token(token):
                    return "server:" + hashlib.sha256(token.encode()).hexdigest()
                if not header_only:
                    session = self._browser_session()
                    if deck._valid_browser_session(session) and (
                        not write or self._valid_write_auth()
                    ):
                        return "browser:" + hashlib.sha256(session.encode()).hexdigest()
                return ""

            def _semantic(self, operation, caller, payload=None):
                if deck._semantic_request is None:
                    self._api_error(503, "service_unavailable", "semantic runtime is unavailable")
                    return
                future = None
                try:
                    future = deck._semantic_request(
                        {"operation": operation, "caller": caller, "payload": payload}
                    )
                    response = future.result(timeout=deck.SEMANTIC_TIMEOUT)
                    self._send_json(response.status, response.body)
                except TimeoutError:
                    if future is not None:
                        future.cancel()
                    self._api_error(504, "timeout", "semantic runtime timed out")
                except Exception:
                    self._api_error(503, "service_unavailable", "semantic runtime is unavailable")

            def _begin_sse(self):
                self.close_connection = True
                self.connection.settimeout(deck.TERMINAL_WRITE_TIMEOUT)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()

            def _emit_sse(self, item) -> bool:
                if item is _TERMINAL_CANCEL:
                    return False
                payload = f"data: {json.dumps(item, ensure_ascii=False)}\n\n".encode()
                try:
                    self.wfile.write(payload)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                    return False
                return True

            def _emit_ping(self) -> bool:
                try:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                    return False
                return True

            def _valid_token(self, token):
                return hmac.compare_digest(token.encode(), deck._press_token.encode())

            def _query_token(self, url):
                return parse_qs(url.query).get("token", [""])[0]

            def _browser_session(self):
                try:
                    cookie = SimpleCookie(self.headers.get("Cookie", ""))
                except CookieError:
                    return ""
                morsel = cookie.get("herdeck_session")
                return morsel.value if morsel is not None else ""

            def _require_token(self, url):
                legacy_token_valid = deck._allow_query_token and self._valid_token(
                    self._query_token(url)
                )
                header_token_valid = self._valid_token(
                    self.headers.get("X-Herdeck-Token", "")
                )
                if (
                    legacy_token_valid
                    or header_token_valid
                    or deck._valid_browser_session(self._browser_session())
                ):
                    return True
                self._send(403, forbidden)
                return False

            def _valid_write_auth(self):
                if self._valid_token(self.headers.get("X-Herdeck-Token", "")):
                    return True
                try:
                    origin = normalize_web_origin(self.headers.get("Origin", ""))
                    if deck._public_origin:
                        expected_origin = deck._public_origin
                    elif deck._bind_host in {"0.0.0.0", "::"}:
                        expected_origin = normalize_web_origin(
                            f"http://{self.headers.get('Host', '')}"
                        )
                    else:
                        expected_origin = deck._browser_origin
                except ValueError:
                    return False
                return (
                    deck._valid_browser_session(self._browser_session())
                    and bool(origin)
                    and origin == expected_origin
                )

            def _route_path(self, raw_path):
                if not deck._base_path:
                    return raw_path
                if raw_path == deck._base_path:
                    return "/"
                if not raw_path.startswith(deck._base_path + "/"):
                    return None
                return raw_path[len(deck._base_path) :]

            def do_GET(self):
                url = urlsplit(self.path)
                path = self._route_path(url.path)
                if path is None:
                    self._send(404)
                    return
                if path == "/healthz":
                    self._send(
                        200,
                        json.dumps(_service_info()).encode(),
                        "application/json",
                        {"Cache-Control": "no-store"},
                    )
                elif path == "/readyz":
                    if not self._require_token(url):
                        return
                    state = deck._state()
                    self._send(
                        200,
                        json.dumps(
                            {
                                **_service_info(),
                                "ready": True,
                                "state_version": state["version"],
                            }
                        ).encode(),
                        "application/json",
                        {"Cache-Control": "no-store"},
                    )
                elif path == "/":
                    token = self._query_token(url)
                    if deck._allow_query_token and self._valid_token(token):
                        session = deck._mint_browser_session()
                        self._send(
                            303,
                            headers={
                                "Location": (deck._base_path or "") + "/",
                                "Set-Cookie": self._session_cookie(session),
                                "Cache-Control": "no-store",
                                "Referrer-Policy": "no-referrer",
                            },
                        )
                        return
                    if not deck._valid_browser_session(self._browser_session()):
                        # A browser tab is the only consumer of "/": a readable
                        # page beats a plaintext dead-end.
                        self._send(
                            403,
                            forbidden_page,
                            "text/html; charset=utf-8",
                            page_headers,
                        )
                        return
                    page = (
                        _PAGE.replace("__BASE_PATH__", deck._base_path)
                        .replace("__BASE_PATH_JSON__", json.dumps(deck._base_path))
                        .replace(
                            "__L_JSON__",
                            json.dumps(
                                {
                                    "pressFailed": tr(deck._language, "web.press_failed"),
                                    "tokenExpired": tr(deck._language, "web.token_expired"),
                                    "disconnected": tr(deck._language, "web.disconnected"),
                                    "termClose": tr(deck._language, "web.term_close"),
                                    "termConnecting": tr(deck._language, "web.term_connecting"),
                                    "termEnded": tr(deck._language, "web.term_ended"),
                                    "termDisconnected": tr(deck._language, "web.term_disconnected"),
                                    "termLive": tr(deck._language, "web.term_live"),
                                    "termReadOnly": tr(deck._language, "web.term_read_only"),
                                    "termTitle": tr(deck._language, "web.term_title"),
                                    "termHint": tr(deck._language, "web.term_hint"),
                                    "termConnectingBadge": tr(
                                        deck._language, "web.term_connecting_badge"
                                    ),
                                    "termEndedBadge": tr(deck._language, "web.term_ended_badge"),
                                },
                                ensure_ascii=False,  # keep em-dashes/diacritics readable in the page
                            ),
                        )
                    )
                    self._send(
                        200,
                        page.encode(),
                        "text/html; charset=utf-8",
                        page_headers,
                    )
                elif path == "/api/v1/agents":
                    caller = self._api_caller()
                    if not caller:
                        self._api_error(401, "unauthorized", "missing or invalid credentials")
                        return
                    self._semantic("inventory", caller)
                elif path == "/state":
                    if not self._require_token(url):
                        return
                    since = parse_qs(url.query).get("since", [None])[0]
                    self._send(200, json.dumps(deck._state(since)).encode(), "application/json")
                elif path == "/panel":
                    if not self._require_token(url):
                        return
                    png = deck._panel_png()
                    self._send(200, png, "image/png") if png else self._send(404)
                elif path.startswith("/tile/"):
                    if not self._require_token(url):
                        return
                    try:
                        index = int(path.rsplit("/", 1)[1])
                        tile_version = int(parse_qs(url.query)["v"][0])
                        png = deck._tile_png(index, tile_version)
                    except (KeyError, ValueError):
                        png = None
                    self._send(200, png, "image/png") if png else self._send(404)
                elif path.startswith("/assets/"):
                    name = path.rsplit("/", 1)[1]
                    ctype = _WEB_ASSET_TYPES.get(name)
                    if ctype is None:
                        self._send(404)
                        return
                    body = files("herdeck").joinpath("assets", "web", name).read_bytes()
                    self._send(
                        200,
                        body,
                        ctype,
                        {"Cache-Control": "public, max-age=3600"},
                    )
                elif path.startswith("/term/"):
                    if not self._require_token(url):
                        return
                    self._serve_terminal(url, path)
                else:
                    self._send(404)

            def _serve_terminal(self, url, path):
                if deck._terminal_open is None or deck._terminal_close is None:
                    self._send(503)
                    return
                try:
                    index = int(path.rsplit("/", 1)[1])
                    cols = int(parse_qs(url.query).get("cols", [80])[0])
                    rows = int(parse_qs(url.query).get("rows", [24])[0])
                    tile_version = int(parse_qs(url.query)["v"][0])
                except (TypeError, ValueError):
                    self._send(400)
                    return
                except KeyError:
                    self._send(400)
                    return
                if not 0 <= index < deck._slots:
                    self._send(400)
                    return
                if not deck.terminal_tile_is_current(index, tile_version):
                    self._send(409)
                    return
                cols = max(20, min(240, cols))
                rows = max(5, min(100, rows))
                stream_id = parse_qs(url.query).get("stream", [""])[0]
                if not _TERMINAL_STREAM_RE.fullmatch(stream_id):
                    self._send(400)
                    return

                stream, error = deck._reserve_terminal_stream(stream_id)
                if error == "duplicate":
                    self._send(409)
                    return
                self._begin_sse()
                if error == "cancelled":
                    return
                if error == "busy":
                    self._emit_sse(
                        {
                            "kind": "closed",
                            "reason": tr(deck._language, "web.term_busy"),
                        }
                    )
                    return
                assert stream is not None

                subscription = None
                try:
                    try:
                        subscription = deck._terminal_open(index, cols, rows, tile_version)
                    except Exception:
                        self._emit_sse(
                            {
                                "kind": "closed",
                                "reason": tr(deck._language, "web.term_ended"),
                            }
                        )
                        return
                    if not deck._bind_terminal_stream(stream_id, stream, subscription):
                        return
                    sub_queue = getattr(subscription, "queue", None)
                    if sub_queue is None:
                        self._emit_sse(
                            {
                                "kind": "closed",
                                "reason": tr(deck._language, "web.term_ended"),
                            }
                        )
                        return
                    while not stream.cancel.is_set():
                        try:
                            item = sub_queue.get(timeout=deck.TERMINAL_PING_INTERVAL)
                        except queue.Empty:
                            if not self._emit_ping():
                                break
                            continue
                        if item is _TERMINAL_CANCEL or not self._emit_sse(item):
                            break
                        if isinstance(item, dict) and item.get("kind") == "closed":
                            break
                finally:
                    if subscription is not None:
                        with contextlib.suppress(Exception):
                            deck._terminal_close(subscription)
                    deck._release_terminal_stream(stream_id, stream)

            def do_POST(self):
                path = self._route_path(urlsplit(self.path).path)
                if path is None:
                    self._send(404)
                    return
                if path == "/api/v1/browser-sessions":
                    caller = self._api_caller(header_only=True)
                    if not caller:
                        self._api_error(401, "unauthorized", "missing or invalid credentials")
                        return
                    session = deck._mint_browser_session()
                    self._send_json(
                        201,
                        {
                            "api_version": "v1",
                            "expires_in": max(1, int(deck._session_ttl)),
                        },
                        {"Set-Cookie": self._session_cookie(session)},
                    )
                elif path in {
                    "/api/v1/actions",
                    "/api/v1/text",
                    "/api/v1/decisions",
                    "/api/v1/choices",
                }:
                    caller = self._api_caller(write=True)
                    if not caller:
                        self._api_error(401, "unauthorized", "missing or invalid credentials")
                        return
                    payload = self._read_json()
                    if payload is None:
                        self._api_error(400, "invalid_json", "request body must be valid JSON")
                        return
                    operation = {
                        "/api/v1/actions": "action",
                        "/api/v1/text": "text",
                        "/api/v1/decisions": "decisions",
                        "/api/v1/choices": "choice",
                    }[path]
                    self._semantic(operation, caller, payload)
                elif path.startswith("/press/"):
                    if not self._valid_write_auth():
                        self._send(403, forbidden)
                        return
                    try:
                        deck.press(int(path.rsplit("/", 1)[1]))
                        self._send(204)
                    except ValueError:
                        self._send(400)
                elif path.startswith("/term-stop/"):
                    if not self._valid_write_auth():
                        self._send(403, forbidden)
                        return
                    stream_id = path.rsplit("/", 1)[1]
                    if not _TERMINAL_STREAM_RE.fullmatch(stream_id):
                        self._send(400)
                        return
                    deck._cancel_terminal_stream(stream_id)
                    self._send(204)
                else:
                    self._send(404)

            def do_DELETE(self):
                path = self._route_path(urlsplit(self.path).path)
                if path != "/api/v1/browser-sessions/current":
                    self._send(404)
                    return
                caller = self._api_caller(write=True)
                if not caller or not caller.startswith("browser:"):
                    self._api_error(401, "unauthorized", "a valid browser session is required")
                    return
                session = self._browser_session()
                deck._revoke_browser_session(session)
                self._send_json(
                    200,
                    {"api_version": "v1", "revoked": True},
                    {"Set-Cookie": self._session_cookie("", clear=True)},
                )

        return Handler


_PAGE = """<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Herdeck simulator</title>
<link rel="stylesheet" href="__BASE_PATH__/assets/xterm.css">
<script src="__BASE_PATH__/assets/xterm.js"></script>
<script src="__BASE_PATH__/assets/addon-fit.js"></script>
<style>
 body{background:#0b0b0d;margin:0;font-family:-apple-system,sans-serif;
   display:flex;align-items:center;justify-content:center;min-height:100vh}
 /* cell size lives in --cell so JS can set the COLUMN COUNT from /state.cols
    (repeat() does not accept var() for its count) — the layout follows the
    configured [deck] grid instead of hardcoding the 5-wide D200 */
 /* --cell derives from the COLUMN COUNT with the padding and gaps included in
    the 96vw budget (2*pad + cols*cell + (cols-1)*gap <= 96vw), so any
    configured grid fits a narrow viewport instead of overflowing sideways. */
 #deck{--cols:5;--gap:10px;--pad:18px;
   --cell:min(calc((96vw - 2*var(--pad) - (var(--cols) - 1)*var(--gap))/var(--cols)),150px);
   background:#2a2a2e;padding:var(--pad);border-radius:18px;
   display:grid;grid-template-columns:repeat(5,var(--cell));gap:var(--gap)}
 .cell{width:var(--cell);height:var(--cell);border-radius:8px;background:#111;cursor:pointer;
   overflow:hidden;border:none;padding:0;
   touch-action:manipulation;-webkit-tap-highlight-color:transparent;
   -webkit-user-select:none;user-select:none;-webkit-touch-callout:none}
 .cell:active{transform:scale(.95);filter:brightness(1.3)} /* instant, local press feedback */
 .cell.active{outline:3px solid #5af}
 .cell img{width:100%;height:100%;display:block}
 #panel{grid-column:4 / 6;width:calc(var(--cell)*2 + var(--gap));height:var(--cell);border-radius:8px;
   overflow:hidden;cursor:pointer;background:#111;
   touch-action:manipulation;-webkit-tap-highlight-color:transparent}
 #panel:active{filter:brightness(1.3)}
 #panel img{width:100%;height:100%;display:block}
 #deck.stale{filter:grayscale(1) opacity(.5);transition:filter .2s}
 #note{position:fixed;left:50%;top:10px;transform:translateX(-50%);max-width:90vw;
   background:#3a1d1d;color:#f0a0a0;padding:6px 12px;border-radius:8px;
   font:13px system-ui;display:none;z-index:9}
 [hidden]{display:none!important}
 #tover{position:fixed;inset:0;z-index:20;background:rgba(4,6,10,.94);
   display:flex;align-items:center;justify-content:center;padding:3vh 3vw;color:#e7ecf3}
 #tshell{width:min(1200px,94vw);height:min(860px,92vh);min-height:280px;
   background:#0b0b0d;border:1px solid #343944;border-radius:12px;overflow:hidden;
   box-shadow:0 18px 60px rgba(0,0,0,.5);display:flex;flex-direction:column}
 #tbar{height:52px;flex:0 0 52px;display:flex;align-items:center;gap:12px;
   padding:0 8px 0 14px;background:#202126;border-bottom:1px solid #343944;
   font:13px/1 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
 #tlive{display:flex;align-items:center;gap:7px;color:#8fc4ff;font-size:11px;
   font-weight:700;letter-spacing:.12em;white-space:nowrap}
 #tlive::before{content:"";width:7px;height:7px;border-radius:50%;background:#5af;
   box-shadow:0 0 0 3px rgba(85,170,255,.12)}
 #tlive.state-ended{color:#9aa7b8}
 #tlive.state-ended::before{background:#566171;box-shadow:none}
 #ttitle{min-width:0;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
   color:#e7ecf3;font-weight:600}
 #treadonly{color:#9aa7b8;font-size:10px;font-weight:700;letter-spacing:.13em;
   white-space:nowrap}
 #tclose{width:44px;height:44px;flex:0 0 44px;border:1px solid transparent;
   border-radius:8px;background:transparent;color:#c9d1dc;cursor:pointer;
   font:26px/1 system-ui;display:grid;place-items:center}
 #tclose:hover{background:#2a2a2e;color:#fff}
 #tclose:focus-visible{outline:3px solid #5af;outline-offset:-3px}
 #tmsg{min-height:28px;box-sizing:border-box;padding:7px 14px 5px;background:#111318;
   color:#9aa7b8;font:12px/1.3 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
 #tterm{flex:1;min-height:0;padding:8px 10px 10px;background:#0b0b0d}
 #tterm .xterm{height:100%}
 @media (max-width:560px){
   #tover{padding:0}
   #tshell{width:100vw;height:100dvh;min-height:100vh;border:0;border-radius:0}
   #tbar{gap:8px;padding-left:10px}
   #treadonly{display:none}
   #tterm{padding:6px}
 }
 @media (prefers-reduced-motion:reduce){
   .cell:active{transform:none}
 }
 /* phone portrait: width is the constraint, so shrink the deck */
 @media (max-width:560px){
   #deck{--gap:6px;--pad:10px;
     --cell:min(calc((96vw - 2*var(--pad) - (var(--cols) - 1)*var(--gap))/var(--cols)),110px)}
 }
 /* phone landscape: HEIGHT is the constraint (3 rows), so size cells by viewport
    height — but also keep the 17vw width cap so a short AND narrow viewport
    (e.g. 320x400, where this rule overrides the portrait one) can't overflow
    sideways. The deck stays within both the short (e.g. 667x375) viewport's
    height and a narrow viewport's width. */
 @media (max-height:430px){
   #deck{--gap:6px;--pad:10px;
     --cell:min(calc((96vw - 2*var(--pad) - (var(--cols) - 1)*var(--gap))/var(--cols)),22vh,110px)}
 }
</style>
<div id=deck></div>
<div id="tover" hidden role="dialog" aria-modal="true" aria-labelledby="ttitle" aria-describedby="tmsg">
  <section id="tshell">
    <header id="tbar">
      <span id="tlive"></span>
      <span id="ttitle"></span>
      <span id="treadonly"></span>
      <button id="tclose" type="button">×</button>
    </header>
    <div id="tmsg" aria-live="polite"></div>
    <div id="tterm" tabindex="0"></div>
  </section>
</div>
<script>
const deck=document.getElementById('deck');
const note=document.createElement('div');note.id='note';document.body.appendChild(note);
const basePath=__BASE_PATH_JSON__;
let cells=[]; const btns=[]; let slotCount=0;
function auth(path){
  return basePath+path;
}
// Stale-state indication: a control surface silently showing dead state is
// actively misleading — a 'blocked' tile may have been resolved minutes ago.
let fails=0, lastOk=Date.now();
const L=__L_JSON__;
function setStale(msg){deck.classList.add('stale');note.textContent=msg;note.style.display='block';}
function clearStale(){fails=0;lastOk=Date.now();deck.classList.remove('stale');note.style.display='none';}
// one press path for clicks and keys: post the press, outline the pressed cell.
async function press(i){
  let r;
  try{
    r=await fetch(basePath+'/press/'+i,{method:'POST'});
  }catch(e){
    setStale(L.pressFailed);
    return;
  }
  if(r.status===403){ setStale(L.tokenExpired); return; }
  if(!r.ok) return;
  btns.forEach(b=>b.classList.remove('active'));   // clear any stale outline first
  if(btns[i]){
    // transient "press registered" flash — a persistent outline ended up
    // highlighting a completely different tile after the view changed
    btns[i].classList.add('active');
    const b=btns[i];
    setTimeout(()=>b.classList.remove('active'),350);
  }
  pollNow();  // the press already re-rendered server-side; don't wait 300ms
}

// --- live terminal preview: long-press / right-click / Shift+Enter --------
const tover=document.getElementById('tover');
const ttitle=document.getElementById('ttitle');
const tlive=document.getElementById('tlive');
const treadonly=document.getElementById('treadonly');
const tclose=document.getElementById('tclose');
const tmsg=document.getElementById('tmsg');
const tterm=document.getElementById('tterm');
tlive.textContent=L.termConnectingBadge;
treadonly.textContent=L.termReadOnly;
tclose.title=L.termClose;
tclose.setAttribute('aria-label',L.termClose);
tterm.setAttribute('aria-label',L.termTitle);
let preview=null,previewGeneration=0,lastPreviewFocus=null,tresizeT=null;

function tshow(message){tmsg.textContent=message||'';}
function newStreamId(){
  if(globalThis.crypto&&typeof crypto.randomUUID==='function')return crypto.randomUUID();
  return 'stream-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2,14);
}
function stopRemote(current){
  if(current.stopSent)return;
  current.stopSent=true;
  void fetch(basePath+'/term-stop/'+encodeURIComponent(current.streamId),{
    method:'POST',keepalive:true
  }).catch(()=>{});
}
function disposePreview(current,notifyRemote){
  if(!current)return;
  if(current.source){
    current.source.onopen=current.source.onmessage=current.source.onerror=null;
    current.source.close();current.source=null;
  }
  if(notifyRemote)stopRemote(current);
  if(current.terminal){current.terminal.dispose();current.terminal=null;}
  tterm.replaceChildren();
}
function finishPreview(current,reason,remoteEnded=false){
  if(preview!==current)return;
  current.ended=true;
  tlive.textContent=L.termEndedBadge;tlive.classList.add('state-ended');
  if(current.source){current.source.close();current.source=null;}
  if(remoteEnded)current.stopSent=true;else stopRemote(current);
  tshow(reason||L.termEnded);
}
function closePreview(){
  clearTimeout(tresizeT);
  const current=preview;
  preview=null;previewGeneration++;
  disposePreview(current,true);
  tover.hidden=true;
  deck.inert=false;deck.removeAttribute('aria-hidden');
  document.body.style.overflow='';
  const restore=lastPreviewFocus;lastPreviewFocus=null;
  if(restore&&restore.isConnected)restore.focus();
}
function frameBytes(encoded){
  const binary=atob(encoded);const bytes=new Uint8Array(binary.length);
  for(let i=0;i<binary.length;i++)bytes[i]=binary.charCodeAt(i);
  return bytes;
}
function openPreview(index,tileVersion,opener,preserveFocus=false){
  const previous=preview;
  preview=null;
  disposePreview(previous,true);
  if(!preserveFocus)lastPreviewFocus=opener||document.activeElement;

  const current={
    index,tileVersion,generation:++previewGeneration,streamId:newStreamId(),source:null,
    terminal:null,fit:null,ended:false,stopSent:false,opener
  };
  preview=current;
  tover.hidden=false;
  deck.inert=true;deck.setAttribute('aria-hidden','true');
  document.body.style.overflow='hidden';
  tlive.textContent=L.termConnectingBadge;tlive.classList.remove('state-ended');
  ttitle.textContent=L.termTitle;tshow(L.termConnecting);

  current.terminal=new Terminal({
    disableStdin:true,screenReaderMode:true,cursorBlink:false,scrollback:1000,
    fontSize:13,lineHeight:1.15,
    fontFamily:'ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,"Liberation Mono",monospace',
    theme:{
      background:'#0b0b0d',foreground:'#e7ecf3',cursor:'#9aa7b8',
      selectionBackground:'#334c66',black:'#0b0b0d',brightBlack:'#566171',
      blue:'#5af',brightBlue:'#8fc4ff',red:'#f0a0a0'
    }
  });
  current.fit=new FitAddon.FitAddon();
  current.terminal.loadAddon(current.fit);
  current.terminal.open(tterm);
  tclose.focus({preventScroll:true});

  requestAnimationFrame(()=>{
    if(preview!==current)return;
    if(!Number.isInteger(current.tileVersion)){
      finishPreview(current,L.termEnded);return;
    }
    current.fit.fit();
    const cols=Math.max(20,Math.min(240,current.terminal.cols||80));
    const rows=Math.max(5,Math.min(100,current.terminal.rows||24));
    if(current.terminal.cols!==cols||current.terminal.rows!==rows){
      current.terminal.resize(cols,rows);
    }
    const path='/term/'+index+'?stream='+encodeURIComponent(current.streamId)+
      '&v='+current.tileVersion+'&cols='+cols+'&rows='+rows;
    const source=new EventSource(auth(path));current.source=source;
    source.onopen=()=>{
      if(preview!==current){source.close();stopRemote(current);return;}
      tlive.textContent=L.termLive;
    };
    source.onmessage=event=>{
      if(preview!==current)return;
      let message;
      try{message=JSON.parse(event.data);}catch(error){
        finishPreview(current,L.termEnded);return;
      }
      if(message.kind==='meta'){
        ttitle.textContent=message.label||L.termTitle;return;
      }
      if(message.kind==='frame'){
        try{
          const frameCols=Math.max(20,Math.min(240,Number(message.cols)||cols));
          const frameRows=Math.max(5,Math.min(100,Number(message.rows)||rows));
          if(current.terminal.cols!==frameCols||current.terminal.rows!==frameRows){
            current.terminal.resize(frameCols,frameRows);
          }
          if(message.full)current.terminal.reset();
          current.terminal.write(frameBytes(message.data));
          tshow('');
        }catch(error){finishPreview(current,L.termEnded);}
        return;
      }
      if(message.kind==='closed'){
        finishPreview(current,message.reason||L.termEnded,true);
      }
    };
    source.onerror=()=>{
      if(preview!==current)return;
      finishPreview(current,L.termDisconnected);
    };
  });
}
tclose.addEventListener('click',closePreview);
tover.addEventListener('click',event=>{if(event.target===tover)closePreview();});
window.addEventListener('resize',()=>{
  const current=preview;
  if(!current||current.ended)return;
  clearTimeout(tresizeT);
  tresizeT=setTimeout(()=>{
    if(preview!==current||current.ended)return;
    openPreview(current.index,current.tileVersion,current.opener,true);
  },400);
});

function addCell(i){
  const b=document.createElement('button');b.className='cell';
  b.title=L.termHint;
  let longTimer=null,startX=0,startY=0,longVersion;
  let openedByLongPress=false,suppressNextClick=false;
  const cancelLongPress=()=>{clearTimeout(longTimer);longTimer=null;};
  b.addEventListener('pointerdown',e=>{
    if(e.button!==0||!tover.hidden)return;
    cancelLongPress();openedByLongPress=false;suppressNextClick=false;
    startX=e.clientX;startY=e.clientY;longVersion=tv[i];
    longTimer=setTimeout(()=>{
      longTimer=null;openedByLongPress=true;suppressNextClick=true;
      openPreview(i,longVersion,b);
    },500);
  });
  b.addEventListener('pointermove',e=>{
    if(longTimer&&Math.hypot(e.clientX-startX,e.clientY-startY)>10)cancelLongPress();
  });
  b.addEventListener('pointerup',cancelLongPress);
  b.addEventListener('pointercancel',()=>{
    cancelLongPress();suppressNextClick=false;
  });
  b.addEventListener('lostpointercapture',cancelLongPress);
  b.addEventListener('contextmenu',e=>{
    e.preventDefault();cancelLongPress();
    if(openedByLongPress&&!tover.hidden)return;
    openedByLongPress=false;
    openPreview(i,tv[i],b);
  });
  b.addEventListener('keydown',e=>{
    if(e.shiftKey&&e.key==='Enter'){
      e.preventDefault();e.stopPropagation();openPreview(i,tv[i],b);
    }
  });
  b.onclick=e=>{
    if(suppressNextClick&&e.detail!==0){
      suppressNextClick=false;e.preventDefault();return;
    }
    if(e.detail===0)suppressNextClick=false;
    press(i);
  };
  const img=document.createElement('img');b.appendChild(img);
  deck.appendChild(b);cells.push(img);btns.push(b);
}
const panel=document.createElement('div');panel.id='panel';
panel.onclick=()=>press(slotCount);
const pimg=document.createElement('img');panel.appendChild(pimg);
let curCols=5;
function applyGrid(cols){
  if(!cols||cols===curCols) return;
  curCols=cols;
  deck.style.setProperty('--cols',cols);  // cell width shrinks with more columns
  deck.style.gridTemplateColumns='repeat('+cols+', var(--cell))';
  panel.style.gridColumn=(cols-1)+' / '+(cols+1);
}
function ensureCells(count){
  if(count===slotCount) return;
  while(btns.length<count) addCell(btns.length);
  while(btns.length>count){
    const i=btns.length-1;
    btns.pop().remove();cells.pop();
    delete tv[i];delete pendingTv[i];delete tileRetry[i];
  }
  slotCount=count;
  deck.appendChild(panel);
}
// keyboard: 1..9 -> tiles 0..8, 0 -> tile 9; ignore when a modifier is held.
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'&&!tover.hidden){e.preventDefault();closePreview();return;}
  if(!tover.hidden){
    if(e.key==='Tab'){
      e.preventDefault();
      if(document.activeElement===tclose)tterm.focus();else tclose.focus();
    }
    return;
  }
  if(e.repeat) return;                                   // don't spam presses on key-hold
  if(e.metaKey||e.ctrlKey||e.altKey||e.shiftKey) return;
  if(e.key>='1'&&e.key<='9') press(e.key.charCodeAt(0)-49);
  else if(e.key==='0') press(9);
});
let lastV=-1; const tv={},pendingTv={},tileRetry={}; let pv=-1;
let pollTimer=null, inFlight=false, pollDelay=100;
async function poll(){
  if(inFlight) return;             // the in-flight poll reschedules; never overlap
  if(document.hidden){             // parked while hidden; visibilitychange resumes
    pollTimer=setTimeout(poll,1000);
    return;
  }
  inFlight=true;
  pollDelay=100;                   // the server paces successes via the long poll
  try{
    // long poll: the server holds the request until the version advances, so
    // idle traffic is ~2 req/min and changes arrive at network latency
    const r=await fetch(auth(lastV>=0?'/state?since='+lastV:'/state'));
    if(r.status===403){
      setStale(L.tokenExpired);
      pollDelay=2000;              // a stale bookmark must not hammer at 10/s
      return;                      // rescheduled in finally; keeps checking
    }
    const s=await r.json();
    clearStale();
    ensureCells(s.slots);
    applyGrid(s.cols);
    if(s.version!==lastV){          // cheap gate: nothing changed at all
      lastV=s.version;
      const t=s.tiles||{};
      for(let i=0;i<slotCount;i++){ // refetch only tiles whose version advanced
        const v=t[i];
        if(v===undefined){          // tile gone -> clear the cell
          delete pendingTv[i];delete tileRetry[i];
          if(tv[i]!==undefined){ delete tv[i]; cells[i].removeAttribute('src'); }
        } else if(v!==tv[i]&&v!==pendingTv[i]){
          pendingTv[i]=v;
          const candidate=new Image();candidate.alt='';
          candidate.onload=()=>{
            if(pendingTv[i]!==v||i>=cells.length)return;
            cells[i].replaceWith(candidate);cells[i]=candidate;
            tv[i]=v;delete pendingTv[i];delete tileRetry[i];
          };
          candidate.onerror=()=>{
            if(pendingTv[i]!==v)return;
            delete pendingTv[i];
            const attempt=(tileRetry[i]||0)+1;tileRetry[i]=attempt;
            const delay=Math.min(5000,250*2**Math.min(attempt,5));
            setTimeout(()=>{
              if(tileRetry[i]!==attempt||tv[i]===v||pendingTv[i]!==undefined)return;
              lastV=-1;pollNow();
            },delay);
          };
          candidate.src=auth('/tile/'+i+'?v='+v);
        }
      }
      if(s.has_panel && s.panel!==pv){ pv=s.panel; pimg.src=auth('/panel?v='+pv); }
    }
  }catch(e){
    fails++;
    if(fails>=2) setStale(L.disconnected.replace('{s}',Math.max(1,Math.round((Date.now()-lastOk)/1000))));
    pollDelay=1000;                // errors are not server-paced: back off
  }finally{
    inFlight=false;
    clearTimeout(pollTimer);
    pollTimer=setTimeout(poll,pollDelay);
  }
}
function pollNow(){ clearTimeout(pollTimer); void poll(); }
// a woken phone should refresh immediately, not after the next timer tick
document.addEventListener('visibilitychange',()=>{ if(!document.hidden) pollNow(); });
poll();
</script>
"""
