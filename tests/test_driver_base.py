from herdeck.driver.base import TileView, COLORS, DeckDriver


def test_colors_cover_all_semantic_states():
    for name in ("green", "blue", "amber", "dim", "red", "grey"):
        assert name in COLORS
        assert len(COLORS[name]) == 3  # RGB tuple


def test_tileview_equality():
    a = TileView(index=0, label="api", color="green", icon=None)
    b = TileView(index=0, label="api", color="green", icon=None)
    assert a == b


def test_deckdriver_is_abstract():
    import pytest

    with pytest.raises(TypeError):
        DeckDriver()  # abstract, cannot instantiate
