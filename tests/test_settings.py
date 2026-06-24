from pathlib import Path

import pytest

from herdeck.config import ConfigError
from herdeck.settings import list_profiles, load_settings, resolve_profile, _profile_overlays, _merged_sections, _build_config

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


def test_legacy_config_merges_local_hardware_settings(tmp_path):
    config = write(tmp_path / "config.toml", '[deck]\ngrid = "5x3"\n')
    local = write(
        tmp_path / "local.toml",
        """
[local]
deck = "web"
herdr_socket = "/tmp/herdr.sock"
web_bind = "100.1.2.3"
web_port = 1234
icons_dir = "/tmp/icons"

[hardware]
brightness = 35
debounce = 0.1
keep_alive_interval = 2.5
tick_interval = 1.25
""",
    )

    cfg = resolve_profile(load_settings(config, local)).config

    assert cfg.hardware.deck == "web"
    assert cfg.hardware.herdr_socket == "/tmp/herdr.sock"
    assert cfg.hardware.web_bind == "100.1.2.3"
    assert cfg.hardware.web_port == 1234
    assert cfg.hardware.icons_dir == "/tmp/icons"
    assert cfg.hardware.brightness == 35
    assert cfg.hardware.debounce == 0.1
    assert cfg.hardware.keep_alive_interval == 2.5
    assert cfg.hardware.tick_interval == 1.25


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


def test_profile_inheritance_overrides_named_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDECK_WORKBOX_TOKEN", "secret")
    text = NEW_CONFIG + """

[profiles.mobile]
extends = "work"
view = "management"

[views.management]
management = "bottom_row"
bottom_row = ["profiles", "new_agent"]
"""
    config = write(
        tmp_path / "config.toml",
        text.replace('active_profile = "work"', 'active_profile = "mobile"'),
    )

    cfg = resolve_profile(load_settings(config)).config

    assert cfg.meta.active_profile == "mobile"
    assert cfg.view.management == "bottom_row"
    assert cfg.view.bottom_row == ["profiles", "new_agent"]
    assert cfg.start_profiles["claude"] == ["claude"]


def test_local_toml_overrides_active_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDECK_WORKBOX_TOKEN", "secret")
    config = write(
        tmp_path / "config.toml",
        NEW_CONFIG
        + """

[profiles.mobile]
extends = "work"
""",
    )
    local = write(tmp_path / "local.toml", 'active_profile = "mobile"\n')

    cfg = resolve_profile(load_settings(config, local)).config

    assert cfg.meta.active_profile == "mobile"


def test_env_profile_locks_profile_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDECK_WORKBOX_TOKEN", "secret")
    monkeypatch.setenv("HERDECK_PROFILE", "work")
    config = write(tmp_path / "config.toml", NEW_CONFIG)
    local = write(tmp_path / "local.toml", 'active_profile = "base"\n')

    snapshot = load_settings(config, local)
    cfg = resolve_profile(snapshot).config

    assert cfg.meta.active_profile == "work"
    assert cfg.meta.env_locked_profile is True
    assert list_profiles(snapshot)[-1] == {"name": "work", "active": True, "locked": True}


def test_inheritance_cycle_fails_with_chain(tmp_path):
    config = write(
        tmp_path / "config.toml",
        """
active_profile = "a"
[profiles.a]
extends = "b"
[profiles.b]
extends = "a"
""",
    )

    with pytest.raises(ConfigError, match="a -> b -> a"):
        resolve_profile(load_settings(config))


def test_unknown_block_reference_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDECK_WORKBOX_TOKEN", "secret")
    config = write(
        tmp_path / "config.toml",
        NEW_CONFIG.replace('view = "dense"', 'view = "missing"'),
    )

    with pytest.raises(ConfigError, match="unknown view 'missing'"):
        resolve_profile(load_settings(config))


