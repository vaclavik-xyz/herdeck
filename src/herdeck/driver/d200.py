from __future__ import annotations

import contextlib
import io
import os
from collections.abc import Callable

from PIL import Image, ImageDraw

from .base import COLORS, DeckDriver, PanelView, TileView

PANEL_W, PANEL_H = 392, 196
_CELL = 196
# strmdck reads tile icons here, relative to CWD:
_ICON_DIR = os.path.join(".cache", "icons", "_generated")
# the two panel cells (grid 3_2 and 4_2):
_PANEL_LEFT_INDEX = 13
_PANEL_RIGHT_INDEX = 14


def compose_panel(panel: PanelView) -> Image.Image:
    """Render a PanelView to a 392x196 image (title + lines on a color)."""
    img = Image.new("RGB", (PANEL_W, PANEL_H), COLORS.get(panel.color, COLORS["dim"]))
    d = ImageDraw.Draw(img)
    d.text((12, 10), panel.title, fill=(255, 255, 255))
    y = 60
    for line in panel.lines[:3]:
        d.text((12, y), line[:48], fill=(235, 235, 235))
        y += 42
    return img


def split_panel(img: Image.Image) -> tuple[Image.Image, Image.Image]:
    left = img.crop((0, 0, _CELL, _CELL))
    right = img.crop((PANEL_W - _CELL, 0, PANEL_W, _CELL))
    return left, right


class D200Driver(DeckDriver):
    """Ulanzi D200 driver. Renders 13 tiles + a 2-cell status panel."""

    KEEP_ALIVE_INTERVAL = 5.0
    BRIGHTNESS = 80
    _CONTROL_USAGE_PAGE = 0x0c

    def __init__(self, workdir: str | None = None, icon_provider=None):
        # Stable working dir so strmdck's relative .build/.cache never collide (R-4).
        self._workdir = workdir or os.path.expanduser("~/.cache/herdeck")
        os.makedirs(self._workdir, exist_ok=True)
        os.chdir(self._workdir)
        os.makedirs(_ICON_DIR, exist_ok=True)
        self._dev = self._open_device()
        self._callback: Callable[[int], None] | None = None
        if icon_provider is None:
            from ..icons import IconProvider, DEFAULT_AGENT_SLUGS
            icon_provider = IconProvider(cache_dir=os.path.abspath(_ICON_DIR),
                                         slug_map=DEFAULT_AGENT_SLUGS,
                                         overrides_dir=os.path.abspath("icons"))
        self._icons = icon_provider
        with contextlib.redirect_stdout(io.StringIO()):
            self._dev.set_brightness(self.BRIGHTNESS, force=True)
            self._set_panel_background_mode()

    def _open_device(self, retries: int = 5, delay: float = 1.0):
        import time
        import hid
        from strmdck.devices.ulanzi_d200 import UlanziD200Device
        vid, pid = UlanziD200Device.USB_VENDOR_ID, UlanziD200Device.USB_PRODUCT_ID
        last = None
        for _ in range(retries):
            matches = [d for d in hid.enumerate()
                       if (d["vendor_id"], d["product_id"]) == (vid, pid)]
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
            self._dev.set_small_window_data({"mode": SmallWindowMode.BACKGROUND}, force=True)

    def slot_count(self) -> int:
        return self._dev.BUTTON_COUNT      # 13

    def _tile_buttons(self, tiles: list[TileView]) -> dict[int, dict]:
        buttons: dict[int, dict] = {}
        for t in tiles:
            if t.index >= self._dev.BUTTON_COUNT:
                continue
            agent = t.agent_type or "_empty"
            name = self._icons.icon_for(agent, t.color, t.spinner)
            buttons[t.index] = {"name": t.label, "icon": name}
        return buttons

    def render(self, tiles: list[TileView]) -> None:
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self._dev.set_buttons(self._tile_buttons(tiles))
        except Exception:
            pass  # never freeze the loop

    def render_panel(self, panel: PanelView) -> None:
        try:
            left, right = split_panel(compose_panel(panel))
            os.makedirs(_ICON_DIR, exist_ok=True)
            left.save(os.path.join(_ICON_DIR, "panel_left.png"))
            right.save(os.path.join(_ICON_DIR, "panel_right.png"))
            with contextlib.redirect_stdout(io.StringIO()):
                self._dev.set_buttons({
                    _PANEL_LEFT_INDEX: {"name": "", "icon": "panel_left.png"},
                    _PANEL_RIGHT_INDEX: {"name": "", "icon": "panel_right.png"},
                })
        except Exception:
            pass

    def render_working(self, tiles: list[TileView]) -> None:
        """Partial re-render of just the working tiles (spinner)."""
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self._dev.set_buttons(self._tile_buttons(tiles), update_only=True)
        except Exception:
            pass

    def on_press(self, callback: Callable[[int], None]) -> None:
        self._callback = callback

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._dev.close()

    async def run_reader(self) -> None:
        async for action in self._dev.read_packet():
            if action is not None and getattr(action, "pressed", False):
                if self._callback is not None:
                    self._callback(action.index)

    async def keep_alive_loop(self) -> None:
        import asyncio
        while True:
            with contextlib.suppress(Exception):
                with contextlib.redirect_stdout(io.StringIO()):
                    self._dev.keep_alive()
            await asyncio.sleep(self.KEEP_ALIVE_INTERVAL)
