from __future__ import annotations

import os
from collections.abc import Callable

from .base import COLORS, DeckDriver, TileView


class D200Driver(DeckDriver):
    """Driver for the Ulanzi Stream Controller D200 via the ``strmdck`` library.

    Notes:
    - The official Ulanzi desktop app must be closed or it holds the USB device.
    - ``strmdck`` is asyncio-based: rendering schedules writes on the running
      loop and key input is read via an async generator (``run_reader``). So this
      driver must be driven from inside the app's event loop.
    - The device renders a label (``name``) on top of a per-button icon image.
      We pre-generate one solid-color PNG per semantic color and reference it,
      which is how we encode agent state as a background color.
    """

    KEEP_ALIVE_INTERVAL = 5.0
    BRIGHTNESS = 80
    _ICON_DIR = os.path.join(".cache", "icons", "_generated")

    # The D200 exposes two HID interfaces: the deck control surface
    # (usage_page 0x0c) and a standalone keyboard (0x01, claimed by macOS).
    _CONTROL_USAGE_PAGE = 0x0c

    def __init__(self):
        self._dev = self._open_device()
        self._callback: Callable[[int], None] | None = None
        self._icons = self._generate_icons()
        self._dev.set_brightness(self.BRIGHTNESS, force=True)

    def _open_device(self, retries: int = 5, delay: float = 1.0):
        """Open the deck's control interface by path.

        strmdck's auto_connect opens by (vendor, product), which on macOS
        nondeterministically grabs the keyboard interface (held by the OS) and
        fails. We pick the control interface (usage_page 0x0c) by path instead,
        with a short retry for reopen contention right after a prior session.
        """
        import time

        import hid  # lazy: no HW import at module load
        from strmdck.devices.ulanzi_d200 import UlanziD200Device

        vid, pid = UlanziD200Device.USB_VENDOR_ID, UlanziD200Device.USB_PRODUCT_ID
        last_err = None
        for _ in range(retries):
            matches = [d for d in hid.enumerate()
                       if (d["vendor_id"], d["product_id"]) == (vid, pid)]
            # Prefer the control interface; fall back to any matching path.
            paths = [d["path"] for d in matches
                     if d.get("usage_page") == self._CONTROL_USAGE_PAGE]
            paths += [d["path"] for d in matches
                      if d.get("usage_page") != self._CONTROL_USAGE_PAGE]
            for path in paths:
                hid_dev = hid.device()
                try:
                    hid_dev.open_path(path)
                    hid_dev.set_nonblocking(True)
                    return UlanziD200Device(hid_dev)
                except Exception as exc:  # interface busy / wrong one
                    last_err = exc
                    try:
                        hid_dev.close()  # don't leak the handle / hold the iface
                    except Exception:
                        pass
            time.sleep(delay)
        raise RuntimeError(
            "No openable Ulanzi D200 control interface found. Is it plugged in "
            "and the official Ulanzi app closed (it holds the USB device)? "
            f"(last error: {last_err})"
        )

    # --- icon generation ---
    def _generate_icons(self) -> dict[str, str]:
        """Create one solid-color PNG per semantic color; return color -> filename."""
        from PIL import Image  # lazy: only needed with real hardware

        os.makedirs(self._ICON_DIR, exist_ok=True)
        size = (self._dev.ICON_WIDTH or 196, self._dev.ICON_HEIGHT or 196)
        names: dict[str, str] = {}
        for color, rgb in COLORS.items():
            name = f"{color}.png"
            Image.new("RGB", size, rgb).save(os.path.join(self._ICON_DIR, name))
            names[color] = name
        return names

    # --- DeckDriver interface ---
    def slot_count(self) -> int:
        return self._dev.BUTTON_COUNT

    def render(self, tiles: list[TileView]) -> None:
        buttons: dict[int, dict] = {}
        for t in tiles:
            if t.index >= self._dev.BUTTON_COUNT:
                continue  # last grid cells are the small status window, not buttons
            icon = self._icons.get(t.color, self._icons["dim"])
            buttons[t.index] = {"name": t.label, "icon": icon}
        self._dev.set_buttons(buttons)

    def on_press(self, callback: Callable[[int], None]) -> None:
        self._callback = callback

    def close(self) -> None:
        self._dev.close()

    # --- async loops (driven by app._run) ---
    async def run_reader(self) -> None:
        """Read button events forever, firing the press callback on key-down."""
        async for action in self._dev.read_packet():
            if action is not None and getattr(action, "pressed", False):
                if self._callback is not None:
                    self._callback(action.index)

    async def keep_alive_loop(self) -> None:
        import asyncio

        while True:
            try:
                self._dev.keep_alive()
            except Exception:
                pass
            await asyncio.sleep(self.KEEP_ALIVE_INTERVAL)
