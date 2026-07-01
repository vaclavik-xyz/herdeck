from pathlib import Path

import pytest

from herdeck.config import ConfigError
from herdeck.settings import (
    _build_config,
    _merged_sections,
    _profile_overlays,
    _view_config,
    list_profiles,
    load_settings,
    resolve_profile,
    set_active_profile,
    validate_settings,
)

# ---------------------------------------------------------------------------
# Overlay-model fixture (flat base + [profiles.*] overlays)
# ---------------------------------------------------------------------------

OVERLAY_CONFIG = """
[[servers]]
id = "local"
url = "ws://x"
token_env = "TOK"

[deck]
grid = "5x3"

[view]
management = "launcher_menu"

[profiles.mobile]
servers = ["local"]
[profiles.mobile.view]
management = "bottom_row"
"""


def _write(tmp_path, text):
    p = tmp_path / "config.toml"
    p.write_text(text)
    return p


def write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


# ---------------------------------------------------------------------------
# New overlay-model tests (Step 1 — written first, fail before cutover)
# ---------------------------------------------------------------------------


def test_resolve_default_is_base(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "secret")
    snap = load_settings(_write(tmp_path, OVERLAY_CONFIG))
    cfg = resolve_profile(snap).config
    assert cfg.view.management == "launcher_menu"  # base, no profile active
    assert cfg.meta.active_profile == "default"
    assert cfg.meta.profile_names == ["default", "mobile"]


def test_resolve_named_profile_applies_overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "secret")
    snap = load_settings(_write(tmp_path, OVERLAY_CONFIG))
    cfg = resolve_profile(snap, "mobile").config
    assert cfg.view.management == "bottom_row"


def test_local_toml_active_profile_selects_overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "secret")
    config_p = _write(tmp_path, OVERLAY_CONFIG)
    (tmp_path / "local.toml").write_text('active_profile = "mobile"\n')
    snap = load_settings(config_p)
    assert resolve_profile(snap).config.view.management == "bottom_row"


def test_validate_rejects_reserved_default_profile(tmp_path):
    text = OVERLAY_CONFIG + "\n[profiles.default]\nservers = []\n"
    snap = load_settings(_write(tmp_path, text))
    errors = validate_settings(snap)
    assert any("default" in e for e in errors)


# ---------------------------------------------------------------------------
# Ported behavior tests (all use OVERLAY_CONFIG or inline overlay-model configs)
# ---------------------------------------------------------------------------


def test_list_profiles_marks_active(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "secret")
    snap = load_settings(_write(tmp_path, OVERLAY_CONFIG))

    assert list_profiles(snap) == [
        {"name": "default", "active": True, "locked": False},
        {"name": "mobile", "active": False, "locked": False},
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
    monkeypatch.delenv("TOK", raising=False)
    snap = load_settings(_write(tmp_path, OVERLAY_CONFIG))

    with pytest.raises(ConfigError, match="TOK"):
        resolve_profile(snap)


def test_unknown_server_reference_fails_with_config_error(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "secret")
    text = OVERLAY_CONFIG.replace('servers = ["local"]', 'servers = ["missing"]')
    snap = load_settings(_write(tmp_path, text))

    with pytest.raises(ConfigError, match="unknown server 'missing'"):
        resolve_profile(snap, "mobile")


def test_local_toml_overrides_active_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "secret")
    config = write(
        tmp_path / "config.toml",
        OVERLAY_CONFIG
        + """
[profiles.work]
servers = ["local"]
""",
    )
    local = write(tmp_path / "local.toml", 'active_profile = "work"\n')

    cfg = resolve_profile(load_settings(config, local)).config

    assert cfg.meta.active_profile == "work"


