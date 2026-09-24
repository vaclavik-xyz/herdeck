from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .. import __version__
from ..config import ConfigError
from ..i18n import tr
from ..model import AgentKey
from ..orchestrator import Orchestrator
from ..pins import PinStore
from ..protocol import WIRE_PROTOCOL
from . import agent_card, bridge_update
from .sinks import RenderFrame
from .source import StateSource

log = logging.getLogger(__name__)
# Under herdeck.notify so the runtime keeps these at INFO (runtime.configure_logging):
# the claim timeline is what explains a banner that came via osascript.
claim_log = logging.getLogger("herdeck.notify.claim")

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
# serve_forever() polls its shutdown flag every poll_interval seconds; the stdlib
# default (0.5 s) makes every close() block up to half a second, which adds up
# fast in tests that start/stop servers constantly. 50 ms keeps shutdown snappy
# at a negligible idle-wakeup cost.
_SERVE_POLL_INTERVAL = 0.05
# A status panel ("reload failed", "profile locked") stays up this long: the
# next render would otherwise replace it within one tick and the user never
# learned why their action had no effect. Any press dismisses it.
STATUS_PANEL_HOLD_S = 4.0

# Body returned for any unauthenticated request: plain text (never
# octet-stream, which browsers offer to download) and free of any token.
_FORBIDDEN = (
    b"herdeck deckapp: missing or invalid access token.\n"
    b"The token is handed to the desktop shell on startup; it is never in logs.\n"
)


