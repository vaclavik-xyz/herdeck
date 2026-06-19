from herdeck.driver.base import TileView, PanelView
from herdeck.driver.fake import FakeRenderer


def test_render_records_last_tiles():
    d = FakeRenderer(slots=13)
    tiles = [TileView(0, "api", "green")]
    d.render(tiles)
    assert d.last == tiles and d.slot_count() == 13


def test_render_panel_records_last_panel():
    d = FakeRenderer(slots=13)
    p = PanelView("t", ["l"], "grey")
    d.render_panel(p)
    assert d.last_panel == p


def test_press_invokes_callback():
    d = FakeRenderer(slots=13)
    seen = []
    d.on_press(seen.append)
    d.simulate_press(3)
    assert seen == [3]
