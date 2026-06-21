import io

from PIL import Image

from herdeck.driver.base import PanelView, TileView
from herdeck.driver.elgato import ElgatoDriver


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
    drv.render([TileView(0, "a", "green"), TileView(1, "b", "blue")])
    assert set(deck.images) == {0, 1}
    assert all(deck.images[k] for k in (0, 1))  # non-empty bytes written
    assert len(deck.images[0]) == 72 * 72 * 3  # resized to the deck key size


def test_render_panel_writes_the_two_reserved_keys(monkeypatch):
    deck = FakeDeck(key_count=15)  # slot_count == 13
    drv = ElgatoDriver(device=deck, icon_provider=FakeIcons())
    monkeypatch.setattr(drv, "_to_native", lambda image: image.tobytes())
    drv.render_panel(PanelView("overview", ["1 working"], "grey"))
    assert set(deck.images) == {13, 14}  # the two reserved panel keys
    assert all(deck.images[k] for k in (13, 14))  # non-empty halves


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
    drv.render_working([TileView(2, "x", "amber"), TileView(5, "y", "amber")])
    assert set(deck.images) == {2, 5}  # untouched keys keep their image
