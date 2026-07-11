from importlib.resources import files
from pathlib import Path


def test_vendored_terminal_assets_are_present_and_declared_as_package_data():
    import tomllib

    assets = files("herdeck").joinpath("assets", "web")
    expected = {
        "xterm.js": 250_000,
        "xterm.css": 5_000,
        "addon-fit.js": 1_000,
        "LICENSE.xterm.txt": 1_000,
        "LICENSE.addon-fit.txt": 1_000,
        "VENDORED.md": 100,
    }
    for name, minimum_size in expected.items():
        assert assets.joinpath(name).read_bytes()
        assert assets.joinpath(name).stat().st_size >= minimum_size

    config = tomllib.loads(Path("pyproject.toml").read_text())
    assert "assets/web/*" in config["tool"]["setuptools"]["package-data"]["herdeck"]
