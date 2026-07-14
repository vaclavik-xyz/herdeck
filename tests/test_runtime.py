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
