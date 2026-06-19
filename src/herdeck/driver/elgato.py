from __future__ import annotations

from collections.abc import Callable

from .base import DeckDriver, PanelView, TileView

BRIGHTNESS = 80
# Keys reserved for the status panel (Elgato decks have no separate window).
_PANEL_KEYS = 2


class ElgatoDriver(DeckDriver):
    """Driver for Elgato Stream Deck hardware (python-elgato-streamdeck).

    The last two physical keys are reserved for the status panel, mirroring the
    D200's 13 tiles + 2-cell window. Pass ``device`` to inject a fake for tests;
    when it is None a real deck is enumerated and opened.
    """

    def __init__(self, device=None, icon_provider=None):
        self._dev = device if device is not None else self._open_device()
        self._icons = icon_provider
        self._callback: Callable[[int], None] | None = None

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
        deck.set_brightness(BRIGHTNESS)
        return deck

    def slot_count(self) -> int:
        return self._dev.key_count() - _PANEL_KEYS

    def _icon_provider(self):
        if self._icons is None:
            import os
            import tempfile
            from ..icons import DEFAULT_AGENT_SLUGS, IconProvider
            cache = os.path.join(tempfile.gettempdir(), "herdeck-elgato-icons")
            self._icons = IconProvider(cache_dir=cache,
                                       slug_map=DEFAULT_AGENT_SLUGS,
                                       overrides_dir=None)
        return self._icons

    def _to_native(self, image):
        # Lazy import: StreamDeck is only needed on the hardware path.
        from StreamDeck.ImageHelpers import PILHelper
        return PILHelper.to_native_key_format(self._dev, image)

    def _native_resized(self, image):
        return self._to_native(image.resize(self._dev.key_image_format()["size"]))

    def _key_image(self, tile: TileView):
        import io
        from PIL import Image
        png = self._icon_provider().render_tile_bytes(tile)
        return self._native_resized(Image.open(io.BytesIO(png)))

    def render(self, tiles: list[TileView]) -> None:
        for tile in tiles:
            self._dev.set_key_image(tile.index, self._key_image(tile))

    def render_panel(self, panel: PanelView) -> None:
        from ..icons import compose_panel
        from .d200 import split_panel
        left, right = split_panel(compose_panel(panel))
        base = self.slot_count()
        self._dev.set_key_image(base, self._native_resized(left))
        self._dev.set_key_image(base + 1, self._native_resized(right))

    def on_press(self, callback: Callable[[int], None]) -> None:
        pass

    def close(self) -> None:
        import contextlib
        with contextlib.suppress(Exception):
            self._dev.close()
