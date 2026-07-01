import io
import os
import time

from PIL import Image

from herdeck.driver.base import PanelView, TileView
from herdeck.driver.elgato import ElgatoDriver


def _wait_until(pred, timeout=2.0):
    """Device writes now land on the RenderPump worker thread."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return False


class FakeIcons:
    """Stand-in icon provider returning real (tiny) PNG bytes per tile."""

    def __init__(self):
        self.rendered: list[TileView] = []

    def render_tile_bytes(self, tile: TileView) -> bytes:
        self.rendered.append(tile)
        buf = io.BytesIO()
        Image.new("RGB", (4, 4), (10, 20, 30)).save(buf, "PNG")
        return buf.getvalue()


class FakeDeck:
    """A hardware-free stand-in for a python-elgato-streamdeck device.

    Records the calls the driver makes so tests can assert on them without the
    StreamDeck library or a physical deck.
    """

    def __init__(self, key_count: int = 15, key_size: tuple[int, int] = (72, 72)):
        self._key_count = key_count
        self._key_size = key_size
        self.images: dict[int, bytes] = {}  # key index -> native image bytes
        self.writes: list[int] = []  # every set_key_image call, in order
        self.callback = None
        self.brightness = None
        self.reset_called = False
        self.closed = False

    def key_count(self) -> int:
        return self._key_count

    def key_image_format(self) -> dict:
        return {"size": self._key_size}

    def set_key_image(self, key: int, image) -> None:
        self.images[key] = image
        self.writes.append(key)

    def set_key_callback(self, cb) -> None:
        self.callback = cb

    def set_brightness(self, pct: int) -> None:
        self.brightness = pct

    def reset(self) -> None:
        self.reset_called = True

    def close(self) -> None:
        self.closed = True


def test_slot_count_reserves_two_keys_for_the_panel():
    assert ElgatoDriver(device=FakeDeck(key_count=15)).slot_count() == 13
    assert ElgatoDriver(device=FakeDeck(key_count=32)).slot_count() == 30


def test_render_resizes_and_writes_native_key_images(monkeypatch):
    deck = FakeDeck(key_count=15, key_size=(72, 72))
    drv = ElgatoDriver(device=deck, icon_provider=FakeIcons())
    monkeypatch.setattr(drv, "_to_native", lambda image: image.tobytes())
    try:
        drv.render([TileView(0, "a", "green"), TileView(1, "b", "blue")])
        assert _wait_until(lambda: set(deck.images) == {0, 1})
        assert all(deck.images[k] for k in (0, 1))  # non-empty bytes written
        assert len(deck.images[0]) == 72 * 72 * 3  # resized to the deck key size
    finally:
        drv.close()


def test_render_panel_writes_the_two_reserved_keys(monkeypatch):
    deck = FakeDeck(key_count=15)  # slot_count == 13
    drv = ElgatoDriver(device=deck, icon_provider=FakeIcons())
    monkeypatch.setattr(drv, "_to_native", lambda image: image.tobytes())
    try:
        drv.render_panel(PanelView("overview", ["1 working"], "grey"))
        assert _wait_until(lambda: set(deck.images) == {13, 14})  # the reserved panel keys
        assert all(deck.images[k] for k in (13, 14))  # non-empty halves
    finally:
        drv.close()


def test_key_down_forwards_index_and_key_up_is_ignored():
    deck = FakeDeck(key_count=15)
    drv = ElgatoDriver(device=deck, icon_provider=FakeIcons())
    seen: list[int] = []
    drv.on_press(seen.append)
    assert deck.callback is not None  # registered with the device
    deck.callback(deck, 7, True)  # key-down -> forward index
    assert seen == [7]
    deck.callback(deck, 7, False)  # key-up -> ignored
    assert seen == [7]


def test_render_working_only_touches_the_given_keys(monkeypatch):
    deck = FakeDeck(key_count=15)
    drv = ElgatoDriver(device=deck, icon_provider=FakeIcons())
    monkeypatch.setattr(drv, "_to_native", lambda image: image.tobytes())
    try:
        drv.render_working([TileView(2, "x", "amber"), TileView(5, "y", "amber")])
        assert _wait_until(lambda: set(deck.images) == {2, 5})  # others keep their image
    finally:
        drv.close()


def test_elgato_brightness_can_be_configured():
    deck = FakeDeck()
    drv = ElgatoDriver(device=deck, brightness=35)

    assert deck.brightness == 35
    drv.close()


def test_elgato_icons_dir_configures_override_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    drv = ElgatoDriver(device=FakeDeck(), icons_dir="~/herdeck-icons")

    icons = drv._icon_provider()

    assert icons._overrides_dir == os.path.join(str(tmp_path), "herdeck-icons")


class VaryingIcons:
    """PNG bytes vary with the tile's colour, so a changed tile changes bytes."""

    def render_tile_bytes(self, tile: TileView) -> bytes:
        buf = io.BytesIO()
        c = sum(tile.color.encode()) % 255
        Image.new("RGB", (4, 4), (c, c, c)).save(buf, "PNG")
        return buf.getvalue()


def test_unchanged_keys_are_never_rewritten(monkeypatch):
    """Elgato firmware retains key images: rewriting an unchanged key every
    tick was pure USB waste (audit: elgato-diff-pump)."""
    deck = FakeDeck(key_count=15)
    drv = ElgatoDriver(device=deck, icon_provider=VaryingIcons())
    monkeypatch.setattr(drv, "_to_native", lambda image: image.tobytes())
    try:
        drv.render([TileView(0, "a", "green")])
        assert _wait_until(lambda: deck.writes.count(0) == 1)
        drv.render([TileView(0, "a", "green")])  # identical content
        drv.render_panel(PanelView("t", ["x"], "grey"))  # marks the cycle done
        assert _wait_until(lambda: 13 in deck.images)
        assert deck.writes.count(0) == 1  # unchanged key was diffed out
        drv.render([TileView(0, "a", "amber")])  # content changed -> rewritten
        assert _wait_until(lambda: deck.writes.count(0) == 2)
    finally:
        drv.close()


def test_unchanged_panel_is_not_recomposed_or_rewritten(monkeypatch):
    deck = FakeDeck(key_count=15)
    drv = ElgatoDriver(device=deck, icon_provider=VaryingIcons())
    monkeypatch.setattr(drv, "_to_native", lambda image: image.tobytes())
    try:
        drv.render_panel(PanelView("t", ["x"], "grey"))
        assert _wait_until(lambda: deck.writes.count(13) == 1)
        drv.render_panel(PanelView("t", ["x"], "grey"))  # identical content
        drv.render([TileView(0, "a", "green")])  # marks the cycle done
        assert _wait_until(lambda: 0 in deck.images)
        assert deck.writes.count(13) == 1  # panel keys untouched
    finally:
        drv.close()


def test_close_stops_the_render_worker():
    deck = FakeDeck(key_count=15)
    drv = ElgatoDriver(device=deck, icon_provider=FakeIcons())
    worker = drv._pump._thread
    drv.close()
    assert deck.closed
    assert not worker.is_alive()
