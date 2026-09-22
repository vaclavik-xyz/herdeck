import asyncio
import threading
import time

from herdeck.deckapp.sinks import D200Sink, ReconnectingD200Sink, RenderFrame
from herdeck.driver.base import PanelView, TileView


class _Tile:
    def __init__(self, index):
        self.index = index


class _RS:
    """Stand-in for the orchestrator RenderState (just .tiles + .panel)."""

    def __init__(self, tiles, panel="PANEL"):
        self.tiles = tiles
        self.panel = panel


class FakeDriver:
    def __init__(self):
        self.full_renders = []  # list of [tile.index, ...]
        self.panels = []
        self.working_renders = []  # list of [tile.index, ...]
        self.press_cb = None
        self.closed = False
        self.reader_ran = threading.Event()

    def render(self, tiles):
        self.full_renders.append([t.index for t in tiles])

    def render_panel(self, panel):
        self.panels.append(panel)

    def render_working(self, tiles):
        self.working_renders.append([t.index for t in tiles])

    def on_press(self, cb):
        self.press_cb = cb

    async def run_reader(self):
        self.reader_ran.set()  # returns immediately (no device)

    def close(self):
        self.closed = True


def _sink(driver, *, slots=13, on_press=None, start_reader=False):
    return D200Sink(
        driver, on_press=(on_press or (lambda i: None)), slots=slots, start_reader=start_reader
    )


def test_full_frame_renders_all_in_range_tiles_and_panel():
    drv = FakeDriver()
    sink = _sink(drv)
    rs = _RS([_Tile(0), _Tile(1), _Tile(13), _Tile(14)])  # 13/14 are panel cells, not tiles
    sink.deliver(RenderFrame(render=rs, working=None, full=True))
    assert drv.full_renders == [[0, 1]]  # only indices < slots(13)
    assert drv.panels == ["PANEL"]
    assert drv.working_renders == []


def test_slot_geometry_can_expand_after_profile_switch():
    drv = FakeDriver()
    sink = _sink(drv, slots=10)
    rs = _RS([_Tile(i) for i in range(13)])

    sink.deliver(RenderFrame(render=rs, working=None, full=True))
    sink.set_slots(13)
    sink.deliver(RenderFrame(render=rs, working=None, full=True))

    assert drv.full_renders == [list(range(10)), list(range(13))]


def test_working_frame_renders_full_frame():
    # D200Sink always renders a full frame regardless of frame.full/frame.working.
    # The D200 firmware drops cells absent from a partial write, so even a
    # working/spinner tick must re-send every tile + the panel to keep the whole
    # deck lit.
    drv = FakeDriver()
    sink = _sink(drv, slots=3)
    rs = _RS([_Tile(0), _Tile(1), _Tile(2), _Tile(5)])  # 5 is out of range (slots=3)
    sink.deliver(RenderFrame(render=rs, working=[1], full=False))
    assert drv.full_renders == [[0, 1, 2]]  # all in-range tiles; not just working=[1]
    assert drv.panels == ["PANEL"]
    assert drv.working_renders == []  # render_working is never called


def test_ticker_frame_does_not_touch_physical_d200():
    drv = FakeDriver()
    sink = _sink(drv, slots=3)
    rs = _RS([_Tile(0), _Tile(1), _Tile(2)])

    sink.deliver(RenderFrame(render=rs, working=[1], full=False, ticker=True))

    assert drv.full_renders == []
    assert drv.panels == []


def test_working_frame_with_no_working_tiles_still_renders_full_frame():
    # Even a working frame with an empty working set triggers a full render —
    # D200Sink ignores frame.working entirely.
    drv = FakeDriver()
    sink = _sink(drv)
    rs = _RS([_Tile(0), _Tile(1)])
    sink.deliver(RenderFrame(render=rs, working=[], full=False))
    assert drv.full_renders == [[0, 1]]
    assert drv.panels == ["PANEL"]
    assert drv.working_renders == []


def test_press_callback_is_registered_on_the_driver():
    drv = FakeDriver()
    got = []
    _sink(drv, on_press=got.append)
    assert drv.press_cb is not None
    drv.press_cb(7)  # a physical button fires
    assert got == [7]


def test_close_closes_the_driver():
    drv = FakeDriver()
    sink = _sink(drv)
    sink.close()
    assert drv.closed is True


