import os

import pytest
from PIL import Image

from herdeck.driver.base import PanelView
from herdeck.driver.d200 import D200Driver, compose_panel, split_panel


def test_compose_panel_size():
    img = compose_panel(PanelView("page 1/2", ["B1 W4 I6", "online"], "grey"))
    assert img.size == (392, 196)


def test_split_panel_halves():
    img = Image.new("RGB", (392, 196), (0, 0, 0))
    left, right = split_panel(img)
    assert left.size == (196, 196) and right.size == (196, 196)


def test_d200_constructor_restores_cwd_when_open_fails(tmp_path):
    class FailingD200(D200Driver):
        def _open_device(self, retries=5, delay=1.0):
            raise RuntimeError("no device")

    before = os.getcwd()
    try:
        with pytest.raises(RuntimeError, match="no device"):
            FailingD200(workdir=str(tmp_path))
        assert os.getcwd() == before
    finally:
        os.chdir(before)
