from herdeck.driver.base import TileView, PanelView, COLORS, DeckDriver


def test_colors_cover_all_semantic_states():
    for name in ("green", "blue", "amber", "dim", "red", "grey"):
        assert name in COLORS and len(COLORS[name]) == 3


def test_tileview_new_fields_default():
    t = TileView(index=0, label="api", color="green")
    assert t.icon is None and t.agent_type is None and t.spinner is None


def test_panelview_fields():
    p = PanelView(title="dev 1/2", lines=["B1 W4 I6", "online"], color="grey")
    assert p.title == "dev 1/2" and p.lines[0] == "B1 W4 I6" and p.color == "grey"


def test_deckdriver_is_abstract():
    import pytest
    with pytest.raises(TypeError):
        DeckDriver()