def test_start_reader_runs_the_driver_reader():
    drv = FakeDriver()
    sink = D200Sink(drv, on_press=lambda i: None, slots=13, start_reader=True)
    try:
        assert drv.reader_ran.wait(timeout=2.0)  # the reader thread ran run_reader()
    finally:
        sink.close()


class FrameDriver(FakeDriver):
    """Driver double exposing the combined-frame API."""

    def __init__(self):
        super().__init__()
        self.frames = []  # (tile indices, panel)

    def render_frame(self, tiles, panel):
        self.frames.append(([t.index for t in tiles], panel))


def test_sink_prefers_the_combined_frame_write():
    drv = FrameDriver()
    sink = D200Sink(drv, on_press=lambda i: None, slots=13, start_reader=False)
    rs = _RS([_Tile(0), _Tile(1), _Tile(13)], panel="P")
    sink.deliver(RenderFrame(render=rs, working=None, full=True))
    assert drv.frames == [([0, 1], "P")]  # one combined call, slots-clipped
    assert drv.full_renders == [] and drv.panels == []  # legacy path untouched


def test_non_ticker_snapshots_freeze_only_spinner_and_elapsed_text():
    class CapturingDriver(FrameDriver):
        def __init__(self):
            super().__init__()
            self.tile_views = []

        def render_frame(self, tiles, panel):
            self.tile_views.append(tiles)

    driver = CapturingDriver()
    sink = _sink(driver, start_reader=False)
    panel = PanelView("Agents")

    first = TileView(0, "api", "blue", spinner=1, time_text="5s", status_text="WORKING")
    sink.deliver(RenderFrame(_RS([first], panel), working=None, full=True))
    volatile_only = TileView(
        0, "api", "blue", spinner=2, time_text="10s", status_text="WORKING"
    )
    sink.deliver(RenderFrame(_RS([volatile_only], panel), working=None, full=True))
    changed = TileView(0, "api", "green", spinner=3, time_text="0s", status_text="DONE")
    sink.deliver(RenderFrame(_RS([changed], panel), working=None, full=True))

    assert driver.tile_views[1][0].spinner == 1
    assert driver.tile_views[1][0].time_text == "5s"
    assert driver.tile_views[2][0].spinner == 3
    assert driver.tile_views[2][0].status_text == "DONE"


def test_spinner_disappearing_is_a_semantic_d200_change():
    class CapturingDriver(FrameDriver):
        def __init__(self):
            super().__init__()
            self.tile_views = []

        def render_frame(self, tiles, panel):
            self.tile_views.append(tiles)

    driver = CapturingDriver()
    sink = _sink(driver, start_reader=False)
    panel = PanelView("Agents")
    sink.deliver(
        RenderFrame(
            _RS([TileView(0, "api", "blue", spinner=2, time_text="5s")], panel),
            working=None,
            full=True,
        )
    )
    sink.deliver(
        RenderFrame(
            _RS([TileView(0, "api", "blue", spinner=None, time_text="10s")], panel),
            working=None,
            full=True,
        )
    )

    assert driver.tile_views[1][0].spinner is None
    assert driver.tile_views[1][0].time_text == "10s"


def test_reconnecting_sink_retries_initially_missing_device_and_paints_latest_frame():
    class ConnectedDriver(FrameDriver):
        def __init__(self):
            super().__init__()
            self.release_reader = threading.Event()

        async def run_reader(self):
            await asyncio.to_thread(self.release_reader.wait)

        def close(self):
            super().close()
            self.release_reader.set()

    driver = ConnectedDriver()
    attempts = []

    def factory():
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("device still resuming")
        return driver

    sink = ReconnectingD200Sink(
        factory,
        on_press=lambda i: None,
        slots=13,
        retry_interval=0.01,
    )
    rs = _RS([_Tile(0), _Tile(1)], panel="latest")
    try:
        sink.deliver(RenderFrame(render=rs, working=None, full=True))
        deadline = time.monotonic() + 2.0
        while not driver.frames and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(attempts) == 2
        assert driver.frames == [([0, 1], "latest")]
    finally:
        sink.close()


