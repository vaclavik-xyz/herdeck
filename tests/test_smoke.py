from pathlib import Path

import herdeck


def test_package_imports():
    expected = (Path(__file__).parents[1] / "VERSION").read_text().strip()
    assert herdeck.__version__ == expected
