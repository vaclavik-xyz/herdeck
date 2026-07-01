import os

from herdeck import runtime


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

    def on_press(self, cb):
        self.press_cb = cb

    async def run_reader(self):
        return None

    def close(self):
        pass


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


def test_build_runtime_http_only_when_no_device(monkeypatch, tmp_path):
    path = _runtime_dir(monkeypatch, tmp_path)
    app = FakeApp()

    def boom_factory(config):
        raise RuntimeError("No openable Ulanzi D200")

    app2, sink, info, p = runtime.build_runtime(
        app_factory=lambda host, port: app,
        driver_factory=boom_factory,
    )
    assert sink is None  # HTTP-only
    assert app.sinks == []
    assert os.path.exists(path)  # discovery still written


def test_build_runtime_can_skip_discovery_write(monkeypatch, tmp_path):
    path = _runtime_dir(monkeypatch, tmp_path)
    app = FakeApp()
    runtime.build_runtime(
        app_factory=lambda host, port: app,
        driver_factory=lambda config: FakeDriver(),
        write_discovery=False,
    )
    assert not os.path.exists(path)


def test_d200_press_routes_to_app_press(monkeypatch, tmp_path):
    _runtime_dir(monkeypatch, tmp_path)
    app = FakeApp()
    pressed = []
    app.press = pressed.append
    drv = FakeDriver()
    runtime.build_runtime(
        app_factory=lambda host, port: app,
        driver_factory=lambda config: drv,
    )
    drv.press_cb(5)  # a physical D200 button
    assert pressed == [5]  # routed into the converged app's press
