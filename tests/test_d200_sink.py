import threading

from herdeck.deckapp.sinks import D200Sink, RenderFrame


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
        self.full_renders = []      # list of [tile.index, ...]
        self.panels = []
        self.working_renders = []   # list of [tile.index, ...]
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
    return D200Sink(driver, on_press=(on_press or (lambda i: None)), slots=slots, start_reader=start_reader)


def test_full_frame_renders_all_in_range_tiles_and_panel():
    drv = FakeDriver()
    sink = _sink(drv)
    rs = _RS([_Tile(0), _Tile(1), _Tile(13), _Tile(14)])  # 13/14 are panel cells, not tiles
    sink.deliver(RenderFrame(render=rs, working=None, full=True))
    assert drv.full_renders == [[0, 1]]  # only indices < slots(13)
    assert drv.panels == ["PANEL"]
    assert drv.working_renders == []


def test_working_frame_renders_only_working_tiles():
    drv = FakeDriver()
    sink = _sink(drv)
    rs = _RS([_Tile(0), _Tile(1), _Tile(2)])
    sink.deliver(RenderFrame(render=rs, working=[1], full=False))
    assert drv.working_renders == [[1]]
    assert drv.full_renders == []
    assert drv.panels == []


def test_working_frame_with_no_working_tiles_is_a_noop():
    drv = FakeDriver()
    sink = _sink(drv)
    rs = _RS([_Tile(0), _Tile(1)])
    sink.deliver(RenderFrame(render=rs, working=[], full=False))
    assert drv.working_renders == []
    assert drv.full_renders == []


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
