import pathlib
import tomllib


def _pyproject() -> dict:
    root = pathlib.Path(__file__).resolve().parents[1]
    return tomllib.loads((root / "pyproject.toml").read_text())


def test_elgato_extra_declares_streamdeck_and_pillow():
    extra = _pyproject()["project"]["optional-dependencies"]["elgato"]
    assert any("streamdeck" in dep for dep in extra)
    assert any("pillow" in dep.lower() for dep in extra)
