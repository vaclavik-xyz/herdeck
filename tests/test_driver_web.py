import io
from PIL import Image

from herdeck.driver.base import PanelView, TileView
from herdeck.driver.web import WebDeck


class StubIcons:
    def render_tile_bytes(self, tile):
        b = io.BytesIO()
        Image.new("RGB", (10, 10), (1, 2, 3)).save(b, "PNG")
        return b.getvalue()


def make_deck():
    return WebDeck(slots=13, serve=False, icon_provider=StubIcons())


def test_render_updates_state_and_serves_png():
    d = make_deck()
    d.render([TileView(0, "", "amber", agent_type="claude", repo="api",
                       branch="x", status_text="BLOCKED", time_text="1m")])
    st = d._state()
    assert st["version"] >= 1 and 0 in st["tiles"]
    assert d._tile_png(0)[:4] == b"\x89PNG"
    assert d._tile_png(5) is None


def test_render_panel_serves_png():
    d = make_deck()
    d.render_panel(PanelView("dev", ["online"], "grey"))
    assert d._state()["has_panel"] is True
    assert d._panel_png()[:4] == b"\x89PNG"


def test_press_invokes_callback():
    d = make_deck()
    seen = []
    d.on_press(seen.append)
    d.press(7)
    assert seen == [7]


def test_version_bumps_on_each_render():
    d = make_deck()
    v0 = d._state()["version"]
    d.render([TileView(0, "Stop", "red")])
    assert d._state()["version"] > v0
