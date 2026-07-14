from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable

from .base import DeckDriver, PanelView, TileView
from .render_pump import RenderPump

BRIGHTNESS = 80
# Keys reserved for the status panel (Elgato decks have no separate window).
_PANEL_KEYS = 2
# Converted native-format images kept per distinct tile PNG (spinner cycles
# reuse 8 frames, so a small LRU means each frame is JPEG-encoded once).
_NATIVE_CACHE_MAX = 64


class ElgatoDriver(DeckDriver):
    """Driver for Elgato Stream Deck hardware (python-elgato-streamdeck).

    The last two physical keys are reserved for the status panel, mirroring the
    D200's 13 tiles + 2-cell window. Pass ``device`` to inject a fake for tests;
    when it is None a real deck is enumerated and opened.

    Mirrors the D200 driver's render architecture: all device writes run on a
    RenderPump worker thread (blocking USB I/O never stalls the event loop),
    and — unlike the D200, whose firmware drops omitted cells — Elgato keys
    RETAIN their image, so unchanged keys are diffed out and never rewritten.
    """

    def __init__(
        self,
        device=None,
        icon_provider=None,
        brightness: int = BRIGHTNESS,
        icons_dir: str | None = None,
    ):
        self._brightness = brightness
        self._icons_dir = icons_dir
        self._dev = device if device is not None else self._open_device()
        self._icons = icon_provider
        self._callback: Callable[[int], None] | None = None
        self._last_png: dict[int, bytes] = {}  # key index -> last-written tile PNG
        self._native_cache: OrderedDict[bytes, object] = OrderedDict()
        self._panel_key: tuple | None = None  # content key of the painted panel
        if device is not None:
            self._dev.set_brightness(brightness)
        self._pump: RenderPump | None = RenderPump(paint=self._paint)
        self._pump.start()

    def _open_device(self):
        # Hardware path (lazy import so the test suite needs neither the library
        # nor a physical deck).
        from StreamDeck.DeviceManager import DeviceManager

        decks = DeviceManager().enumerate()
        if not decks:
            raise RuntimeError("No Elgato Stream Deck found")
        deck = decks[0]
        deck.open()
        deck.reset()
        deck.set_brightness(self._brightness)
        return deck

    def slot_count(self) -> int:
        return self._dev.key_count() - _PANEL_KEYS

    def _icon_provider(self):
        if self._icons is None:
            import os
            import tempfile

            from ..icons import DEFAULT_AGENT_SLUGS, IconProvider

            cache = os.path.join(tempfile.gettempdir(), "herdeck-elgato-icons")
            overrides = (
                os.path.abspath(os.path.expanduser(self._icons_dir)) if self._icons_dir else None
            )
            self._icons = IconProvider(
                cache_dir=cache,
                slug_map=DEFAULT_AGENT_SLUGS,
                overrides_dir=overrides,
            )
        return self._icons

    def _to_native(self, image):
        # Lazy import: StreamDeck is only needed on the hardware path.
        from StreamDeck.ImageHelpers import PILHelper

        return PILHelper.to_native_format(self._dev, image)

    def _native_resized(self, image):
        return self._to_native(image.resize(self._dev.key_image_format()["size"]))

    def _native_png(self, png: bytes):
        """Native-format image for a tile PNG, LRU-cached so a repeating
        spinner frame is decoded + JPEG-encoded once, not every tick."""
        cached = self._native_cache.get(png)
        if cached is not None:
            self._native_cache.move_to_end(png)
            return cached
        import io

        from PIL import Image

        native = self._native_resized(Image.open(io.BytesIO(png)))
        self._native_cache[png] = native
        while len(self._native_cache) > _NATIVE_CACHE_MAX:
            self._native_cache.popitem(last=False)
        return native

    # --- DeckDriver render API: hand frames to the worker and return ---
    def render(self, tiles: list[TileView]) -> None:
        self._submit("tiles", list(tiles))

    def render_working(self, tiles: list[TileView]) -> None:
        # Each set_key_image updates one key independently, so a partial refresh
        # of just the working (spinner) tiles leaves every other key untouched.
        self._submit("working", list(tiles))

    def render_panel(self, panel: PanelView) -> None:
        self._submit("panel", panel)

    def _submit(self, channel: str, payload) -> None:
        # Blocking USB writes happen on the pump worker, never on the caller
        # (the event loop). Falls back to inline paint after close().
        if self._pump is not None:
            self._pump.submit(channel, payload)
        else:
            self._paint(channel, payload)

    def _paint(self, channel: str, payload) -> None:
        if channel in ("tiles", "working"):
            self._write_tiles(payload)
        elif channel == "panel":
            self._write_panel(payload)

    def _write_tiles(self, tiles: list[TileView]) -> None:
        for tile in tiles:
            png = self._icon_provider().render_tile_bytes(tile)
            if self._last_png.get(tile.index) == png:
                continue  # keys retain their image; unchanged writes are pure waste
            self._dev.set_key_image(tile.index, self._native_png(png))
            self._last_png[tile.index] = png

    def _write_panel(self, panel: PanelView) -> None:
        key = panel.cache_key()
        if key == self._panel_key:
            return  # the two panel keys already show exactly this content
        from ..icons import PANEL_W_TWO_CELL, compose_panel
        from .d200 import split_panel

        # Compose at exactly two cells wide so the halves are 1:1 key images
        # (the default width is the D200 window's native 458px).
        left, right = split_panel(compose_panel(panel, width=PANEL_W_TWO_CELL))
        base = self.slot_count()
        self._dev.set_key_image(base, self._native_resized(left))
        self._dev.set_key_image(base + 1, self._native_resized(right))
        self._panel_key = key

    def on_press(self, callback: Callable[[int], None]) -> None:
        self._callback = callback
        self._dev.set_key_callback(self._on_key)

    def _on_key(self, deck, key: int, state: bool) -> None:
        # Device key index == orchestrator tile index (panel keys are the last
        # two). Forward only key-down; the app marshals to the loop (like web).
        if state and self._callback is not None:
            self._callback(key)

    def close(self) -> None:
        import contextlib

        if self._pump is not None:
            with contextlib.suppress(Exception):
                self._pump.close()
            self._pump = None
        with contextlib.suppress(Exception):
            self._dev.close()
