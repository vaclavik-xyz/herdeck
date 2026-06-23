from __future__ import annotations

import hmac
import io
import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from ..orchestrator import Orchestrator
from .source import StateSource

# NOTE: herdeck.icons (and its Pillow dependency) is imported lazily inside the
# render path, not at module import time, so `import herdeck.deckapp` — and the
# Pillow-free surface (MockSource, demo agents, config) — works on a base install
# that has not pulled the rendering stack. Pillow is required to actually render;
# declaring it as a packaged dependency of the desktop sidecar (in pyproject)
# belongs to the packaging slice and is outside this slice's owned paths.

# Body returned for any unauthenticated request: plain text (never
# octet-stream, which browsers offer to download) and free of any token.
_FORBIDDEN = (
    b"herdeck deckapp: missing or invalid access token.\n"
    b"The token is handed to the desktop shell on startup; it is never in logs.\n"
)


class DeckApp:
    """Token-authed loopback HTTP sidecar for the herdeck desktop app.

    Composes the core ``Orchestrator`` (render) with a ``StateSource`` (mock or,
    later, live) and serves the deck over loopback HTTP/JSON + PNG tiles. Modeled
    on ``driver.web.WebDeck``: same per-tile version diffing, same constant-time
    token check, bind to 127.0.0.1 only.
    """

    def __init__(
        self,
        source: StateSource,
        *,
        slots: int | None = None,
        host: str = "127.0.0.1",
        port: int = 8800,
        icon_provider=None,
        token: str | None = None,
        serve: bool = True,
        clock=None,
    ):
        self._source = source
        config = source.config
        cols, rows = config.grid
        # Match the established deck geometry: the two status-window cells are not
        # addressable tiles, so slots = grid - 2 (e.g. 13 for a 5x3 grid).
        self._slots = slots if slots is not None else cols * rows - 2
        # A fixed clock keeps the mock fully deterministic (stable elapsed text,
        # so repeated /state polls do not churn tile versions).
        self._orch = Orchestrator(config, slots=self._slots, clock=clock or (lambda: 0.0))
        self._icons = icon_provider if icon_provider is not None else _default_icons()
        self._token = token or secrets.token_urlsafe(24)

        self._lock = threading.Lock()
        self._tiles: dict[int, bytes] = {}
        self._tile_ver: dict[int, int] = {}
        self._panel: bytes | None = None
        self._panel_ver = 0
        self._version = 0

        # Hand the source the render orchestrator so a live press can drive
        # Orchestrator.on_press against the very deck being rendered (no-op for mock).
        self._source.attach(self._orch)

        self.refresh()  # render the initial deck so /state is non-empty at once

        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        if serve:
            self._server = ThreadingHTTPServer((host, port), self._handler_class())
            self.host, self.port = self._server.server_address[0], self._server.server_address[1]
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()
        else:
            self.host, self.port = host, port

    @property
    def token(self) -> str:
        return self._token

    @property
    def source_name(self) -> str:
        return self._source.source_name

    def _bump(self) -> int:
        """Assign the next monotonic version. Call while holding self._lock."""
        self._version += 1
        return self._version

    # --- render pipeline (reuses Orchestrator + icons) ---
    def refresh(self) -> None:
        """Pull state from the source, render via the orchestrator, and diff the
        result into versioned tile/panel PNGs (only changed cells bump)."""
        with self._lock:
            self._refresh_locked()

    def _refresh_locked(self) -> None:
        # The orchestrator and source are not thread-safe; ThreadingHTTPServer
        # serves each request on its own thread, so all access to them (render,
        # press, state reads) is serialized under self._lock.
        from ..icons import compose_panel

        self._source.apply_to(self._orch)
        rs = self._orch.render()
        new: dict[int, bytes] = {}
        for tile in rs.tiles:
            if tile.index >= self._slots:
                continue
            new[tile.index] = self._icons.render_tile_bytes(tile)
        buf = io.BytesIO()
        compose_panel(rs.panel).convert("RGB").save(buf, "PNG")
        panel_png = buf.getvalue()
        for i, png in new.items():
            if self._tiles.get(i) != png:  # bump only changed/new tiles
                self._tile_ver[i] = self._bump()
        removed = set(self._tile_ver) - set(new)
        for i in removed:
            del self._tile_ver[i]
        if removed:  # a pure removal must still trip the client's gate
            self._bump()
        self._tiles = new
        if self._panel != panel_png:
            self._panel = panel_png
            self._panel_ver = self._bump()

    def press(self, index: int) -> None:
        """Inject a press (called from the HTTP thread). Out-of-range/crafted
        indices are ignored; valid ones update mock state and re-render."""
        if 0 <= index < self._slots + 2:
            with self._lock:
                self._source.press(index)
                self._refresh_locked()

    def close(self) -> None:
        try:
            self._source.close()  # stop the live connector/loop (no-op for mock)
        except Exception:
            pass
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

    # --- state snapshots ---
    def _state(self) -> dict:
        with self._lock:
            return {
                "version": self._version,
                "slots": self._slots,
                "has_panel": self._panel is not None,
                "panel": self._panel_ver,
                "tiles": dict(self._tile_ver),
                "summary": self._source.summary(),
                "source": self._source.source_name,
                "connected": self._source.connected,
            }

    def _health(self) -> dict:
        return {
            "ok": True,
            "source": self._source.source_name,
            "connected": self._source.connected,
            "server_id": self._source.server_id,
        }

    def _tile_png(self, index: int) -> bytes | None:
        with self._lock:
            return self._tiles.get(index)

    def _panel_png(self) -> bytes | None:
        with self._lock:
            return self._panel

    # --- HTTP ---
    def _valid_token(self, token: str) -> bool:
        return hmac.compare_digest(token.encode(), self._token.encode())

    def _handler_class(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # never log requests (could carry the token)
                pass

            def _send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _query_token(self, url):
                return parse_qs(url.query).get("token", [""])[0]

            def _require_query_token(self, url):
                if app._valid_token(self._query_token(url)):
                    return True
                self._send(403, _FORBIDDEN)
                return False

            def _require_header_token(self):
                if app._valid_token(self.headers.get("X-Herdeck-Token", "")):
                    return True
                self._send(403, _FORBIDDEN)
                return False

            def do_GET(self):
                url = urlsplit(self.path)
                path = url.path
                if path == "/state":
                    if not self._require_query_token(url):
                        return
                    self._send(200, json.dumps(app._state()).encode(), "application/json")
                elif path == "/health":
                    if not self._require_query_token(url):
                        return
                    self._send(200, json.dumps(app._health()).encode(), "application/json")
                elif path == "/panel":
                    if not self._require_query_token(url):
                        return
                    png = app._panel_png()
                    self._send(200, png, "image/png") if png else self._send(404)
                elif path.startswith("/tile/"):
                    if not self._require_query_token(url):
                        return
                    try:
                        png = app._tile_png(int(path.rsplit("/", 1)[1]))
                    except ValueError:
                        png = None
                    self._send(200, png, "image/png") if png else self._send(404)
                else:
                    self._send(404)

            def do_POST(self):
                path = urlsplit(self.path).path
                if path.startswith("/press/"):
                    if not self._require_header_token():
                        return
                    try:
                        app.press(int(path.rsplit("/", 1)[1]))
                        self._send(204)
                    except ValueError:
                        self._send(400)
                else:
                    self._send(404)

        return Handler


def _default_icons():
    """The shared IconProvider, configured for the mock: no network fetch, so the
    deck renders deterministically and offline (bundled SVG assets, else a letter
    glyph). Reuses herdeck.icons — no rendering logic is reimplemented here."""
    import os
    import tempfile

    from ..icons import DEFAULT_AGENT_SLUGS, IconProvider

    cache = os.path.join(tempfile.gettempdir(), "herdeck-deckapp-icons")
    return IconProvider(
        cache_dir=cache,
        slug_map=DEFAULT_AGENT_SLUGS,
        fetch=lambda slug: None,  # mock stays offline + deterministic
    )


def create_mock_app(
    *, host: str = "127.0.0.1", port: int = 0, icon_provider=None, serve: bool = True
) -> DeckApp:
    """Build a serving DeckApp backed by the deterministic MockSource."""
    from .mock import MockSource

    return DeckApp(
        MockSource(), host=host, port=port, icon_provider=icon_provider, serve=serve
    )


def select_live():
    """Decide live vs mock from the on-disk config + bridge-token presence.

    Returns ``(config, server)`` to drive a LiveSource, or ``None`` to fall back to
    the deterministic mock. Mock wins when ``HERDECK_MOCK`` is set, when no config
    file is discovered, or when the resolved server has no bridge token — the token
    lives in env/keychain (``ServerConfig.token``), never in the config file, so a
    missing one means we cannot connect and should show the mock + hint.
    """
    if os.environ.get("HERDECK_MOCK"):
        return None
    from ..bootstrap import _discover_config_path, _discover_local_config_path
    from ..config import ConfigError
    from ..settings import load_settings, resolve_profile

    path = _discover_config_path()
    if not path:
        return None
    try:
        snapshot = load_settings(path, _discover_local_config_path(path))
        config = resolve_profile(snapshot).config
    except (ConfigError, OSError):
        # A config that needs a token whose env var is unset raises ConfigError;
        # treat any unreadable/invalid config as "no live target" -> mock.
        return None
    if not config.servers:
        return None
    server = config.servers[0]
    if not server.token:
        return None
    return (config, server)


def create_live_app(
    config,
    server,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    icon_provider=None,
    serve: bool = True,
    connector_factory=None,
) -> DeckApp:
    """Build a serving DeckApp backed by a LiveSource (real bridge via Connector).

    Uses a real wall clock so elapsed-time tile text advances (the mock pins it for
    determinism). ``connector_factory`` is injectable for tests (no real bridge).
    """
    import time

    from .live import build_live_source

    kwargs = {} if connector_factory is None else {"connector_factory": connector_factory}
    source = build_live_source(config, server, **kwargs)
    return DeckApp(
        source,
        host=host,
        port=port,
        icon_provider=icon_provider,
        serve=serve,
        clock=time.monotonic,
    )


def create_app(
    *, host: str = "127.0.0.1", port: int = 0, icon_provider=None, serve: bool = True
) -> DeckApp:
    """Build the sidecar with the right source: live when a server + token are
    configured, otherwise the deterministic mock."""
    selected = select_live()
    if selected is None:
        return create_mock_app(host=host, port=port, icon_provider=icon_provider, serve=serve)
    config, server = selected
    return create_live_app(
        config, server, host=host, port=port, icon_provider=icon_provider, serve=serve
    )
