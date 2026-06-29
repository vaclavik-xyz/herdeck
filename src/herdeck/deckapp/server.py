from __future__ import annotations

import hmac
import io
import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from ..config import ConfigError
from ..orchestrator import Orchestrator
from .source import StateSource

# NOTE: herdeck.icons (and its Pillow dependency) is imported lazily inside the
# render path, not at module import time, so `import herdeck.deckapp` — and the
# Pillow-free surface (MockSource, demo agents, config) — works on a base install
# that has not pulled the rendering stack. Pillow is required to actually render;
# declaring it as a packaged dependency of the desktop sidecar (in pyproject)
# belongs to the packaging slice and is outside this slice's owned paths.

# Sentinel returned by _json_body() when the request body is not a valid JSON
# object (parse error or wrong type). Using a distinct singleton means callers
# can safely distinguish it from None, False, or any other falsy value.
_BAD_BODY = object()

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
        config_service=None,
        reloader=None,
    ):
        self._source = source
        config = source.config
        cols, rows = config.grid
        # Match the established deck geometry: the two status-window cells are not
        # addressable tiles, so slots = grid - 2 (e.g. 13 for a 5x3 grid).
        self._slots = slots if slots is not None else cols * rows - 2
        # Store the clock so swap_source can rebuild the orchestrator with the same clock.
        self._clock = clock or (lambda: 0.0)
        # A fixed clock keeps the mock fully deterministic (stable elapsed text,
        # so repeated /state polls do not churn tile versions).
        self._orch = Orchestrator(config, slots=self._slots, clock=self._clock)
        self._icons = icon_provider if icon_provider is not None else _default_icons()
        self._token = token or secrets.token_urlsafe(24)
        self._config_service = config_service
        self._reloader = reloader

        self._lock = threading.Lock()
        self._tiles: dict[int, bytes] = {}
        self._tile_ver: dict[int, int] = {}
        self._tile_sections: dict[int, str] = {}
        self._panel: bytes | None = None
        self._panel_ver = 0
        self._version = 0

        # Hand the source the render orchestrator (plus this lock and the lock-free
        # render) so a live source can drive on_press/read-results against the very
        # deck being rendered and apply each bridge update atomically under this lock
        # (all no-ops for the mock).
        self._source.attach(self._orch, lock=self._lock, refresh_locked=self._refresh_locked)

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
        self._tile_sections = {
            tile.index: tile.section
            for tile in rs.tiles
            if tile.index < self._slots and tile.section
        }
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
        watcher = getattr(self, "_watcher", None)
        if watcher is not None:
            try:
                watcher.close()
            except Exception:
                pass
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

    def swap_source(self, new_source) -> None:
        """Replace the state source and rebuild the orchestrator from its config
        (grid/slots may have changed), then re-render. The single lock serializes
        this against in-flight HTTP reads/presses."""
        with self._lock:
            old = self._source
            self._source = new_source
            cols, rows = new_source.config.grid
            self._slots = cols * rows - 2
            self._orch = Orchestrator(new_source.config, slots=self._slots, clock=self._clock)
            new_source.attach(self._orch, lock=self._lock, refresh_locked=self._refresh_locked)
            self._refresh_locked()
        try:
            old.close()
        except Exception:
            pass

    def reload(self) -> None:
        """Apply an edited on-disk config in place. The real sidecar injects a
        reloader (via create_app) that re-selects the source and swap_source()s
        it; tests may inject a stub. No reloader -> safe no-op."""
        if self._reloader is not None:
            self._reloader()

    # --- state snapshots ---
    def _state(self) -> dict:
        with self._lock:
            return {
                "version": self._version,
                "slots": self._slots,
                "has_panel": self._panel is not None,
                "panel": self._panel_ver,
                "tiles": dict(self._tile_ver),
                "tile_sections": dict(self._tile_sections),
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
                elif path == "/config":
                    if not self._require_query_token(url):
                        return
                    if app._config_service is None:
                        self._send(404)
                        return
                    self._send(200, json.dumps(app._config_service.read()).encode(),
                               "application/json")
                else:
                    self._send(404)

            def _json_body(self):
                """Parse the request body as a JSON object (dict).

                Returns the parsed dict on success; sends 400 and returns
                ``_BAD_BODY`` if the body is not valid JSON or is not a
                JSON object (i.e. not a dict). Callers must check
                ``if body is _BAD_BODY: return``.
                An empty/absent body is treated as ``{}`` (empty object).
                """
                try:
                    length = int(self.headers.get("Content-Length", 0))
                except (TypeError, ValueError):
                    self._send(400)
                    return _BAD_BODY
                raw = self.rfile.read(length) if length else b""
                try:
                    result = json.loads(raw or b"{}")
                except (json.JSONDecodeError, ValueError):
                    self._send(400)
                    return _BAD_BODY
                if not isinstance(result, dict):
                    self._send(400)
                    return _BAD_BODY
                return result

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
                elif path in ("/config/validate", "/config", "/profiles/active", "/secret"):
                    if not self._require_header_token():
                        return
                    if app._config_service is None:
                        self._send(404)
                        return
                    if path == "/config/validate":
                        body = self._json_body()
                        if body is _BAD_BODY:
                            return
                        errors = app._config_service.validate(body)
                        self._send(200, json.dumps({"errors": errors}).encode(), "application/json")
                    elif path == "/config":
                        body = self._json_body()
                        if body is _BAD_BODY:
                            return
                        errors = app._config_service.write(body)
                        if not errors:
                            app.reload()
                        self._send(200, json.dumps({"errors": errors}).encode(), "application/json")
                    elif path == "/profiles/active":
                        body = self._json_body()
                        if body is _BAD_BODY:
                            return
                        name = body.get("name")
                        if not isinstance(name, str) or not name.strip():
                            self._send(400)
                            return
                        try:
                            changed = app._config_service.set_active(name)
                        except ConfigError:
                            self._send(400)
                            return
                        self._send(200, json.dumps({"changed": changed}).encode(), "application/json")
                    elif path == "/secret":
                        b = self._json_body()
                        if b is _BAD_BODY:
                            return
                        token_env = b.get("token_env")
                        value = b.get("value")
                        if not token_env or not value:
                            self._send(400)
                            return
                        app._config_service.set_secret(token_env, value)
                        self._send(204)
                else:
                    self._send(404)

            def do_DELETE(self):
                path = urlsplit(self.path).path
                if path.startswith("/secret/"):
                    if not self._require_header_token():
                        return
                    if app._config_service is None:
                        self._send(404)
                        return
                    app._config_service.clear_secret(unquote(path.rsplit("/", 1)[1]))
                    self._send(204)
                else:
                    self._send(404)

        return Handler


def _default_icons():
    """The shared IconProvider, configured for the mock: no network fetch, so the
    deck renders deterministically and offline (bundled SVG assets, else a letter
    glyph). Reuses herdeck.icons — no rendering logic is reimplemented here.

    When running frozen (PyInstaller bundle) there is no cairosvg, so glyphs are
    served from pre-baked PNGs: pass BOTH the PNG rasterizer and the bundled
    assets dir, matching the Elgato frozen session."""
    import os
    import tempfile

    from ..frozen import baked_assets_dir, is_frozen, make_png_rasterizer
    from ..icons import DEFAULT_AGENT_SLUGS, IconProvider

    if is_frozen():
        cache = os.path.join(tempfile.gettempdir(), "herdeck-deckapp-icons-frozen")
        baked = baked_assets_dir()
        return IconProvider(
            cache_dir=cache,
            slug_map=DEFAULT_AGENT_SLUGS,
            fetch=lambda slug: None,  # offline-first when frozen
            rasterize=make_png_rasterizer(baked),
            assets_dir=baked,
        )
    cache = os.path.join(tempfile.gettempdir(), "herdeck-deckapp-icons")
    return IconProvider(
        cache_dir=cache,
        slug_map=DEFAULT_AGENT_SLUGS,
        fetch=lambda slug: None,  # mock stays offline + deterministic
    )


def create_mock_app(
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    icon_provider=None,
    serve: bool = True,
    config_service=None,
    reloader=None,
) -> DeckApp:
    """Build a serving DeckApp backed by the deterministic MockSource."""
    from .mock import MockSource

    return DeckApp(
        MockSource(),
        host=host,
        port=port,
        icon_provider=icon_provider,
        serve=serve,
        config_service=config_service,
        reloader=reloader,
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


def select_source_kind(*, mock_env, remote, choice, socket_path, socket_exists):
    """Pure source-selection precedence over already-gathered facts.

    Returns ("remote", config, server) | ("local", socket_path) | ("mock", reason).
    All IO (env, select_live result, persisted choice, socket existence) is passed
    in, so every branch is unit-testable without touching the filesystem."""
    if mock_env:
        return ("mock", "mock_env")
    # An explicit onboarding choice wins over a remote config on disk: a remote connect
    # CLEARS the marker, so a remote config always implies "no marker" and falls through to
    # the remote branch below. This makes a demo/local choice stick across restarts even
    # when a remote config.toml is present.
    if choice == "local":
        return ("local", socket_path) if socket_exists else ("mock", "local_unavailable")
    if choice == "demo":
        return ("mock", "demo")
    if remote is not None:
        config, server = remote
        return ("remote", config, server)
    return ("mock", "first_run")


def _resolve_source_kind():
    """Gather the facts and apply select_source_kind."""
    from ..bootstrap import resolve_socket_path
    from .onboarding import read_choice

    socket_path = resolve_socket_path(None)
    config_path = _default_config_paths()[0]
    return select_source_kind(
        mock_env=bool(os.environ.get("HERDECK_MOCK")),
        remote=select_live(),
        choice=read_choice(config_path),
        socket_path=socket_path,
        socket_exists=os.path.exists(socket_path),
    )


def create_live_app(
    config,
    server,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    icon_provider=None,
    serve: bool = True,
    connector_factory=None,
    config_service=None,
    reloader=None,
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
        config_service=config_service,
        reloader=reloader,
    )


def _default_config_paths():
    """Return ``(config_path, local_path)`` for the on-disk config files.

    Both are ``str`` paths (local may be ``None`` when absent). Factored out so
    both ``_default_config_service()`` and the ``ConfigWatcher`` in
    ``create_app`` watch the exact same files the editor reads and writes.
    """
    from ..bootstrap import _discover_config_path, _discover_local_config_path

    path = _discover_config_path() or os.path.expanduser("~/.config/herdeck/config.toml")
    return path, _discover_local_config_path(path)


def _default_config_service():
    from .config_service import ConfigService

    path, local = _default_config_paths()
    return ConfigService(path, local)


def _select_source():
    """Re-read the on-disk config and build the appropriate source (live or mock)."""
    selected = select_live()
    if selected is None:
        from .mock import MockSource

        return MockSource()
    config, server = selected
    from .live import build_live_source

    return build_live_source(config, server)


def create_app(
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    icon_provider=None,
    serve: bool = True,
    config_service=None,
    reloader=None,
) -> DeckApp:
    """Build the sidecar with the right source: live when a server + token are
    configured, otherwise the deterministic mock. Wires up a default ConfigService
    and a disk-re-select reloader so the GUI can edit + reload in place.

    Also starts a ``ConfigWatcher`` over the same paths the ConfigService reads so
    that an external edit to the config files triggers an in-app reload automatically.
    The watcher is stopped when ``DeckApp.close()`` is called.
    """
    from .watcher import ConfigWatcher

    cfg_path, local_path = _default_config_paths()
    svc = config_service if config_service is not None else _default_config_service()
    selected = select_live()
    if selected is None:
        app = create_mock_app(
            host=host, port=port, icon_provider=icon_provider, serve=serve, config_service=svc
        )
    else:
        config, server = selected
        app = create_live_app(
            config,
            server,
            host=host,
            port=port,
            icon_provider=icon_provider,
            serve=serve,
            config_service=svc,
        )
    if reloader is None:
        app._reloader = lambda: app.swap_source(_select_source())
    else:
        app._reloader = reloader

    # Watch the config files; fire the reloader when any changes on disk.
    # Filter out None (local path is absent when no local override exists).
    watch_paths = [p for p in (cfg_path, local_path) if p is not None]
    app._watcher = ConfigWatcher(watch_paths, app.reload, interval=1.0)
    app._watcher.start()
    return app
