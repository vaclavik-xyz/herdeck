from __future__ import annotations

import binascii
import contextlib
import hashlib
import io
import json
import logging
import os
import time
import zipfile
from collections.abc import Callable

from PIL import Image

from ..icons import TILE_VERSION, compose_panel
from .base import DeckDriver, PanelView, TileView
from .render_pump import RenderPump

log = logging.getLogger(__name__)

PANEL_W = 392
_CELL = 196
# strmdck reads tile icons here, relative to CWD:
_ICON_DIR = os.path.join(".cache", "icons", "_generated")
# the two panel cells (grid 3_2 and 4_2):
_PANEL_LEFT_INDEX = 13
_PANEL_RIGHT_INDEX = 14


class _SleeplessTime:
    """Proxy for the time module whose sleep() is a no-op; every other attribute
    passes through to the real module. Used to neutralize strmdck's retry-loop
    time.sleep(0.05) (a pure-CPU throttle between zip-rebuild attempts) without
    touching the global time.sleep."""

    def __init__(self, real):
        self._real = real

    def sleep(self, *_args, **_kwargs):
        return None

    def __getattr__(self, name):
        return getattr(self._real, name)


def _neutralize_retry_sleep() -> None:
    """strmdck's UlanziD200._prepare_zip retries zip-building in a tight loop with
    time.sleep(0.05) between attempts (working around a device firmware bug where
    bytes 0x00/0x7c at packet boundaries glitch the deck). That sleep waits on
    nothing but ran ~8-15x per set_buttons, freezing the D200 render worker
    400-800ms (occasionally seconds) on every spinner frame. Replace the module's
    time with a sleepless proxy so retries spin without delay. Idempotent and
    fail-safe."""
    try:
        import strmdck.devices.ulanzi_d200 as ud

        if isinstance(ud.time, _SleeplessTime):
            return
        ud.time = _SleeplessTime(ud.time)
    except Exception:
        log.warning("could not neutralize strmdck retry sleep; D200 spinner may stall")


# The D200 firmware glitches when the byte at file offset 1016, 1016+1024, …
# of the uploaded zip is one of these (packet-boundary bytes) — mirrored from
# strmdck's _prepare_zip.
_INVALID_CHUNK_BYTES = (0x00, 0x7C)
_FIRST_CHUNK_DATA = 1016  # first packet carries chunk_size-8 payload bytes


def _zip_chunk_bytes_valid(data: bytes) -> bool:
    return all(
        data[i] not in _INVALID_CHUNK_BYTES for i in range(_FIRST_CHUNK_DATA, len(data), 1024)
    )


def build_button_zip(manifest_bytes: bytes, icons: dict[str, bytes], *, rand=os.urandom) -> bytes:
    """In-memory replica of strmdck's on-disk page build (_prepare_zip +
    compress_folder): entries dummy.txt? → manifest.json → icons/<name>, deflate
    level 1, retried with a growing random dummy entry until no packet-boundary
    byte hits the firmware's invalid values. The stock implementation rebuilds
    the whole folder ON DISK per attempt (rmtree + copyfile per icon + rezip),
    which measured 280-530ms per frame (3.6s worst) and made the deck stutter;
    in memory the same work is single-digit ms."""
    dummy = b""
    retries = 0
    while True:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
            if dummy:
                z.writestr("dummy.txt", dummy.decode("ascii"))
            z.writestr("manifest.json", manifest_bytes)
            for name, blob in icons.items():
                z.writestr(f"icons/{name}", blob)
        data = buf.getvalue()
        if _zip_chunk_bytes_valid(data):
            return data
        retries += 1
        if retries > 64:  # never observed >~15 in the stock loop; guard runaway
            raise RuntimeError("could not build a firmware-safe button zip")
        dummy += binascii.hexlify(rand(4 * retries))


def split_panel(img: Image.Image) -> tuple[Image.Image, Image.Image]:
    left = img.crop((0, 0, _CELL, _CELL))
    right = img.crop((PANEL_W - _CELL, 0, PANEL_W, _CELL))
    return left, right