def test_set_active_profile_persists_to_local_toml(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDECK_WORKBOX_TOKEN", "secret")
    config = write(
        tmp_path / "config.toml",
        NEW_CONFIG
        + """

[profiles.mobile]
extends = "work"
""",
    )
    local = tmp_path / "local.toml"
    snapshot = load_settings(config, local)

    from herdeck.settings import set_active_profile

    changed = set_active_profile(snapshot, "mobile")

    assert changed is True
    assert 'active_profile = "mobile"' in local.read_text()


def test_set_active_profile_refuses_env_locked_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDECK_WORKBOX_TOKEN", "secret")
    monkeypatch.setenv("HERDECK_PROFILE", "work")
    config = write(
        tmp_path / "config.toml",
        NEW_CONFIG
        + """

[profiles.mobile]
extends = "work"
""",
    )
    local = tmp_path / "local.toml"
    snapshot = load_settings(config, local)

    from herdeck.settings import set_active_profile

    changed = set_active_profile(snapshot, "mobile")

    assert changed is False
    assert not local.exists()


def test_set_active_profile_escapes_toml_strings(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDECK_WORKBOX_TOKEN", "secret")
    profile_name = 'mobile "quoted"'
    config = write(
        tmp_path / "config.toml",
        NEW_CONFIG
        + """

[profiles."mobile \\"quoted\\""]
extends = "work"
""",
    )
    local = write(tmp_path / "local.toml", '[local]\ndeck = "desk \\"one\\""\n')
    snapshot = load_settings(config, local)

    from herdeck.settings import set_active_profile

    changed = set_active_profile(snapshot, profile_name)
    reread = load_settings(config, local)

    assert changed is True
    assert reread.local_data["active_profile"] == profile_name
    assert reread.local_data["local"]["deck"] == 'desk "one"'


def test_set_active_profile_refuses_to_persist_invalid_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDECK_WORKBOX_TOKEN", "secret")
    config = write(
        tmp_path / "config.toml",
        NEW_CONFIG
        + """

[profiles.bad]
extends = "work"
view = "missing"
""",
    )
    local = tmp_path / "local.toml"
    snapshot = load_settings(config, local)

    from herdeck.settings import set_active_profile

    with pytest.raises(ConfigError, match="unknown view 'missing'"):
        set_active_profile(snapshot, "bad")

    assert not local.exists()


def test_validate_settings_reports_missing_references(tmp_path):
    from herdeck.settings import validate_settings

    config = write(
        tmp_path / "config.toml",
        """
active_profile = "work"
[profiles.work]
theme = "missing"
""",
    )

    errors = validate_settings(load_settings(config))

    assert any("unknown theme 'missing'" in err for err in errors)


def test_validate_settings_reports_unknown_active_profile(tmp_path):
    from herdeck.settings import validate_settings

    config = write(
        tmp_path / "config.toml",
        """
active_profile = "work"
[profiles.work]
""",
    )
    local = write(tmp_path / "local.toml", 'active_profile = "missing"\n')

    errors = validate_settings(load_settings(config, local))

    assert any("unknown profile 'missing'" in err for err in errors)


def test_merge_section_merges_tables_field_by_field():
    from herdeck.settings import _merge_section

    base = {"management": "launcher_menu", "tile_fields": ["repo"]}
    overlay = {"management": "bottom_row"}
    assert _merge_section(base, overlay) == {
        "management": "bottom_row",
        "tile_fields": ["repo"],
    }


def test_merge_section_replaces_lists_and_scalars():
    from herdeck.settings import _merge_section

    assert _merge_section(["a", "b"], ["c"]) == ["c"]
    assert _merge_section("5x3", "4x3") == "4x3"


def test_merge_section_recurses_into_nested_tables():
    from herdeck.settings import _merge_section

    base = {"colors": {"blocked": "amber", "idle": "blue"}}
    overlay = {"colors": {"blocked": "red"}}
    assert _merge_section(base, overlay) == {
        "colors": {"blocked": "red", "idle": "blue"}
    }


def test_profile_overlays_orders_parents_before_child():
    profiles = {
        "base": {"view": {"management": "launcher_menu"}},
        "work": {"extends": "base", "view": {"management": "bottom_row"}},
    }
    chain = _profile_overlays(profiles, "work")
    assert chain == [profiles["base"], profiles["work"]]


def test_profile_overlays_single_profile_without_extends():
    profiles = {"mobile": {"servers": ["local"]}}
    assert _profile_overlays(profiles, "mobile") == [profiles["mobile"]]


def test_profile_overlays_unknown_name_raises():
    with pytest.raises(ConfigError, match="unknown profile 'ghost'"):
        _profile_overlays({}, "ghost")


def test_profile_overlays_cycle_raises_with_chain():
    profiles = {"a": {"extends": "b"}, "b": {"extends": "a"}}
    with pytest.raises(ConfigError, match="inheritance cycle"):
        _profile_overlays(profiles, "a")


def test_merged_sections_base_only_when_default():
    data = {"view": {"management": "launcher_menu"}, "deck": {"grid": "5x3"}}
    merged, selection = _merged_sections(data, "default")
    assert merged["view"] == {"management": "launcher_menu"}
    assert merged["deck"] == {"grid": "5x3"}
    assert selection is None


def test_merged_sections_applies_profile_overlay():
    data = {
        "view": {"management": "launcher_menu", "tile_fields": ["repo"]},
        "profiles": {"mobile": {"view": {"management": "bottom_row"}}},
    }
    merged, selection = _merged_sections(data, "mobile")
    assert merged["view"] == {"management": "bottom_row", "tile_fields": ["repo"]}
    assert selection is None


def test_merged_sections_captures_server_selection_from_profile():
    data = {"profiles": {"mobile": {"servers": ["local"]}}}
    _merged, selection = _merged_sections(data, "mobile")
    assert selection == ["local"]


def test_build_config_reads_flat_base_including_theme_view_safety():
    data = {
        "servers": [{"id": "local", "url": "ws://x", "token_env": "TOK"}],
        "deck": {"grid": "4x3"},
        "theme": {"colors": {"blocked": "red"}},
        "view": {"management": "bottom_row"},
        "safety": {"approve_always": False},
    }
    merged, selection = _merged_sections(data, "default")
    import os

    os.environ["TOK"] = "secret"
    try:
        cfg = _build_config(
            data, merged, selection, {}, profile_name="default", env_profile=None
        )
    finally:
        del os.environ["TOK"]
    assert cfg.grid == (4, 3)
    assert cfg.theme.colors["blocked"] == "red"
    assert cfg.view.management == "bottom_row"
    assert cfg.safety.approve_always is False
    assert [s.id for s in cfg.servers] == ["local"]
    assert cfg.overview_order == ["local"]


def test_build_config_profile_overrides_grid_and_answer_profiles(monkeypatch):
    monkeypatch.setenv("TOK", "secret")
    data = {
        "servers": [{"id": "local", "url": "ws://x", "token_env": "TOK"}],
        "deck": {"grid": "5x3"},
        "answer_profiles": {"claude": {"approve": ["1"], "deny": ["esc"], "stop": ["ctrl+c"]}},
        "profiles": {
            "mobile": {
                "deck": {"grid": "4x3"},
                "answer_profiles": {"claude": {"approve": ["y"]}},
            }
        },
    }
    merged, selection = _merged_sections(data, "mobile")
    cfg = _build_config(data, merged, selection, {}, profile_name="mobile", env_profile=None)
    assert cfg.grid == (4, 3)
    assert cfg.profiles["claude"].approve == ["y"]
    assert cfg.profiles["claude"].deny == ["esc"]  # kept from base (field merge)
