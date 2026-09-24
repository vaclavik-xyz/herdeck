import asyncio
import os
import threading
import time

from herdeck import runtime
from herdeck.deckapp.sinks import RenderFrame


class FakeApp:
    """Minimal DeckApp stand-in for the runtime entry (no bridge, no HTTP)."""

    def __init__(self):
        self.host = "127.0.0.1"
        self.port = 8800
        self.token = "t0ken"
        self.source_name = "live"
        self.slots = 13
        self.config = object()
        self.sinks = []
        self.closed = False

    def add_sink(self, sink):
        self.sinks.append(sink)

    def press(self, index):
        pass

    def close(self):
        self.closed = True


class FakeDriver:
    def __init__(self):
        self.press_cb = None
        self.disconnect = threading.Event()
        self.closed = False
        self.frames = []

    def on_press(self, cb):
        self.press_cb = cb

    async def run_reader(self):
        await asyncio.to_thread(self.disconnect.wait)
        if not self.closed:
            raise OSError("read error")

    def render_frame(self, tiles, panel):
        self.frames.append(([tile.index for tile in tiles], panel))

    def close(self):
        self.closed = True
        self.disconnect.set()


class _RenderState:
    def __init__(self):
        self.tiles = []
        self.panel = "panel"


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _runtime_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HERDECK_RUNTIME_DIR", str(tmp_path))
    return str(tmp_path / "runtime.json")


def test_frozen_runtime_selftest_covers_dynamic_d200_imports():
    assert runtime.SELFTEST_IMPORTS == (
        "herdeck.deckapp.onboarding",
        "herdeck.deckapp.local_bridge",
        "herdeck.bridge",
        "herdeck.runtime",
        "herdeck.deckapp.maintenance",
        "herdeck.service",
        "herdeck.managed",
        "herdeck.driver.d200",
        "strmdck",
        "strmdck.devices.ulanzi_d200",
        "hid",
        "resvg_py",
    )


def test_managed_runtime_does_not_own_shared_discovery(monkeypatch):
    monkeypatch.delenv("HERDECK_RUNTIME_MANAGED", raising=False)
    assert runtime._should_write_discovery() is True

    monkeypatch.setenv("HERDECK_RUNTIME_MANAGED", "1")
    assert runtime._should_write_discovery() is False


def test_build_runtime_attaches_d200_sink_when_device_present(monkeypatch, tmp_path):
    path = _runtime_dir(monkeypatch, tmp_path)
    app = FakeApp()
    app2, sink, info, p = runtime.build_runtime(
        app_factory=lambda host, port: app,
        driver_factory=lambda config: FakeDriver(),
    )
    try:
        assert _wait_until(lambda: app.sinks and app.sinks[0] is sink)
        assert app2 is app
        assert sink is not None and app.sinks == [sink]  # a sink was attached
        assert p == path
        assert info == {
            "url": "http://127.0.0.1:8800",
            "host": "127.0.0.1",
            "port": 8800,
            "token": "t0ken",
            "source": "live",
        }
        assert os.path.exists(path)  # discovery written
    finally:
        sink.close()


def test_build_runtime_http_only_when_no_device(monkeypatch, tmp_path):
    path = _runtime_dir(monkeypatch, tmp_path)
    app = FakeApp()

    def boom_factory(config):
        raise RuntimeError("No openable Ulanzi D200")

    app2, sink, info, p = runtime.build_runtime(
        app_factory=lambda host, port: app,
        driver_factory=boom_factory,
    )
    try:
        assert app2 is app
        assert sink is not None  # supervisor remains ready for a later USB attach
        assert app.sinks == [sink]
        assert os.path.exists(path)  # discovery still written
    finally:
        sink.close()


def test_build_runtime_can_skip_discovery_write(monkeypatch, tmp_path):
    path = _runtime_dir(monkeypatch, tmp_path)
    app = FakeApp()
    _, sink, _, _ = runtime.build_runtime(
        app_factory=lambda host, port: app,
        driver_factory=lambda config: FakeDriver(),
        write_discovery=False,
    )
    try:
        assert not os.path.exists(path)
    finally:
        sink.close()


