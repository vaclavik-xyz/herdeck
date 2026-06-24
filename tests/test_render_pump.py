import threading
import time

from herdeck.driver.render_pump import RenderPump


def _wait_until(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return False


def test_pump_paints_submitted_channel():
    painted = []
    pump = RenderPump(paint=lambda ch, p: painted.append((ch, p)))
    pump.start()
    try:
        pump.submit("tiles", "A")
        assert _wait_until(lambda: ("tiles", "A") in painted)
    finally:
        pump.close()


def test_pump_coalesces_bursts_to_latest():
    # While the worker is busy painting the first job, a burst of later submits
    # to the same channel must collapse to only the LATEST — intermediates dropped.
    seen = []
    started = threading.Event()
    release = threading.Event()

    def paint(ch, p):
        seen.append((ch, p))
        if p == "first":
            started.set()
            release.wait(2.0)

    pump = RenderPump(paint=paint)
    pump.start()
    try:
        pump.submit("tiles", "first")
        assert started.wait(2.0)  # worker is now blocked inside the first paint
        pump.submit("tiles", "second")  # queued behind the in-flight paint...
        pump.submit("tiles", "third")  # ...latest-wins, "second" must be dropped
        release.set()
        assert _wait_until(lambda: ("tiles", "third") in seen)
    finally:
        release.set()
        pump.close()
    tiles_painted = [p for c, p in seen if c == "tiles"]
    assert tiles_painted == ["first", "third"]


def test_submit_is_non_blocking_while_worker_busy():
    started = threading.Event()
    release = threading.Event()

    def paint(ch, p):
        started.set()
        release.wait(2.0)

    pump = RenderPump(paint=paint)
    pump.start()
    try:
        pump.submit("tiles", "x")
        assert started.wait(2.0)  # worker stuck painting "x"
        t0 = time.monotonic()
        pump.submit("tiles", "y")  # must return at once, not wait for the paint
        elapsed = time.monotonic() - t0
        assert elapsed < 0.1
    finally:
        release.set()
        pump.close()


def test_distinct_channels_all_painted_newest_each():
    seen = []
    pump = RenderPump(paint=lambda ch, p: seen.append((ch, p)))
    pump.start()
    try:
        pump.submit("tiles", "t1")
        pump.submit("panel", "p1")
        assert _wait_until(lambda: ("tiles", "t1") in seen and ("panel", "p1") in seen)
    finally:
        pump.close()


def test_keep_alive_fires_when_idle():
    beats = []
    pump = RenderPump(
        paint=lambda ch, p: None,
        keep_alive=lambda: beats.append(1),
        keep_alive_interval=0.05,
    )
    pump.start()
    try:
        assert _wait_until(lambda: len(beats) >= 2, timeout=2.0)
    finally:
        pump.close()


def test_paint_exception_does_not_kill_worker():
    seen = []

    def paint(ch, p):
        if p == "boom":
            raise RuntimeError("render blew up")
        seen.append(p)

    pump = RenderPump(paint=paint)
    pump.start()
    try:
        pump.submit("tiles", "boom")
        pump.submit("tiles", "ok")
        assert _wait_until(lambda: "ok" in seen)
    finally:
        pump.close()


def test_close_stops_worker_thread():
    pump = RenderPump(paint=lambda ch, p: None)
    pump.start()
    pump.close()
    assert not pump._thread.is_alive()