def tile_accessible_label(tile, lang: str = "en") -> str:
    """Screen-reader description of one rendered tile (/state "tile_labels").

    Built only from the TileView the deck just rendered, so it always says
    what the tile shows: agent tiles read like ``claude · herdeck · main ·
    working 3m``; control tiles give their label (plus the option text of a
    drill choice); blank cells say so instead of staying silent.
    """
    label = (tile.label or "").strip()
    if tile.color == "empty" and not label:
        return tr(lang, "a11y.empty_tile", n=tile.index + 1)
    if tile.agent_type:
        parts = [tile.agent_type]
        for part in (tile.repo or label, tile.branch):
            part = (part or "").strip()
            if part and part not in parts:
                parts.append(part)
        status = " ".join(
            p for p in ((tile.status_text or "").strip().lower(), tile.time_text or "") if p
        )
        if status:
            parts.append(status)
        if tile.pinned:
            parts.append(tr(lang, "a11y.pinned"))
        return " · ".join(parts)
    parts = [p for p in (label, (tile.subtext or "").strip()) if p]
    return " · ".join(parts) or tr(lang, "a11y.empty_tile", n=tile.index + 1)


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
        pin_store=None,
        run_ticker: bool | None = None,
    ):
        self._serve_enabled = serve
        # The animation ticker runs for a serving deck by default; a host that
        # serves through its own front (the web cockpit, a physical deck) and
        # not this HTTP API asks for it explicitly.
        self._ticker_enabled = serve if run_ticker is None else run_ticker
        self._started_at = time.monotonic()
        self._source = source
        config = source.config
        cols, rows = config.grid
        # Match the established deck geometry: the two status-window cells are not
        # addressable tiles, so slots = grid - 2 (e.g. 13 for a 5x3 grid).
        # An explicit `slots` is the physical deck's geometry (an Elgato Stream
        # Deck's key count - 2, whatever the grid): it is kept across source
        # swaps, as the device cannot change shape with a profile.
        self._fixed_slots = slots
        self._slots = slots if slots is not None else cols * rows - 2
        # Store the clock so swap_source can rebuild the orchestrator with the same clock.
        self._clock = clock or (lambda: 0.0)
        # A fixed clock keeps the mock fully deterministic (stable elapsed text,
        # so repeated /state polls do not churn tile versions).
        config_path = getattr(config_service, "_config_path", None)
        self._pin_store = pin_store or (PinStore(Path(config_path).with_name("pins.json")) if config_path else None)
        self._orch = Orchestrator(config, slots=self._slots, clock=self._clock)
        self._load_pins(self._orch)
        self._owns_icons = icon_provider is None
        self._icons_dir = config.hardware.icons_dir
        self._icons = (
            icon_provider if icon_provider is not None else _default_icons(self._icons_dir)
        )
        self._token = token or secrets.token_urlsafe(24)
        self._config_service = config_service
        self._reloader = reloader
        self._local_bridge = None  # compatibility alias for the first local bridge
        self._local_bridges: dict[str, object] = {}
        self._suppress_reload = False  # set by the onboarding commit to mute the watcher
        self._setup_lock = threading.RLock()  # shared mutation lock (/setup/connect + config-write routes + reload); RLock because the config routes call reload() while holding it
        self._shell_claim_lock = threading.Lock()

        # Provider usage poller (a daemon thread; None when [usage] is off).
        # Renders read its latest snapshot; no render ever blocks on the CLI.
        self._usage_cfg = getattr(config, "usage", None)
        self._usage_poller = self._build_usage_poller(self._usage_cfg)

        self._lock = threading.Lock()
        self._status_panel = None  # held PanelView (hold_status_panel)
        self._status_panel_until = 0.0
        self._panel_memo: tuple[tuple, bytes] | None = None  # (panel content key, png)
        self._tiles: dict[int, bytes] = {}
        self._tile_ver: dict[int, int] = {}
        self._tile_sections: dict[int, str] = {}
        self._panel: bytes | None = None
        self._panel_ver = 0
        self._version = 0
        self._sinks: list = []  # RenderSink fan-out targets (HTTP buffer is DeckApp's own)
        self._ticks = 0
        # Accessible per-tile descriptions (/state "tile_labels", contract C1).
        self._tile_labels: dict[int, str] = {}
        # Long-poll /state waiters block on this; every version bump notifies
        # (it shares self._lock, so waiting releases the render lock).
        self._state_changed = threading.Condition(self._lock)
        # Digest of the /state fields that are NOT image versions (summary,
        # connection flags, sections, labels, language). Some changes leave
        # every PNG byte-identical (a bridge dropping, an off-screen agent
        # unblocking while drilled), so _apply_rendered_locked bumps the
        # version on a digest change too — else long-poll waiters sleep on
        # stale data until their timeout.
        self._state_digest: str | None = None
        # Rasterization (IconProvider + Pillow) is not thread-safe, but it must
        # not hold self._lock either: the ticker rasterizes OUTSIDE the state
        # lock under this one. Lock order is always self._lock -> _raster_lock.
        self._raster_lock = threading.Lock()
        # Render sequencing: a frame rendered outside the lock is applied only
        # if no newer frame landed meanwhile, so versions/tiles never go back.
        self._render_seq = 0
        self._applied_seq = 0
        # Headless ticker: skip animation renders nobody reads (see _tick_once).
        self._ticker_stale = False
        self._state_waiters = 0
        self._state_last_read = float("-inf")
        self._setup_cache: tuple[tuple, float, dict] | None = None

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
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                kwargs={"poll_interval": _SERVE_POLL_INTERVAL},
                daemon=True,
            )
            self._thread.start()
        else:
            self.host, self.port = host, port

        # Background ticker: advance the spinner phase + re-render every
        # tick_interval seconds so working tiles animate in the served /state.
        # Only when actually serving (mock/test path leaves it off -> deterministic).
        self._tick_interval = tick_interval
        self._ticker_stop = threading.Event()
        self._ticker_wake = threading.Event()
        self._ticker_thread: threading.Thread | None = None
        if self._ticker_enabled and tick_interval > 0:
            self._ticker_thread = threading.Thread(
                target=self._ticker_loop, name="herdeck-deckapp-tick", daemon=True
            )
            self._ticker_thread.start()

        # Bridge usage frames (usage_hub.py) may repaint from here on.
        self._usage_wired = True
        self._wire_usage(self._source)

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
        """Assign the next monotonic version and wake long-poll /state
        waiters. Call while holding self._lock."""
        self._version += 1
        self._state_changed.notify_all()
        return self._version

    def _build_usage_poller(self, usage_cfg):
        """The usage input (usage_hub.UsageHub: bridge frames or the local
        poller per [usage].source); None when [usage] names no providers."""
        from .. import usage as usage_mod
        from ..usage_hub import UsageHub

        if usage_cfg is None or not usage_cfg.providers:
            return None
        return UsageHub(
            usage_cfg,
            # Looked up at call time (tests patch usage.poller_from_config).
            local_factory=lambda: usage_mod.poller_from_config(
                usage_cfg, on_alert=self._deliver_usage_alerts
            ),
            on_alert=self._deliver_usage_alerts,
            on_change=self._on_usage_changed,
            server_ids=self._usage_server_ids(self._source),
        )

    @staticmethod
    def _usage_server_ids(source) -> list[str]:
        """Servers that may offer usage, in config order. T3 servers never
        do, so [usage].source = "auto" must not wait for them."""
        ids = getattr(source, "server_ids", None)
        if not isinstance(ids, list):
            return []
        servers = getattr(getattr(source, "config", None), "servers", None) or []
        t3 = {server.id for server in servers if getattr(server, "backend", "herdr") == "t3"}
        return [sid for sid in ids if sid not in t3]

    def _on_usage_changed(self) -> None:
        """The hub switched input or bridge numbers changed: repaint now
        instead of waiting for the next tick (never called under a hub lock)."""
        if getattr(self, "_usage_wired", False):
            self.refresh()

    def _wire_usage(self, source) -> None:
        """Route ``source``'s bridge usage reports (LiveSource) into the hub.
        Call WITHOUT self._lock: the replay may repaint."""
        hub = self._usage_poller
        bridge_update = getattr(hub, "bridge_update", None)
        if hub is not None and hasattr(hub, "set_servers"):
            hub.set_servers(self._usage_server_ids(source))
        setter = getattr(source, "set_usage_sink", None)
        if callable(setter):
            setter(bridge_update if callable(bridge_update) else None)

    def _deliver_usage_alerts(self, alerts) -> None:
        """Usage-limit alerts from the poller thread ride the CURRENT source's
        agent notification pipeline (LiveSource: [notifications] enabled/
        backends, shell banner feed, telegram). A source without one (the
        mock/demo deck) drops them."""
        deliver = getattr(self._source, "notify_usage", None)
        if deliver is not None:
            deliver(alerts)

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

    def _render_locked(self, source, orch, slots, *, icons=None):
        """Render `source` through `orch` → (rs, tiles, panel_png, sections, labels).
        This is the FALLIBLE part of a refresh (apply_to / orchestrator render / icon
        raster / panel compose); apart from the value-keyed panel memo it mutates no
        self state, so it can run on a throwaway orchestrator in `_prepare_swap` or on
        the live deck inside `_refresh_locked`."""
        rs = self._snapshot_render(source, orch)
        return (rs, *self._rasterize(rs, slots, icons=icons, source=source))

    def _snapshot_render(self, source, orch):
        """Feed the orchestrator and take its RenderState. Mutates `orch`, so the
        live deck calls it under self._lock; the result is plain view data."""
        # ALWAYS feed usage state (empty when off): the orchestrator may carry
        # usage lines from before a swap that disabled [usage] — only an
        # unconditional set clears them (roborev e0eeb95).
        poller = self._usage_poller
        orch.set_usage(poller.snapshot() if poller is not None else [])
        source.apply_to(orch)
        rs = orch.render()
        held = self._status_panel
        if held is not None and time.monotonic() < self._status_panel_until:
            from ..orchestrator import RenderState

            rs = RenderState(rs.tiles, held)
        return rs

    def hold_status_panel(self, title: str, lines: list[str], color: str = "amber") -> None:
        """Show a short status message on the panel for STATUS_PANEL_HOLD_S
        (or until the next press) instead of the deck's own panel."""
        from ..driver.base import PanelView

        with self._lock:
            self._status_panel = PanelView(title, lines, color)
            self._status_panel_until = time.monotonic() + STATUS_PANEL_HOLD_S
            self._refresh_locked()

    def _status_view(self, title: str, lines: list[str], color: str):
        """A held status panel (caller holds self._lock and re-renders)."""
        from ..driver.base import PanelView

        self._status_panel_until = time.monotonic() + STATUS_PANEL_HOLD_S
        return PanelView(title, lines, color)

    def _consume_expired_status_locked(self) -> bool:
        """True once when a held status panel just lapsed (a full render then
        brings the deck's own panel back)."""
        if self._status_panel is not None and time.monotonic() >= self._status_panel_until:
            self._status_panel = None
            return True
        return False

    def _rasterize(self, rs, slots, *, icons=None, source=None):
        """RenderState → (tiles, panel_png, sections, labels). Touches no deck
        state except the panel memo, so the ticker runs it outside self._lock."""
        with self._raster_lock:
            return self._rasterize_serialized(rs, slots, icons, source or self._source)

    def _rasterize_serialized(self, rs, slots, icons, source):
        import io

        from ..icons import PANEL_W_TWO_CELL, compose_panel

        icon_provider = icons if icons is not None else self._icons
        tiles = {
            t.index: icon_provider.render_tile_bytes(t) for t in rs.tiles if t.index < slots
        }
        # Memoize the encoded panel by content: panel text changes every few
        # seconds at most, while refreshes run per tick — recomposing + PNG-encoding
        # an identical panel dominated the steady-state tick cost.
        panel_key = rs.panel.cache_key()
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
        lang = getattr(source, "language", "en")
        labels = {
            t.index: tile_accessible_label(t, lang) for t in rs.tiles if t.index < slots
        }
        return tiles, panel_png, sections, labels

    def _apply_rendered_locked(self, tiles, panel_png, sections, labels=None):
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
        if labels is not None:
            self._tile_labels = labels
        if self._panel != panel_png:
            self._panel = panel_png
            self._panel_ver = self._bump()
        digest = self._state_digest_locked()
        if digest != self._state_digest:
            self._state_digest = digest
            self._bump()

    def _state_meta_locked(self) -> dict:
        """The /state fields derived from the source/bridges rather than from
        rendered images. Callers hold self._lock."""
        meta = {
            "summary": self._source.summary(),
            "source": self._source.source_name,
            "connected": self._source.connected,
            "language": getattr(self._source, "language", "en"),
        }
        connections = getattr(self._source, "connections", None)
        if isinstance(connections, dict):
            meta["connections"] = connections
        local_connections = {
            session_name: server_id
            for server_id, runner in getattr(self, "_local_bridges", {}).items()
            if isinstance(
                session_name := getattr(runner, "_herdeck_session_name", None),
                str,
            )
            and session_name
        }
        if local_connections:
            meta["local_connections"] = local_connections
        return meta

    def _state_digest_locked(self) -> str:
        return repr(
            (
                sorted(self._state_meta_locked().items()),
                sorted(self._tile_sections.items()),
                sorted(self._tile_labels.items()),
            )
        )

    def _refresh_locked(self, *, working=None, full=True, ticker=False) -> None:
        rs, tiles, panel_png, sections, labels = self._render_locked(
            self._source, self._orch, self._slots
        )
        self._render_seq += 1
        self._applied_seq = self._render_seq  # newest frame: supersedes any in flight
        self._ticker_stale = False
        self._apply_rendered_locked(tiles, panel_png, sections, labels)
        # Commit the tile -> agent map of the frame just served (the desktop
        # agent card resolves a clicked tile through agent_for_preview).
        self._orch.confirm_rendered_preview()
        self._fan_out_locked(rs, working, full, ticker)

    def _refresh_split(self, rs, seq, orch, slots, *, working, full, ticker) -> bool:
        """Rasterize a RenderState snapshot taken under self._lock WITHOUT holding
        it, then apply it under the lock — unless a newer frame (or a source
        swap) already landed, which keeps versions and tiles monotonic.

        Callers take ``rs`` + ``seq`` under self._lock and release it first.
        Returns True when the frame was applied."""
        tiles, panel_png, sections, labels = self._rasterize(rs, slots)
        with self._lock:
            if seq <= self._applied_seq or orch is not self._orch:
                return False
            self._applied_seq = seq
            self._ticker_stale = False
            self._apply_rendered_locked(tiles, panel_png, sections, labels)
            # Safe to confirm now: any orchestrator mutation since `rs` was
            # taken re-rendered under the lock and bumped _applied_seq, so a
            # frame that reaches this point still matches the orchestrator.
            orch.confirm_rendered_preview()
            self._fan_out_locked(rs, working, full, ticker)
            return True

    def _fan_out_locked(self, rs, working, full, ticker=False) -> None:
        """Deliver the rendered frame to every sink under self._lock. A sink that
        raises is isolated — the HTTP buffer (already updated above) and the other
        sinks must not be affected."""
        if not self._sinks:
            return
        frame = RenderFrame(render=rs, working=working, full=full, ticker=ticker)
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
            self._set_sink_slots_locked(sink, self._slots)
            self._refresh_locked(working=None, full=True)

    @staticmethod
    def _set_sink_slots_locked(sink, slots: int) -> None:
        setter = getattr(sink, "set_slots", None)
        if setter is None:
            return
        try:
            setter(slots)
        except Exception:
            log.warning("render sink %r failed to adopt %s slots", sink, slots, exc_info=True)

    @staticmethod
    def _reconfigure_sink_locked(sink) -> None:
        reconfigure = getattr(sink, "reconfigure", None)
        if reconfigure is None:
            return
        try:
            reconfigure()
        except Exception:
            log.warning("render sink %r failed to reconfigure", sink, exc_info=True)

    @staticmethod
    def _d200_hardware_signature(hardware) -> tuple:
        return (
            hardware.brightness,
            hardware.debounce,
            hardware.keep_alive_interval,
            hardware.icons_dir,
            hardware.d200_standard_writer,
        )

    def _adopt_tick_interval_locked(self, interval: float) -> None:
        if interval == self._tick_interval:
            return
        self._tick_interval = interval
        self._ticker_wake.set()
        if self._ticker_enabled and self._ticker_thread is None:
            self._ticker_thread = threading.Thread(
                target=self._ticker_loop, name="herdeck-deckapp-tick", daemon=True
            )
            self._ticker_thread.start()

    # A /state reader within this window (or a long-poll waiter) keeps the
    # ticker rendering; with none, animation frames are skipped (headless).
    STATE_READER_TTL_S = 5.0

    def _note_state_read(self) -> None:
        self._state_last_read = time.monotonic()

    def _has_state_reader(self) -> bool:
        return (
            self._state_waiters > 0
            or time.monotonic() - self._state_last_read < self.STATE_READER_TTL_S
        )

    def _ticker_consumers_locked(self) -> bool:
        """Does anyone consume ticker (animation/elapsed) frames right now?
        The D200 sink drops them (full-page uploads blink); unknown sinks are
        assumed to want them."""
        if self._has_state_reader():
            return True
        return any(getattr(sink, "wants_ticker_frames", True) for sink in self._sinks)

    def _tick_once(self) -> None:
        """Advance the spinner phase and re-render. A tick renders only when
        something actually animates (a WORKING tile) or on the periodic full
        refresh — bridge updates and presses trigger their own refresh, so an
        idle deck does no per-tick render/encode/device work at all (matching
        the old App.handle_tick). Every FULL_REFRESH_TICKS-th tick is a full
        frame so idle elapsed text advances and every sink resyncs.

        The orchestrator tick + RenderState snapshot run under self._lock
        (atomic w.r.t. presses and bridge updates); the costly rasterization
        runs OUTSIDE it (see _refresh_split), so /state, /tile, presses and
        bridge updates are not stalled behind a cold ~70ms frame.

        Ticker frames nobody reads are skipped: a headless runtime driving
        only a D200 (which ignores ticker frames) marks the deck stale instead,
        and the next /state read renders on demand."""
        with self._lock:
            working = self._orch.tick()
            self._ticks += 1
            hold_expired = self._orch.consume_expired_panel_hold()
            hold_expired = self._consume_expired_status_locked() or hold_expired
            if hold_expired:
                kwargs = {"working": None, "full": True, "ticker": False}
            elif self._ticks % self.FULL_REFRESH_TICKS == 0:
                kwargs = {"working": None, "full": True, "ticker": True}
            elif working:
                kwargs = {"working": working, "full": False, "ticker": True}
            else:
                return
            if kwargs["ticker"] and not self._ticker_consumers_locked():
                self._ticker_stale = True
                return
            rs = self._snapshot_render(self._source, self._orch)
            self._render_seq += 1
            seq, orch, slots = self._render_seq, self._orch, self._slots
        self._refresh_split(rs, seq, orch, slots, **kwargs)

    def _refresh_if_stale(self) -> None:
        """Render on demand for a reader after headless ticks were skipped."""
        with self._lock:
            if not self._ticker_stale:
                return
            rs = self._snapshot_render(self._source, self._orch)
            self._render_seq += 1
            seq, orch, slots = self._render_seq, self._orch, self._slots
        self._refresh_split(rs, seq, orch, slots, working=None, full=True, ticker=True)

    def _ticker_loop(self) -> None:
        # A config reload wakes the current wait so a shorter interval takes
        # effect immediately; close sets both stop+wake for a prompt exit.
        while not self._ticker_stop.is_set():
            interval = self._tick_interval
            if interval <= 0:
                self._ticker_wake.wait()
                self._ticker_wake.clear()
                continue
            if self._ticker_wake.wait(interval):
                self._ticker_wake.clear()
                continue
            self._tick_once()

    def press(self, index: int) -> None:
        """Inject a press (called from the HTTP thread). Out-of-range/crafted
        indices are ignored; valid ones update mock state and re-render."""
        local_commands = []
        if 0 <= index < self._slots + 2:
            with self._lock:
                self._status_panel = None  # any press dismisses a held status panel
                local_commands = self._source.press(index) or []
                for command in local_commands:
                    if command.kind == "toggle_pin":
                        old = dict(self._orch.pins)
                        self._orch.toggle_pin(AgentKey(command.server_id, command.pane_id), command.payload["position"])
                        try:
                            if self._pin_store is not None:
                                self._pin_store.save(self._source.config.meta.active_profile, self._orch.pins)
                        except (OSError, ValueError, KeyError, TypeError):
                            self._orch.pins = old
                            log.warning("deck pin save failed", exc_info=True)
                            lang = getattr(self._source, "language", "en")
                            self._status_panel = self._status_view(
                                tr(lang, "status.pin_failed"), [tr(lang, "status.try_again")], "red"
                            )
                self._refresh_locked()
        for command in local_commands:
            if command.kind == "switch_profile":
                self._switch_profile_from_deck(command.text or command.server_id)

    def triage(self) -> bool:
        """Open the longest-blocked agent's drill (called from the HTTP thread).

        False when the source cannot triage (the demo mock has no drills)."""
        triage = getattr(self._source, "triage", None)
        if not callable(triage):
            return False
        with self._lock:
            triage()
            self._refresh_locked()
        return True

    def open_agent(self, server_id: str, pane_id: str) -> bool:
        """Open one agent's drill (a notification banner click, HTTP thread).
        False when the source has no drills or the agent is unknown."""
        open_agent = getattr(self._source, "open_agent", None)
        if not callable(open_agent):
            return False
        with self._lock:
            opened = open_agent(AgentKey(server_id, pane_id))
            if opened:
                self._refresh_locked()
        return opened

    def answer_agent(self, server_id: str, pane_id: str, episode: str, **answer) -> str:
        """Answer a blocked agent from a banner (LiveSource.answer_agent result,
        or "unsupported" for a source without banner answers)."""
        answer_agent = getattr(self._source, "answer_agent", None)
        if not callable(answer_agent):
            return "unsupported"
        with self._lock:
            result = answer_agent(AgentKey(server_id, pane_id), episode, **answer)
            if result == "ok":
                self._refresh_locked()
        return result

    def _load_pins(self, orch):
        if self._pin_store is not None:
            try:
                orch.pins = self._pin_store.load(orch.config.meta.active_profile)
            except (OSError, ValueError, KeyError, TypeError):
                log.warning("deck pin load failed", exc_info=True)

    def _switch_profile_from_deck(self, name: str) -> None:
        """Persist and immediately apply a profile selected on the physical deck."""
        if self._config_service is None:
            return
        lang = getattr(self._source, "language", "en")
        try:
            with self._setup_lock:
                if not self._config_service.set_active(name):
                    # HERDECK_PROFILE pins the profile (or there is no local.toml)
                    self.hold_status_panel(
                        tr(lang, "status.profile_locked"),
                        [self._source.config.meta.active_profile],
                    )
                    return
                self.reload()
                watcher = getattr(self, "_watcher", None)
                if watcher is not None:
                    watcher.resync()
        except (ConfigError, OSError) as exc:
            log.warning("deck profile switch failed for %s", name, exc_info=True)
            self.hold_status_panel(tr(lang, "status.profile_failed"), [str(exc)[:60]])

    def close(self) -> None:
        ticker = getattr(self, "_ticker_thread", None)
        if ticker is not None:
            self._ticker_stop.set()
            self._ticker_wake.set()
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
        bridges = dict(getattr(self, "_local_bridges", {}))
        legacy_bridge = getattr(self, "_local_bridge", None)
        if legacy_bridge is not None and legacy_bridge not in bridges.values():
            bridges["local"] = legacy_bridge
        for bridge in bridges.values():
            try:
                bridge.close()
            except Exception:
                pass
        self._local_bridges = {}
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
        self._set_local_bridges({"local": runner} if runner is not None else {})

    def _set_local_bridges(self, runners: dict[str, object]) -> None:
        """Adopt all embedded bridges, closing only runners no longer owned."""
        old = dict(getattr(self, "_local_bridges", {}))
        legacy = getattr(self, "_local_bridge", None)
        if legacy is not None and legacy not in old.values():
            old["local"] = legacy
        kept = set(runners.values())
        for previous in old.values():
            if previous in kept:
                continue
            try:
                previous.close()
            except Exception:
                pass
        self._local_bridges = dict(runners)
        self._local_bridge = next(iter(runners.values()), None)

    def _prepare_swap(self, new_source, *, clock=None):
        """Build the orchestrator AND render `new_source` into it — all the FALLIBLE parts
        of a swap (grid parse, Orchestrator construction, render). Returns a prepared bundle
        `(slots, orch, clock, icons, icons_dir, rs, tiles, panel_png, sections, labels)` for an
        assignment-only commit;
        mutates NO live deck state (throwaway orchestrator), so any failure raises here,
        BEFORE anything is swapped or persisted. Pass `clock=time.monotonic` for a LIVE
        source so its elapsed-time text advances (else a connect from the mock app keeps
        the mock's frozen clock)."""
        clk = clock if clock is not None else self._clock
        cols, rows = new_source.config.grid
        fixed = getattr(self, "_fixed_slots", None)
        slots = fixed if fixed is not None else cols * rows - 2
        orch = Orchestrator(new_source.config, slots=slots, clock=clk)
        self._load_pins(orch)
        icons_dir = new_source.config.hardware.icons_dir
        icons = (
            _default_icons(icons_dir)
            if self._owns_icons and icons_dir != self._icons_dir
            else self._icons
        )
        rs, tiles, panel_png, sections, labels = self._render_locked(
            new_source, orch, slots, icons=icons
        )
        return slots, orch, clk, icons, icons_dir, rs, tiles, panel_png, sections, labels

    def _commit_swap(self, new_source, prepared) -> None:
        """Assign the prepared source/orchestrator/clock + its pre-rendered tiles under the
        lock — **pure assignment, no render**, so it cannot raise for a validated config:
        the post-persist swap is guaranteed not to half-swap. The single lock serializes
        against in-flight reads/presses. After applying the new tiles the sink list is
        fanned out a full frame so physical sinks repaint immediately on swap."""
        slots, orch, clk, icons, icons_dir, rs, tiles, panel_png, sections, labels = prepared
        usage_changed = self._adopt_usage_config(new_source.config)
        old_sink = getattr(self._source, "set_usage_sink", None)
        if callable(old_sink):
            old_sink(None)  # the outgoing connectors must not report into the hub
        # A swapped-in live source (profile switch, config reload, connect from
        # the demo) must keep posting through the shell; without this its gate
        # stayed at the "no shell" default and every alert fell back to osascript.
        self._wire_notify_gate(new_source)
        with self._lock:
            old = self._source
            hardware_changed = self._d200_hardware_signature(
                old.config.hardware
            ) != self._d200_hardware_signature(new_source.config.hardware)
            self._source = new_source
            self._slots = slots
            # Keep the local status timers (agents from a bridge without
            # status_since, other backends) across the swap; same clock only.
            orch.inherit_status_times(self._orch)
            self._orch = orch
            self._clock = clk  # adopt the clock the orchestrator was built with
            self._icons = icons
            self._icons_dir = icons_dir
            new_source.attach(orch, lock=self._lock, refresh_locked=self._refresh_locked)
            self._adopt_tick_interval_locked(new_source.config.hardware.tick_interval)
            for sink in self._sinks:
                self._set_sink_slots_locked(sink, slots)
                if hardware_changed:
                    self._reconfigure_sink_locked(sink)
            self._render_seq += 1
            self._applied_seq = self._render_seq
            self._ticker_stale = False
            self._apply_rendered_locked(tiles, panel_png, sections, labels)
            self._fan_out_locked(rs, None, True)
            if usage_changed:
                # The prepared frame was rendered with the OLD poller's data
                # (prepare must not mutate live state); re-render once so a
                # disabled/changed [usage] doesn't linger on the panel.
                self._refresh_locked()
        self._wire_usage(new_source)
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
            self._invalidate_setup_cache()
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
            self._invalidate_setup_cache()
            if self._reloader is not None:
                self._reloader()

    # --- state snapshots ---
    # --- deck-shell notification claim (banner identity lives in the shell) --

    _SHELL_CLAIM_TTL_S = 60.0

    def note_shell_claim(self, shell_gen: str | None = None) -> None:
        """A shell GET /state with X-Herdeck-Shell means "I post the banners".

        Feed delivery has its own generation + acknowledged cursor, so a fresh
        shell never resets pending runtime state. This method tracks liveness
        only; resetting here used to race fallback and duplicate/drop alerts.
        """
        now = time.monotonic()
        with self._shell_claim_lock:
            last = getattr(self, "_shell_last_seen", None)
            previous_gen = getattr(self, "_shell_gen", None)
            live = last is not None and now - last < self._SHELL_CLAIM_TTL_S
            if shell_gen is not None:
                self._shell_gen = shell_gen
            self._shell_last_seen = now
            self._shell_claim_lapse_logged = False
        if not live:
            claim_log.info(
                "notification shell claim acquired gen=%s (%s)",
                shell_gen,
                "first claim" if last is None else f"after {now - last:.0f}s without one",
            )
        elif shell_gen is not None and previous_gen is not None and shell_gen != previous_gen:
            claim_log.info(
                "notification shell claim moved gen=%s -> %s", previous_gen, shell_gen
            )

    def _wire_notify_gate(self, source) -> None:
        """Hand a notifying source the shell-claim predicates (banner duty)."""
        setter = getattr(source, "set_notify_gate", None)
        if callable(setter):
            setter(
                self.shell_claims_banners,
                claim_age=self.shell_claim_age,
                features=self.shell_features,
            )

    def note_shell_features(self, raw: str | None) -> None:
        """Remember what the polling shell understands (X-Herdeck-Shell-Features,
        comma-separated, e.g. "withdraw"). A shell that sends none gets none."""
        features = frozenset(part.strip() for part in (raw or "").split(",") if part.strip())
        with self._shell_claim_lock:
            self._shell_features = features

    def shell_features(self) -> frozenset[str]:
        with self._shell_claim_lock:
            return getattr(self, "_shell_features", frozenset())

    def shell_claim_age(self) -> float | None:
        """Seconds since a shell last claimed banner duty; None if none ever did."""
        with self._shell_claim_lock:
            last = getattr(self, "_shell_last_seen", None)
        return None if last is None else max(0.0, time.monotonic() - last)

    def shell_claims_banners(self) -> bool:
        """True while a shell polled /state recently (within the claim TTL).

        Consulted for every notification, so this is where a lapse (a shell that
        stopped polling) is noticed and logged — once per lapse."""
        now = time.monotonic()
        with self._shell_claim_lock:
            last = getattr(self, "_shell_last_seen", None)
            live = last is not None and now - last < self._SHELL_CLAIM_TTL_S
            report_lapse = (
                not live
                and last is not None
                and not getattr(self, "_shell_claim_lapse_logged", False)
            )
            if report_lapse:
                self._shell_claim_lapse_logged = True
            gen = getattr(self, "_shell_gen", None)
        if report_lapse:
            claim_log.warning(
                "notification shell claim lapsed gen=%s last_claim_age=%.0fs (ttl %.0fs)",
                gen,
                now - last,
                self._SHELL_CLAIM_TTL_S,
            )
        return live

    def shell_owns_claim(self, shell_gen: str | None) -> bool:
        with self._shell_claim_lock:
            return (
                shell_gen is not None
                and shell_gen == getattr(self, "_shell_gen", None)
                and time.monotonic() - getattr(self, "_shell_last_seen", -1e9)
                < self._SHELL_CLAIM_TTL_S
            )

    STATE_WAIT_MAX_MS = 25_000

    def _wait_state(self, after: int, wait_ms: int) -> dict:
        """Long-poll /state (contract C2): return at once when the version
        already differs from ``after``, else block until it changes or
        ``wait_ms`` (clamped to STATE_WAIT_MAX_MS) elapses. A waiter counts as
        a live /state reader, so the ticker keeps animating for it."""
        timeout = min(self.STATE_WAIT_MAX_MS, max(0, wait_ms)) / 1000.0
        with self._lock:
            self._state_waiters += 1
        try:
            self._note_state_read()
            self._refresh_if_stale()
            with self._lock:
                self._state_changed.wait_for(lambda: self._version != after, timeout=timeout)
        finally:
            with self._lock:
                self._state_waiters -= 1
        return self._state()

    def _state(self) -> dict:
        self._note_state_read()
        self._refresh_if_stale()
        with self._lock:
            state = {
                "version": self._version,
                "slots": self._slots,
                "has_panel": self._panel is not None,
                "panel": self._panel_ver,
                "tiles": dict(self._tile_ver),
                "tile_sections": dict(self._tile_sections),
                "tile_labels": {
                    i: label for i, label in self._tile_labels.items() if i in self._tile_ver
                },
                # Notifications are NOT part of /state: the shell long-polls
                # /notifications (acknowledged feed). The old "notify" mirror
                # had no consumer and copied the whole feed on every poll.
            }
            state.update(self._state_meta_locked())
            return state

    @property
    def config_error(self) -> str | None:
        """Why the existing config does not load (the deck shows an error state
        instead of agents), or None. Never carries a token value."""
        if self._source.source_name != "config_error":
            return None
        return getattr(self._source, "message", None)

    def _health(self) -> dict:
        health = {
            "ok": True,
            "source": self._source.source_name,
            "connected": self._source.connected,
            "server_id": self._source.server_id,
        }
        config_error = self.config_error
        if config_error is not None:
            health["config_error"] = config_error
        connections = getattr(self._source, "connections", None)
        if isinstance(connections, dict):
            health["connections"] = connections
            health["server_ids"] = list(connections)
        # "Why is the deck dark?" — everything below is token-gated like the
        # route itself and carries no secrets (ids, versions, error text).
        health["version"] = __version__
        health["protocol"] = WIRE_PROTOCOL
        health["pid"] = os.getpid()
        health["uptime_s"] = int(time.monotonic() - getattr(self, "_started_at", time.monotonic()))
        server_health = getattr(self._source, "server_health", None)
        if callable(server_health):
            health["servers"] = server_health()
        usage_health = getattr(getattr(self, "_usage_poller", None), "health", None)
        if callable(usage_health):
            health["usage"] = usage_health()
        stats = getattr(self._source, "notification_stats", None)
        if callable(stats):
            health["notifications"] = stats()
        for sink in list(getattr(self, "_sinks", [])):
            sink_health = getattr(sink, "health", None)
            if callable(sink_health):
                health["d200"] = sink_health()
                break
        return health

    @property
    def maintenance(self):
        """The Maintenance facade behind /maintenance* (deckapp/maintenance.py);
        built on first use so a test can install its own beforehand."""
        existing = getattr(self, "_maintenance", None)
        if existing is None:
            from .maintenance import Maintenance

            existing = self._maintenance = Maintenance(self)
        return existing

    # /setup is polled by the desktop onboarding card. Its disk facts (two
    # TOML reads, the onboarding marker, a sessions glob and one socket probe
    # per session) are cached while the files they come from are unchanged,
    # for at most this long so a started/stopped Herdr still shows up quickly.
    _SETUP_CACHE_TTL_S = 2.0

    def _invalidate_setup_cache(self) -> None:
        self._setup_cache = None

    def _setup_signature(self, config_path) -> tuple:
        from .onboarding import state_path

        herdr_dir = Path.home() / ".config" / "herdr"
        paths = (
            config_path,
            getattr(self._config_service, "_local_path", None),
            state_path(config_path),
            herdr_dir,
            herdr_dir / "sessions",
        )
        stats = []
        for path in paths:
            try:
                st = os.stat(path) if path is not None else None
            except OSError:
                st = None
            stats.append((st.st_mtime_ns, st.st_size, st.st_ino) if st else None)
        env = tuple(
            os.environ.get(name)
            for name in ("HERDR_SOCKET", "HERDR_SOCKET_PATH", "HERDR_SESSION", "HERDECK_MOCK")
        )
        return (str(config_path), tuple(stats), env)

    def _setup_disk_facts(self, config_path) -> dict:
        from ..bootstrap import resolve_saved_socket_path
        from .onboarding import read_choice
        from .sessions import discover_local_sessions

        signature = self._setup_signature(config_path)
        now = time.monotonic()
        cached = self._setup_cache
        if (
            cached is not None
            and cached[0] == signature
            and now - cached[1] < self._SETUP_CACHE_TTL_S
        ):
            return cached[2]
        facts = {
            "socket_path": resolve_saved_socket_path(config_path),
            "local_sessions": discover_local_sessions(
                getattr(self._config_service, "_local_path", None)
            ),
            "choice": read_choice(config_path),
            "saved_remote": _has_saved_remote(self._config_service),
        }
        self._setup_cache = (signature, now, facts)
        return facts

    def _setup_status(self) -> dict:
        config_path = str(self._config_service._config_path) if self._config_service else None
        facts = self._setup_disk_facts(config_path)
        socket_path = facts["socket_path"]
        local_sessions = facts["local_sessions"]
        selected_sessions = [session for session in local_sessions if session.selected]
        socket_exists = any(session.available for session in local_sessions)
        selected_socket_exists = any(session.available for session in selected_sessions)
        if selected_sessions:
            socket_path = selected_sessions[0].socket_path
        choice = facts["choice"]
        live = self._source.source_name == "live"
        if live:
            local_ids = set(getattr(self, "_local_bridges", {}))
            source_ids = set(getattr(self._source, "server_ids", []))
            if local_ids and source_ids - local_ids:
                mode = "mixed"
            elif local_ids:
                mode = "local"
            else:
                mode = "remote"
            reason = None
        elif self._source.source_name == "config_error":
            # Not first-run onboarding: the config exists and must be fixed
            # (Settings / Maintenance); the deck window shows the error.
            mode = "error"
            reason = "config_error"
        else:
            mode = "mock"
            if os.environ.get("HERDECK_MOCK"):
                reason = "mock_env"
            elif choice == "demo":
                reason = "demo"
            elif choice == "local" and not selected_socket_exists:
                reason = "local_unavailable"
            else:
                reason = "first_run"
        return {
            "mode": mode,
            "connected": self._source.connected,
            "reason": reason,
            "local_herdr_available": socket_exists,
            "saved_remote_available": facts["saved_remote"],
            "choice": choice,
            "socket_path": socket_path,
            "local_sessions": [session.public() for session in local_sessions],
            "connections": getattr(self._source, "connections", {}),
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
                    if self.headers.get("X-Herdeck-Shell") == "1":
                        app.note_shell_claim(self.headers.get("X-Herdeck-Shell-Gen"))
                    params = parse_qs(url.query)
                    if "after" in params:
                        try:
                            after = int(params["after"][0])
                            wait_ms = int(params.get("wait_ms", ["0"])[0])
                        except (TypeError, ValueError):
                            self._send(400)
                            return
                        state = app._wait_state(after, wait_ms)
                    else:
                        state = app._state()
                    self._send(200, json.dumps(state).encode(), "application/json")
                elif path == "/notifications":
                    if not self._require_query_token(url):
                        return
                    wait = getattr(app._source, "notifications_feed_wait", None)
                    if not callable(wait):
                        self._send(404)
                        return
                    shell_gen = self.headers.get("X-Herdeck-Shell-Gen")
                    if self.headers.get("X-Herdeck-Shell") == "1":
                        app.note_shell_claim(shell_gen)
                        app.note_shell_features(self.headers.get("X-Herdeck-Shell-Features"))
                    params = parse_qs(url.query)
                    generation = params.get("generation", [None])[0] or None
                    try:
                        after = max(0, int(params.get("after", ["0"])[0]))
                        wait_ms = min(30_000, max(0, int(params.get("wait_ms", ["25000"])[0])))
                    except (TypeError, ValueError):
                        self._send(400)
                        return
                    state = wait(generation, after, timeout=wait_ms / 1000.0)
                    if shell_gen is not None and not app.shell_owns_claim(shell_gen):
                        self._send(409)
                        return
                    self._send(200, json.dumps(state).encode(), "application/json")
                elif path == "/health":
                    if not self._require_query_token(url):
                        return
                    self._send(200, json.dumps(app._health()).encode(), "application/json")
                elif path == "/maintenance":
                    if not self._require_query_token(url):
                        return
                    self._send(
                        200, json.dumps(app.maintenance.status()).encode(), "application/json"
                    )
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
                elif path.startswith("/agent/"):
                    if not self._require_query_token(url):
                        return
                    code, payload = agent_card.handle_get(app._source, path, parse_qs(url.query))
                    self._send_agent(code, payload)
                elif bridge_update.route_server_id(path) is not None:
                    # Bridge self-update status long-poll (bridge_update.py).
                    if not self._require_query_token(url):
                        return
                    code, payload = bridge_update.handle_get(
                        app._source, path, parse_qs(url.query)
                    )
                    self._send_agent(code, payload)
                else:
                    self._send(404)

            def _send_agent(self, code, payload):
                if payload is None:
                    self._send(code)
                else:
                    self._send(code, json.dumps(payload).encode(), "application/json")

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
                elif path == "/triage":
                    if not self._require_header_token():
                        return
                    self._send(204) if app.triage() else self._send(404)
                elif path == "/agents/drill":
                    if not self._require_header_token():
                        return
                    body = self._json_body()
                    if body is _BAD_BODY:
                        return
                    ref = _agent_ref(body)
                    if ref is None:
                        self._send(400)
                        return
                    self._send(204) if app.open_agent(*ref) else self._send(404)
                elif path == "/agents/answer":
                    if not self._require_header_token():
                        return
                    body = self._json_body()
                    if body is _BAD_BODY:
                        return
                    ref = _agent_ref(body)
                    episode = body.get("episode")
                    answer = {k: body[k] for k in ("choice", "sig", "text") if k in body}
                    if (
                        ref is None
                        or not isinstance(episode, str)
                        or not episode
                        or not all(isinstance(v, str) for v in answer.values())
                    ):
                        self._send(400)
                        return
                    result = app.answer_agent(*ref, episode, **answer)
                    self._send(_ANSWER_STATUS.get(result, 500))
                elif path == "/notifications/ack":
                    if not self._require_header_token():
                        return
                    ack = getattr(app._source, "notifications_feed_ack", None)
                    if not callable(ack):
                        self._send(404)
                        return
                    body = self._json_body()
                    if body is _BAD_BODY:
                        return
                    generation = body.get("generation")
                    seq = body.get("seq")
                    if (
                        not isinstance(generation, str)
                        or not generation
                        or not isinstance(seq, int)
                        or isinstance(seq, bool)
                    ):
                        self._send(400)
                        return
                    if not ack(generation, seq):
                        self._send(409)
                        return
                    self._send(204)
                elif path == "/notifications/fallback":
                    if not self._require_header_token():
                        return
                    fallback = getattr(app._source, "notifications_feed_fallback", None)
                    if not callable(fallback):
                        self._send(404)
                        return
                    body = self._json_body()
                    if body is _BAD_BODY:
                        return
                    generation = body.get("generation")
                    seq = body.get("seq")
                    shell_gen = body.get("shell_gen")
                    if (
                        not isinstance(generation, str)
                        or not generation
                        or not isinstance(seq, int)
                        or isinstance(seq, bool)
                        or not isinstance(shell_gen, str)
                        or not shell_gen
                    ):
                        self._send(400)
                        return
                    if not app.shell_owns_claim(shell_gen):
                        self._send(409)
                        return
                    error = body.get("error")
                    claim_log.warning(
                        "notification fallback=osascript reason=shell_native_failed "
                        "id=%s:%s error=%s",
                        generation,
                        seq,
                        (error[:300] if isinstance(error, str) and error else "unknown"),
                    )
                    try:
                        delivered = fallback(generation, seq)
                    except Exception:
                        log.warning(
                            "notification fallback failed id=%s:%s",
                            generation,
                            seq,
                            exc_info=True,
                        )
                        self._send(502)
                        return
                    if not delivered:
                        self._send(409)
                        return
                    self._send(204)
                elif path.startswith("/agent/"):
                    # Desktop agent card actions (agent_card.py). A card action
                    # waits up to CARD_REPLY_TIMEOUT_S for the bridge reply.
                    if not self._require_header_token():
                        return
                    body = self._json_body()
                    if body is _BAD_BODY:
                        return
                    code, payload = agent_card.handle_post(app._source, path, body)
                    self._send_agent(code, payload)
                elif bridge_update.route_server_id(path) is not None:
                    # POST /maintenance/servers/{id}/update: ask that bridge to
                    # update itself to this runtime's version (bridge_update.py).
                    if not self._require_header_token():
                        return
                    body = self._json_body()
                    if body is _BAD_BODY:
                        return
                    code, payload = bridge_update.handle_post(app._source, path, body)
                    self._send_agent(code, payload)
                elif path in ("/maintenance/deck/restart", "/maintenance/deck/power-cycle"):
                    if not self._require_header_token():
                        return
                    if self._json_body() is _BAD_BODY:
                        return
                    action = (
                        app.maintenance.restart_deck
                        if path.endswith("/restart")
                        else app.maintenance.power_cycle
                    )
                    self._send(200, json.dumps(action()).encode(), "application/json")
                elif path == "/setup/connect":
                    if not self._require_header_token():
                        return
                    body = self._json_body()
                    if body is _BAD_BODY:
                        return
                    with app._setup_lock:  # serialize concurrent connects (ThreadingHTTPServer)
                        try:
                            result = connect(app, body)
                        finally:
                            app._invalidate_setup_cache()
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


def _default_icons(overrides_dir: str | None = None):
    """The shared IconProvider, configured for the mock: no network fetch, so the
    deck renders deterministically and offline (bundled SVG assets, else a letter
    glyph). Reuses herdeck.icons — no rendering logic is reimplemented here.

    When running frozen (PyInstaller bundle) bundled glyphs are served from the
    pre-baked PNGs (resvg only renders unbaked SVGs): pass BOTH the PNG rasterizer and the bundled
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
            overrides_dir=(
                os.path.abspath(os.path.expanduser(overrides_dir)) if overrides_dir else None
            ),
            fetch=lambda slug: None,  # offline-first when frozen
            rasterize=make_png_rasterizer(baked),
            assets_dir=baked,
        )
    cache = os.path.join(tempfile.gettempdir(), "herdeck-deckapp-icons")
    return IconProvider(
        cache_dir=cache,
        slug_map=DEFAULT_AGENT_SLUGS,
        overrides_dir=(
            os.path.abspath(os.path.expanduser(overrides_dir)) if overrides_dir else None
        ),
        fetch=lambda slug: None,  # mock stays offline + deterministic
    )


def _device_local_hardware(config_service=None):
    import tomllib
    from pathlib import Path

    from ..settings import load_local_hardware

    local_path = getattr(config_service, "_local_path", None) or _default_config_paths()[1]
    hardware = load_local_hardware(local_path)
    try:
        data = tomllib.loads(Path(local_path).read_text(encoding="utf-8"))
        section = data.get("hardware")
        explicit_tick = isinstance(section, dict) and "tick_interval" in section
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        explicit_tick = False
    if not explicit_tick:
        # Mock/demo mode stays deterministic unless the user explicitly opts
        # into a ticker in local.toml.
        hardware.tick_interval = 0.0
    return hardware


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

    hardware = _device_local_hardware(config_service)
    return DeckApp(
        MockSource(hardware),
        host=host,
        port=port,
        icon_provider=icon_provider,
        serve=serve,
        tick_interval=hardware.tick_interval,
        config_service=config_service,
        reloader=reloader,
    )


def select_live():
    """Decide live vs mock from the on-disk config + bridge-token presence.

    Returns ``(config, first_server)`` for compatibility; LiveSource connects
    every selected server in ``config.servers``. Returns ``None`` to fall back to
    the deterministic mock. Mock wins when ``HERDECK_MOCK`` is set, when no config
    file is discovered, or when the resolved server has no bridge token. None does
    NOT mean "show the demo": callers ask ``_config_load_error()`` whether an
    existing config failed to load and then show the error state instead.
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
        # no live target; _config_load_error() turns it into the error state.
        return None
    if not config.servers:
        return None
    server = config.servers[0]
    if not server.token:
        return None
    return (config, server)


def _config_load_error():
    """Why an EXISTING config file cannot be loaded, or None.

    None when it loads, when there is no config file at all (first run keeps
    its onboarding) and under ``HERDECK_MOCK``. Otherwise the ConfigError /
    OSError itself: its message names the problem without any token value.
    A broken config must never fall back to demo agents that look healthy."""
    if os.environ.get("HERDECK_MOCK"):
        return None
    from ..bootstrap import _discover_config_path, _discover_local_config_path
    from ..config import ConfigError
    from ..settings import load_settings, resolve_profile

    path = _discover_config_path()
    if not path or not os.path.exists(path):
        return None
    try:
        snapshot = load_settings(path, _discover_local_config_path(path))
        resolve_profile(snapshot)
    except (ConfigError, OSError) as exc:
        return exc
    return None


def _config_error_source(error, config_service=None):
    from .config_error import config_error_source

    return config_error_source(
        error, _default_config_paths()[0], _device_local_hardware(config_service)
    )


def _fallback_source(config_service=None):
    """The source when no live target resolves: the config-error screen for a
    config that exists but does not load, else the demo mock (no config)."""
    error = _config_load_error()
    if error is not None:
        return _config_error_source(error, config_service)
    from .config_error import log_config_error
    from .mock import MockSource

    log_config_error(None)
    return MockSource(_device_local_hardware(config_service))


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


def select_source_kind(
    *, mock_env, remote, choice, socket_path, socket_exists, config_error=None
):
    """Pure source-selection precedence over already-gathered facts.

    Returns ("remote", config, server) | ("local", socket_path) | ("mock", reason)
    | ("error", exc). ``config_error`` is why an EXISTING config does not load:
    it yields the explicit error state, never the demo — only HERDECK_MOCK, a
    demo choice or no config at all (first run) pick the mock.
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
    if config_error is not None:
        return ("error", config_error)
    return ("mock", "first_run")


def _resolve_source_kind():
    """Gather the facts and apply select_source_kind."""
    from ..bootstrap import resolve_saved_socket_path
    from .onboarding import read_choice
    from .sessions import selected_local_sessions, socket_alive

    config_path, local_path = _default_config_paths()
    socket_path = resolve_saved_socket_path(config_path)
    choice = read_choice(config_path)
    socket_exists = socket_alive(socket_path)  # a stale socket after a crash is not "local"
    exact_session_override = any(
        os.environ.get(name)
        for name in ("HERDR_SOCKET", "HERDR_SOCKET_PATH", "HERDR_SESSION")
    )
    if choice == "local" and not socket_exists and not exact_session_override:
        selected = selected_local_sessions(local_path)
        if selected:
            socket_path = selected[0].socket_path
            socket_exists = True
    mock_env = bool(os.environ.get("HERDECK_MOCK"))
    remote = select_live()
    # Only probed when it can decide the outcome (no mock env / marker / live target).
    config_error = (
        _config_load_error()
        if not mock_env and choice not in ("local", "demo") and remote is None
        else None
    )
    kind = select_source_kind(
        mock_env=mock_env,
        remote=remote,
        choice=choice,
        socket_path=socket_path,
        socket_exists=socket_exists,
        config_error=config_error,
    )
    if kind[0] != "error":
        from .config_error import log_config_error

        log_config_error(None)
    return kind


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
    include_selected_locals: bool = True,
) -> DeckApp:
    """Build a serving DeckApp backed by a LiveSource (real bridge via Connector).

    Uses a real wall clock so elapsed-time tile text advances (the mock pins it for
    determinism). ``connector_factory`` is injectable for tests (no real bridge).
    """
    import time

    from .live import build_live_source

    local_runners = {}
    if config_service is not None and include_selected_locals:
        source, local_runners = _build_remote_source_with_selected_locals(
            config,
            config_service=config_service,
            connector_factory=connector_factory,
        )
    else:
        kwargs = {} if connector_factory is None else {"connector_factory": connector_factory}
        source = build_live_source(config, server, **kwargs)
    app = DeckApp(
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
    if local_runners:
        app._set_local_bridges(local_runners)
    # The shell claims banner duty only while it polls /state with a
    # granted notification permission; otherwise the runtime falls back
    # to plain osascript alerts.
    app._wire_notify_gate(source)
    return app


# POST /agents/answer outcome -> HTTP status. 409 tells the shell the banner
# is stale (it then opens the agent's drill instead).
_ANSWER_STATUS = {
    "ok": 204,
    "invalid": 400,
    "unknown": 404,
    "unsupported": 404,
    "stale": 409,
    "unavailable": 503,
}


def _agent_ref(body: dict) -> tuple[str, str] | None:
    """``(server_id, pane_id)`` from a JSON body, or None when malformed."""
    server_id, pane_id = body.get("server_id"), body.get("pane_id")
    if isinstance(server_id, str) and server_id and isinstance(pane_id, str) and pane_id:
        return server_id, pane_id
    return None


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
    if kind[0] == "error":
        return _config_error_source(kind[1])
    from .mock import MockSource

    return MockSource(_device_local_hardware())


def _remote_reloader(app):
    """Reload a remote/mixed fleet, including device-local session selection."""

    def reload_() -> None:
        selected = select_live()
        if selected is None:
            # A config that broke under a running remote deck shows the error,
            # not demo agents; only a removed config falls back to the mock.
            app.swap_source(_fallback_source(app._config_service))
            app._set_local_bridges({})
            return
        config, _server = selected
        source, runners = _build_remote_source_with_selected_locals(
            config,
            config_service=app._config_service,
        )
        try:
            app.swap_source(source)
        except Exception:
            source.close()
            for runner in runners.values():
                runner.close()
            raise
        app._set_local_bridges(runners)

    return reload_


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


def _start_local_session_bridges(
    sessions,
    *,
    partial=None,
    runner_factory=None,
):
    """Start one embedded loopback bridge per selected local Herdr session.

    Returns ``(combined_config, runners)``. If any bridge fails, every bridge
    started by this call is closed before the error escapes.
    """
    import dataclasses

    from ..config import ServerConfig
    from .local_bridge import LocalBridgeRunner

    runners: dict[str, object] = {}
    local_servers: list[ServerConfig] = []
    used_ids = {server.id for server in (partial.servers if partial is not None else [])}
    try:
        for session in sessions:
            server_id = session.server_id
            if server_id in used_ids:
                base = server_id
                suffix = 2
                while f"{base}:{suffix}" in used_ids:
                    suffix += 1
                server_id = f"{base}:{suffix}"
            used_ids.add(server_id)
            runner = (runner_factory or LocalBridgeRunner)(session.socket_path)
            runner._herdeck_session_name = session.name
            try:
                _host, port, token = runner.start()
            except Exception:
                runner.close()
                raise
            runners[server_id] = runner
            local_servers.append(
                ServerConfig(server_id, f"ws://127.0.0.1:{port}", token)
            )
    except Exception:
        for runner in runners.values():
            runner.close()
        raise

    if not local_servers:
        if partial is None:
            raise ValueError("no local sessions selected")
        return partial, runners

    if partial is None:
        from ..bootstrap import local_config

        port = int(local_servers[0].url.rsplit(":", 1)[1])
        base = local_config(port, local_servers[0].token)
    else:
        base = partial
    servers = [*local_servers, *(partial.servers if partial is not None else [])]
    order = [server.id for server in servers]
    return dataclasses.replace(base, servers=servers, overview_order=order), runners


def _explicit_selected_local_sessions(config_service):
    """Selected, available sessions when the user saved an explicit selection."""
    from .sessions import (
        has_explicit_local_session_selection,
        selected_local_sessions,
    )

    local_path = getattr(config_service, "_local_path", None)
    if not has_explicit_local_session_selection(local_path):
        return []
    return selected_local_sessions(local_path)


def _build_remote_source_with_selected_locals(
    config,
    *,
    config_service,
    connector_factory=None,
):
    """Build a fleet source from remote config plus explicitly selected locals."""
    sessions = _explicit_selected_local_sessions(config_service)
    runners = {}
    combined = config
    if sessions:
        combined, runners = _start_local_session_bridges(sessions, partial=config)
    kwargs = {} if connector_factory is None else {"connector_factory": connector_factory}
    try:
        source = build_live_source_for_connect(combined, None, **kwargs)
    except Exception:
        for runner in runners.values():
            runner.close()
        raise
    return source, runners


def _local_reloader(app):
    """Reload local-only mode, expanding an explicit multi-session selection."""
    import dataclasses

    from ..bootstrap import local_config

    def reload_() -> None:
        sessions = _explicit_selected_local_sessions(
            getattr(app, "_config_service", None)
        )
        if sessions:
            existing = dict(getattr(app, "_local_bridges", {}))
            wanted = {session.server_id for session in sessions}
            if existing and set(existing) == wanted:
                from ..config import ServerConfig

                servers = []
                for server_id, runner in existing.items():
                    bound = getattr(runner, "bound", None)
                    if bound is None:
                        break
                    _host, port, token = bound
                    servers.append(
                        ServerConfig(server_id, f"ws://127.0.0.1:{port}", token)
                    )
                else:
                    partial = _load_partial_config()
                    if partial is None:
                        config = local_config(
                            int(servers[0].url.rsplit(":", 1)[1]),
                            servers[0].token,
                        )
                    else:
                        config = dataclasses.replace(
                            partial, servers=[], overview_order=[]
                        )
                    config = dataclasses.replace(
                        config,
                        servers=servers,
                        overview_order=[server.id for server in servers],
                    )
                    new_source = build_live_source_for_connect(config, config.servers[0])
                    try:
                        app.swap_source(new_source)
                    except Exception:
                        new_source.close()
                        raise
                    return

            partial = _load_partial_config()
            if partial is not None:
                partial = dataclasses.replace(partial, servers=[], overview_order=[])
            config, runners = _start_local_session_bridges(sessions, partial=partial)
            new_source = build_live_source_for_connect(config, config.servers[0])
            try:
                app.swap_source(new_source)
            except Exception:
                new_source.close()
                for runner in runners.values():
                    runner.close()
                raise
            app._set_local_bridges(runners)
            return

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


def _mock_reloader(app, kind, select_source):
    """Keep explicit mock/demo mode sticky across unrelated reloads.

    A genuinely new Settings selection may promote mock to local live, while a
    mock caused by an unavailable saved local session promotes itself when the
    socket appears. ``HERDECK_MOCK`` always wins.
    """
    from .sessions import (
        discover_local_sessions,
        has_explicit_local_session_selection,
    )

    config_service = getattr(app, "_config_service", None)
    local_path = getattr(config_service, "_local_path", None)

    def fingerprint() -> tuple[bool, tuple[str, ...]]:
        return (
            has_explicit_local_session_selection(local_path),
            tuple(
                session.name
                for session in discover_local_sessions(local_path)
                if session.selected
            ),
        )

    last_fingerprint = fingerprint()
    pending_fingerprint = None

    def reload_() -> None:
        nonlocal last_fingerprint, pending_fingerprint

        reason = kind[1] if len(kind) > 1 else None
        current_fingerprint = fingerprint()
        selection_changed = current_fingerprint != last_fingerprint
        last_fingerprint = current_fingerprint
        if selection_changed:
            explicit, names = current_fingerprint
            pending_fingerprint = current_fingerprint if explicit and names else None
        should_try_local = reason != "mock_env" and (
            reason == "local_unavailable"
            or selection_changed
            or pending_fingerprint == current_fingerprint
        )
        sessions = (
            _explicit_selected_local_sessions(config_service)
            if should_try_local
            else []
        )
        if not sessions:
            app.swap_source(select_source())
            return

        import dataclasses

        partial = _load_partial_config()
        if partial is not None:
            partial = dataclasses.replace(partial, servers=[], overview_order=[])
        config, runners = _start_local_session_bridges(sessions, partial=partial)
        new_source = None
        try:
            new_source = build_live_source_for_connect(config, config.servers[0])
            app.swap_source(new_source)
        except Exception:
            if new_source is not None:
                new_source.close()
            for runner in runners.values():
                runner.close()
            raise
        app._set_local_bridges(runners)
        pending_fingerprint = None
        app._reloader = _reloader_for(app, ("local",), select_source)

    return reload_


def _reloader_for(app, kind, select_source):
    """The config-watch reloader for the built source. LOCAL mode rebuilds the
    live source against the running embedded bridge (the bridge lifecycle stays
    owned by create_app startup + /setup/connect); mock/remote re-select from
    disk."""
    if kind[0] == "local":
        return _local_reloader(app)
    if kind[0] == "remote":
        return _remote_reloader(app)
    return _mock_reloader(app, kind, select_source)


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


def build_live_source_for_connect(config, server=None, **kwargs):
    from .live import build_live_source

    return build_live_source(config, server, **kwargs)


# Stable machine-readable ids for /setup* failures (contract C3). Every
# {"ok": False} response keeps its human "error" message and adds one of these
# as "code"; the desktop maps known codes to localized text and falls back to
# the raw message. Keep this list in sync with the _fail() call sites:
#   demo_switch_failed          switching to the demo deck failed
#   unknown_local_session       a requested local session name does not exist
#   no_session_selected         nothing running selected and no saved connection
#   connections_build_failed    starting the selected local/saved connections failed
#   config_read_failed          config.toml / local.toml could not be read
#   selection_save_failed       persisting the session selection failed
#   herdr_socket_not_found      no live herdr socket at the resolved path
#   herdr_too_old               herdr lacks session.snapshot (run `herdr update`)
#   local_start_failed          the embedded local bridge could not start
#   token_env_conflict          HERDECK_<ID>_TOKEN in the env shadows the typed token
#   bad_token                   the bridge rejected the token (probe)
#   bridge_unreachable          the bridge URL did not answer (probe)
#   config_unreadable           the existing config is not valid TOML
#   config_malformed            the existing config has a malformed [[servers]] list
#   token_env_in_use            the derived token env name is used elsewhere
#   server_not_in_profile       the active profile does not include this server
#   remote_build_failed         building the remote live source failed
#   token_read_failed           the keychain could not be read
#   token_store_failed          the keychain could not store the token
#   config_write_failed         writing config.toml failed
#   config_invalid              the merged config failed validation
#   onboarding_finalize_failed  clearing the onboarding marker failed
#   no_saved_connection         "use saved" but no resolvable saved server
#   saved_restore_failed        reconnecting the saved server failed
SETUP_ERROR_CODES: frozenset[str] = frozenset(
    {
        "demo_switch_failed",
        "unknown_local_session",
        "no_session_selected",
        "connections_build_failed",
        "config_read_failed",
        "selection_save_failed",
        "herdr_socket_not_found",
        "herdr_too_old",
        "local_start_failed",
        "token_env_conflict",
        "bad_token",
        "bridge_unreachable",
        "config_unreadable",
        "config_malformed",
        "token_env_in_use",
        "server_not_in_profile",
        "remote_build_failed",
        "token_read_failed",
        "token_store_failed",
        "config_write_failed",
        "config_invalid",
        "onboarding_finalize_failed",
        "no_saved_connection",
        "saved_restore_failed",
    }
)


def _fail(code: str, error: str) -> dict:
    """A /setup* failure body: human message + stable ``code`` (C3)."""
    assert code in SETUP_ERROR_CODES, code
    return {"ok": False, "error": error, "code": code}


_PROBE_CODES = {"bad_token": "bad_token", "unreachable": "bridge_unreachable"}


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
        new_source = MockSource(_device_local_hardware(app._config_service))
        try:
            prepared = app._prepare_swap(new_source)  # render mock (fallible) BEFORE persisting
            write_choice(config_path, "demo")  # persist
        except Exception:
            _restore_choice(config_path, prior_choice)
            new_source.close()
            return _fail("demo_switch_failed", "could not switch to demo")
        app._commit_swap(new_source, prepared)  # assignment-only
        app._set_local_bridge(None)
        app._reloader = _reloader_for(app, ("mock",), _select_source)  # mock/remote reloads resume
        return {"ok": True}

    if choice == "sessions":
        import dataclasses

        from .sessions import discover_local_sessions

        names = body.get("sessions")
        include_saved = body.get("include_saved") is True
        if not isinstance(names, list) or not all(
            isinstance(name, str) and name for name in names
        ):
            return None
        discovered = discover_local_sessions(app._config_service._local_path)
        by_name = {session.name: session for session in discovered}
        unknown = [name for name in names if name not in by_name]
        if unknown:
            return _fail("unknown_local_session", f"unknown local session: {unknown[0]}")
        selected_sessions = [
            by_name[name] for name in dict.fromkeys(names) if by_name[name].available
        ]
        remote = select_live() if include_saved else None
        remote_config = remote[0] if remote is not None else None
        runners = {}
        try:
            live_selection = True
            if selected_sessions:
                partial = remote_config or _load_partial_config()
                if partial is not None and remote_config is None:
                    partial = dataclasses.replace(partial, servers=[], overview_order=[])
                config, runners = _start_local_session_bridges(
                    selected_sessions,
                    partial=partial,
                )
            elif remote_config is not None:
                config = remote_config
            elif names:
                live_selection = False
                new_source = MockSource(_device_local_hardware(app._config_service))
                prepared = app._prepare_swap(new_source)
            else:
                return _fail(
                    "no_session_selected",
                    "select a running local session or include the saved connection",
                )
            if live_selection:
                new_source = build_live_source_for_connect(config, config.servers[0])
                prepared = app._prepare_swap(new_source, clock=time.monotonic)
        except Exception:
            for runner in runners.values():
                runner.close()
            return _fail("connections_build_failed", "could not build the selected connections")

        prior_choice = read_choice(config_path)
        try:
            prior_config, prior_local = _snapshot_config(app._config_service)
        except OSError:
            new_source.close()
            for runner in runners.values():
                runner.close()
            return _fail("config_read_failed", "could not read config")
        app._suppress_reload = True
        try:
            app._config_service.set_local_sessions(list(dict.fromkeys(names)))
            if remote_config is not None:
                clear_choice(config_path)
            else:
                write_choice(config_path, "local")
        except Exception:
            _restore_file(app._config_service._config_path, prior_config)
            _restore_file(app._config_service._local_path, prior_local)
            _restore_choice(config_path, prior_choice)
            new_source.close()
            for runner in runners.values():
                runner.close()
            return _fail("selection_save_failed", "could not save the selected connections")
        finally:
            watcher = getattr(app, "_watcher", None)
            if watcher is not None:
                watcher.resync()
            app._suppress_reload = False
        app._commit_swap(new_source, prepared)
        app._set_local_bridges(runners)
        kind = ("remote",) if remote_config is not None else ("local",)
        app._reloader = _reloader_for(app, kind, _select_source)
        return {
            "ok": True,
            "connected": live_selection and app._source.connected,
            "active": list(getattr(app._source, "server_ids", [])),
        }

    if choice == "local":
        from ..bootstrap import resolve_saved_socket_path
        from ..bridge import _SNAPSHOT_UNSUPPORTED

        socket_path = resolve_saved_socket_path(config_path)
        from .sessions import socket_alive

        if not socket_alive(socket_path):
            return _fail("herdr_socket_not_found", f"herdr socket not found at {socket_path}")
        new_source = None
        runner = None
        prior_choice = read_choice(config_path)  # snapshot the marker for rollback
        try:
            prior_config, prior_local = _snapshot_config(app._config_service)
        except OSError:
            return _fail("config_read_failed", "could not read config")
        try:
            config, server, runner = _start_local_bridge(socket_path)  # may raise (bridge bind)
            new_source = build_live_source_for_connect(config, server)  # build ...
            prepared = app._prepare_swap(new_source, clock=time.monotonic)  # ... pre-build orch (live clock) BEFORE the marker ...
            write_choice(config_path, "local")  # ... persist (durable)
            from .sessions import discover_local_sessions

            session_name = next(
                (
                    item.name
                    for item in discover_local_sessions(app._config_service._local_path)
                    if os.path.abspath(item.socket_path) == os.path.abspath(socket_path)
                ),
                "custom",
            )
            app._config_service.set_local_sessions([session_name])
        except Exception as exc:
            _restore_choice(config_path, prior_choice)  # undo the marker if it was written
            _restore_file(app._config_service._config_path, prior_config)
            _restore_file(app._config_service._local_path, prior_local)
            if new_source is not None:
                new_source.close()  # don't leak the built source / its connector runner
            if runner is not None:
                runner.close()  # ... or the just-started bridge; previous source untouched
            log.warning("local source start failed", exc_info=True)
            # The bridge's hard version floor (herdr < 0.7.2, no session.snapshot) carries
            # actionable guidance ('run herdr update') that must reach the desktop user
            # verbatim, not the generic message below.
            if str(exc) == _SNAPSHOT_UNSUPPORTED:
                return _fail("herdr_too_old", str(exc))
            return _fail("local_start_failed", "could not start local source")
        app._commit_swap(new_source, prepared)  # non-failing: all fallible work done; sets the live clock
        server_id = getattr(server, "id", "local")
        app._set_local_bridges({server_id: runner})  # adopt new bridge (closes old ones)
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
            return _fail(
                "token_env_conflict",
                f"{token_env} is set in the environment and would override the saved token; unset it or connect with that value",
            )
        result = _probe_sync(url, token)
        if not result.ok:
            return _fail(_PROBE_CODES.get(result.reason, "bridge_unreachable"), result.reason)
        try:
            data = app._config_service.read()  # a malformed/unreadable existing config must not 500
        except (OSError, tomllib.TOMLDecodeError):
            return _fail("config_unreadable", "existing config is unreadable — fix it in Settings")
        payload = {
            "base": dict(data.get("base") or {}),
            "profiles": data.get("profiles") or {},
            "local": data.get("local") or {},
        }
        existing = payload["base"].get("servers")
        if existing is not None and not (isinstance(existing, list) and all(isinstance(s, dict) for s in existing)):
            # parseable TOML but a wrong shape (e.g. `servers = ["bad"]`) would crash the upsert
            return _fail("config_malformed", "existing config is malformed (servers) — fix it in Settings")
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
            return _fail(
                "token_env_in_use",
                f"token env {token_env} is already used elsewhere in the config — pick a different id",
            )
        # BUILD-BEFORE-PERSIST: resolve the merged payload (placeholder tokens) to confirm
        # selection, then build the live source with the REAL token baked into the chosen
        # ServerConfig — all BEFORE mutating keychain/config, so any selection / validation
        # / build failure persists NOTHING (no orphaned secret, no serverful-but-dead config).
        config = app._config_service.resolve_config(payload, assume_present=token_env)
        selected_server = next(
            (server for server in (config.servers if config is not None else []) if server.id == server_id),
            None,
        )
        if config is None or selected_server is None:
            return _fail(
                "server_not_in_profile",
                "the active profile does not include this server (check overview_order / profile servers) — fix it in Settings",
            )
        config = dataclasses.replace(
            config,
            servers=[
                dataclasses.replace(item, token=token) if item.id == server_id else item
                for item in config.servers
            ],
        )
        local_runners = {}
        try:
            new_source, local_runners = _build_remote_source_with_selected_locals(
                config,
                config_service=app._config_service,
            )
        except Exception:
            return _fail("remote_build_failed", "could not build the remote source")
        # Persist + swap as one watcher-suppressed transaction (see _commit_remote).
        return _commit_remote(
            app,
            payload,
            token_env,
            token,
            new_source,
            config_path,
            local_runners=local_runners,
        )

    if choice == "saved":
        # One-click escape from the demo trap: re-select the on-disk remote (token from
        # the keychain) and clear the demo/local marker. Transactional like the others —
        # build + prepare BEFORE clearing the marker; any failure restores it and closes
        # the just-built source. NO _suppress_reload (this writes only onboarding.toml,
        # which the watcher does not track) and NO probe (select_live() confirms token
        # PRESENCE, not validity; the live source dials async, so connected may be False).
        remote = select_live()  # (config, server) from disk + keychain, or None
        if remote is None:
            return _fail("no_saved_connection", "no saved connection")
        config, server = remote
        prior_choice = read_choice(config_path)
        new_source = None
        local_runners = {}
        try:
            new_source, local_runners = _build_remote_source_with_selected_locals(
                config,
                config_service=app._config_service,
            )
            prepared = app._prepare_swap(new_source, clock=time.monotonic)  # render (fallible)
            clear_choice(config_path)  # persist: drop the demo/local marker
        except Exception:
            _restore_choice(config_path, prior_choice)  # marker untouched / restored
            if new_source is not None:
                new_source.close()
            for runner in local_runners.values():
                runner.close()
            return _fail("saved_restore_failed", "could not restore saved connection")
        app._commit_swap(new_source, prepared)  # assignment-only, non-failing
        app._set_local_bridges(local_runners)
        app._reloader = _reloader_for(app, ("remote",), _select_source)
        return {"ok": True, "connected": app._source.connected}

    return None  # unknown choice -> 400


def _commit_remote(
    app,
    payload,
    token_env,
    token,
    new_source,
    config_path,
    *,
    local_runners=None,
) -> dict:
    """Persist (secret-then-config) and swap to `new_source` as ONE transaction, with the
    config watcher SUPPRESSED so its mtime poll can't reload mid-commit (double-swapping
    to a second source) or swap to the half-written config during a rollback. Any failure
    restores the prior secret + config and closes the just-built source. The watcher
    baseline is resynced on exit so it doesn't fire on our own writes/restores."""
    import time

    from .. import secrets as secret_store
    from .onboarding import clear_choice

    app._suppress_reload = True
    local_runners = local_runners or {}

    def _close_new() -> None:
        new_source.close()
        for runner in local_runners.values():
            runner.close()

    try:
        # Pre-build the orchestrator (the only fallible part of the swap) BEFORE persisting,
        # so the post-persist commit (_commit_swap) is guaranteed non-throwing.
        try:
            prepared = app._prepare_swap(new_source, clock=time.monotonic)  # live clock
        except Exception:
            _close_new()
            return _fail("remote_build_failed", "could not build the remote source")
        # Snapshot the prior keychain value AND the on-disk config BEFORE any mutation, so a
        # read fault can't strand a secret, and a partial write (config ok, local faults) is
        # undone — never leaving a serverful-but-tokenless config or a destroyed prior token.
        # peek_keychain raises (not None) on a backend READ error, so we abort here rather
        # than risk erasing an existing token we couldn't actually read.
        try:
            prior_secret = secret_store.peek_keychain(token_env)
        except Exception:
            _close_new()
            return _fail("token_read_failed", "could not read the existing token — check the keychain")
        svc = app._config_service
        try:
            prior_config, prior_local = _snapshot_config(svc)
        except OSError:
            _close_new()  # nothing mutated yet
            return _fail("config_read_failed", "could not read config")
        try:
            secret_store.set_secret(token_env, token)
        except Exception:
            _restore_secret(token_env, prior_secret)  # set may have partially overwritten
            _close_new()
            return _fail("token_store_failed", "could not store token")

        def _rollback():
            _restore_file(svc._config_path, prior_config)
            _restore_file(svc._local_path, prior_local)
            _restore_secret(token_env, prior_secret)  # restore prior token, don't destroy it
            _close_new()

        try:
            errors = svc.write(payload)
        except OSError:  # atomic write can fault, possibly after a partial write
            _rollback()
            return _fail("config_write_failed", "could not write config")
        if errors:  # structural validation runs before any write, so nothing was written
            _rollback()
            return _fail("config_invalid", "; ".join(errors))
        # Clear the stale local/demo marker as PART OF THE COMMIT: remote == a usable config,
        # no opt-in marker. If the unlink faults, roll everything back so a later-removed
        # config falls to first_run (the card), never to a stale marker that would mask it.
        try:
            clear_choice(config_path)
        except OSError:
            _rollback()
            return _fail("onboarding_finalize_failed", "could not finalize onboarding")
        app._commit_swap(new_source, prepared)  # non-failing: all fallible work done; sets the live clock
        app._set_local_bridges(local_runners)
        app._reloader = _reloader_for(app, ("remote",), _select_source)  # config-edit reloads resume
        return {"ok": True, "connected": app._source.connected}  # honest: connector dials async
    finally:
        watcher = getattr(app, "_watcher", None)
        if watcher is not None:
            watcher.resync()  # adopt our writes as the baseline; no spurious reload
        app._suppress_reload = False


