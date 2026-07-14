import asyncio
import threading
import time

from herdeck.deckapp.sinks import D200Sink, ReconnectingD200Sink, RenderFrame


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