def test_d200_press_routes_to_app_press(monkeypatch, tmp_path):
    _runtime_dir(monkeypatch, tmp_path)
    app = FakeApp()
    pressed = []
    app.press = pressed.append
    drv = FakeDriver()
    _, sink, _, _ = runtime.build_runtime(
        app_factory=lambda host, port: app,
        driver_factory=lambda config: drv,
    )
    try:
        assert _wait_until(lambda: drv.press_cb is not None)
        drv.press_cb(5)  # a physical D200 button
        assert pressed == [5]  # routed into the converged app's press
    finally:
        sink.close()


def test_runtime_reopens_and_repaints_d200_after_reader_disconnect(monkeypatch, tmp_path):
    """A stale HID reader after Mac sleep must be replaced without restarting runtime."""
    _runtime_dir(monkeypatch, tmp_path)
    app = FakeApp()
    drivers = []

    def factory(config):
        driver = FakeDriver()
        drivers.append(driver)
        return driver

    _, sink, _, _ = runtime.build_runtime(
        app_factory=lambda host, port: app,
        driver_factory=factory,
    )
    frame = RenderFrame(render=_RenderState(), working=None, full=True)
    try:
        assert _wait_until(lambda: len(drivers) == 1)
        sink.deliver(frame)
        assert _wait_until(lambda: drivers[0].frames == [([], "panel")])

        drivers[0].disconnect.set()  # macOS invalidates the HID handle during sleep

        assert _wait_until(lambda: len(drivers) >= 2)
        assert _wait_until(lambda: drivers[1].frames == [([], "panel")])
    finally:
        sink.close()


def test_logging_keeps_notification_routes_at_info(monkeypatch):
    import logging

    calls = []
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: calls.append(kw))
    for name in ("herdeck.notify", "herdeck.deckapp.live"):
        monkeypatch.setattr(logging.getLogger(name), "level", logging.NOTSET)

    runtime.configure_logging(debug=False)

    assert calls[0]["level"] == logging.WARNING
    assert logging.getLogger("herdeck.notify").getEffectiveLevel() == logging.INFO
    assert logging.getLogger("herdeck.deckapp.live").getEffectiveLevel() == logging.INFO
    # The shell-claim timeline + fallback reasons ride the herdeck.notify tree.
    assert logging.getLogger("herdeck.notify.claim").getEffectiveLevel() == logging.INFO

    runtime.configure_logging(debug=True)
    assert calls[1] == {"level": logging.DEBUG}


def test_two_runtimes_share_one_d200_owner(monkeypatch, tmp_path):
    """The launchd runtime and an app's own sidecar must not both open the D200."""
    _runtime_dir(monkeypatch, tmp_path)
    first_opens, second_opens = [], []

    def factory(opens):
        def make(config):
            opens.append(1)
            return FakeDriver()

        return make

    _, first, _, _ = runtime.build_runtime(
        app_factory=lambda host, port: FakeApp(), driver_factory=factory(first_opens)
    )
    second = None
    try:
        assert _wait_until(lambda: first_opens == [1])
        _, second, _, _ = runtime.build_runtime(
            app_factory=lambda host, port: FakeApp(),
            driver_factory=factory(second_opens),
            write_discovery=False,
        )
        time.sleep(0.1)
        assert second_opens == []
        assert os.path.exists(tmp_path / "d200.lock")
    finally:
        first.close()
        if second is not None:
            second.close()


def test_import_selftest_fails_without_the_vendored_font(monkeypatch):
    # The frozen bundle must carry assets/fonts: without it every tile silently
    # falls back to a per-OS system font.
    import pytest

    from herdeck import icons

    # The D200 modules (strmdck, hid) are not installed on the CI test job;
    # the import list itself is pinned above.
    monkeypatch.setattr(runtime, "SELFTEST_IMPORTS", ())
    assert runtime._run_import_selftest() == 0
    monkeypatch.setattr(icons, "bundled_font_path", lambda *, bold=True: None)

    with pytest.raises(RuntimeError, match="font"):
        runtime._run_import_selftest()
