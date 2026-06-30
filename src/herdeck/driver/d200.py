from __future__ import annotations

import contextlib
import io
import logging
import os
import time
from collections.abc import Callable

from PIL import Image

from ..icons import compose_panel
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
                self._dev.set_buttons(buttons, update_only=update_only)
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

    def _write_tiles(self, tiles: list[TileView]) -> None:
        buttons = self._tile_buttons(tiles)
        if not self._last_icon:
            # First paint establishes the full button layout.
            if self._timed_set_buttons("tiles", buttons, update_only=False):
                self._record(buttons)
            return
        changed = self._diff(buttons)
        if not changed:
            return
        if self._timed_set_buttons("tiles", changed, update_only=True):
            self._record(changed)

    def _write_panel(self, panel: PanelView) -> None:
        try:
            left, right = split_panel(compose_panel(panel))
            os.makedirs(_ICON_DIR, exist_ok=True)
            left.save(os.path.join(_ICON_DIR, "panel_left.png"))
            right.save(os.path.join(_ICON_DIR, "panel_right.png"))
        except Exception:
            return
        # update_only so refreshing the panel never clears the 13 tiles.
        self._timed_set_buttons(
            "panel",
            {
                _PANEL_LEFT_INDEX: {"name": "", "icon": "panel_left.png"},
                _PANEL_RIGHT_INDEX: {"name": "", "icon": "panel_right.png"},
            },
            update_only=True,
        )

    def _write_working(self, tiles: list[TileView]) -> None:
        changed = self._diff(self._tile_buttons(tiles))
        if not changed:
            return
        if self._timed_set_buttons("working", changed, update_only=True):
            self._record(changed)

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