def test_env_profile_locks_profile_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "secret")
    monkeypatch.setenv("HERDECK_PROFILE", "mobile")
    write(tmp_path / "local.toml", 'active_profile = "default"\n')
    snap2 = load_settings(_write(tmp_path, OVERLAY_CONFIG), tmp_path / "local.toml")

    cfg = resolve_profile(snap2).config

    assert cfg.meta.active_profile == "mobile"
    assert cfg.meta.env_locked_profile is True
    assert list_profiles(snap2)[-1] == {"name": "mobile", "active": True, "locked": True}


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


def test_set_active_profile_persists_to_local_toml(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "secret")
    config = _write(tmp_path, OVERLAY_CONFIG)
    local = tmp_path / "local.toml"
    snapshot = load_settings(config, local)

    from herdeck.settings import set_active_profile

    changed = set_active_profile(snapshot, "mobile")

    assert changed is True
    assert 'active_profile = "mobile"' in local.read_text()


def test_set_active_profile_refuses_env_locked_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "secret")
    monkeypatch.setenv("HERDECK_PROFILE", "mobile")
    config = _write(tmp_path, OVERLAY_CONFIG)
    local = tmp_path / "local.toml"
    snapshot = load_settings(config, local)

    from herdeck.settings import set_active_profile

    changed = set_active_profile(snapshot, "mobile")

    assert changed is False
    assert not local.exists()