def _token_file_paths(config_path) -> list[str]:
    """Every ``[[servers]].token_file`` in the config (raw TOML, no secret read), so
    creating or fixing a token file reloads the deck like a config edit."""
    import tomllib

    try:
        data = tomllib.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, TypeError):
        return []
    servers = data.get("servers")
    if not isinstance(servers, list):
        return []
    return [
        os.path.expanduser(raw["token_file"].strip())
        for raw in servers
        if isinstance(raw, dict)
        and isinstance(raw.get("token_file"), str)
        and raw["token_file"].strip()
    ]


# While the deck shows a config error, re-probe the config this often: a fix
# the file watcher cannot see (a keychain entry added from the CLI) still
# recovers the deck without a restart.
CONFIG_ERROR_RETRY_S = 10.0


def _config_error_probe(app, *, interval=CONFIG_ERROR_RETRY_S, clock=time.monotonic):
    """ConfigWatcher ``state_provider``: the current config-error message while the
    deck is in the error state (re-probed at most every ``interval``), else None.
    A changed value — above all the error going away — fires the reloader."""
    last = {"at": None, "value": None}

    def probe():
        if app.source_name != "config_error":
            last["at"] = None
            return None
        now = clock()
        if last["at"] is None:
            # Just entered the error state: adopt what the source already shows.
            last["at"], last["value"] = now, app.config_error
        elif now - last["at"] >= interval:
            error = _config_load_error()
            last["at"], last["value"] = now, (str(error) or type(error).__name__) if error else None
        return last["value"]

    return probe


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
    configured, the config-error state when an existing config does not load,
    otherwise the deterministic mock. Wires up a default ConfigService
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
        sessions = _explicit_selected_local_sessions(svc)
        runners = {}
        if sessions:
            import dataclasses

            partial = _load_partial_config()
            if partial is not None:
                partial = dataclasses.replace(partial, servers=[], overview_order=[])
            config, runners = _start_local_session_bridges(sessions, partial=partial)
            server = config.servers[0]
        else:
            config, server, runner = _start_local_bridge(socket_path)
            runners = {server.id: runner}
        try:
            app = create_live_app(
                config, server, host=host, port=port, icon_provider=icon_provider,
                serve=serve, config_service=svc,
                include_selected_locals=False,
            )
        except Exception:
            for runner in runners.values():
                runner.close()
            raise
        app._set_local_bridges(runners)
    elif kind[0] == "error":
        app = DeckApp(
            _config_error_source(kind[1], svc),
            host=host,
            port=port,
            icon_provider=icon_provider,
            serve=serve,
            config_service=svc,
        )
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

    def selected_socket_paths() -> list[str]:
        from .sessions import discover_local_sessions

        service_local_path = getattr(svc, "_local_path", local_path)
        return [
            session.socket_path
            for session in discover_local_sessions(service_local_path)
            if session.selected
        ] + _token_file_paths(cfg_path)

    app._watcher = ConfigWatcher(
        watch_paths,
        app._watcher_reload,
        interval=1.0,
        adopt_before_fire=False,
        paths_provider=selected_socket_paths,
        state_provider=_config_error_probe(app),
    )
    app._watcher.start()
    return app
