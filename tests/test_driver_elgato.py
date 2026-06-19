from herdeck.driver.elgato import ElgatoDriver


class FakeDeck:
    """A hardware-free stand-in for a python-elgato-streamdeck device.

    Records the calls the driver makes so tests can assert on them without the
    StreamDeck library or a physical deck.
    """

    def __init__(self, key_count: int = 15, key_size: tuple[int, int] = (72, 72)):
        self._key_count = key_count
        self._key_size = key_size
        self.images: dict[int, bytes] = {}     # key index -> native image bytes
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
