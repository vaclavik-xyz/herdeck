from pathlib import Path

import pytest

from herdeck.config import ConfigError
from herdeck.settings import list_profiles, load_settings, resolve_profile

NEW_CONFIG = """
active_profile = "work"

[[servers]]
id = "workbox"
url = "ws://100.x.y.z:8788"
token_env = "HERDECK_WORKBOX_TOKEN"

[profiles.base]
theme = "default"
view = "dense"
notifications = "normal"
safety = "standard"
macros = "default"
launcher = "default"
servers = ["workbox"]

[profiles.work]
extends = "base"

[themes.default.colors]
working = "green"
idle = "blue"
blocked = "amber"
done = "dim"
unknown = "grey"
offline = "red"

[views.dense]
management = "launcher_menu"
show_profile_on_panel = true
agent_slots = "max"
tile_fields = ["repo", "branch", "status", "time", "server"]

[[macro_sets.default]]
label = "continue"
text = "continue"

[launchers.default]
claude = ["claude"]
codex = ["codex"]

[notification_profiles.normal]
enabled = true
backends = ["macos"]
on = ["blocked"]
sound = false

[safety.standard]
approve_always = true
require_confirm_for = []
"""


def write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def test_new_schema_resolves_active_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDECK_WORKBOX_TOKEN", "secret")
    config = write(tmp_path / "config.toml", NEW_CONFIG)

    snapshot = load_settings(config)
    resolved = resolve_profile(snapshot)

    assert resolved.config.meta.active_profile == "work"
    assert resolved.config.meta.profile_names == ["base", "work"]
    assert resolved.config.servers[0].id == "workbox"
    assert resolved.config.servers[0].token == "secret"
    assert resolved.config.view.show_profile_on_panel is True
    assert resolved.config.notifications.enabled is True
    assert resolved.config.notifications.sound is False
    assert resolved.config.start_profiles["codex"] == ["codex"]
    assert resolved.config.macros[0].label == "continue"


def test_list_profiles_marks_active(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDECK_WORKBOX_TOKEN", "secret")
    config = write(tmp_path / "config.toml", NEW_CONFIG)

    snapshot = load_settings(config)

    assert list_profiles(snapshot) == [
        {"name": "base", "active": False, "locked": False},
        {"name": "work", "active": True, "locked": False},
    ]


def test_list_profiles_returns_default_for_legacy_config(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDECK_PROFILE", raising=False)
    config = write(tmp_path / "config.toml", '[deck]\ngrid = "5x3"\n')

    assert list_profiles(load_settings(config)) == [
        {"name": "default", "active": True, "locked": False}
    ]


def test_missing_token_still_fails_without_secret_value(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDECK_WORKBOX_TOKEN", raising=False)
    config = write(tmp_path / "config.toml", NEW_CONFIG)

    with pytest.raises(ConfigError, match="HERDECK_WORKBOX_TOKEN"):
        resolve_profile(load_settings(config))


@pytest.mark.parametrize(
    ("field", "original", "message"),
    [
        ("theme", "default", "unknown theme 'missing'"),
        ("view", "dense", "unknown view 'missing'"),
        ("notifications", "normal", "unknown notification profile 'missing'"),
        ("safety", "standard", "unknown safety 'missing'"),
        ("macros", "default", "unknown macro set 'missing'"),
        ("launcher", "default", "unknown launcher 'missing'"),
    ],
)
def test_unknown_named_block_reference_fails(tmp_path, monkeypatch, field, original, message):
    monkeypatch.setenv("HERDECK_WORKBOX_TOKEN", "secret")
    config = write(
        tmp_path / "config.toml",
        NEW_CONFIG.replace(f'{field} = "{original}"', f'{field} = "missing"'),
    )

    with pytest.raises(ConfigError, match=message):
        resolve_profile(load_settings(config))


def test_unknown_server_reference_fails_with_config_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDECK_WORKBOX_TOKEN", "secret")
    config = write(
        tmp_path / "config.toml",
        NEW_CONFIG.replace('servers = ["workbox"]', 'servers = ["missing"]'),
    )

    with pytest.raises(ConfigError, match="unknown server 'missing'"):
        resolve_profile(load_settings(config))
