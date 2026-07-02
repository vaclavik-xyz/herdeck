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


def test_d200_unchanged_tiles_still_write(tmp_path):
    # render() is always a full-set write (update_only=False), even when all
    # tile icons are identical to the last write. The diff optimisation is gone;
    # every render() call produces exactly one USB write with all tiles.
    dev = _FakeDev()
    before = os.getcwd()
    driver = _make_driver(tmp_path, dev)
    tiles = [TileView(0, "a", "blue"), TileView(1, "b", "green")]
    try:
        driver.render(tiles)
        assert _wait_until(lambda: len(dev.calls) == 1)
        driver.render(tiles)  # identical icons -> still a full write (no diff skip)
        assert _wait_until(lambda: len(dev.calls) == 2)
        buttons, update_only = dev.calls[1]
        assert set(buttons) == {0, 1}
        assert update_only is False
    finally:
        driver.close()
        os.chdir(before)


def test_d200_render_always_sends_all_tiles(tmp_path):
    # render() sends every tile as a single full-set write (update_only=False),
    # regardless of which tiles changed. This replaces the old per-index diff.
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
        )  # only index 0 changed, but ALL tiles are re-sent
        assert _wait_until(lambda: len(dev.calls) == 2)
        buttons, update_only = dev.calls[1]
        assert set(buttons) == {0, 1}  # both tiles sent, not just the changed one
        assert update_only is False
    finally:
        driver.close()
        os.chdir(before)


def test_d200_working_write_updates_diff_state(tmp_path):
    # render_working() still uses the per-index diff (only changed working tiles
    # are written, update_only=True). A subsequent render() for the same content
    # that the working frame already painted still sends all tiles (full set,
    # update_only=False) — the diff optimisation does NOT apply to render().
    dev = _FakeDev()
    before = os.getcwd()
    driver = _make_driver(tmp_path, dev, icons=_ContentIcons())
    try:
        driver.render([TileView(0, "a", "blue", time_text="1s")])
        assert _wait_until(lambda: len(dev.calls) == 1)
        driver.render_working([TileView(0, "a", "blue", time_text="2s")])
        assert _wait_until(lambda: len(dev.calls) == 2)
        # working write used update_only=True (partial, only the changed spinner tile)
        working_buttons, working_update_only = dev.calls[1]
        assert set(working_buttons) == {0}
        assert working_update_only is True
        # render() always sends a full set regardless of what the working frame painted
        driver.render([TileView(0, "a", "blue", time_text="2s")])
        assert _wait_until(lambda: len(dev.calls) == 3)
        full_buttons, full_update_only = dev.calls[2]
        assert set(full_buttons) == {0}
        assert full_update_only is False
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


def test_neutralize_retry_sleep_noops_strmdck_sleep(monkeypatch):
    # The function must replace strmdck.devices.ulanzi_d200's `time` so sleep() is a
    # no-op (kills the retry-loop throttle) while every other time.* passes through.
    # strmdck is not installed locally, so fake the module tree in sys.modules.
    import sys
    import time as real_time
    import types

    fake_strmdck = types.ModuleType("strmdck")
    fake_devices = types.ModuleType("strmdck.devices")
    fake_mod = types.ModuleType("strmdck.devices.ulanzi_d200")
    fake_mod.time = real_time
    fake_devices.ulanzi_d200 = fake_mod
    fake_strmdck.devices = fake_devices
    monkeypatch.setitem(sys.modules, "strmdck", fake_strmdck)
    monkeypatch.setitem(sys.modules, "strmdck.devices", fake_devices)
    monkeypatch.setitem(sys.modules, "strmdck.devices.ulanzi_d200", fake_mod)

    from herdeck.driver.d200 import _neutralize_retry_sleep

    _neutralize_retry_sleep()

    # sleep() is now a no-op: a 2s sleep returns instantly
    t0 = real_time.monotonic()
    fake_mod.time.sleep(2.0)
    assert real_time.monotonic() - t0 < 0.1
    # other time.* still work (passthrough to the real module)
    assert isinstance(fake_mod.time.monotonic(), float)
    # idempotent: a second call does not re-wrap the proxy
    patched = fake_mod.time
    _neutralize_retry_sleep()
    assert fake_mod.time is patched


def test_neutralize_retry_sleep_is_failsafe_when_strmdck_missing(monkeypatch):
    # If strmdck can't be imported, the function must swallow the error (driver still works).
    import sys

    monkeypatch.setitem(sys.modules, "strmdck", None)  # forces ImportError on import
    from herdeck.driver.d200 import _neutralize_retry_sleep

    _neutralize_retry_sleep()  # must not raise


