from herdeck.driver.base import TileView
from herdeck.driver.fake import FakeRenderer


def test_render_records_last_tiles():
    d = FakeRenderer(slots=15)
    tiles = [TileView(0, "api", "green")]
    d.render(tiles)
    assert d.last == tiles
    assert d.slot_count() == 15


def test_press_invokes_callback():
    d = FakeRenderer(slots=15)
    seen = []
    d.on_press(seen.append)
    d.simulate_press(3)
    assert seen == [3]
