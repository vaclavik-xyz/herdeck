import logging
import os
import threading
import time

import pytest
from PIL import Image

from herdeck.driver.base import PanelView, TileView
from herdeck.driver.d200 import D200Driver, compose_panel, split_panel


class _ContentIcons:
    """render_tile filename varies with tile content (time_text), so a changed
    tile yields a different filename and the driver's diff can detect it."""

    def render_tile(self, tile):
        return f"icon_{tile.index}_{tile.time_text}.png"


def _wait_until(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return False


class _FakeDev:
    BUTTON_COUNT = 13

    def __init__(self, block=None):
        self.calls = []
        self.kept_alive = 0
        self.closed = False
        self._block = block

    def set_brightness(self, value, force=False):
        pass

    def set_buttons(self, buttons, update_only=False):
        if self._block is not None:
            self._block.wait(1.0)
        self.calls.append((dict(buttons), update_only))

    def keep_alive(self):
        self.kept_alive += 1

    def close(self):
        self.closed = True


class _FakeIcons:
    def render_tile(self, tile):
        return f"icon_{tile.index}.png"


def _make_driver(tmp_path, dev, icons=None):
    class _Driver(D200Driver):
        def _open_device(self, retries=5, delay=1.0):
            return dev

        def _set_panel_background_mode(self):
            pass

    return _Driver(workdir=str(tmp_path), icon_provider=icons or _FakeIcons())


def test_d200_render_is_offloaded_and_non_blocking(tmp_path):
    # The device write blocks; render() must return at once (offloaded to the worker),
    # then the write lands on the worker thread.
    block = threading.Event()
    dev = _FakeDev(block=block)
    before = os.getcwd()
    driver = _make_driver(tmp_path, dev)
    try:
        t0 = time.monotonic()
        driver.render([TileView(0, "x", "blue")])
        assert time.monotonic() - t0 < 0.2  # did NOT wait for the blocking USB write
        block.set()
        assert _wait_until(lambda: dev.calls)  # the write eventually happened
    finally:
        block.set()
        driver.close()
        os.chdir(before)


def test_d200_close_stops_worker_and_closes_device(tmp_path):
    dev = _FakeDev()
    before = os.getcwd()
    driver = _make_driver(tmp_path, dev)
    pump_thread = driver._pump._thread
    try:
        driver.close()
        assert dev.closed
        assert not pump_thread.is_alive()
    finally:
        os.chdir(before)


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


def test_d200_write_records_timing_and_count(tmp_path):
    dev = _FakeDev()
    before = os.getcwd()
    driver = _make_driver(tmp_path, dev)
    try:
        driver.render([TileView(0, "x", "blue"), TileView(1, "y", "green")])
        assert _wait_until(lambda: dev.calls)
        assert driver._last_write_count == 2
        assert driver._last_write_ms is not None
    finally:
        driver.close()
        os.chdir(before)


def test_d200_slow_write_warns(tmp_path, caplog):
    block = threading.Event()  # never set -> set_buttons waits the full 1.0s
    dev = _FakeDev(block=block)
    before = os.getcwd()
    driver = _make_driver(tmp_path, dev)
    try:
        with caplog.at_level(logging.WARNING, logger="herdeck.driver.d200"):
            driver.render([TileView(0, "x", "blue")])
            assert _wait_until(lambda: dev.calls, timeout=3.0)
        assert "slow tiles write" in caplog.text
    finally:
        block.set()
        driver.close()
        os.chdir(before)


def test_d200_write_failure_is_swallowed_and_warns(tmp_path, caplog):
    class _RaisingDev(_FakeDev):
        def set_buttons(self, buttons, update_only=False):
            raise RuntimeError("usb boom")

    dev = _RaisingDev()
    before = os.getcwd()
    driver = _make_driver(tmp_path, dev)
    pump_thread = driver._pump._thread
    try:
        with caplog.at_level(logging.WARNING, logger="herdeck.driver.d200"):
            driver.render([TileView(0, "x", "blue")])
            assert _wait_until(lambda: "tiles write failed" in caplog.text, timeout=2.0)
        assert pump_thread.is_alive()  # a failed write must not kill the worker
    finally:
        driver.close()
        os.chdir(before)


def test_d200_first_paint_sends_all_update_only_false(tmp_path):
    dev = _FakeDev()
    before = os.getcwd()
    driver = _make_driver(tmp_path, dev)
    try:
        driver.render([TileView(0, "a", "blue"), TileView(1, "b", "green")])
        assert _wait_until(lambda: dev.calls)
        buttons, update_only = dev.calls[0]
        assert set(buttons) == {0, 1}
        assert update_only is False
    finally:
        driver.close()
        os.chdir(before)


def test_d200_unchanged_tiles_skip_write(tmp_path):
    dev = _FakeDev()
    before = os.getcwd()
    driver = _make_driver(tmp_path, dev)
    tiles = [TileView(0, "a", "blue"), TileView(1, "b", "green")]
    try:
        driver.render(tiles)
        assert _wait_until(lambda: len(dev.calls) == 1)
        driver.render(tiles)  # identical icons -> empty diff -> no write
        time.sleep(0.15)
        assert len(dev.calls) == 1
    finally:
        driver.close()
        os.chdir(before)


def test_d200_only_changed_tile_is_sent(tmp_path):
    dev = _FakeDev()
    before = os.getcwd()
    driver = _make_driver(tmp_path, dev, icons=_ContentIcons())
    try:
        driver.render(
            [TileView(0, "a", "blue", time_text="1s"), TileView(1, "b", "green", time_text="2s")]
        )
        assert _wait_until(lambda: len(dev.calls) == 1)
        driver.render(
            [TileView(0, "a", "blue", time_text="9s"), TileView(1, "b", "green", time_text="2s")]
        )  # only index 0 changed
        assert _wait_until(lambda: len(dev.calls) == 2)
        buttons, update_only = dev.calls[1]
        assert set(buttons) == {0}
        assert update_only is True
    finally:
        driver.close()
        os.chdir(before)


def test_d200_working_write_updates_diff_state(tmp_path):
    dev = _FakeDev()
    before = os.getcwd()
    driver = _make_driver(tmp_path, dev, icons=_ContentIcons())
    try:
        driver.render([TileView(0, "a", "blue", time_text="1s")])
        assert _wait_until(lambda: len(dev.calls) == 1)
        driver.render_working([TileView(0, "a", "blue", time_text="2s")])
        assert _wait_until(lambda: len(dev.calls) == 2)
        # full render with the SAME content the working frame already painted -> skipped
        driver.render([TileView(0, "a", "blue", time_text="2s")])
        time.sleep(0.15)
        assert len(dev.calls) == 2
    finally:
        driver.close()
        os.chdir(before)


def test_d200_periodic_resync_resends_all_tiles(tmp_path):
    # After RESYNC_INTERVAL, every tile is re-sent (one small write each) even with
    # no content change, so a dropped async USB write self-heals within the interval.
    dev = _FakeDev()
    before = os.getcwd()
    driver = _make_driver(tmp_path, dev)
    tiles = [TileView(0, "a", "blue"), TileView(1, "b", "green")]
    try:
        driver.render(tiles)
        assert _wait_until(lambda: len(dev.calls) == 1)  # first paint: one full write
        driver.render(tiles)  # unchanged -> diff empty -> skipped
        time.sleep(0.15)
        assert len(dev.calls) == 1
        driver._last_full_ts -= driver.RESYNC_INTERVAL + 1  # force the re-sync window open
        driver.render(tiles)  # identical content, but re-sync re-sends every tile
        assert _wait_until(lambda: len(dev.calls) == 3)  # +2: one small write per tile
        resync = dev.calls[1:]
        assert {next(iter(b)) for b, _ in resync} == {0, 1}
        assert all(update_only for _, update_only in resync)
    finally:
        driver.close()
        os.chdir(before)


def test_d200_resync_window_stays_open_on_partial_failure(tmp_path):
    # If a per-tile resync write fails, _last_full_ts must NOT advance, so the next
    # render retries the resync instead of waiting the full RESYNC_INTERVAL.
    class _ResyncFailDev(_FakeDev):
        def set_buttons(self, buttons, update_only=False):
            # fail only the single-tile resync write for index 1
            if update_only and len(buttons) == 1 and 1 in buttons:
                raise RuntimeError("usb boom")
            super().set_buttons(buttons, update_only)

    dev = _ResyncFailDev()
    before = os.getcwd()
    driver = _make_driver(tmp_path, dev)
    tiles = [TileView(0, "a", "blue"), TileView(1, "b", "green")]
    try:
        driver.render(tiles)  # first paint: both tiles in one update_only=False write
        assert _wait_until(lambda: len(dev.calls) == 1)
        driver._last_full_ts -= driver.RESYNC_INTERVAL + 1  # open the resync window
        driver.render(tiles)  # resync: index 0 ok, index 1 raises -> window stays open
        assert _wait_until(lambda: len(dev.calls) == 2)  # only index 0's write landed
        driver.render(tiles)  # window still open -> resync retried -> index 0 re-sent again
        assert _wait_until(lambda: len(dev.calls) == 3)
    finally:
        driver.close()
        os.chdir(before)


def test_d200_failed_write_is_retried(tmp_path):
    class _FlakyDev(_FakeDev):
        def __init__(self):
            super().__init__()
            self.fail_next = True

        def set_buttons(self, buttons, update_only=False):
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("usb boom")
            super().set_buttons(buttons, update_only)

    dev = _FlakyDev()
    before = os.getcwd()
    driver = _make_driver(tmp_path, dev)
    tiles = [TileView(0, "a", "blue")]
    try:
        driver.render(tiles)  # first paint raises -> _last_icon stays empty
        time.sleep(0.15)
        assert dev.calls == []
        driver.render(tiles)  # retry -> succeeds as a fresh first paint
        assert _wait_until(lambda: len(dev.calls) == 1)
        buttons, update_only = dev.calls[0]
        assert set(buttons) == {0}
        assert update_only is False
    finally:
        driver.close()
        os.chdir(before)
