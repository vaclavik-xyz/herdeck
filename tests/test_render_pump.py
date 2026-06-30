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


def test_keep_alive_fires_even_during_continuous_rendering():
    # handle_tick submits render_working faster than the keep-alive interval; the
    # device must still get its keep-alive on schedule, not be starved by activity.
    beats = []
    pump = RenderPump(
        paint=lambda ch, p: None,
        keep_alive=lambda: beats.append(1),
        keep_alive_interval=0.05,
    )
    pump.start()
    stop = threading.Event()

    def spam():
        while not stop.is_set():
            pump.submit("working", "w")
            time.sleep(0.01)

    spammer = threading.Thread(target=spam)
    spammer.start()
    try:
        assert _wait_until(lambda: len(beats) >= 2, timeout=2.0)
    finally:
        stop.set()
        spammer.join()
        pump.close()


def test_full_tiles_render_not_clobbered_by_stale_working_in_same_batch():
    # When a full "tiles" render and a "working" partial coalesce into one batch,
    # the stale partial must not be painted over the fresh full render.
    seen = []
    started = threading.Event()
    release = threading.Event()

    def paint(ch, p):
        seen.append((ch, p))
        if p == "hold":
            started.set()
            release.wait(2.0)

    pump = RenderPump(paint=paint)
    pump.start()
    try:
        pump.submit("tiles", "hold")
        assert started.wait(2.0)  # worker stuck on the first paint
        pump.submit("working", "stale_spinner")  # older partial...
        pump.submit("tiles", "fresh_full")  # ...superseded by this full render
        release.set()
        assert _wait_until(lambda: ("tiles", "fresh_full") in seen)
    finally:
        release.set()
        pump.close()
    after_fresh = seen[seen.index(("tiles", "fresh_full")) :]
    assert ("working", "stale_spinner") not in after_fresh


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


def test_paint_runs_inside_a_running_event_loop():
    """Device libraries (strmdck D200) schedule USB writes via
    asyncio.get_running_loop()+create_task, so a paint MUST run inside a running
    loop — else the write is silently dropped and the device stays blank."""
    import asyncio

    outcome = []

    def paint(ch, p):
        try:
            asyncio.get_running_loop()
            outcome.append("loop")
        except RuntimeError:
            outcome.append("noloop")

    pump = RenderPump(paint=paint)
    pump.start()
    try:
        pump.submit("tiles", "x")
        assert _wait_until(lambda: bool(outcome))
    finally:
        pump.close()
    assert outcome == ["loop"]


def test_fire_and_forget_task_from_paint_runs_to_completion():
    """strmdck's _write_packet does loop.create_task(write()) and returns at once;
    the pump must let that scheduled write actually run each cycle (mirrors the
    real device-write path)."""
    import asyncio

    ran = threading.Event()

    async def _write():
        ran.set()

    def paint(ch, p):
        asyncio.get_running_loop().create_task(_write())

    pump = RenderPump(paint=paint)
    pump.start()
    try:
        pump.submit("tiles", "x")
        assert ran.wait(2.0)  # the create_task'd coroutine executed before close
    finally:
        pump.close()


def test_paint_block_timing_includes_async_drain():
    """The worker-block time (the freeze metric) must include the async USB-write
    tasks paints schedule via create_task and the pump drains each cycle — not
    just the synchronous return of paint."""
    import asyncio

    async def _slow_write():
        await asyncio.sleep(0.05)

    def paint(ch, p):
        asyncio.get_running_loop().create_task(_slow_write())

    pump = RenderPump(paint=paint)
    pump.start()
    try:
        pump.submit("tiles", "x")
        assert _wait_until(lambda: pump._last_paint_ms is not None)
    finally:
        pump.close()
    assert pump._last_paint_ms >= 50.0  # the 50ms async drain is included


def test_slow_paint_block_warns(caplog):
    import asyncio
    import logging

    async def _slow_write():
        await asyncio.sleep(0.05)

    def paint(ch, p):
        asyncio.get_running_loop().create_task(_slow_write())

    pump = RenderPump(paint=paint)
    pump.SLOW_PAINT_MS = 10.0  # instance override so the 50ms drain trips the warning
    pump.start()
    try:
        with caplog.at_level(logging.WARNING, logger="herdeck.driver.render_pump"):
            pump.submit("tiles", "x")
            assert _wait_until(lambda: "render worker blocked" in caplog.text, timeout=2.0)
    finally:
        pump.close()