def test_d200_unchanged_panel_skips_recompose_but_still_writes(tmp_path, monkeypatch):
    import herdeck.driver.d200 as d200_mod

    calls = {"n": 0}
    real = d200_mod.compose_panel

    def counting(panel):
        calls["n"] += 1
        return real(panel)

    monkeypatch.setattr(d200_mod, "compose_panel", counting)
    dev = _FakeDev()
    before = os.getcwd()
    driver = _make_driver(tmp_path, dev)
    try:
        driver.render_panel(PanelView("t", ["a"], "grey"))
        assert _wait_until(lambda: len(dev.calls) == 1)
        driver.render_panel(PanelView("t", ["a"], "grey"))
        assert _wait_until(lambda: len(dev.calls) == 2)  # device write still issued
        assert calls["n"] == 1  # identical content composed only once
        driver.render_panel(PanelView("t2", ["a"], "grey"))
        assert _wait_until(lambda: len(dev.calls) == 3)
        assert calls["n"] == 2
    finally:
        driver.close()
        os.chdir(before)


# --- combined frame write (tiles + panel in ONE set) ------------------------


def test_render_frame_is_one_combined_full_set_write(tmp_path):
    dev = _FakeDev()
    before = os.getcwd()
    driver = _make_driver(tmp_path, dev)
    try:
        panel = PanelView("5 agents", ["online"], "grey")
        driver.render_frame([TileView(0, "x", "blue"), TileView(1, "y", "green")], panel)
        assert _wait_until(lambda: dev.calls)
        assert len(dev.calls) == 1
        buttons, update_only = dev.calls[0]
        assert update_only is False  # full set: nothing for the firmware to drop
        assert 0 in buttons and 1 in buttons
        assert 13 in buttons and 14 in buttons  # panel cells ride the same set
    finally:
        driver.close()
        os.chdir(before)


def test_render_frame_skips_byte_identical_frames(tmp_path):
    dev = _FakeDev()
    before = os.getcwd()
    driver = _make_driver(tmp_path, dev)
    try:
        panel = PanelView("t", ["l"], "grey")
        tiles = [TileView(0, "x", "blue")]
        driver.render_frame(tiles, panel)
        assert _wait_until(lambda: len(dev.calls) == 1)
        # identical content (same icon names, same panel key) -> no device write
        driver.render_frame(tiles, panel)
        time.sleep(0.1)
        assert len(dev.calls) == 1
        # any change (here: the panel) writes a fresh combined set
        driver.render_frame(tiles, PanelView("t2", ["l"], "grey"))
        assert _wait_until(lambda: len(dev.calls) == 2)
    finally:
        driver.close()
        os.chdir(before)


def test_legacy_split_write_invalidates_the_frame_signature(tmp_path):
    dev = _FakeDev()
    before = os.getcwd()
    driver = _make_driver(tmp_path, dev)
    try:
        panel = PanelView("t", ["l"], "grey")
        tiles = [TileView(0, "x", "blue")]
        driver.render_frame(tiles, panel)
        assert _wait_until(lambda: len(dev.calls) == 1)
        driver.render(tiles)  # legacy path touches the device behind the frame cache
        assert _wait_until(lambda: len(dev.calls) == 2)
        driver.render_frame(tiles, panel)  # must NOT be skipped after a split write
        assert _wait_until(lambda: len(dev.calls) == 3)
    finally:
        driver.close()
        os.chdir(before)


# --- in-memory zip builder ---------------------------------------------------


def test_build_button_zip_layout_and_validity():
    import io as _io
    import zipfile as _zipfile

    from herdeck.driver.d200 import _zip_chunk_bytes_valid, build_button_zip

    manifest = b'{"0_0":{"State":0}}'
    icons = {"a.png": b"PNGDATA-A", "b.png": b"PNGDATA-B"}
    data = build_button_zip(manifest, icons)
    assert _zip_chunk_bytes_valid(data)
    with _zipfile.ZipFile(_io.BytesIO(data)) as z:
        names = z.namelist()
        assert names[0] == "manifest.json"  # no dummy on a clean first attempt
        assert set(names) == {"manifest.json", "icons/a.png", "icons/b.png"}
        assert z.read("manifest.json") == manifest
        assert z.read("icons/a.png") == b"PNGDATA-A"


def test_build_button_zip_retries_with_dummy_until_valid(monkeypatch):
    import herdeck.driver.d200 as d200mod

    real = d200mod._zip_chunk_bytes_valid
    fails = {"n": 2}

    def flaky(data):
        if fails["n"] > 0:
            fails["n"] -= 1
            return False
        return real(data)

    monkeypatch.setattr(d200mod, "_zip_chunk_bytes_valid", flaky)
    data = d200mod.build_button_zip(b"{}", {"a.png": b"x"}, rand=lambda n: b"\x42" * n)
    import io as _io
    import zipfile as _zipfile

    with _zipfile.ZipFile(_io.BytesIO(data)) as z:
        assert z.namelist()[0] == "dummy.txt"  # dummy leads the archive, like strmdck