class D200Driver(DeckDriver):
    """Ulanzi D200 driver. Renders 13 tiles + a 2-cell status panel."""

    KEEP_ALIVE_INTERVAL = 5.0
    SLOW_WRITE_MS = 250.0  # device writes slower than this get a WARNING log
    BRIGHTNESS = 80
    DEBOUNCE = 0.25  # ignore repeats of the same key within this window
    _CONTROL_USAGE_PAGE = 0x0C

    def __init__(
        self,
        workdir: str | None = None,
        icon_provider=None,
        brightness: int = BRIGHTNESS,
        debounce: float = DEBOUNCE,
        keep_alive_interval: float = KEEP_ALIVE_INTERVAL,
        icons_dir: str | None = None,
    ):
        # Stable working dir so strmdck's relative .build/.cache never collide (R-4).
        self.DEBOUNCE = debounce
        self.KEEP_ALIVE_INTERVAL = keep_alive_interval
        self._last_write_ms: float | None = None
        self._last_write_count = 0
        self._last_icon: dict[int, str] = {}
        self._last_panel_key: tuple | None = None
        self._panel_names: tuple[str, str] | None = None
        self._last_frame_buttons: dict[int, dict] | None = None
        self._fast_path_ok = True
        self._icons_dir = os.path.abspath(os.path.expanduser(icons_dir)) if icons_dir else None
        self._workdir = workdir or os.path.expanduser("~/.cache/herdeck")
        self._previous_cwd = os.getcwd()
        self._pump: RenderPump | None = None
        try:
            os.makedirs(self._workdir, exist_ok=True)
            os.chdir(self._workdir)
            os.makedirs(_ICON_DIR, exist_ok=True)
            self._dev = self._open_device()
            self._callback: Callable[[int], None] | None = None
            if icon_provider is None:
                from ..icons import DEFAULT_AGENT_SLUGS, IconProvider

                icon_provider = IconProvider(
                    cache_dir=os.path.abspath(_ICON_DIR),
                    slug_map=DEFAULT_AGENT_SLUGS,
                    overrides_dir=self._icons_dir or os.path.abspath("icons"),
                )
            self._icons = icon_provider
            with contextlib.redirect_stdout(io.StringIO()):
                self._dev.set_brightness(brightness, force=True)
                self._set_panel_background_mode()
            # All device writes run on this single worker thread so blocking USB I/O
            # never stalls the event loop (which would let presses pile up and replay).
            self._pump = RenderPump(
                paint=self._paint,
                keep_alive=self._keep_alive_write,
                keep_alive_interval=self.KEEP_ALIVE_INTERVAL,
            )
            self._pump.start()
        except Exception:
            with contextlib.suppress(Exception):
                os.chdir(self._previous_cwd)
            raise

    def _open_device(self, retries: int = 5, delay: float = 1.0):
        import hid
        from strmdck.devices.ulanzi_d200 import UlanziD200Device

        _neutralize_retry_sleep()
        vid, pid = UlanziD200Device.USB_VENDOR_ID, UlanziD200Device.USB_PRODUCT_ID
        last = None
        for _ in range(retries):
            matches = [
                d for d in hid.enumerate() if (d["vendor_id"], d["product_id"]) == (vid, pid)
            ]
            paths = [d["path"] for d in matches if d.get("usage_page") == self._CONTROL_USAGE_PAGE]
            paths += [d["path"] for d in matches if d.get("usage_page") != self._CONTROL_USAGE_PAGE]
            for path in paths:
                h = hid.device()
                try:
                    h.open_path(path)
                    h.set_nonblocking(True)
                    return UlanziD200Device(h)
                except Exception as exc:
                    last = exc
                    with contextlib.suppress(Exception):
                        h.close()
            time.sleep(delay)
        raise RuntimeError(f"No openable Ulanzi D200 control interface (last: {last})")

    def _set_panel_background_mode(self):
        from strmdck.devices.ulanzi_d200 import SmallWindowMode

        with contextlib.suppress(Exception):
            # set_small_window_mode persists the mode internally; every later
            # set_small_window_data (incl. keep_alive's) then defaults to it, so
            # the window stays BACKGROUND instead of reverting to CLOCK/STATS.
            self._dev.set_small_window_mode(SmallWindowMode.BACKGROUND)
            self._dev.set_small_window_data({"mode": SmallWindowMode.BACKGROUND}, force=True)

    def slot_count(self) -> int:
        return self._dev.BUTTON_COUNT  # 13

    def _tile_buttons(self, tiles: list[TileView]) -> dict[int, dict]:
        buttons: dict[int, dict] = {}
        for t in tiles:
            if t.index >= self._dev.BUTTON_COUNT:
                continue
            icon = self._icons.render_tile(t)
            # name="" so the device draws no label of its own — all text is
            # baked into the icon with our own font.
            buttons[t.index] = {"name": "", "icon": icon}
        return buttons

    def render(self, tiles: list[TileView]) -> None:
        self._submit("tiles", list(tiles))

    def render_frame(self, tiles: list[TileView], panel: PanelView) -> None:
        """Render tiles + panel as ONE full-set device write (indices 0-14).

        The panel cells (13/14) share the button namespace with the tiles, so a
        13-tile full set actually CLEARS them until the follow-up panel write
        repaints them ~one transaction later — two zip preps + two USB uploads
        per frame and a visible panel blink. One combined set is atomic, halves
        the per-frame cost, and lets identical frames be skipped entirely."""
        self._submit("frame", (list(tiles), panel))

    def render_panel(self, panel: PanelView) -> None:
        self._submit("panel", panel)

    def render_working(self, tiles: list[TileView]) -> None:
        """Partial re-render of just the working tiles (spinner)."""
        self._submit("working", list(tiles))

    def _submit(self, channel: str, payload) -> None:
        # Hand the latest frame to the render worker and return — the blocking USB
        # write happens off the event loop. Falls back to inline paint if the worker
        # isn't up (before start / after close), so behavior degrades gracefully.
        if self._pump is not None:
            self._pump.submit(channel, payload)
        else:
            self._paint(channel, payload)

    def _paint(self, channel: str, payload) -> None:
        if channel == "tiles":
            self._write_tiles(payload)
        elif channel == "panel":
            self._write_panel(payload)
        elif channel == "working":
            self._write_working(payload)
        elif channel == "frame":
            tiles, panel = payload
            self._write_frame(tiles, panel)

    def _diff(self, buttons: dict[int, dict]) -> dict[int, dict]:
        """Keep only buttons whose icon filename differs from the last write."""
        return {
            i: b for i, b in buttons.items() if b.get("icon") != self._last_icon.get(i)
        }

    def _record(self, buttons: dict[int, dict]) -> None:
        for i, b in buttons.items():
            icon = b.get("icon")
            if icon is not None:
                self._last_icon[i] = icon

    def _timed_set_buttons(
        self, channel: str, buttons: dict[int, dict], *, update_only: bool
    ) -> bool:
        """Write buttons to the device, timing and logging the USB write. Returns
        True on success; False if the device write raised (the caller must then
        treat the buttons as not-yet-applied)."""
        # NOTE: strmdck's set_buttons is synchronous only for _prepare_zip (+ its
        # retry loop); the actual USB packet writes are create_task'd and drained
        # later by RenderPump. So this times the synchronous prepare+schedule cost
        # (the retry-loop amplifier); the full worker-block incl. the async USB
        # drain is measured by RenderPump._last_paint_ms.
        if not buttons:
            return False
        t0 = time.perf_counter()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self._set_buttons(buttons, update_only=update_only)
        except Exception:
            log.warning("d200 %s write failed (%d tiles)", channel, len(buttons))
            return False
        dt_ms = (time.perf_counter() - t0) * 1000.0
        self._last_write_ms = dt_ms
        self._last_write_count = len(buttons)
        level = logging.WARNING if dt_ms >= self.SLOW_WRITE_MS else logging.DEBUG
        prefix = "slow " if dt_ms >= self.SLOW_WRITE_MS else ""
        log.log(
            level,
            "d200 %s%s write: %.1fms, %d tiles, update_only=%s",
            prefix,
            channel,
            dt_ms,
            len(buttons),
            update_only,
        )
        return True

    def _set_buttons(self, buttons: dict[int, dict], *, update_only: bool) -> None:
        """Prefer the in-memory zip build; on ANY failure fall back (permanently)
        to strmdck's stock disk-based set_buttons, which is slow but proven."""
        if self._fast_path_ok:
            try:
                self._fast_set_buttons(buttons, update_only=update_only)
                return
            except Exception:
                self._fast_path_ok = False
                log.warning(
                    "d200 fast zip path failed; falling back to strmdck set_buttons",
                    exc_info=True,
                )
        self._dev.set_buttons(buttons, update_only=update_only)

    def _fast_set_buttons(self, buttons: dict[int, dict], *, update_only: bool) -> None:
        """strmdck's set_buttons rebuilt in memory: identical manifest/zip layout
        and packet framing, none of the per-frame rmtree/copyfile/rezip disk churn
        (the measured 280-530ms per frame that made the deck stutter)."""
        from strmdck.devices.ulanzi_d200 import CommandProtocol, PacketStruct

        dev = self._dev
        manifest: dict[str, dict] = {}
        icons: dict[str, bytes] = {}
        for button_index, button in buttons.items():
            i = int(button_index)
            row, col = divmod(i, dev.BUTTON_COLS)
            entry: dict = {"State": 0, "ViewParam": [{}]}
            if button:
                if "name" in button:
                    entry["ViewParam"][0]["Text"] = button["name"]
                if "icon" in button:
                    name = button["icon"]
                    if name not in icons:
                        with open(os.path.join(_ICON_DIR, name), "rb") as fp:
                            icons[name] = fp.read()
                    entry["ViewParam"][0]["Icon"] = f"icons/{name}"
            manifest[f"{col}_{row}"] = entry
        manifest_bytes = json.dumps(
            manifest, sort_keys=True, separators=(",", ":"), indent=2
        ).encode()
        data = build_button_zip(manifest_bytes, icons)
        command = (
            CommandProtocol.OUT_PARTIALLY_UPDATE_BUTTONS
            if update_only
            else CommandProtocol.OUT_SET_BUTTONS
        )
        packets = [
            PacketStruct.build(
                dict(
                    command_protocol=command.value,
                    length=len(data),
                    data=data[:_FIRST_CHUNK_DATA].ljust(_FIRST_CHUNK_DATA, b"\x00"),
                )
            )
        ]
        for i in range(_FIRST_CHUNK_DATA, len(data), 1024):
            packets.append(data[i : i + 1024].ljust(1024, b"\x00"))
        dev._write_packet(packets)

    def _write_tiles(self, tiles: list[TileView]) -> None:
        # Always a FULL set (update_only=False) of every tile. The D200 drops the
        # cells NOT included in a partial (update_only=True) write — so static tiles
        # (e.g. the "New" launcher) and idle tiles vanish, and only the tiles touched
        # by the frequent working/spinner updates stay lit. Re-sending every tile on
        # each full render re-establishes the whole layout. The per-index write-diff +
        # periodic resync that this replaces existed only to dodge strmdck's retry-loop
        # stall on a big zip; with that sleep neutralized a full set is cheap (~12ms
        # prepare, one USB write), so the optimization is no longer worth its cost.
        buttons = self._tile_buttons(tiles)
        self._last_frame_buttons = None  # split-path write invalidates the frame signature
        if self._timed_set_buttons("tiles", buttons, update_only=False):
            self._record(buttons)

    def _panel_buttons(self, panel: PanelView) -> dict[int, dict] | None:
        """Compose the two panel-cell buttons, (re)saving their PNGs only when
        the panel content changed. Names are content-keyed (panel key hash +
        TILE_VERSION) so a frame with an unchanged panel is byte-identical to
        the previous one (frame-skip) and a composition change re-bakes."""
        key = (panel.title, tuple(panel.lines), panel.color)
        names = self._panel_names
        paths_ok = names is not None and all(
            os.path.exists(os.path.join(_ICON_DIR, n)) for n in names
        )
        if key != self._last_panel_key or not paths_ok:
            try:
                left, right = split_panel(compose_panel(panel))
                os.makedirs(_ICON_DIR, exist_ok=True)
                h = hashlib.sha1(f"{TILE_VERSION}|{key!r}".encode()).hexdigest()[:12]
                names = (f"panel_l_{h}.png", f"panel_r_{h}.png")
                left.save(os.path.join(_ICON_DIR, names[0]))
                right.save(os.path.join(_ICON_DIR, names[1]))
            except Exception:
                # compose failed: a STALE panel (previous names, files intact) is
                # better than none — a full set without the panel cells would
                # blank them (the firmware drops omitted cells)
                if paths_ok:
                    return {
                        _PANEL_LEFT_INDEX: {"name": "", "icon": names[0]},
                        _PANEL_RIGHT_INDEX: {"name": "", "icon": names[1]},
                    }
                return None
            self._last_panel_key = key
            self._panel_names = names
        return {
            _PANEL_LEFT_INDEX: {"name": "", "icon": names[0]},
            _PANEL_RIGHT_INDEX: {"name": "", "icon": names[1]},
        }

    def _write_panel(self, panel: PanelView) -> None:
        buttons = self._panel_buttons(panel)
        if buttons is None:
            return
        # legacy split path: a later panel-only write means the combined-frame
        # signature no longer reflects the device — drop it so the next frame writes
        self._last_frame_buttons = None
        # update_only so refreshing the panel never clears the 13 tiles.
        self._timed_set_buttons("panel", buttons, update_only=True)

    def _write_frame(self, tiles: list[TileView], panel: PanelView) -> None:
        buttons = self._tile_buttons(tiles)
        panel_buttons = self._panel_buttons(panel)
        if panel_buttons is None:
            # no panel cells available at all (first-ever compose failed): a full
            # set without them would BLANK the panel — skip this frame instead
            return
        buttons.update(panel_buttons)
        # Byte-identical frame (icon names are content-addressed): sending
        # NOTHING is safe — no set is issued, so the firmware drops nothing and
        # keeps displaying the current layout (keep-alive maintains it). This
        # zeroes the per-tick zip+USB cost of an idle deck.
        if buttons == self._last_frame_buttons:
            return
        # ALWAYS a full set. Device-tested 2026-07-02: OUT_PARTIALLY_UPDATE_BUTTONS
        # blanks most cells EVEN when the write carries all 15 — partial updates
        # are broken on this firmware at page scale, do not retry them. The
        # full-set page reload can blink; the mitigation is writing only frames
        # whose content actually changed (see the identical-frame skip above).
        if self._timed_set_buttons("frame", buttons, update_only=False):
            self._last_frame_buttons = buttons
            self._record(buttons)

    def _write_working(self, tiles: list[TileView]) -> None:
        changed = self._diff(self._tile_buttons(tiles))
        if not changed:
            return
        if self._timed_set_buttons("working", changed, update_only=True):
            self._record(changed)
            # the partial write mutated the device behind the combined-frame
            # cache — a later identical render_frame must NOT be skipped
            self._last_frame_buttons = None

    def _keep_alive_write(self) -> None:
        with contextlib.suppress(Exception):
            with contextlib.redirect_stdout(io.StringIO()):
                self._dev.keep_alive()

    def on_press(self, callback: Callable[[int], None]) -> None:
        self._callback = callback

    def close(self) -> None:
        if self._pump is not None:
            with contextlib.suppress(Exception):
                self._pump.close()
            self._pump = None
        with contextlib.suppress(Exception):
            self._dev.close()
        with contextlib.suppress(Exception):
            os.chdir(self._previous_cwd)

    async def run_reader(self) -> None:
        last_index = None
        last_time = 0.0
        async for action in self._dev.read_packet():
            if action is not None and getattr(action, "pressed", False):
                # Debounce hardware double-fire: ignore the same key repeated
                # within DEBOUNCE seconds (distinct keys are never suppressed).
                now = time.monotonic()
                if action.index == last_index and now - last_time < self.DEBOUNCE:
                    continue
                last_index, last_time = action.index, now
                if self._callback is not None:
                    self._callback(action.index)