def test_set_active_profile_escapes_toml_strings(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "secret")
    profile_name = 'mobile "quoted"'
    config = write(
        tmp_path / "config.toml",
        OVERLAY_CONFIG
        + """
[profiles."mobile \\"quoted\\""]
servers = ["local"]
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


def test_set_active_profile_accepts_default_and_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "secret")
    config_p = _write(tmp_path, OVERLAY_CONFIG)
    (tmp_path / "local.toml").write_text('active_profile = "mobile"\n[local]\ndeck = "d200"\n')
    snap = load_settings(config_p)
    assert set_active_profile(snap, "default") is True
    local_text = (tmp_path / "local.toml").read_text()
    assert 'active_profile = "default"' in local_text
    assert 'deck = "d200"' in local_text  # other local sections preserved


def test_set_active_profile_rejects_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "secret")
    snap = load_settings(_write(tmp_path, OVERLAY_CONFIG))
    with pytest.raises(ConfigError, match="unknown profile 'ghost'"):
        set_active_profile(snap, "ghost")


def test_set_active_profile_refuses_to_persist_invalid_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "secret")
    config = write(
        tmp_path / "config.toml",
        OVERLAY_CONFIG
        + """
[profiles.bad]
servers = ["nonexistent"]
""",
    )
    local = tmp_path / "local.toml"
    snapshot = load_settings(config, local)

    from herdeck.settings import set_active_profile

    with pytest.raises(ConfigError, match="unknown server 'nonexistent'"):
        set_active_profile(snapshot, "bad")

    assert not local.exists()


def test_set_active_profile_default_validates_base_builds(tmp_path, monkeypatch):
    monkeypatch.delenv("TOK", raising=False)  # base server token unset -> base won't resolve
    snap = load_settings(_write(tmp_path, OVERLAY_CONFIG))
    with pytest.raises(ConfigError):
        set_active_profile(snap, "default")
    assert not (tmp_path / "local.toml").exists()  # nothing persisted


def test_validate_settings_reports_missing_references(tmp_path, monkeypatch):
    config = write(
        tmp_path / "config.toml",
        """
[[servers]]
id = "local"
url = "ws://x"
token_env = "TOK"

active_profile = "work"
[profiles.work]
servers = ["missing"]
""",
    )

    monkeypatch.setenv("TOK", "secret")
    errors = validate_settings(load_settings(config))

    assert any("unknown server 'missing'" in err for err in errors)


def test_validate_settings_reports_unknown_active_profile(tmp_path):
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


# ---------------------------------------------------------------------------
# Unit tests for pure helpers (_merge_section, _profile_overlays, _merged_sections, _build_config)
# These were added in Tasks 1–4 and remain unchanged.
# ---------------------------------------------------------------------------


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
    assert _merge_section(base, overlay) == {"colors": {"blocked": "red", "idle": "blue"}}


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
        cfg = _build_config(data, merged, selection, {}, profile_name="default", env_profile=None)
    finally:
        del os.environ["TOK"]
    assert cfg.grid == (4, 3)
    assert cfg.theme.colors["blocked"] == "red"
    assert cfg.view.management == "bottom_row"
    assert cfg.safety.approve_always is False
    assert [s.id for s in cfg.servers] == ["local"]
    assert cfg.overview_order == ["local"]


def test_profile_overlays_extends_default_terminates_at_base():
    profiles = {"mobile": {"extends": "default", "view": {"management": "bottom_row"}}}
    assert _profile_overlays(profiles, "mobile") == [profiles["mobile"]]


def test_build_config_respects_explicit_empty_overview_order(monkeypatch):
    monkeypatch.setenv("TOK", "secret")
    data = {
        "servers": [{"id": "local", "url": "ws://x", "token_env": "TOK"}],
        "deck": {"overview_order": []},
    }
    merged, selection = _merged_sections(data, "default")
    cfg = _build_config(data, merged, selection, {}, profile_name="default", env_profile=None)
    assert cfg.overview_order == []
    assert cfg.servers == []


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


def test_build_config_rejects_unknown_overview_order_server(monkeypatch):
    monkeypatch.setenv("TOK", "secret")
    data = {
        "servers": [{"id": "local", "url": "ws://x", "token_env": "TOK"}],
        "deck": {"overview_order": ["local", "ghost"]},
    }
    merged, selection = _merged_sections(data, "default")
    with pytest.raises(ConfigError, match="unknown server 'ghost'"):
        _build_config(data, merged, selection, {}, profile_name="default", env_profile=None)


# ---------------------------------------------------------------------------
# tile_primary / tile_secondary parsing and validation
# ---------------------------------------------------------------------------


def test_view_config_parses_tile_lines():
    view = _view_config({"tile_primary": ["workspace"], "tile_secondary": ["tab", "branch"]})
    assert view.tile_primary == ["workspace"]
    assert view.tile_secondary == ["tab", "branch"]


def test_view_config_defaults_tile_lines_to_none():
    view = _view_config({})
    assert view.tile_primary is None
    assert view.tile_secondary is None


def test_view_config_keeps_explicit_empty_list():
    view = _view_config({"tile_primary": []})
    assert view.tile_primary == []
    assert view.tile_secondary is None


def test_view_config_rejects_unknown_tile_token():
    with pytest.raises(ConfigError, match="unknown tile token 'bogus'"):
        _view_config({"tile_secondary": ["branch", "bogus"]})


def test_view_config_parses_working_animation():
    assert _view_config({"working_animation": "pulse"}).working_animation == "pulse"


def test_view_config_defaults_working_animation_to_spin():
    assert _view_config({}).working_animation == "spin"


def test_view_config_rejects_unknown_working_animation():
    with pytest.raises(ConfigError, match="unknown view.working_animation 'spinny'"):
        _view_config({"working_animation": "spinny"})


def test_view_config_parses_tile_fill():
    assert _view_config({"tile_fill": "solid"}).tile_fill == "solid"
    assert _view_config({"tile_fill": "tint"}).tile_fill == "tint"


def test_view_config_defaults_tile_fill_to_none():
    assert _view_config({}).tile_fill == "none"


def test_view_config_rejects_unknown_tile_fill():
    with pytest.raises(ConfigError, match="unknown view.tile_fill 'glow'"):
        _view_config({"tile_fill": "glow"})


def test_profile_view_overlay_merges_tile_primary():
    data = {
        "view": {"tile_fields": ["repo"]},
        "profiles": {"solo": {"view": {"tile_primary": ["workspace"]}}},
    }
    merged, _ = _merged_sections(data, "solo")
    assert merged["view"] == {"tile_fields": ["repo"], "tile_primary": ["workspace"]}
