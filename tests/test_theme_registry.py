"""Deck theme registry: built-ins, user themes from <config dir>/themes, load
errors, base chains and the edit fingerprint (themes/registry.py)."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

import pytest

from herdeck.themes import registry as reg_mod
from herdeck.themes.registry import ThemeInfo, ThemeRegistry, themes_dir

EXAMPLE = Path(__file__).resolve().parents[1] / "docs" / "themes" / "minimal.py"


def _theme_src(theme_id: str, base: str | None = "classic", api_version: int = 1) -> str:
    return (
        "from herdeck.themes.api import Theme\n\n\n"
        "class T(Theme):\n"
        f"    api_version = {api_version}\n"
        f"    id = {theme_id!r}\n"
        f"    name = {{'en': {theme_id!r}, 'cs': {theme_id!r}}}\n"
        f"    base = {base!r}\n\n\n"
        "THEME = T()\n"
    )


def _write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def _ids(chain) -> list[str]:
    return [t.id for t in chain]


def _errors(reg: ThemeRegistry) -> dict[str, str | None]:
    return {info.id: info.error for info in reg.list()}


BUILTIN_IDS = list(reg_mod.BUILTIN_MODULES)  # derived, so adding a built-in keeps these valid


def test_classic_is_the_listed_builtin(tmp_path):
    infos = ThemeRegistry(str(tmp_path)).list()
    assert [i.id for i in infos] == BUILTIN_IDS
    assert BUILTIN_IDS[0] == "classic" and all(i.builtin and i.error is None for i in infos)
    assert infos[0] == ThemeInfo("classic", {"en": "Classic", "cs": "Klasický"}, True, None)
    assert infos[0].as_json() == {
        "id": "classic",
        "name": {"en": "Classic", "cs": "Klasický"},
        "builtin": True,
        "error": None,
    }


def test_every_builtin_module_exposes_its_theme():
    for theme_id, module in reg_mod.BUILTIN_MODULES.items():
        theme = __import__(module, fromlist=["THEME"]).THEME
        assert theme.id == theme_id and theme.api_version == 1
        assert set(theme.name) == {"en", "cs"}


def test_the_documented_example_theme_loads(tmp_path):
    shutil.copy(EXAMPLE, tmp_path / "minimal.py")
    reg = ThemeRegistry(str(tmp_path))
    assert _ids(reg.chain("minimal")) == ["minimal", "classic"]
    assert reg.resolve("minimal").name == {"en": "Minimal", "cs": "Minimální"}
    assert _errors(reg)["minimal"] is None


@pytest.mark.parametrize(
    ("name", "text", "error"),
    [
        ("broken.py", "def oops(:\n", "SyntaxError"),
        ("raises.py", "raise RuntimeError('boom')\n", "RuntimeError: boom"),
        ("empty.py", "X = 1\n", "THEME"),
        ("future.py", _theme_src("future", api_version=2), "api_version is 2"),
        ("exits.py", "import sys\nsys.exit(3)\n", "SystemExit"),
        (
            "prop.py",
            _theme_src("prop").replace(
                "    base = 'classic'\n",
                "    base = 'classic'\n    @property\n    def api_version(self):\n"
                "        raise RuntimeError('prop boom')\n",
            ).replace("    api_version = 1\n", ""),
            "prop boom",
        ),
        ("listbase.py", _theme_src("listbase", base=["a"]), "base must be"),
        ("named.py", _theme_src("other"), "does not match the file name"),
    ],
)
def test_a_broken_user_theme_is_listed_with_its_error_and_draws_classic(
    tmp_path, name, text, error
):
    _write(tmp_path, name, text)
    reg = ThemeRegistry(str(tmp_path))
    theme_id = name[:-3]
    assert error in (_errors(reg)[theme_id] or "")
    assert _ids(reg.chain(theme_id)) == ["classic"]


def test_a_user_file_cannot_shadow_a_builtin(tmp_path):
    _write(tmp_path, "classic.py", _theme_src("classic", base=None))
    reg = ThemeRegistry(str(tmp_path))
    assert reg.resolve("classic") is __import__("herdeck.themes.classic", fromlist=["x"]).THEME
    shadow = [i for i in reg.list() if i.id == "classic" and not i.builtin]
    assert shadow and "built-in" in shadow[0].error


def test_only_well_named_python_files_are_themes(tmp_path):
    _write(tmp_path, "Bad Name.py", _theme_src("Bad Name"))
    _write(tmp_path, "notes.txt", "hello")
    _write(tmp_path, "ok_1.py", _theme_src("ok_1"))
    assert [i.id for i in ThemeRegistry(str(tmp_path)).list()] == [*BUILTIN_IDS, "ok_1"]


def test_a_missing_folder_lists_only_builtins(tmp_path):
    assert [i.id for i in ThemeRegistry(str(tmp_path / "nope")).list()] == BUILTIN_IDS


def test_unknown_id_draws_classic_and_warns_once(tmp_path, caplog):
    reg = ThemeRegistry(str(tmp_path))
    with caplog.at_level(logging.WARNING, logger="herdeck.themes.registry"):
        assert reg.resolve("ghost").id == "classic"
        assert reg.resolve("ghost").id == "classic"
    assert sum("ghost" in r.getMessage() for r in caplog.records) == 1


def test_base_chains_follow_bases_and_end_at_classic(tmp_path):
    _write(tmp_path, "parent.py", _theme_src("parent"))
    _write(tmp_path, "child.py", _theme_src("child", base="parent"))
    _write(tmp_path, "orphan.py", _theme_src("orphan", base="nope"))
    _write(tmp_path, "rootless.py", _theme_src("rootless", base=None))
    reg = ThemeRegistry(str(tmp_path))
    assert _ids(reg.chain("child")) == ["child", "parent", "classic"]
    assert _ids(reg.chain("orphan")) == ["orphan", "classic"]
    assert _ids(reg.chain("rootless")) == ["rootless", "classic"]
    assert _ids(reg.chain("classic")) == ["classic"]


def test_a_base_cycle_terminates(tmp_path):
    _write(tmp_path, "a.py", _theme_src("a", base="b"))
    _write(tmp_path, "b.py", _theme_src("b", base="a"))
    assert _ids(ThemeRegistry(str(tmp_path)).chain("a")) == ["a", "b", "classic"]


def test_an_edited_theme_reloads_after_the_rescan_window(tmp_path):
    now = [100.0]
    path = _write(tmp_path, "live.py", _theme_src("live"))
    reg = ThemeRegistry(str(tmp_path), clock=lambda: now[0])
    first = reg.fingerprint("live")
    assert first.startswith("live:") and first.endswith("|classic")
    path.write_text(_theme_src("live", base=None) + "# edited, and longer\n", encoding="utf-8")
    os.utime(path, ns=(1, 1_000_000_000))
    assert reg.fingerprint("live") == first  # inside the rescan window
    now[0] += reg_mod.RESCAN_S
    assert reg.fingerprint("live") != first
    assert reg.fingerprint("classic") == ""


def test_a_deleted_theme_falls_back_to_classic(tmp_path):
    now = [0.0]
    path = _write(tmp_path, "gone.py", _theme_src("gone"))
    reg = ThemeRegistry(str(tmp_path), clock=lambda: now[0])
    assert reg.resolve("gone").id == "gone"
    path.unlink()
    now[0] += reg_mod.RESCAN_S
    assert reg.resolve("gone").id == "classic"


def test_themes_dir_sits_next_to_the_config(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDECK_THEMES_DIR", raising=False)
    monkeypatch.setenv("HERDECK_CONFIG", str(tmp_path / "cfg" / "config.toml"))
    assert themes_dir() == str(tmp_path / "cfg" / "themes")
    monkeypatch.setenv("HERDECK_THEMES_DIR", str(tmp_path / "elsewhere"))
    assert themes_dir() == str(tmp_path / "elsewhere")


def test_default_registry_is_swappable(tmp_path):
    custom = ThemeRegistry(str(tmp_path))
    reg_mod.set_default_registry(custom)
    try:
        assert reg_mod.default_registry() is custom
    finally:
        reg_mod.set_default_registry(None)
    assert reg_mod.default_registry() is not custom


@pytest.mark.parametrize("bad_name", ["5", "{'en': object()}", "{1: 'x'}", "'Mine'"])
def test_a_malformed_name_is_listed_with_an_error_and_draws_classic(tmp_path, bad_name):
    _write(tmp_path, "mine.py", _theme_src("mine").replace(
        "    name = {'mine': 'mine'}", "").replace(
        "    name = {'en': 'mine', 'cs': 'mine'}", f"    name = {bad_name}"))
    reg = ThemeRegistry(str(tmp_path))
    infos = {i.id: i for i in reg.list()}  # must not raise
    assert "THEME.name must be a dict" in infos["mine"].error
    assert infos["mine"].name == {}
    assert _ids(reg.chain("mine")) == ["classic"]


def test_the_shadow_error_names_the_file_not_its_path(tmp_path):
    _write(tmp_path, "classic.py", _theme_src("classic", base=None))
    shadow = [i for i in ThemeRegistry(str(tmp_path)).list() if not i.builtin][0]
    assert shadow.error.startswith("classic.py ignored")
    assert str(tmp_path) not in shadow.error
