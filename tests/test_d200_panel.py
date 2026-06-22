import os

import pytest
from PIL import Image

from herdeck.driver.base import PanelView
from herdeck.driver.d200 import D200Driver, compose_panel, split_panel


def test_compose_panel_size():
    img = compose_panel(PanelView("page 1/2", ["B1 W4 I6", "online"], "grey"))
    assert img.size == (392, 196)


def test_split_panel_halves():
    img = Image.new("RGB", (392, 196), (0, 0, 0))
    left, right = split_panel(img)
    assert left.size == (196, 196) and right.size == (196, 196)


def test_d200_constructor_restores_cwd_when_open_fails(tmp_path):
    class FailingD200(D200Driver):
        def _open_device(self, retries=5, delay=1.0):
            raise RuntimeError("no device")

    before = os.getcwd()
    try:
        with pytest.raises(RuntimeError, match="no device"):
            FailingD200(workdir=str(tmp_path))
        assert os.getcwd() == before
    finally:
        os.chdir(before)


def test_d200_hardware_settings_can_be_configured(tmp_path):
    class FakeD200Device:
        BUTTON_COUNT = 13

        def __init__(self):
            self.brightness = None
            self.small_window_mode = None
            self.small_window_data = None
            self.closed = False

        def set_brightness(self, value, force=False):
            self.brightness = (value, force)

        def set_small_window_mode(self, mode):
            self.small_window_mode = mode

        def set_small_window_data(self, data, force=False):
            self.small_window_data = (data, force)

        def close(self):
            self.closed = True

    device = FakeD200Device()

    class ConfiguredD200(D200Driver):
        def _open_device(self, retries=5, delay=1.0):
            return device

        def _set_panel_background_mode(self):
            self._dev.set_small_window_mode("background")
            self._dev.set_small_window_data({"mode": "background"}, force=True)

    before = os.getcwd()
    try:
        driver = ConfiguredD200(
            workdir=str(tmp_path),
            icon_provider=object(),
            brightness=35,
            debounce=0.1,
            keep_alive_interval=2.5,
        )

        assert device.brightness == (35, True)
        assert driver.DEBOUNCE == 0.1
        assert driver.KEEP_ALIVE_INTERVAL == 2.5
        driver.close()
    finally:
        os.chdir(before)


def test_d200_icons_dir_configures_override_provider(monkeypatch, tmp_path):
    class FakeD200Device:
        BUTTON_COUNT = 13

        def set_brightness(self, value, force=False):
            pass

        def close(self):
            pass

    class ConfiguredD200(D200Driver):
        def _open_device(self, retries=5, delay=1.0):
            return FakeD200Device()

        def _set_panel_background_mode(self):
            pass

    monkeypatch.setenv("HOME", str(tmp_path))
    before = os.getcwd()
    try:
        driver = ConfiguredD200(workdir=str(tmp_path / "work"), icons_dir="~/herdeck-icons")

        assert driver._icons._overrides_dir == os.path.join(str(tmp_path), "herdeck-icons")
        driver.close()
    finally:
        os.chdir(before)