def test_reconnecting_sink_reopens_active_driver_on_reconfigure():
    class ConnectedDriver(FrameDriver):
        def __init__(self):
            super().__init__()
            self.release_reader = threading.Event()

        async def run_reader(self):
            await asyncio.to_thread(self.release_reader.wait)

        def close(self):
            super().close()
            self.release_reader.set()

    drivers = []

    def factory():
        driver = ConnectedDriver()
        drivers.append(driver)
        return driver

    sink = ReconnectingD200Sink(
        factory,
        on_press=lambda i: None,
        slots=13,
        retry_interval=0.01,
    )
    frame = RenderFrame(render=_RS([_Tile(0)], panel="latest"), working=None, full=True)
    try:
        sink.deliver(frame)
        deadline = time.monotonic() + 2.0
        while (not drivers or not drivers[0].frames) and time.monotonic() < deadline:
            time.sleep(0.01)

        sink.reconfigure()

        deadline = time.monotonic() + 2.0
        while len(drivers) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(drivers) >= 2
        assert drivers[0].closed is True
        deadline = time.monotonic() + 2.0
        while not drivers[1].frames and time.monotonic() < deadline:
            time.sleep(0.01)
        assert drivers[1].frames == [([0], "latest")]
    finally:
        sink.close()


def test_reconnecting_sink_cannot_overwrite_concurrent_frame_with_stale_repaint():
    class BlockingDriver(FakeDriver):
        def __init__(self):
            super().__init__()
            self.frames = []
            self.reader_release = threading.Event()
            self.old_frame_started = threading.Event()
            self.old_frame_release = threading.Event()

        async def run_reader(self):
            await asyncio.to_thread(self.reader_release.wait)

        def render_frame(self, tiles, panel):
            if panel == "old":
                self.old_frame_started.set()
                self.old_frame_release.wait(timeout=2.0)
            self.frames.append(panel)

        def close(self):
            super().close()
            self.reader_release.set()
            self.old_frame_release.set()

    driver = BlockingDriver()
    allow_attach = threading.Event()

    def factory():
        allow_attach.wait(timeout=2.0)
        return driver

    sink = ReconnectingD200Sink(factory, on_press=lambda i: None, slots=13)
    old = RenderFrame(render=_RS([], panel="old"), working=None, full=True)
    new = RenderFrame(render=_RS([], panel="new"), working=None, full=True)
    new_done = threading.Event()
    try:
        sink.deliver(old)
        allow_attach.set()
        assert driver.old_frame_started.wait(timeout=2.0)

        delivery = threading.Thread(target=lambda: (sink.deliver(new), new_done.set()))
        delivery.start()
        new_done.wait(timeout=0.2)
        driver.old_frame_release.set()
        delivery.join(timeout=2.0)

        assert driver.frames == ["old", "new"]
    finally:
        driver.old_frame_release.set()
        sink.close()


def test_d200_sinks_opt_out_of_ticker_frames():
    # DeckApp reads this to skip rendering animation frames nobody consumes.
    assert D200Sink.wants_ticker_frames is False
    assert ReconnectingD200Sink.wants_ticker_frames is False


def test_reconnecting_sink_reopens_immediately_after_disconnect():
    """The supervisor waits on events, not a 250ms poll: a disconnect is
    followed by the reopen attempt right away (every time)."""

    class DroppingDriver(FrameDriver):
        def __init__(self):
            super().__init__()
            self.drop = threading.Event()

        async def run_reader(self):
            await asyncio.to_thread(self.drop.wait)  # returning = device gone

        def close(self):
            super().close()
            self.drop.set()

    drivers = []
    opened_at = []

    def factory():
        opened_at.append(time.monotonic())
        driver = DroppingDriver()
        drivers.append(driver)
        return driver

    sink = ReconnectingD200Sink(factory, on_press=lambda i: None, slots=13, retry_interval=5)
    try:
        for cycle in range(3):
            deadline = time.monotonic() + 2.0
            while len(drivers) <= cycle and time.monotonic() < deadline:
                time.sleep(0.002)
            assert len(drivers) == cycle + 1
            dropped_at = time.monotonic()
            drivers[cycle].drop.set()
            deadline = time.monotonic() + 2.0
            while len(opened_at) <= cycle + 1 and time.monotonic() < deadline:
                time.sleep(0.002)
            assert opened_at[cycle + 1] - dropped_at < 0.1
    finally:
        t0 = time.monotonic()
        sink.close()
        assert time.monotonic() - t0 < 1.0
