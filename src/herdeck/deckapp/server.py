from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from ..config import ConfigError
from ..orchestrator import Orchestrator
from .sinks import RenderFrame
from .source import StateSource

log = logging.getLogger(__name__)

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

    FULL_REFRESH_TICKS = 25  # every Nth tick re-renders all tiles + panel (advances idle elapsed on the D200); other ticks send working-only frames

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
        tick_interval: float = 0.0,
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
        self._local_bridge = None  # LocalBridgeRunner when in local mode, else None
        self._suppress_reload = False  # set by the onboarding commit to mute the watcher
        self._setup_lock = threading.RLock()  # shared mutation lock (/setup/connect + config-write routes + reload); RLock because the config routes call reload() while holding it

        # CodexBar usage poller (a daemon thread; None when [usage] is off).
        # Renders read its latest snapshot; no render ever blocks on the CLI.
        self._usage_cfg = getattr(config, "usage", None)
        self._usage_poller = self._build_usage_poller(self._usage_cfg)

        self._lock = threading.Lock()
        self._panel_memo: tuple[tuple, bytes] | None = None  # (panel content key, png)
        self._tiles: dict[int, bytes] = {}
        self._tile_ver: dict[int, int] = {}
        self._tile_sections: dict[int, str] = {}
        self._panel: bytes | None = None
        self._panel_ver = 0
        self._version = 0
        self._sinks: list = []  # RenderSink fan-out targets (HTTP buffer is DeckApp's own)
        self._ticks = 0

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

        # Background ticker: advance the spinner phase + re-render every
        # tick_interval seconds so working tiles animate in the served /state.
        # Only when actually serving (mock/test path leaves it off -> deterministic).
        self._tick_interval = tick_interval
        self._ticker_stop = threading.Event()
        self._ticker_thread: threading.Thread | None = None
        if serve and tick_interval > 0:
            self._ticker_thread = threading.Thread(
                target=self._ticker_loop, name="herdeck-deckapp-tick", daemon=True
            )
            self._ticker_thread.start()

    @property
    def token(self) -> str:
        return self._token

    @property
    def source_name(self) -> str:
        return self._source.source_name

    @property
    def config(self):
        """The live config (from the source) — the runtime entry builds the D200 driver from config.hardware."""
        return self._source.config

    @property
    def slots(self) -> int:
        return self._slots

    def _bump(self) -> int:
        """Assign the next monotonic version. Call while holding self._lock."""
        self._version += 1
        return self._version

    @staticmethod
    def _build_usage_poller(usage_cfg):
        from ..usage import poller_from_config

        poller = poller_from_config(usage_cfg)
        if poller is not None:
            poller.start()
        return poller

    def _adopt_usage_config(self, config) -> bool:
        """Rebuild the poller when a config swap changed [usage] (providers,
        cadence or CLI path); unchanged config keeps the running thread.
        Returns True when the poller was rebuilt."""
        new_cfg = getattr(config, "usage", None)
        if new_cfg == self._usage_cfg:
            return False
        old = self._usage_poller
        self._usage_cfg = new_cfg
        self._usage_poller = self._build_usage_poller(new_cfg)
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        return True

    # --- render pipeline (reuses Orchestrator + icons) ---
    def refresh(self) -> None:
        """Pull state from the source, render via the orchestrator, and diff the
        result into versioned tile/panel PNGs (only changed cells bump)."""
        with self._lock:
            self._refresh_locked()

    def _render_locked(self, source, orch, slots):
        """Render `source` through `orch` → (tiles, panel_png, sections). This is the
        FALLIBLE part of a refresh (apply_to / orchestrator render / icon raster / panel
        compose); apart from the value-keyed panel memo it mutates no self state, so it
        can run on a throwaway orchestrator in `_prepare_swap` or on the live deck
        inside `_refresh_locked`."""
        import io

        from ..icons import PANEL_W_TWO_CELL, compose_panel

        # ALWAYS feed usage state (empty when off): the orchestrator may carry
        # usage lines from before a swap that disabled [usage] — only an
        # unconditional set clears them (roborev e0eeb95).
        poller = self._usage_poller
        orch.set_usage(poller.snapshot() if poller is not None else [])
        source.apply_to(orch)
        rs = orch.render()
        tiles = {t.index: self._icons.render_tile_bytes(t) for t in rs.tiles if t.index < slots}
        # Memoize the encoded panel by content: panel text changes every few
        # seconds at most, while refreshes run per tick — recomposing + PNG-encoding
        # an identical panel dominated the steady-state tick cost.
        panel_key = (rs.panel.title, tuple(rs.panel.lines), rs.panel.color)
        memo = self._panel_memo
        if memo is not None and memo[0] == panel_key:
            panel_png = memo[1]
        else:
            buf = io.BytesIO()
            # The desktop window shows the panel in a 2-cells-wide grid box, so
            # compose at the two-cell width — the native 458px would be squeezed.
            compose_panel(rs.panel, width=PANEL_W_TWO_CELL).convert("RGB").save(buf, "PNG")
            panel_png = buf.getvalue()
            self._panel_memo = (panel_key, panel_png)
        sections = {t.index: t.section for t in rs.tiles if t.index < slots and t.section}
        return rs, tiles, panel_png, sections

    def _apply_rendered_locked(self, tiles, panel_png, sections):
        """Assign pre-rendered tiles/panel/sections with version bumps — pure dict/int ops
        (no rendering), so it CANNOT raise. Callers hold self._lock. Byte-for-byte the tail
        of the original `_refresh_locked`."""
        for i, png in tiles.items():
            if self._tiles.get(i) != png:
                self._tile_ver[i] = self._bump()
        removed = set(self._tile_ver) - set(tiles)
        for i in removed:
            del self._tile_ver[i]
        if removed:
            self._bump()
        self._tiles = tiles
        self._tile_sections = sections
        if self._panel != panel_png:
            self._panel = panel_png
            self._panel_ver = self._bump()

    def _refresh_locked(self, *, working=None, full=True) -> None:
        rs, tiles, panel_png, sections = self._render_locked(self._source, self._orch, self._slots)
        self._apply_rendered_locked(tiles, panel_png, sections)
        self._fan_out_locked(rs, working, full)

    def _fan_out_locked(self, rs, working, full) -> None:
        """Deliver the rendered frame to every sink under self._lock. A sink that
        raises is isolated — the HTTP buffer (already updated above) and the other
        sinks must not be affected."""
        if not self._sinks:
            return
        frame = RenderFrame(render=rs, working=working, full=full)
        for sink in self._sinks:
            try:
                sink.deliver(frame)
            except Exception:
                log.warning("render sink %r failed to deliver a frame", sink, exc_info=True)

    def add_sink(self, sink) -> None:
        """Register a render sink and immediately paint it one full frame so it
        starts in sync with the current deck state (the live ticker keeps it
        animated thereafter)."""
        with self._lock:
            self._sinks.append(sink)
            self._refresh_locked(working=None, full=True)

    def _tick_once(self) -> None:
        """Advance the spinner phase and re-render, atomically w.r.t. presses
        and bridge updates (same lock). A tick renders only when something
        actually animates (a WORKING tile) or on the periodic full refresh —
        bridge updates and presses trigger their own refresh, so an idle deck
        does no per-tick render/encode/device work at all (matching the legacy
        App.handle_tick). Every FULL_REFRESH_TICKS-th tick is a full frame so
        idle elapsed text advances and every sink resyncs."""
        with self._lock:
            working = self._orch.tick()
            self._ticks += 1
            if self._ticks % self.FULL_REFRESH_TICKS == 0:
                self._refresh_locked(working=None, full=True)
            elif working:
                self._refresh_locked(working=working, full=False)

    def _ticker_loop(self) -> None:
        # Event.wait returns False on timeout (a tick is due) and True once close()
        # sets the event (clean stop) — so this never busy-waits and exits promptly.
        while not self._ticker_stop.wait(self._tick_interval):
            self._tick_once()

    def press(self, index: int) -> None:
        """Inject a press (called from the HTTP thread). Out-of-range/crafted
        indices are ignored; valid ones update mock state and re-render."""
        if 0 <= index < self._slots + 2:
            with self._lock:
                self._source.press(index)
                self._refresh_locked()

    def close(self) -> None:
        ticker = getattr(self, "_ticker_thread", None)
        if ticker is not None:
            self._ticker_stop.set()
            if ticker is not threading.current_thread():
                ticker.join(timeout=2)
            self._ticker_thread = None
        with self._lock:
            sinks = getattr(self, "_sinks", [])
            self._sinks = []
        for sink in sinks:
            try:
                sink.close()
            except Exception:
                pass
        watcher = getattr(self, "_watcher", None)
        if watcher is not None:
            try:
                watcher.close()
            except Exception:
                pass
        poller = getattr(self, "_usage_poller", None)
        if poller is not None:
            try:
                poller.close()
            except Exception:
                pass
            self._usage_poller = None
        bridge = getattr(self, "_local_bridge", None)
        if bridge is not None:
            try:
                bridge.close()
            except Exception:
                pass
            self._local_bridge = None
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

    def _set_local_bridge(self, runner) -> None:
        """Adopt `runner` as the embedded-bridge owner, closing any previous one.
        Pass None to drop the bridge (e.g. when switching to remote/mock)."""
        old = getattr(self, "_local_bridge", None)
        if old is not None and old is not runner:
            try:
                old.close()
            except Exception:
                pass
        self._local_bridge = runner

    def _prepare_swap(self, new_source, *, clock=None):
        """Build the orchestrator AND render `new_source` into it — all the FALLIBLE parts
        of a swap (grid parse, Orchestrator construction, render). Returns a prepared bundle
        `(slots, orch, clock, rs, tiles, panel_png, sections)` for an assignment-only commit;
        mutates NO live deck state (throwaway orchestrator), so any failure raises here,
        BEFORE anything is swapped or persisted. Pass `clock=time.monotonic` for a LIVE
        source so its elapsed-time text advances (else a connect from the mock app keeps
        the mock's frozen clock)."""
        clk = clock if clock is not None else self._clock
        cols, rows = new_source.config.grid
        slots = cols * rows - 2
        orch = Orchestrator(new_source.config, slots=slots, clock=clk)
        rs, tiles, panel_png, sections = self._render_locked(new_source, orch, slots)
        return slots, orch, clk, rs, tiles, panel_png, sections

    def _commit_swap(self, new_source, prepared) -> None:
        """Assign the prepared source/orchestrator/clock + its pre-rendered tiles under the
        lock — **pure assignment, no render**, so it cannot raise for a validated config:
        the post-persist swap is guaranteed not to half-swap. The single lock serializes
        against in-flight reads/presses. After applying the new tiles the sink list is
        fanned out a full frame so physical sinks repaint immediately on swap."""
        slots, orch, clk, rs, tiles, panel_png, sections = prepared
        usage_changed = self._adopt_usage_config(new_source.config)
        with self._lock:
            old = self._source
            self._source = new_source
            self._slots = slots
            self._orch = orch
            self._clock = clk  # adopt the clock the orchestrator was built with
            new_source.attach(orch, lock=self._lock, refresh_locked=self._refresh_locked)
            self._apply_rendered_locked(tiles, panel_png, sections)
            self._fan_out_locked(rs, None, True)
            if usage_changed:
                # The prepared frame was rendered with the OLD poller's data
                # (prepare must not mutate live state); re-render once so a
                # disabled/changed [usage] doesn't linger on the panel.
                self._refresh_locked()
        try:
            old.close()
        except Exception:
            pass

    def swap_source(self, new_source) -> None:
        """Prepare + commit in one call (the reloader and other callers). The render runs in
        prepare (before any assignment), so a malformed config raises without half-swapping."""
        self._commit_swap(new_source, self._prepare_swap(new_source))

    def reload(self) -> None:
        """Apply an edited on-disk config in place. The real sidecar injects a
        reloader (via create_app) that re-selects the source and swap_source()s
        it; tests may inject a stub. No reloader -> safe no-op."""
        if getattr(self, "_suppress_reload", False):
            return
        with self._setup_lock:  # serialize against in-flight connect / config-write transactions
            if self._reloader is not None:
                self._reloader()

    def _watcher_reload(self) -> None:
        """ConfigWatcher entry point: reload only if the files REALLY differ
        from the adopted baseline once the transaction lock is held. A poll
        that fired mid-transaction (after a route write changed the mtime but
        before that route's reload+resync finished) queues a callback that
        must NOT replay the reload — resync() cannot cancel a callback
        already in flight (roborev a59985b)."""
        if getattr(self, "_suppress_reload", False):
            return
        with self._setup_lock:
            if getattr(self, "_suppress_reload", False):
                return
            watcher = getattr(self, "_watcher", None)
            if watcher is not None:
                if not watcher.dirty():
                    return  # already handled by the route that held the lock
                watcher.resync()  # adopt BEFORE reloading (this callback owns it)
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
                "language": getattr(self._source, "language", "en"),
            }

    def _health(self) -> dict:
        return {
            "ok": True,
            "source": self._source.source_name,
            "connected": self._source.connected,
            "server_id": self._source.server_id,
        }

    def _setup_status(self) -> dict:
        from ..bootstrap import resolve_socket_path
        from .onboarding import read_choice

        socket_path = resolve_socket_path(None)
        socket_exists = os.path.exists(socket_path)
        config_path = str(self._config_service._config_path) if self._config_service else None
        choice = read_choice(config_path)
        live = self._source.source_name == "live"
        if live:
            mode = "local" if getattr(self, "_local_bridge", None) is not None else "remote"
            reason = None
        else:
            mode = "mock"
            if os.environ.get("HERDECK_MOCK"):
                reason = "mock_env"
            elif choice == "demo":
                reason = "demo"
            elif choice == "local" and not socket_exists:
                reason = "local_unavailable"
            else:
                reason = "first_run"
        return {
            "mode": mode,
            "connected": self._source.connected,
            "reason": reason,
            "local_herdr_available": socket_exists,
            "saved_remote_available": _has_saved_remote(self._config_service),
            "choice": choice,
            "socket_path": socket_path,
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
            # HTTP/1.1 keep-alive: the desktop polls every 300ms and fetches
            # each changed tile separately — HTTP/1.0's close-per-response
            # churned a fresh TCP connection + server thread for every one.
            # Safe because _send always emits Content-Length.
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # never log requests (could carry the token)
                pass

            def _send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
                # keep-alive safety: a rejected POST (bad token, 404) may leave
                # its request body unread on the persistent connection — the
                # next request would be parsed from those leftover bytes.
                if (
                    self.command == "POST"
                    and not getattr(self, "_body_consumed", False)
                    and int(self.headers.get("Content-Length") or 0) > 0
                ):
                    self.close_connection = True
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
                elif path == "/setup":
                    if not self._require_query_token(url):
                        return
                    self._send(200, json.dumps(app._setup_status()).encode(), "application/json")
                else:
                    self._send(404)

            def _read_body(self, length):
                self._body_consumed = True
                return self.rfile.read(length)

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
                raw = self._read_body(length) if length else b""
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
                # handler instances persist across keep-alive requests: the
                # consumed flag must reset per request or a later rejected
                # POST on the same connection would skip the close guard
                self._body_consumed = False
                path = urlsplit(self.path).path
                if path.startswith("/press/"):
                    if not self._require_header_token():
                        return
                    try:
                        app.press(int(path.rsplit("/", 1)[1]))
                        self._send(204)
                    except ValueError:
                        self._send(400)
                elif path == "/setup/connect":
                    if not self._require_header_token():
                        return
                    body = self._json_body()
                    if body is _BAD_BODY:
                        return
                    with app._setup_lock:  # serialize concurrent connects (ThreadingHTTPServer)
                        result = connect(app, body)
                    if result is None:
                        self._send(400)
                        return
                    self._send(200, json.dumps(result).encode(), "application/json")
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
                        # Same semantics as write(): structural only, so live
                        # validation never flags a missing secret Apply accepts.
                        # Under _setup_lock: the structural pass temporarily
                        # placeholders token envs in os.environ, which must not
                        # race a concurrent write/connect/reload.
                        with app._setup_lock:
                            errors = app._config_service.validate_for_write(body)
                        self._send(200, json.dumps({"errors": errors}).encode(), "application/json")
                    elif path == "/config":
                        body = self._json_body()
                        if body is _BAD_BODY:
                            return
                        with app._setup_lock:
                            errors = app._config_service.write(body)
                            if not errors:
                                app.reload()
                                # Adopt our own write as the watcher baseline so it
                                # does not re-fire on the mtime change and reload a
                                # SECOND time (two source swaps = two reconnects and
                                # a double disconnected/empty flash per editor save).
                                watcher = getattr(app, "_watcher", None)
                                if watcher is not None:
                                    watcher.resync()
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
                            with app._setup_lock:
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
                        with app._setup_lock:
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
                    with app._setup_lock:
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


def _has_saved_remote(config_service) -> bool:
    """True when an on-disk config has at least one ``[[servers]]`` entry — a RAW
    TOML read with NO token/keychain resolution, so it is safe to call on the hot
    ``/setup`` poll. Authoritative resolution (does the token actually resolve?) is
    deferred to connect-time ``select_live()`` (fail-soft "no saved connection").
    Mock-gated: under ``HERDECK_MOCK`` there is no saved button, matching the
    existing ``reason="mock_env"`` special-casing."""
    import tomllib

    if os.environ.get("HERDECK_MOCK") or config_service is None:
        return False
    path = config_service._config_path
    if not path.exists():
        return False
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    servers = data.get("servers")
    return isinstance(servers, list) and len(servers) > 0


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
        tick_interval=config.hardware.tick_interval,
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
    """Re-select the source for a config-watch reload, RESPECTING the onboarding precedence
    (a demo/local marker is honored, not overridden by a resolvable remote config). Only the
    NORMAL reloader (remote/demo/mock) calls this; a `local` result is a defensive fallback
    to mock — the bridge is never (re)started from a reload."""
    kind = _resolve_source_kind()
    if kind[0] == "remote":
        from .live import build_live_source

        return build_live_source(kind[1], kind[2])
    from .mock import MockSource

    return MockSource()


def _load_partial_config():
    """The on-disk config (resolved profile) for local mode's overlay, or None if absent
    or unloadable. Lets local mode preserve the user's grid/profiles/view/theme even with
    no [[servers]] — matching the CLI's local mode."""
    from ..bootstrap import _discover_config_path, _discover_local_config_path
    from ..config import ConfigError
    from ..settings import load_settings, resolve_profile

    path = _discover_config_path()
    if not path:
        return None
    try:
        snapshot = load_settings(path, _discover_local_config_path(path))
        return resolve_profile(snapshot).config
    except (ConfigError, OSError):
        return None


def _start_local_bridge(socket_path, *, runner_factory=None):
    """Start the embedded bridge and synthesize its loopback (config, server).
    Returns (config, server, runner); the caller owns runner teardown."""
    from ..bootstrap import local_config
    from .local_bridge import LocalBridgeRunner

    runner = (runner_factory or LocalBridgeRunner)(socket_path)
    try:
        _host, port, token = runner.start()
    except Exception:
        runner.close()  # clean up a partially-started runner before re-raising
        raise
    config = local_config(port, token, _load_partial_config())
    return config, config.servers[0], runner


def _local_reloader(app):
    """LOCAL-mode reloader: rebuild the live source against the RUNNING embedded
    bridge (same port/token — the bridge itself is never restarted or swapped
    out by a reload), so an editor Apply / on-disk edit actually reaches the
    deck. The old no-op silently ignored every Apply on the primary
    ('herdr běží lokálně') onboarding path."""
    from ..bootstrap import local_config

    def reload_() -> None:
        runner = getattr(app, "_local_bridge", None)
        bound = runner.bound if runner is not None else None
        if bound is None:
            return  # defensive: no live bridge to rebuild against
        _host, port, token = bound
        config = local_config(port, token, _load_partial_config())
        new_source = build_live_source_for_connect(config, config.servers[0])
        try:
            app.swap_source(new_source)
        except Exception:
            new_source.close()  # don't leak the built source / its connector runner
            raise

    return reload_


def _reloader_for(app, kind, select_source):
    """The config-watch reloader for the built source. LOCAL mode rebuilds the
    live source against the running embedded bridge (the bridge lifecycle stays
    owned by create_app startup + /setup/connect); mock/remote re-select from
    disk."""
    if kind[0] == "local":
        return _local_reloader(app)
    return lambda: app.swap_source(select_source())


def _token_env_for(server_id: str) -> str:
    slug = "".join(c if c.isalnum() else "_" for c in server_id).upper()
    return f"HERDECK_{slug}_TOKEN"


def _restore_secret(name: str, prior: str | None) -> None:
    """Restore the keychain entry for `name` to its snapshot `prior`: re-store the prior
    value if it existed, else clear it. So a rollback after overwriting an existing token
    (reconnecting an existing server) never destroys the previously-stored secret.
    Best-effort: never raises."""
    from .. import secrets as secret_store

    try:
        if prior is None:
            secret_store.clear_secret(name)
        else:
            secret_store.set_secret(name, prior)
    except Exception:
        pass


def _restore_file(path, prior_text) -> None:
    """Restore a file to its prior contents (or remove it if it did not exist before).
    Used to undo a partial/failed config write so no serverful-but-tokenless config is
    left behind. Best-effort: never raises."""
    try:
        if prior_text is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(prior_text, encoding="utf-8")
    except OSError:
        pass


def _snapshot_config(svc):
    """Read the current config.toml/local.toml text (or None if absent) for rollback.
    Raises OSError on a read fault — the caller snapshots BEFORE mutating anything, so a
    failure here persists nothing."""
    cfg = svc._config_path.read_text(encoding="utf-8") if svc._config_path.exists() else None
    local = svc._local_path.read_text(encoding="utf-8") if svc._local_path.exists() else None
    return cfg, local


def _restore_choice(config_path, prior: str | None) -> None:
    """Restore the onboarding marker to its snapshot: re-write the prior choice if there
    was one, else clear it. Used to undo a local connect whose swap failed after the
    marker was written. Best-effort: never raises."""
    from .onboarding import clear_choice, write_choice

    try:
        if prior is None:
            clear_choice(config_path)
        else:
            write_choice(config_path, prior)
    except OSError:
        pass


def _probe_sync(url: str, token: str):
    """Sync wrapper over the async probe (the HTTP handler runs on a plain thread)."""
    import asyncio

    from .probe import probe_server

    return asyncio.run(probe_server(url, token))


def build_live_source_for_connect(config, server):
    from .live import build_live_source

    return build_live_source(config, server)


def connect(app, body) -> dict | None:
    """Run the onboarding connect flow. Returns the response dict, or None for a
    malformed body (the route maps None -> HTTP 400). Live swaps follow
    build -> swap -> adopt so a failed build never strands the app on a closed
    bridge; remote builds the live source BEFORE persisting (no half-commit)."""
    import dataclasses
    import time
    import tomllib

    from .mock import MockSource
    from .onboarding import clear_choice, read_choice, write_choice

    choice = body.get("choice")
    config_path = str(app._config_service._config_path) if app._config_service else None

    if choice == "demo":
        # Transactional like local/remote: prepare (render mock) BEFORE the marker, commit after.
        prior_choice = read_choice(config_path)
        new_source = MockSource()
        try:
            prepared = app._prepare_swap(new_source)  # render mock (fallible) BEFORE persisting
            write_choice(config_path, "demo")  # persist
        except Exception:
            _restore_choice(config_path, prior_choice)
            new_source.close()
            return {"ok": False, "error": "could not switch to demo"}
        app._commit_swap(new_source, prepared)  # assignment-only
        app._set_local_bridge(None)
        app._reloader = _reloader_for(app, ("mock",), _select_source)  # mock/remote reloads resume
        return {"ok": True}

    if choice == "local":
        from ..bootstrap import resolve_socket_path

        socket_path = resolve_socket_path(None)
        if not os.path.exists(socket_path):
            return {"ok": False, "error": f"herdr socket not found at {socket_path}"}
        new_source = None
        runner = None
        prior_choice = read_choice(config_path)  # snapshot the marker for rollback
        try:
            config, server, runner = _start_local_bridge(socket_path)  # may raise (bridge bind)
            new_source = build_live_source_for_connect(config, server)  # build ...
            prepared = app._prepare_swap(new_source, clock=time.monotonic)  # ... pre-build orch (live clock) BEFORE the marker ...
            write_choice(config_path, "local")  # ... persist (durable)
        except Exception:
            _restore_choice(config_path, prior_choice)  # undo the marker if it was written
            if new_source is not None:
                new_source.close()  # don't leak the built source / its connector runner
            if runner is not None:
                runner.close()  # ... or the just-started bridge; previous source untouched
            return {"ok": False, "error": "could not start local source"}
        app._commit_swap(new_source, prepared)  # non-failing: all fallible work done; sets the live clock
        app._set_local_bridge(runner)  # adopt new bridge (closes old one)
        app._reloader = _reloader_for(app, ("local",), _select_source)  # no-op: don't swap out the bridge
        return {"ok": True, "connected": app._source.connected}

    if choice == "remote":
        url, token, server_id = body.get("url"), body.get("token"), body.get("id") or "herdr"
        if not (isinstance(url, str) and url and isinstance(token, str) and token
                and isinstance(server_id, str) and server_id):
            return None  # -> 400: url/token/id must be non-empty strings (e.g. {"id": 123} is invalid)
        token_env = _token_env_for(server_id)
        # Secret resolution is ENV-FIRST: if token_env is already exported with a DIFFERENT
        # value, that env value would shadow whatever we store in the keychain, so the
        # persisted config would NOT resolve to the typed token. Reject before doing anything.
        env_token = os.environ.get(token_env)
        if env_token is not None and env_token != token:
            return {
                "ok": False,
                "error": f"{token_env} is set in the environment and would override the saved token; unset it or connect with that value",
            }
        result = _probe_sync(url, token)
        if not result.ok:
            return {"ok": False, "error": result.reason}
        try:
            data = app._config_service.read()  # a malformed/unreadable existing config must not 500
        except (OSError, tomllib.TOMLDecodeError):
            return {"ok": False, "error": "existing config is unreadable — fix it in Settings"}
        payload = {
            "base": dict(data.get("base") or {}),
            "profiles": data.get("profiles") or {},
            "local": data.get("local") or {},
        }
        existing = payload["base"].get("servers")
        if existing is not None and not (isinstance(existing, list) and all(isinstance(s, dict) for s in existing)):
            # parseable TOML but a wrong shape (e.g. `servers = ["bad"]`) would crash the upsert
            return {"ok": False, "error": "existing config is malformed (servers) — fix it in Settings"}
        entry = {"id": server_id, "url": url, "token_env": token_env}
        rebuilt = []
        replaced = False
        for s in (existing or []):
            if isinstance(s, dict) and s.get("id") == server_id:
                if not replaced:
                    rebuilt.append(entry)  # replace the first match in place
                    replaced = True
                # drop any further duplicate with the same id
            else:
                rebuilt.append(s)
        if not replaced:
            rebuilt.append(entry)
        servers = rebuilt
        payload["base"]["servers"] = servers
        # token_env (HERDECK_<ID>_TOKEN) lives in ONE flat keychain namespace shared by ALL
        # config sections — other servers, `notifications.telegram`, profile overlays. Two ids
        # can collide (`foo-bar`/`foo_bar`), and a derived name can clash with a NON-server
        # secret. Collect every token_env the EXISTING config references except the server we
        # are replacing; reject if ours is already in use, so we never overwrite another secret.
        from .config_service import ConfigService

        base_wo_ours = dict(data.get("base") or {})
        base_wo_ours["servers"] = [
            s for s in (base_wo_ours.get("servers") or [])
            if not (isinstance(s, dict) and s.get("id") == server_id)
        ]
        in_use = []
        ConfigService._collect_token_envs(base_wo_ours, in_use)
        ConfigService._collect_token_envs(data.get("profiles") or {}, in_use)
        if token_env in in_use:
            return {
                "ok": False,
                "error": f"token env {token_env} is already used elsewhere in the config — pick a different id",
            }
        # BUILD-BEFORE-PERSIST: resolve the merged payload (placeholder tokens) to confirm
        # selection, then build the live source with the REAL token baked into the chosen
        # ServerConfig — all BEFORE mutating keychain/config, so any selection / validation
        # / build failure persists NOTHING (no orphaned secret, no serverful-but-dead config).
        resolved = app._config_service.resolve_selected_server(payload, assume_present=token_env)
        if resolved is None or resolved[1].id != server_id:
            return {
                "ok": False,
                "error": "config does not resolve to this server (check the active profile / overview_order / other servers' tokens) — fix it in Settings",
            }
        config, placeholder_server = resolved
        real_server = dataclasses.replace(placeholder_server, token=token)  # real token, not keychain
        try:
            new_source = build_live_source_for_connect(config, real_server)  # build BEFORE persist
        except Exception:
            return {"ok": False, "error": "could not build the remote source"}
        # Persist + swap as one watcher-suppressed transaction (see _commit_remote).
        return _commit_remote(app, payload, token_env, token, new_source, config_path)

    if choice == "saved":
        # One-click escape from the demo trap: re-select the on-disk remote (token from
        # the keychain) and clear the demo/local marker. Transactional like the others —
        # build + prepare BEFORE clearing the marker; any failure restores it and closes
        # the just-built source. NO _suppress_reload (this writes only onboarding.toml,
        # which the watcher does not track) and NO probe (select_live() confirms token
        # PRESENCE, not validity; the live source dials async, so connected may be False).
        remote = select_live()  # (config, server) from disk + keychain, or None
        if remote is None:
            return {"ok": False, "error": "no saved connection"}
        config, server = remote
        prior_choice = read_choice(config_path)
        new_source = None
        try:
            new_source = build_live_source_for_connect(config, server)  # build (fallible)
            prepared = app._prepare_swap(new_source, clock=time.monotonic)  # render (fallible)
            clear_choice(config_path)  # persist: drop the demo/local marker
        except Exception:
            _restore_choice(config_path, prior_choice)  # marker untouched / restored
            if new_source is not None:
                new_source.close()
            return {"ok": False, "error": "could not restore saved connection"}
        app._commit_swap(new_source, prepared)  # assignment-only, non-failing
        app._set_local_bridge(None)  # saved targets remote; drop any local bridge
        app._reloader = _reloader_for(app, ("remote",), _select_source)
        return {"ok": True, "connected": app._source.connected}

    return None  # unknown choice -> 400


def _commit_remote(app, payload, token_env, token, new_source, config_path) -> dict:
    """Persist (secret-then-config) and swap to `new_source` as ONE transaction, with the
    config watcher SUPPRESSED so its mtime poll can't reload mid-commit (double-swapping
    to a second source) or swap to the half-written config during a rollback. Any failure
    restores the prior secret + config and closes the just-built source. The watcher
    baseline is resynced on exit so it doesn't fire on our own writes/restores."""
    import time

    from .. import secrets as secret_store
    from .onboarding import clear_choice

    app._suppress_reload = True
    try:
        # Pre-build the orchestrator (the only fallible part of the swap) BEFORE persisting,
        # so the post-persist commit (_commit_swap) is guaranteed non-throwing.
        try:
            prepared = app._prepare_swap(new_source, clock=time.monotonic)  # live clock
        except Exception:
            new_source.close()
            return {"ok": False, "error": "could not build the remote source"}
        # Snapshot the prior keychain value AND the on-disk config BEFORE any mutation, so a
        # read fault can't strand a secret, and a partial write (config ok, local faults) is
        # undone — never leaving a serverful-but-tokenless config or a destroyed prior token.
        # peek_keychain raises (not None) on a backend READ error, so we abort here rather
        # than risk erasing an existing token we couldn't actually read.
        try:
            prior_secret = secret_store.peek_keychain(token_env)
        except Exception:
            new_source.close()
            return {"ok": False, "error": "could not read the existing token — check the keychain"}
        svc = app._config_service
        try:
            prior_config, prior_local = _snapshot_config(svc)
        except OSError:
            new_source.close()  # nothing mutated yet
            return {"ok": False, "error": "could not read config"}
        try:
            secret_store.set_secret(token_env, token)
        except Exception:
            _restore_secret(token_env, prior_secret)  # set may have partially overwritten
            new_source.close()
            return {"ok": False, "error": "could not store token"}

        def _rollback():
            _restore_file(svc._config_path, prior_config)
            _restore_file(svc._local_path, prior_local)
            _restore_secret(token_env, prior_secret)  # restore prior token, don't destroy it
            new_source.close()

        try:
            errors = svc.write(payload)
        except OSError:  # atomic write can fault, possibly after a partial write
            _rollback()
            return {"ok": False, "error": "could not write config"}
        if errors:  # structural validation runs before any write, so nothing was written
            _rollback()
            return {"ok": False, "error": "; ".join(errors)}
        # Clear the stale local/demo marker as PART OF THE COMMIT: remote == a usable config,
        # no opt-in marker. If the unlink faults, roll everything back so a later-removed
        # config falls to first_run (the card), never to a stale marker that would mask it.
        try:
            clear_choice(config_path)
        except OSError:
            _rollback()
            return {"ok": False, "error": "could not finalize onboarding"}
        app._commit_swap(new_source, prepared)  # non-failing: all fallible work done; sets the live clock
        app._set_local_bridge(None)  # ... then drop any local bridge
        app._reloader = _reloader_for(app, ("remote",), _select_source)  # config-edit reloads resume
        return {"ok": True, "connected": app._source.connected}  # honest: connector dials async
    finally:
        watcher = getattr(app, "_watcher", None)
        if watcher is not None:
            watcher.resync()  # adopt our writes as the baseline; no spurious reload
        app._suppress_reload = False


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
    kind = _resolve_source_kind()
    if kind[0] == "remote":
        _, config, server = kind
        app = create_live_app(
            config, server, host=host, port=port, icon_provider=icon_provider,
            serve=serve, config_service=svc,
        )
    elif kind[0] == "local":
        # create_live_app already builds with clock=time.monotonic, so live elapsed
        # time advances; the embedded bridge runner is tracked for teardown on close.
        _, socket_path = kind
        config, server, runner = _start_local_bridge(socket_path)
        try:
            app = create_live_app(
                config, server, host=host, port=port, icon_provider=icon_provider,
                serve=serve, config_service=svc,
            )
        except Exception:
            runner.close()  # don't leak the bridge thread/socket if app construction fails
            raise
        app._set_local_bridge(runner)
    else:
        app = create_mock_app(
            host=host, port=port, icon_provider=icon_provider, serve=serve, config_service=svc
        )
    if reloader is None:
        app._reloader = _reloader_for(app, kind, _select_source)
    else:
        app._reloader = reloader

    # Watch the config files; fire the reloader when any changes on disk.
    # Filter out None (local path is absent when no local override exists).
    # adopt_before_fire=False: _watcher_reload re-checks dirtiness under the
    # transaction lock and owns baseline adoption, so a poll that fired during
    # a route write/reload transaction cannot replay the reload.
    watch_paths = [p for p in (cfg_path, local_path) if p is not None]
    app._watcher = ConfigWatcher(
        watch_paths, app._watcher_reload, interval=1.0, adopt_before_fire=False
    )
    app._watcher.start()
    return app
