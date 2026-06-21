# Customization Profiles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement portable Herdeck customization profiles with runtime deck switching, shareable/local config layering, configurable view/theme/macros/launcher/notifications/safety, and an app-ready settings service.

**Architecture:** Keep the existing `Config` runtime contract compatible by appending defaulted customization metadata. Add a new `settings.py` resolver/service layer that turns shareable config plus `local.toml` plus environment overrides into runtime `Config`. Then extend the orchestrator and app runtime to switch profiles, update config in-place, and diff connectors without restarting.

**Tech Stack:** Python 3.12, TOML via stdlib `tomllib`, dataclasses, existing pytest/pytest-asyncio tests, existing WebSocket `Connector`, existing D200/Elgato/web/fake deck drivers.

---

## File Structure

- Modify `src/herdeck/config.py`
  - Keep legacy config parsing working.
  - Add defaulted runtime customization dataclasses: `ThemeConfig`, `ViewConfig`, `SafetyConfig`, `HardwareConfig`, `ConfigMeta`.
  - Add these fields to `Config` at the end so existing positional tests still work.
- Create `src/herdeck/settings.py`
  - Parse new profile config schema.
  - Resolve profile inheritance and named blocks.
  - Merge shareable config, `local.toml`, and environment overrides.
  - Provide app-ready service functions: `load_settings`, `resolve_profile`, `list_profiles`, `set_active_profile`, `validate_settings`.
- Modify `src/herdeck/layout.py`
  - Allow status colors and tile field visibility to come from runtime `Config`.
  - Keep default behavior identical when customization fields are defaults.
- Modify `src/herdeck/orchestrator.py`
  - Add profile menu state.
  - Add management row support.
  - Emit `Command("switch_profile", server_id="", text=profile_name)`.
  - Enforce basic safety policy for `approve_always` and `act_force`.
- Modify `src/herdeck/app.py`
  - Add `ConnectorManager`.
  - Add profile switching callback and runtime config update path.
  - Use settings resolver in `main`.
  - Preserve local zero-config and mock behavior.
- Modify `src/herdeck/icons.py`
  - Use configurable theme colors and server accent palette through `TileView`/runtime values.
- Modify `src/herdeck/driver/d200.py` and `src/herdeck/driver/elgato.py`
  - Allow brightness/debounce/keep-alive settings to be injected from local hardware settings.
- Modify `config.example.toml`
  - Replace legacy example with new profile schema.
- Modify `README.md`
  - Document profile config, `local.toml`, profile switching, and future GUI boundary.
- Tests:
  - Create `tests/test_settings.py`.
  - Modify `tests/test_config.py`, `tests/test_orchestrator.py`, `tests/test_orchestrator_nav.py`, `tests/test_app.py`, `tests/test_layout.py`, `tests/test_icons.py`, `tests/test_local_mode.py`, `tests/test_driver_elgato.py`, and `tests/test_d200_panel.py`.

Every task below ends with:

```bash
sha=$(git rev-parse HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

If Roborev reports findings, fix them before starting the next task.

---

### Task 1: Runtime Customization Dataclasses

**Files:**
- Modify: `src/herdeck/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write failing tests for runtime customization defaults**

Append to `tests/test_config.py`:

```python
def test_runtime_customization_defaults_on_config():
    from herdeck.config import Config, ViewConfig, ThemeConfig, SafetyConfig, HardwareConfig

    cfg = Config(servers=[], profiles={}, overview_order=[], grid=(5, 3))

    assert isinstance(cfg.theme, ThemeConfig)
    assert cfg.theme.colors["blocked"] == "amber"
    assert cfg.theme.colors["offline"] == "red"
    assert cfg.theme.server_accents[:2] == ["teal", "violet"]
    assert isinstance(cfg.view, ViewConfig)
    assert cfg.view.management == "launcher_menu"
    assert cfg.view.bottom_row == ["profiles", "notifications", "safety", "theme", "new_agent"]
    assert cfg.view.tile_fields == ["repo", "branch", "status", "time", "server"]
    assert isinstance(cfg.safety, SafetyConfig)
    assert cfg.safety.approve_always is True
    assert isinstance(cfg.hardware, HardwareConfig)
    assert cfg.hardware.brightness == 80
    assert cfg.meta.active_profile == "default"
    assert cfg.meta.profile_names == ["default"]
    assert cfg.meta.env_locked_profile is False
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_config.py::test_runtime_customization_defaults_on_config -v
```

Expected: FAIL with `ImportError` or `AttributeError` for missing customization types/fields.

- [ ] **Step 3: Add runtime customization dataclasses**

In `src/herdeck/config.py`, after `Notifications`, add:

```python
DEFAULT_STATUS_COLORS: dict[str, str] = {
    "working": "green",
    "idle": "blue",
    "blocked": "amber",
    "done": "dim",
    "unknown": "grey",
    "offline": "red",
}

DEFAULT_SERVER_ACCENTS: list[str] = ["teal", "violet", "orange", "pink", "lime"]
DEFAULT_TILE_FIELDS: list[str] = ["repo", "branch", "status", "time", "server"]
DEFAULT_BOTTOM_ROW: list[str] = ["profiles", "notifications", "safety", "theme", "new_agent"]


@dataclass
class ThemeConfig:
    colors: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_STATUS_COLORS))
    server_accents: list[str] = field(default_factory=lambda: list(DEFAULT_SERVER_ACCENTS))


@dataclass
class ViewConfig:
    management: str = "launcher_menu"  # launcher_menu|bottom_row
    bottom_row: list[str] = field(default_factory=lambda: list(DEFAULT_BOTTOM_ROW))
    show_profile_on_panel: bool = False
    agent_slots: str = "max"
    tile_fields: list[str] = field(default_factory=lambda: list(DEFAULT_TILE_FIELDS))


@dataclass
class SafetyConfig:
    approve_always: bool = True
    require_confirm_for: list[str] = field(default_factory=list)


@dataclass
class HardwareConfig:
    deck: str | None = None
    herdr_socket: str | None = None
    web_bind: str | None = None
    web_port: int | None = None
    icons_dir: str | None = None
    brightness: int = 80
    debounce: float = 0.25
    keep_alive_interval: float = 5.0
    tick_interval: float = 0.4


@dataclass
class ConfigMeta:
    active_profile: str = "default"
    profile_names: list[str] = field(default_factory=lambda: ["default"])
    env_locked_profile: bool = False
    restart_required: bool = False
```

Then extend `Config` by appending defaulted fields:

```python
@dataclass
class Config:
    servers: list[ServerConfig]
    profiles: dict[str, AnswerProfile]
    overview_order: list[str]
    grid: tuple[int, int]
    macros: list[Macro] = field(default_factory=lambda: list(DEFAULT_MACROS))
    start_profiles: dict[str, list[str]] = field(
        default_factory=lambda: dict(DEFAULT_START_PROFILES)
    )
    notifications: Notifications = field(default_factory=Notifications)
    theme: ThemeConfig = field(default_factory=ThemeConfig)
    view: ViewConfig = field(default_factory=ViewConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    hardware: HardwareConfig = field(default_factory=HardwareConfig)
    meta: ConfigMeta = field(default_factory=ConfigMeta)
```

- [ ] **Step 4: Run focused test**

Run:

```bash
.venv/bin/python -m pytest tests/test_config.py::test_runtime_customization_defaults_on_config -v
```

Expected: PASS.

- [ ] **Step 5: Run full config suite**

Run:

```bash
.venv/bin/python -m pytest tests/test_config.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/herdeck/config.py tests/test_config.py
git commit -m "feat(config): add runtime customization defaults"
sha=$(git rev-parse HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

---

### Task 2: Settings Resolver Happy Path

**Files:**
- Create: `src/herdeck/settings.py`
- Create: `tests/test_settings.py`

- [ ] **Step 1: Write failing tests for new schema resolution**

Create `tests/test_settings.py`:

```python
from pathlib import Path

import pytest

from herdeck.config import ConfigError
from herdeck.settings import load_settings, resolve_profile, list_profiles


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


def test_missing_token_still_fails_without_secret_value(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDECK_WORKBOX_TOKEN", raising=False)
    config = write(tmp_path / "config.toml", NEW_CONFIG)

    with pytest.raises(ConfigError, match="HERDECK_WORKBOX_TOKEN"):
        resolve_profile(load_settings(config))
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_settings.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'herdeck.settings'`.

- [ ] **Step 3: Implement settings resolver happy path**

Create `src/herdeck/settings.py` with:

```python
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .config import (
    DEFAULT_MACROS,
    DEFAULT_PROFILES,
    DEFAULT_START_PROFILES,
    AnswerProfile,
    Config,
    ConfigError,
    ConfigMeta,
    HardwareConfig,
    Macro,
    Notifications,
    SafetyConfig,
    ServerConfig,
    TelegramConfig,
    ThemeConfig,
    ViewConfig,
)


@dataclass
class SettingsSnapshot:
    config_path: Path
    local_path: Path | None
    data: dict
    local_data: dict
    env_profile: str | None


@dataclass
class ResolvedSettings:
    config: Config
    local_path: Path | None
    startup_only_changed: bool = False


def load_settings(
    config_path: str | Path,
    local_path: str | Path | None = None,
    *,
    getenv=os.environ.get,
) -> SettingsSnapshot:
    config_p = Path(config_path)
    local_p = Path(local_path) if local_path is not None else config_p.with_name("local.toml")
    data = tomllib.loads(config_p.read_text())
    local_data = tomllib.loads(local_p.read_text()) if local_p.exists() else {}
    return SettingsSnapshot(
        config_path=config_p,
        local_path=local_p,
        data=data,
        local_data=local_data,
        env_profile=getenv("HERDECK_PROFILE"),
    )


def list_profiles(snapshot: SettingsSnapshot) -> list[dict]:
    active = _active_profile_name(snapshot)
    locked = snapshot.env_profile is not None
    return [
        {"name": name, "active": name == active, "locked": locked}
        for name in sorted(snapshot.data.get("profiles", {}))
    ]


def resolve_profile(snapshot: SettingsSnapshot, name: str | None = None) -> ResolvedSettings:
    data = snapshot.data
    if "profiles" not in data:
        return ResolvedSettings(_resolve_legacy(snapshot), snapshot.local_path)
    profile_name = name or _active_profile_name(snapshot)
    profiles = data.get("profiles", {})
    if profile_name not in profiles:
        raise ConfigError(f"unknown profile '{profile_name}'")
    profile = _profile_chain(profiles, profile_name)
    config = _runtime_config(data, snapshot.local_data, profile_name, profile, snapshot.env_profile)
    return ResolvedSettings(config=config, local_path=snapshot.local_path)


def _active_profile_name(snapshot: SettingsSnapshot) -> str:
    return (
        snapshot.env_profile
        or snapshot.local_data.get("active_profile")
        or snapshot.data.get("active_profile")
        or "default"
    )


def _profile_chain(profiles: dict, name: str) -> dict:
    chain: list[str] = []
    merged: dict = {}
    cur = name
    while cur:
        if cur in chain:
            raise ConfigError("profile inheritance cycle: " + " -> ".join(chain + [cur]))
        if cur not in profiles:
            raise ConfigError(f"unknown profile '{cur}'")
        chain.append(cur)
        raw = dict(profiles[cur])
        parent = raw.pop("extends", None)
        merged = {**raw, **merged}
        cur = parent
    return merged


def _runtime_config(
    data: dict,
    local_data: dict,
    profile_name: str,
    profile: dict,
    env_profile: str | None,
) -> Config:
    servers_by_id = {s["id"]: s for s in data.get("servers", [])}
    selected_server_ids = list(profile.get("servers", servers_by_id))
    servers = [_server_config(servers_by_id[sid]) for sid in selected_server_ids]

    theme = _theme_config(data.get("themes", {}).get(profile.get("theme", "default"), {}))
    view = _view_config(data.get("views", {}).get(profile.get("view", "default"), {}))
    notifications = _notifications_config(
        data.get("notification_profiles", {}).get(profile.get("notifications", "default"), {})
    )
    safety = _safety_config(data.get("safety", {}).get(profile.get("safety", "default"), {}))
    macros = _macro_set(data.get("macro_sets", {}).get(profile.get("macros", "default")))
    launcher = _launcher(data.get("launchers", {}).get(profile.get("launcher", "default")))
    hardware = _hardware_config(local_data)

    return Config(
        servers=servers,
        profiles=dict(DEFAULT_PROFILES),
        overview_order=selected_server_ids,
        grid=(5, 3),
        macros=macros,
        start_profiles=launcher,
        notifications=notifications,
        theme=theme,
        view=view,
        safety=safety,
        hardware=hardware,
        meta=ConfigMeta(
            active_profile=profile_name,
            profile_names=sorted(data.get("profiles", {})),
            env_locked_profile=env_profile is not None,
        ),
    )


def _server_config(raw: dict) -> ServerConfig:
    env = raw["token_env"]
    token = os.environ.get(env)
    if not token:
        raise ConfigError(f"env var '{env}' for server '{raw['id']}' is not set")
    return ServerConfig(raw["id"], raw["url"], token)


def _theme_config(raw: dict) -> ThemeConfig:
    theme = ThemeConfig()
    colors = raw.get("colors", {})
    if colors:
        theme.colors.update({k: str(v) for k, v in colors.items()})
    if "server_accents" in raw:
        theme.server_accents = list(raw["server_accents"])
    return theme


def _view_config(raw: dict) -> ViewConfig:
    view = ViewConfig()
    for key in ("management", "agent_slots"):
        if key in raw:
            setattr(view, key, raw[key])
    if "bottom_row" in raw:
        view.bottom_row = list(raw["bottom_row"])
    if "tile_fields" in raw:
        view.tile_fields = list(raw["tile_fields"])
    if "show_profile_on_panel" in raw:
        view.show_profile_on_panel = bool(raw["show_profile_on_panel"])
    return view


def _notifications_config(raw: dict) -> Notifications:
    telegram = None
    tg_raw = raw.get("telegram")
    if isinstance(tg_raw, dict) and "token_env" in tg_raw and "chat_id" in tg_raw:
        telegram = TelegramConfig(tg_raw["token_env"], str(tg_raw["chat_id"]))
    return Notifications(
        enabled=raw.get("enabled", False),
        on=list(raw.get("on", ["blocked"])),
        sound=raw.get("sound", True),
        backends=list(raw.get("backends", ["macos"])),
        telegram=telegram,
    )


def _safety_config(raw: dict) -> SafetyConfig:
    return SafetyConfig(
        approve_always=raw.get("approve_always", True),
        require_confirm_for=list(raw.get("require_confirm_for", [])),
    )


def _macro_set(raw) -> list[Macro]:
    if raw is None:
        return list(DEFAULT_MACROS)
    return [Macro(label=m["label"], text=m["text"]) for m in raw]


def _launcher(raw) -> dict[str, list[str]]:
    if raw is None:
        return dict(DEFAULT_START_PROFILES)
    return {k: list(v) for k, v in raw.items()}


def _hardware_config(local_data: dict) -> HardwareConfig:
    raw = local_data.get("local", {})
    hw = local_data.get("hardware", {})
    return HardwareConfig(
        deck=raw.get("deck"),
        herdr_socket=raw.get("herdr_socket"),
        web_bind=raw.get("web_bind"),
        web_port=raw.get("web_port"),
        icons_dir=raw.get("icons_dir"),
        brightness=hw.get("brightness", 80),
        debounce=hw.get("debounce", 0.25),
        keep_alive_interval=hw.get("keep_alive_interval", 5.0),
        tick_interval=hw.get("tick_interval", 0.4),
    )


def _resolve_legacy(snapshot: SettingsSnapshot) -> Config:
    from .config import load_config

    cfg = load_config(snapshot.config_path)
    cfg.meta.active_profile = "default"
    cfg.meta.profile_names = ["default"]
    cfg.meta.env_locked_profile = snapshot.env_profile is not None
    return cfg
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_settings.py -v
```

Expected: PASS.

- [ ] **Step 5: Run config and settings suites**

Run:

```bash
.venv/bin/python -m pytest tests/test_config.py tests/test_settings.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/herdeck/settings.py tests/test_settings.py
git commit -m "feat(settings): resolve profile config schema"
sha=$(git rev-parse HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

---

### Task 3: Inheritance Validation, Local Override, Env Lock, Persistence

**Files:**
- Modify: `src/herdeck/settings.py`
- Test: `tests/test_settings.py`

- [ ] **Step 1: Add failing resolver tests**

Append to `tests/test_settings.py`:

```python
def test_profile_inheritance_overrides_named_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDECK_WORKBOX_TOKEN", "secret")
    text = NEW_CONFIG + '''

[profiles.mobile]
extends = "work"
view = "management"

[views.management]
management = "bottom_row"
bottom_row = ["profiles", "new_agent"]
'''
    config = write(tmp_path / "config.toml", text.replace('active_profile = "work"', 'active_profile = "mobile"'))

    cfg = resolve_profile(load_settings(config)).config

    assert cfg.meta.active_profile == "mobile"
    assert cfg.view.management == "bottom_row"
    assert cfg.view.bottom_row == ["profiles", "new_agent"]
    assert cfg.start_profiles["claude"] == ["claude"]


def test_local_toml_overrides_active_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDECK_WORKBOX_TOKEN", "secret")
    config = write(tmp_path / "config.toml", NEW_CONFIG + '''

[profiles.mobile]
extends = "work"
''')
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
        '''
active_profile = "a"
[profiles.a]
extends = "b"
[profiles.b]
extends = "a"
''',
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
    config = write(tmp_path / "config.toml", NEW_CONFIG + '''

[profiles.mobile]
extends = "work"
''')
    local = tmp_path / "local.toml"
    snapshot = load_settings(config, local)

    from herdeck.settings import set_active_profile

    changed = set_active_profile(snapshot, "mobile")

    assert changed is True
    assert 'active_profile = "mobile"' in local.read_text()


def test_set_active_profile_refuses_env_locked_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDECK_WORKBOX_TOKEN", "secret")
    monkeypatch.setenv("HERDECK_PROFILE", "work")
    config = write(tmp_path / "config.toml", NEW_CONFIG + '''

[profiles.mobile]
extends = "work"
''')
    local = tmp_path / "local.toml"
    snapshot = load_settings(config, local)

    from herdeck.settings import set_active_profile

    changed = set_active_profile(snapshot, "mobile")

    assert changed is False
    assert not local.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_settings.py -k "inheritance or local_toml or env_profile or cycle or unknown_block or set_active" -v
```

Expected: FAIL on missing validation and persistence behavior.

- [ ] **Step 3: Implement validation and local persistence**

In `src/herdeck/settings.py`:

1. Add helper:

```python
def _named_block(data: dict, group: str, name: str | None) -> dict | list | None:
    if name is None:
        return None
    blocks = data.get(group, {})
    if name not in blocks:
        singular = group[:-1] if group.endswith("s") else group
        raise ConfigError(f"unknown {singular} '{name}'")
    return blocks[name]
```

2. Replace direct block access in `_runtime_config`:

```python
theme = _theme_config(_named_block(data, "themes", profile.get("theme")))
view = _view_config(_named_block(data, "views", profile.get("view")))
notifications = _notifications_config(
    _named_block(data, "notification_profiles", profile.get("notifications"))
)
safety = _safety_config(_named_block(data, "safety", profile.get("safety")))
macros = _macro_set(_named_block(data, "macro_sets", profile.get("macros")))
launcher = _launcher(_named_block(data, "launchers", profile.get("launcher")))
```

3. Make `_theme_config`, `_view_config`, `_notifications_config`, `_safety_config` accept `None`:

```python
def _theme_config(raw: dict | None) -> ThemeConfig:
    raw = raw or {}
    return ThemeConfig(
        colors=dict(raw.get("colors", {})),
        server_accents=dict(raw.get("server_accents", {})),
        icon_theme=raw.get("icon_theme", "default"),
        icon_overrides=dict(raw.get("icon_overrides", {})),
    )
```

Apply the same `raw = raw or {}` pattern to the other functions before reading optional keys, for example:

```python
def _view_config(raw: dict | None) -> ViewConfig:
    raw = raw or {}
    return ViewConfig(
        agent_order=list(raw.get("agent_order", [])),
        tile_fields=list(raw.get("tile_fields", DEFAULT_TILE_FIELDS)),
        management=raw.get("management", "launcher"),
        bottom_row=list(raw.get("bottom_row", [])),
    )
```

4. Add `set_active_profile`:

```python
def set_active_profile(snapshot: SettingsSnapshot, name: str, *, persist: bool = True) -> bool:
    profiles = snapshot.data.get("profiles", {})
    if name not in profiles:
        raise ConfigError(f"unknown profile '{name}'")
    if snapshot.env_profile is not None:
        return False
    if not persist:
        return True
    local_path = snapshot.local_path
    if local_path is None:
        return False
    local_path.parent.mkdir(parents=True, exist_ok=True)
    existing = snapshot.local_data
    lines = [f'active_profile = "{name}"']
    for section, values in existing.items():
        if section == "active_profile":
            continue
        if isinstance(values, dict):
            lines.append("")
            lines.append(f"[{section}]")
            for key, value in values.items():
                lines.append(_toml_line(key, value))
    local_path.write_text("\n".join(lines) + "\n")
    return True


def _toml_line(key: str, value) -> str:
    if isinstance(value, bool):
        return f"{key} = {'true' if value else 'false'}"
    if isinstance(value, int | float):
        return f"{key} = {value}"
    return f'{key} = "{value}"'
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_settings.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/herdeck/settings.py tests/test_settings.py
git commit -m "feat(settings): support profile overrides and persistence"
sha=$(git rev-parse HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

---

### Task 4: Integrate New Settings With `load_config` and Local Discovery

**Files:**
- Modify: `src/herdeck/config.py`
- Modify: `src/herdeck/app.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_local_mode.py`

- [ ] **Step 1: Add failing integration tests**

Append to `tests/test_config.py`:

```python
def test_load_config_uses_new_profile_schema(tmp_path, monkeypatch):
    from tests.test_settings import NEW_CONFIG

    monkeypatch.setenv("HERDECK_WORKBOX_TOKEN", "secret123")
    path = _write(tmp_path, NEW_CONFIG)

    cfg = load_config(path)

    assert cfg.meta.active_profile == "work"
    assert cfg.view.show_profile_on_panel is True
    assert cfg.notifications.enabled is True
    assert cfg.servers[0].token == "secret123"
```

Append to `tests/test_local_mode.py`:

```python
def test_discover_local_config_next_to_config(monkeypatch, tmp_path):
    from herdeck.app import _discover_local_config_path

    cfg = tmp_path / "config.toml"
    cfg.write_text("")
    local = tmp_path / "local.toml"

    monkeypatch.delenv("HERDECK_LOCAL_CONFIG", raising=False)

    assert _discover_local_config_path(str(cfg)) == str(local)


def test_discover_local_config_prefers_env(monkeypatch, tmp_path):
    from herdeck.app import _discover_local_config_path

    env = tmp_path / "device.toml"
    monkeypatch.setenv("HERDECK_LOCAL_CONFIG", str(env))

    assert _discover_local_config_path("/x/config.toml") == str(env)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_config.py::test_load_config_uses_new_profile_schema tests/test_local_mode.py::test_discover_local_config_next_to_config tests/test_local_mode.py::test_discover_local_config_prefers_env -v
```

Expected: FAIL because `load_config` ignores profiles and `_discover_local_config_path` does not exist.

- [ ] **Step 3: Update `load_config` to delegate new schema**

In `src/herdeck/config.py`, split current `load_config` body into `_load_legacy_config` and add:

```python
def load_config(path: str | Path) -> Config:
    data = tomllib.loads(Path(path).read_text())
    if "profiles" in data:
        from .settings import load_settings, resolve_profile

        return resolve_profile(load_settings(path)).config
    return _load_legacy_config(path, data=data)


def _load_legacy_config(path: str | Path, *, data: dict | None = None) -> Config:
    data = data if data is not None else tomllib.loads(Path(path).read_text())
```

Move the old body into `_load_legacy_config` unchanged.

- [ ] **Step 4: Add local config discovery**

In `src/herdeck/app.py`, after `_discover_config_path`, add:

```python
def _discover_local_config_path(config_path: str | None):
    p = os.environ.get("HERDECK_LOCAL_CONFIG")
    if p:
        return os.path.abspath(p)
    if config_path:
        return os.path.join(os.path.dirname(config_path), "local.toml")
    return os.path.expanduser("~/.config/herdeck/local.toml")
```

In `main()`, replace:

```python
file_config = load_config(config_path) if config_path else None
```

with:

```python
local_config_path = _discover_local_config_path(config_path) if not mock else None
if config_path:
    from .settings import load_settings, resolve_profile

    file_config = resolve_profile(load_settings(config_path, local_config_path)).config
else:
    file_config = None
```

- [ ] **Step 5: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_config.py::test_load_config_uses_new_profile_schema tests/test_local_mode.py::test_discover_local_config_next_to_config tests/test_local_mode.py::test_discover_local_config_prefers_env -v
```

Expected: PASS.

- [ ] **Step 6: Run config/local/settings suites**

Run:

```bash
.venv/bin/python -m pytest tests/test_config.py tests/test_local_mode.py tests/test_settings.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/herdeck/config.py src/herdeck/app.py tests/test_config.py tests/test_local_mode.py
git commit -m "feat(config): load profile settings with local overrides"
sha=$(git rev-parse HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

---

### Task 5: Theme and Tile Content Preferences

**Files:**
- Modify: `src/herdeck/layout.py`
- Modify: `src/herdeck/orchestrator.py`
- Modify: `src/herdeck/icons.py`
- Test: `tests/test_layout.py`
- Test: `tests/test_orchestrator.py`
- Test: `tests/test_icons.py`

- [ ] **Step 1: Add failing tests for theme colors and tile fields**

Append to `tests/test_orchestrator.py`:

```python
def test_theme_status_colors_apply_to_agent_tiles():
    cfg = make_config()
    cfg.theme.colors["blocked"] = "pink"
    o = Orchestrator(cfg, slots=13)
    o.apply_snapshot("dev", [state("p1", Status.BLOCKED)])

    assert o.render().tiles[0].color == "pink"


def test_tile_fields_can_hide_branch_status_time_and_server_tag():
    cfg = make_multi_config()
    cfg.view.tile_fields = ["repo"]
    o = Orchestrator(cfg, slots=13)
    s = AgentState(AgentKey("alpha", "p1"), "claude", "api", Status.IDLE)
    s.repo = "repo"
    s.branch = "feat/x"
    o.apply_snapshot("alpha", [s])
    o.apply_event("bravo", AgentState(AgentKey("bravo", "p1"), "codex", "rb", Status.IDLE))

    tile = o.render().tiles[0]

    assert tile.repo == "repo"
    assert tile.branch == ""
    assert tile.status_text is None
    assert tile.time_text is None
    assert tile.server_tag is None
```

Append to `tests/test_icons.py`:

```python
def test_theme_server_accent_color_renders(tmp_path):
    from herdeck.driver.base import TileView

    p = make_provider(tmp_path)
    tile = TileView(
        0,
        "",
        "blue",
        agent_type="claude",
        repo="api",
        branch="",
        status_text="IDLE",
        server_tag="DEV",
        server_accent="#334455",
    )

    assert p.render_tile_bytes(tile)[:4] == b"\x89PNG"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_orchestrator.py::test_theme_status_colors_apply_to_agent_tiles tests/test_orchestrator.py::test_tile_fields_can_hide_branch_status_time_and_server_tag tests/test_icons.py::test_theme_server_accent_color_renders -v
```

Expected: FAIL because hard-coded colors/fields are still used.

- [ ] **Step 3: Apply theme colors in orchestrator**

In `src/herdeck/orchestrator.py`:

1. Change `SERVER_ACCENTS` fallback to use config:

```python
def server_accent(server_id: str, accents: list[str] | None = None) -> str:
    palette = accents or list(SERVER_ACCENTS)
    digest = hashlib.sha1(server_id.encode()).digest()
    return palette[digest[0] % len(palette)]
```

2. Change `_agent_color`:

```python
def _agent_color(self, s: AgentState) -> str:
    if s.key.server_id in self._down:
        return self.config.theme.colors.get("offline", "red")
    return self.config.theme.colors.get(s.status.value, layout.status_color(s.status))
```

3. Add helper:

```python
def _tile_field_enabled(self, name: str) -> bool:
    return name in self.config.view.tile_fields
```

4. In `_render_overview`, set tile fields:

```python
fields = self.config.view.tile_fields
show_server_tags = "server" in fields and len({s.key.server_id for s in ordered}) > 1
for i in range(self.slots):
    if i in self._management_indices():
        tiles.append(TileView(i, self._management_indices()[i], "grey"))
    elif i < len(shown):
        s = shown[i]
        down = s.key.server_id in self._down
        tag = s.key.server_id[:3].upper() if show_server_tags else None
        accent = (
            server_accent(s.key.server_id, self.config.theme.server_accents)
            if show_server_tags
            else None
        )
        tiles.append(
            TileView(
                i,
                s.label,
                self._agent_color(s),
                icon=self.config.theme.icon_overrides.get(s.agent_type),
                agent_type=s.agent_type,
                spinner=self._phase if s.status is Status.WORKING else None,
                repo=(s.repo or s.label) if "repo" in fields else None,
                branch=(s.branch or "") if "branch" in fields else "",
                status_text=(
                    "OFFLINE" if down else s.status.value.upper()
                )
                if "status" in fields
                else None,
                time_text=self._elapsed_text(s.key) if "time" in fields else None,
                server_tag=tag,
                server_accent=accent,
            )
        )
```

- [ ] **Step 4: Support hex server chip colors**

In `src/herdeck/icons.py`, add:

```python
def _rgb_color(name: str, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    if isinstance(name, str) and name.startswith("#") and len(name) == 7:
        try:
            return tuple(int(name[i : i + 2], 16) for i in (1, 3, 5))
        except ValueError:
            return fallback
    return SERVER_CHIP_COLORS.get(name, fallback)
```

Replace:

```python
chip_fill = SERVER_CHIP_COLORS.get(tile.server_accent or "", (95, 95, 105))
```

with:

```python
chip_fill = _rgb_color(tile.server_accent or "", (95, 95, 105))
```

- [ ] **Step 5: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_orchestrator.py::test_theme_status_colors_apply_to_agent_tiles tests/test_orchestrator.py::test_tile_fields_can_hide_branch_status_time_and_server_tag tests/test_icons.py::test_theme_server_accent_color_renders -v
```

Expected: PASS.

- [ ] **Step 6: Run render/orchestrator suites**

Run:

```bash
.venv/bin/python -m pytest tests/test_layout.py tests/test_orchestrator.py tests/test_icons.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/herdeck/orchestrator.py src/herdeck/icons.py tests/test_orchestrator.py tests/test_icons.py
git commit -m "feat(view): apply theme and tile content preferences"
sha=$(git rev-parse HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

---

### Task 6: Profile Menu and Management Row in Orchestrator

**Files:**
- Modify: `src/herdeck/orchestrator.py`
- Test: `tests/test_orchestrator_nav.py`
- Test: `tests/test_orchestrator.py`

- [ ] **Step 1: Add failing navigation tests**

Append to `tests/test_orchestrator_nav.py`:

```python
def test_launcher_contains_profiles_entry_when_multiple_profiles_exist():
    cfg = make_config()
    cfg.meta.profile_names = ["work", "mobile"]
    cfg.meta.active_profile = "work"
    o = Orchestrator(cfg, slots=13)

    o.on_press(12)  # + New
    labels = [t.label for t in o.render().tiles if t.label]

    assert "Profiles" in labels


def test_profile_menu_lists_profiles_and_switches():
    cfg = make_config()
    cfg.meta.profile_names = ["work", "mobile"]
    cfg.meta.active_profile = "work"
    o = Orchestrator(cfg, slots=13)

    o.on_press(12)  # + New
    profiles_index = [t.label for t in o.render().tiles].index("Profiles")
    assert o.on_press(profiles_index) == []
    rs = o.render()
    assert rs.panel.title == "profiles"
    assert rs.tiles[0].label == "* work"
    assert rs.tiles[1].label == "mobile"

    assert o.on_press(1) == [Command("switch_profile", "mobile", text="mobile")]
    assert o.render().tiles[12].label == "+ New"


def test_management_row_can_expose_profiles_and_new_agent():
    cfg = make_config()
    cfg.meta.profile_names = ["work", "mobile"]
    cfg.meta.active_profile = "work"
    cfg.view.management = "bottom_row"
    cfg.view.bottom_row = ["profiles", "new_agent"]
    o = Orchestrator(cfg, slots=13)
    o.apply_snapshot("dev", [st(f"p{i}", Status.IDLE, label=f"a{i}") for i in range(1, 13)])

    labels = [t.label for t in o.render().tiles]

    assert labels[10] == "Profiles"
    assert labels[11] == "+ New"
    assert labels[12] == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_orchestrator_nav.py::test_launcher_contains_profiles_entry_when_multiple_profiles_exist tests/test_orchestrator_nav.py::test_profile_menu_lists_profiles_and_switches tests/test_orchestrator_nav.py::test_management_row_can_expose_profiles_and_new_agent -v
```

Expected: FAIL because profile menu and management row do not exist.

- [ ] **Step 3: Extend command and orchestrator state**

In `src/herdeck/orchestrator.py`:

1. Update comment on `Command.kind`:

```python
kind: str  # list|read|focus|act_if_blocked|act_force|send_text|start|switch_profile
```

2. In `__init__`, add:

```python
self._profile_menu: bool = False
```

3. Update `render`:

```python
if self._profile_menu:
    return self._render_profile_menu()
```

4. Add:

```python
def update_config(self, config: Config) -> None:
    self.config = config
    self._launcher = False
    self._profile_menu = False
    self._drill = None
    self._detection = ""
    self._page = 0
```

- [ ] **Step 4: Implement profile menu rendering and pressing**

Add methods:

```python
def _render_profile_menu(self) -> RenderState:
    names = list(self.config.meta.profile_names)
    back_i = self.slots - 1
    tiles: list[TileView] = []
    for i in range(self.slots):
        if i < len(names) and i < back_i:
            name = names[i]
            label = f"* {name}" if name == self.config.meta.active_profile else name
            tiles.append(TileView(i, label[:_OPTION_LABEL_MAX], "blue"))
        elif i == back_i:
            tiles.append(TileView(i, "Back", "grey"))
        else:
            tiles.append(TileView(i, "", "dim"))
    locked = "locked by env" if self.config.meta.env_locked_profile else "pick a profile"
    return RenderState(tiles, PanelView("profiles", [locked], "grey"))


def _press_profile_menu(self, index: int) -> list[Command]:
    names = list(self.config.meta.profile_names)
    back_i = self.slots - 1
    if index == back_i:
        self._profile_menu = False
        self._launcher = True
        return []
    if index < len(names) and index < back_i:
        name = names[index]
        self._profile_menu = False
        self._launcher = False
        return [Command("switch_profile", name, text=name)]
    return []
```

Update `on_press`:

```python
if self._profile_menu:
    return self._press_profile_menu(index)
```

- [ ] **Step 5: Add launcher `Profiles` entry and management row**

In `_render_launcher`, append a synthetic `Profiles` item before back when `len(self.config.meta.profile_names) > 1`:

```python
types = list(self.config.start_profiles)
entries = types + (["Profiles"] if len(self.config.meta.profile_names) > 1 else [])
```

Use `entries` for labels. In `_press_launcher`, if selected entry is `Profiles`, set `_profile_menu = True`, `_launcher = False`, return `[]`; otherwise preserve start behavior.

For management row, add helpers:

```python
def _management_indices(self) -> dict[int, str]:
    if self.config.view.management != "bottom_row":
        return {}
    # The D200 has 13 addressable tiles: rows 0-1 are 10 agent/control tiles,
    # row 2 has three tile positions before the two-cell panel. Management row
    # actions fill those bottom-row tile positions left-to-right and leave
    # unused positions blank.
    start = max(0, self.slots - 3)
    count = min(len(self.config.view.bottom_row), self.slots - start)
    return {start + i: action for i, action in enumerate(self.config.view.bottom_row)}


def _management_label(self, action: str) -> str:
    return {
        "profiles": "Profiles",
        "notifications": "Notify",
        "safety": "Safety",
        "theme": "Theme",
        "new_agent": "+ New",
    }.get(action, action)
```

In `_render_overview`, reserve these indices for management actions before agent tiles. In `_press_overview`, handle:

```python
management = self._management_indices()
if index in management:
    action = management[index]
    if action == "profiles":
        self._profile_menu = True
    elif action == "new_agent":
        self._launcher = True
    return []
```

- [ ] **Step 6: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_orchestrator_nav.py::test_launcher_contains_profiles_entry_when_multiple_profiles_exist tests/test_orchestrator_nav.py::test_profile_menu_lists_profiles_and_switches tests/test_orchestrator_nav.py::test_management_row_can_expose_profiles_and_new_agent -v
```

Expected: PASS.

- [ ] **Step 7: Run orchestrator suites**

Run:

```bash
.venv/bin/python -m pytest tests/test_orchestrator.py tests/test_orchestrator_nav.py tests/test_orchestrator_tick.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/herdeck/orchestrator.py tests/test_orchestrator_nav.py
git commit -m "feat(deck): add profile menu and management row"
sha=$(git rev-parse HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

---

### Task 7: App Profile Switching and Connector Manager

**Files:**
- Modify: `src/herdeck/app.py`
- Modify: `src/herdeck/orchestrator.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Add failing tests for app-side profile switch**

Append to `tests/test_app.py`:

```python
def test_app_switch_profile_updates_config_and_persists():
    deck = FakeRenderer(13)
    base = make_config()
    base.meta.profile_names = ["work", "mobile"]
    base.meta.active_profile = "work"
    next_cfg = make_config()
    next_cfg.meta.profile_names = ["work", "mobile"]
    next_cfg.meta.active_profile = "mobile"
    persisted = []
    connector_updates = []

    app = App(
        base,
        deck,
        send=lambda c: None,
        switch_profile=lambda name: (persisted.append(name), next_cfg)[1],
        update_connectors=lambda cfg: connector_updates.append([s.id for s in cfg.servers]),
    )

    app._handle_press(12)  # + New
    profiles_index = [t.label for t in deck.last].index("Profiles")
    app._handle_press(profiles_index)
    app._handle_press(1)  # mobile

    assert persisted == ["mobile"]
    assert connector_updates == [["dev"]]
    assert app.config.meta.active_profile == "mobile"
    assert app.orch.config.meta.active_profile == "mobile"


def test_app_env_locked_profile_switch_shows_panel_message():
    deck = FakeRenderer(13)
    cfg = make_config()
    cfg.meta.profile_names = ["work", "mobile"]
    cfg.meta.active_profile = "work"
    cfg.meta.env_locked_profile = True

    app = App(cfg, deck, send=lambda c: None, switch_profile=lambda name: None)
    app._handle_press(12)
    profiles_index = [t.label for t in deck.last].index("Profiles")
    app._handle_press(profiles_index)
    app._handle_press(1)

    assert deck.last_panel.title == "profile locked"
```

- [ ] **Step 2: Add failing tests for connector diff**

Append to `tests/test_app.py`:

```python
def test_connector_manager_diffs_servers():
    from herdeck.app import ConnectorManager
    from herdeck.config import ServerConfig

    made = []
    stopped = []

    class FakeConnector:
        def __init__(self, server):
            self.server = server
            made.append(server.id)

        def stop(self):
            stopped.append(self.server.id)

    mgr = ConnectorManager(
        make_connector=lambda server: FakeConnector(server),
        start_connector=lambda conn: None,
    )
    mgr.update([ServerConfig("a", "ws://a", "t"), ServerConfig("b", "ws://b", "t")])
    mgr.update([ServerConfig("b", "ws://b", "t"), ServerConfig("c", "ws://c", "t")])

    assert made == ["a", "b", "c"]
    assert stopped == ["a"]
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_app.py::test_app_switch_profile_updates_config_and_persists tests/test_app.py::test_app_env_locked_profile_switch_shows_panel_message tests/test_app.py::test_connector_manager_diffs_servers -v
```

Expected: FAIL because `App` has no `switch_profile` callback and `ConnectorManager` is missing.

- [ ] **Step 4: Add `switch_profile` callback to App**

In `App.__init__`, add argument:

```python
switch_profile: Callable[[str], Config | None] | None = None,
update_connectors: Callable[[Config], None] | None = None,
```

Store:

```python
self._switch_profile = switch_profile
self._update_connectors = update_connectors or (lambda cfg: None)
self._status_panel: PanelView | None = None
```

Add method:

```python
def _set_status_panel(self, title: str, lines: list[str], color: str = "grey") -> None:
    self._status_panel = PanelView(title, lines, color)
    try:
        self.deck.render_panel(self._status_panel)
    except Exception:
        pass
```

Update `_handle_press` loop:

```python
for cmd in cmds:
    if cmd.kind == "switch_profile":
        self._handle_switch_profile(cmd.text or cmd.server_id)
    else:
        self._send(cmd)
```

Add:

```python
def _handle_switch_profile(self, name: str) -> None:
    if self.config.meta.env_locked_profile or self._switch_profile is None:
        self._set_status_panel("profile locked", [self.config.meta.active_profile], "amber")
        return
    new_config = self._switch_profile(name)
    if new_config is None:
        self._set_status_panel("profile locked", [self.config.meta.active_profile], "amber")
        return
    self.config = new_config
    self.notifier = _build_notifier(new_config)
    self.orch.update_config(new_config)
    self._update_connectors(new_config)
    for server in new_config.servers:
        self.orch.set_connection(server.id, False)
    self._refresh()
```

- [ ] **Step 5: Add ConnectorManager**

In `src/herdeck/app.py`, before `_run`, add:

```python
class ConnectorManager:
    def __init__(self, *, make_connector, start_connector):
        self._make_connector = make_connector
        self._start_connector = start_connector
        self.connectors: dict[str, Connector] = {}
        self._fingerprints: dict[str, tuple[str, str]] = {}

    def update(self, servers: list[ServerConfig]) -> None:
        wanted = {s.id: s for s in servers}
        for sid in list(self.connectors):
            old = self._fingerprints[sid]
            new = wanted.get(sid)
            if new is None or (new.url, new.token) != old:
                self.connectors[sid].stop()
                del self.connectors[sid]
                del self._fingerprints[sid]
        for sid, server in wanted.items():
            fp = (server.url, server.token)
            if sid not in self.connectors:
                conn = self._make_connector(server)
                self.connectors[sid] = conn
                self._fingerprints[sid] = fp
                self._start_connector(conn)

    def get(self, server_id: str) -> Connector | None:
        return self.connectors.get(server_id)

    def stop_all(self) -> None:
        for conn in list(self.connectors.values()):
            conn.stop()
        self.connectors.clear()
        self._fingerprints.clear()
```

Import `ServerConfig` from `.config` at top.

- [ ] **Step 6: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_app.py::test_app_switch_profile_updates_config_and_persists tests/test_app.py::test_app_env_locked_profile_switch_shows_panel_message tests/test_app.py::test_connector_manager_diffs_servers -v
```

Expected: PASS.

- [ ] **Step 7: Wire manager into `_run`**

Refactor `_run`:

- Build `ConnectorManager` with `make_connector(server)` containing the existing `Connector` construction and its `on_snapshot`, `on_event`, `on_connection`, and `on_result` callbacks.
- Use `manager.get(cmd.server_id)` in `send`.
- Replace initial connector loop with `manager.update(config.servers)`.
- Pass `update_connectors=lambda cfg: manager.update(cfg.servers)` to the `App` constructor.
- In a later task, also pass `switch_profile=make_profile_switcher(snapshot)`.

Keep behavior identical for remote run tests.

- [ ] **Step 8: Run app/connector suites**

Run:

```bash
.venv/bin/python -m pytest tests/test_app.py tests/test_connector.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/herdeck/app.py tests/test_app.py
git commit -m "feat(app): switch profiles at runtime"
sha=$(git rev-parse HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

---

### Task 8: Settings Service Boundary Connected to App

**Files:**
- Modify: `src/herdeck/settings.py`
- Modify: `src/herdeck/app.py`
- Test: `tests/test_settings.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Add failing service tests**

Append to `tests/test_settings.py`:

```python
def test_validate_settings_reports_missing_references(tmp_path):
    from herdeck.settings import validate_settings

    config = write(
        tmp_path / "config.toml",
        '''
active_profile = "work"
[profiles.work]
theme = "missing"
''',
    )

    errors = validate_settings(load_settings(config))

    assert any("unknown theme 'missing'" in err for err in errors)
```

Append to `tests/test_app.py`:

```python
def test_make_profile_switcher_resolves_and_persists(tmp_path, monkeypatch):
    from tests.test_settings import NEW_CONFIG
    from herdeck.app import make_profile_switcher
    from herdeck.settings import load_settings

    monkeypatch.setenv("HERDECK_WORKBOX_TOKEN", "secret")
    config = tmp_path / "config.toml"
    config.write_text(NEW_CONFIG + '''

[profiles.mobile]
extends = "work"
''')
    local = tmp_path / "local.toml"
    snapshot = load_settings(config, local)

    switch = make_profile_switcher(snapshot)
    cfg = switch("mobile")

    assert cfg.meta.active_profile == "mobile"
    assert 'active_profile = "mobile"' in local.read_text()
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_settings.py::test_validate_settings_reports_missing_references tests/test_app.py::test_make_profile_switcher_resolves_and_persists -v
```

Expected: FAIL because `validate_settings` and `make_profile_switcher` are missing.

- [ ] **Step 3: Implement validation**

In `src/herdeck/settings.py`, add:

```python
def validate_settings(snapshot: SettingsSnapshot) -> list[str]:
    if "profiles" not in snapshot.data:
        try:
            resolve_profile(snapshot)
        except ConfigError as exc:
            return [str(exc)]
        return []

    errors: list[str] = []
    for name in sorted(snapshot.data.get("profiles", {})):
        try:
            resolve_profile(snapshot, name)
        except ConfigError as exc:
            errors.append(f"{name}: {exc}")
    return errors
```

- [ ] **Step 4: Implement profile switcher factory**

In `src/herdeck/app.py`, add:

```python
def make_profile_switcher(snapshot):
    from .settings import resolve_profile, set_active_profile

    def switch(name: str) -> Config | None:
        changed = set_active_profile(snapshot, name)
        if not changed:
            return None
        # Re-read from disk so local.toml changes become the source of truth.
        from .settings import load_settings

        refreshed = load_settings(snapshot.config_path, snapshot.local_path)
        return resolve_profile(refreshed).config

    return switch
```

In `main()`, where settings are loaded, keep `snapshot` and pass `switch_profile=make_profile_switcher(snapshot)` into `_run`. To keep `_run` simple, change signature:

```python
async def _run(config: Config, deck: DeckDriver, switch_profile=None) -> None:
```

Pass `switch_profile` to the `App` constructor.

- [ ] **Step 5: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_settings.py::test_validate_settings_reports_missing_references tests/test_app.py::test_make_profile_switcher_resolves_and_persists -v
```

Expected: PASS.

- [ ] **Step 6: Run app/settings suites**

Run:

```bash
.venv/bin/python -m pytest tests/test_settings.py tests/test_app.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/herdeck/settings.py src/herdeck/app.py tests/test_settings.py tests/test_app.py
git commit -m "feat(settings): expose app-ready settings service"
sha=$(git rev-parse HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

---

### Task 9: Safety Policy Enforcement

**Files:**
- Modify: `src/herdeck/orchestrator.py`
- Test: `tests/test_orchestrator_nav.py`

- [ ] **Step 1: Add failing safety tests**

Append to `tests/test_orchestrator_nav.py`:

```python
def test_safety_can_hide_approve_always_action():
    o = Orchestrator(make_config(), slots=13)
    o.config.safety.approve_always = False
    o.apply_snapshot("dev", [st("p1", Status.BLOCKED, agent_type="claude")])
    o.on_press(0)
    o.set_detection("Proceed? (y/n)")

    labels = [t.label for t in o.render().tiles[:3]]

    assert labels == ["Approve", "Deny", ""]


def test_safety_confirmation_blocks_force_stop_until_second_press():
    o = Orchestrator(make_config(), slots=13)
    o.config.safety.require_confirm_for = ["act_force"]
    o.apply_snapshot("dev", [st("p1", Status.WORKING)])
    o.on_press(0)

    first = o.on_press(11)
    second = o.on_press(11)

    assert first == []
    assert second == [Command("act_force", "dev", "p1", keys=["ctrl+c"])]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_orchestrator_nav.py::test_safety_can_hide_approve_always_action tests/test_orchestrator_nav.py::test_safety_confirmation_blocks_force_stop_until_second_press -v
```

Expected: FAIL because safety policy is not enforced.

- [ ] **Step 3: Implement safety policy**

In `Orchestrator.__init__`, add:

```python
self._pending_confirm: tuple[str, AgentKey] | None = None
```

In `_drill_layout`, build fallback actions conditionally:

```python
fallback = [("Approve", profile.approve, "approve")]
if self.config.safety.approve_always:
    fallback.append(("Approve!", profile.approve_always, "approve_always"))
fallback.append(("Deny", profile.deny, "deny"))
```

Use `fallback` instead of the hard-coded tuple.

In `_press_drill`, for stop:

```python
if index == stop_i:
    action = "act_force"
    if action in self.config.safety.require_confirm_for and self._pending_confirm != (action, key):
        self._pending_confirm = (action, key)
        return []
    self._pending_confirm = None
    cmd = Command("act_force", key.server_id, key.pane_id, keys=self._profile_for(key).stop)
    self._drill = None
    return [cmd]
```

Clear `_pending_confirm` when backing out or switching modes.

- [ ] **Step 4: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_orchestrator_nav.py::test_safety_can_hide_approve_always_action tests/test_orchestrator_nav.py::test_safety_confirmation_blocks_force_stop_until_second_press -v
```

Expected: PASS.

- [ ] **Step 5: Run orchestrator suites**

Run:

```bash
.venv/bin/python -m pytest tests/test_orchestrator.py tests/test_orchestrator_nav.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/herdeck/orchestrator.py tests/test_orchestrator_nav.py
git commit -m "feat(safety): enforce runtime action policy"
sha=$(git rev-parse HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

---

### Task 10: Hardware and Startup Local Settings

**Files:**
- Modify: `src/herdeck/app.py`
- Modify: `src/herdeck/driver/d200.py`
- Modify: `src/herdeck/driver/elgato.py`
- Test: `tests/test_local_mode.py`
- Test: `tests/test_driver_elgato.py`
- Test: `tests/test_d200_panel.py` or create `tests/test_driver_d200.py`

- [ ] **Step 1: Add failing tests for hardware/local settings**

Append to `tests/test_local_mode.py`:

```python
def test_make_deck_uses_hardware_web_bind_and_port():
    from herdeck.config import HardwareConfig

    seen = {}

    def web_factory(host=None, port=None):
        seen["host"] = host
        seen["port"] = port
        return _Web()

    hw = HardwareConfig(web_bind="100.1.2.3", web_port=1234)
    make_deck("web", 13, web_factory=web_factory, hardware=hw)

    assert seen == {"host": "100.1.2.3", "port": 1234}


def test_runtime_startup_settings_prefer_env_over_local(monkeypatch):
    from herdeck.app import _resolve_deck_kind, _resolve_socket_path, _resolve_tick_interval
    from herdeck.config import Config, HardwareConfig

    cfg = Config(servers=[], profiles={}, overview_order=[], grid=(5, 3))
    cfg.hardware = HardwareConfig(deck="web", herdr_socket="/local.sock", tick_interval=1.25)

    monkeypatch.setenv("HERDECK_DECK", "fake")
    monkeypatch.setenv("HERDR_SOCKET", "/env.sock")

    assert _resolve_deck_kind(cfg) == "fake"
    assert _resolve_socket_path(cfg) == "/env.sock"
    assert _resolve_tick_interval(cfg) == 1.25


def test_runtime_startup_settings_use_local_when_env_absent(monkeypatch):
    from herdeck.app import _resolve_deck_kind, _resolve_socket_path, _resolve_tick_interval
    from herdeck.config import Config, HardwareConfig

    cfg = Config(servers=[], profiles={}, overview_order=[], grid=(5, 3))
    cfg.hardware = HardwareConfig(deck="web", herdr_socket="/local.sock", tick_interval=1.25)

    monkeypatch.delenv("HERDECK_DECK", raising=False)
    monkeypatch.delenv("HERDECK_FAKE_DECK", raising=False)
    monkeypatch.delenv("HERDR_SOCKET", raising=False)

    assert _resolve_deck_kind(cfg) == "web"
    assert _resolve_socket_path(cfg) == "/local.sock"
    assert _resolve_tick_interval(cfg) == 1.25
```

Append to `tests/test_driver_elgato.py`:

```python
def test_elgato_brightness_can_be_configured(fake_deck):
    from herdeck.driver.elgato import ElgatoDriver

    d = ElgatoDriver(device=fake_deck, brightness=35)

    assert fake_deck.brightness == 35
    d.close()
```

If no `fake_deck` fixture has `brightness`, update the test fake to store the value passed to `set_brightness`.

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_local_mode.py::test_make_deck_uses_hardware_web_bind_and_port tests/test_local_mode.py::test_runtime_startup_settings_prefer_env_over_local tests/test_local_mode.py::test_runtime_startup_settings_use_local_when_env_absent tests/test_driver_elgato.py::test_elgato_brightness_can_be_configured -v
```

Expected: FAIL because hardware settings are not accepted.

- [ ] **Step 3: Add hardware parameters to `make_deck`**

In `src/herdeck/app.py`, change signature and initialize a default hardware object:

```python
def make_deck(kind, slots, *, hardware=None, d200_factory=None, elgato_factory=None, web_factory=None):
    from .config import HardwareConfig

    hardware = hardware or HardwareConfig()
```

In default web factory:

```python
host = hardware.web_bind or os.environ.get("HERDECK_WEB_BIND", "127.0.0.1")
port = int(hardware.web_port or os.environ.get("HERDECK_WEB_PORT", "8800"))
d = WebDeck(slots, host=host, port=port)
```

If an injected `web_factory` accepts `host`/`port`, call `web_factory(host=host, port=port)`. If old tests inject zero-arg factories, support both:

```python
try:
    return web_factory(host=host, port=port)
except TypeError:
    return web_factory()
```

In `main`, call:

```python
deck = make_deck(kind, slots, hardware=file_config.hardware if file_config else None)
```

Add startup resolver helpers in `src/herdeck/app.py`:

```python
def _resolve_deck_kind(config: Config | None, *, getenv=os.environ.get):
    env_kind = getenv("HERDECK_DECK")
    if env_kind:
        return env_kind
    if getenv("HERDECK_FAKE_DECK"):
        return "fake"
    return config.hardware.deck if config and config.hardware.deck else None


def _resolve_socket_path(config: Config | None, *, getenv=os.environ.get) -> str:
    raw = getenv("HERDR_SOCKET") or (
        config.hardware.herdr_socket if config and config.hardware.herdr_socket else None
    )
    return os.path.expanduser(raw or "~/.config/herdr/herdr.sock")


def _resolve_tick_interval(config: Config | None) -> float:
    return config.hardware.tick_interval if config else TICK_INTERVAL
```

Change `_ticker` to accept an interval:

```python
async def _ticker(app: App, loop, interval: float = TICK_INTERVAL) -> None:
    while True:
        await asyncio.sleep(interval)
        loop.call_soon_threadsafe(app.handle_tick)
```

Change `_run` to accept and use the interval:

```python
async def _run(
    config: Config,
    deck: DeckDriver,
    switch_profile=None,
    tick_interval: float | None = None,
) -> None:
    if not config.servers:
        raise ConfigError("no servers configured for remote run")
    loop = asyncio.get_running_loop()
    tasks = []

    def make_connector(server: ServerConfig) -> Connector:
        return Connector(
            server,
            on_snapshot=lambda sid, st: loop.call_soon_threadsafe(app.handle_snapshot, sid, st),
            on_event=lambda sid, s: loop.call_soon_threadsafe(app.handle_event, sid, s),
            on_connection=lambda sid, up: loop.call_soon_threadsafe(app.handle_connection, sid, up),
            on_result=lambda req, data, sid=server.id: loop.call_soon_threadsafe(
                app.handle_result, sid, req, data
            ),
        )

    def start_connector(conn: Connector) -> None:
        tasks.append(_guarded(conn))

    manager = ConnectorManager(
        make_connector=make_connector,
        start_connector=start_connector,
    )

    def send(cmd: Command) -> None:
        conn = manager.get(cmd.server_id)
        if conn is not None:
            asyncio.run_coroutine_threadsafe(conn.send(_command_to_msg(cmd, app)), loop)

    app = App(
        config,
        deck,
        send,
        schedule=lambda fn: loop.call_soon_threadsafe(fn),
        notifier=_build_notifier(config),
        notify_schedule=lambda fn: loop.run_in_executor(None, fn),
        switch_profile=switch_profile,
        update_connectors=lambda cfg: manager.update(cfg.servers),
    )
    manager.update(config.servers)
    tasks.append(_guard(_ticker(app, loop, tick_interval or config.hardware.tick_interval)))
    await asyncio.gather(*tasks)
```

Keep the existing connector construction inside the manager factory from Task 7; do not leave a separate unmanaged `connectors` dict in `_run`.

In `main()`, replace direct env/default reads:

```python
snapshot = None
switch_profile = None
if config_path:
    from .settings import load_settings, resolve_profile

    local_config_path = _discover_local_config_path(config_path)
    snapshot = load_settings(config_path, local_config_path)
    file_config = resolve_profile(snapshot).config
    switch_profile = make_profile_switcher(snapshot)
else:
    file_config = None

socket_path = _resolve_socket_path(file_config)
kind = _resolve_deck_kind(file_config)
deck = make_deck(kind, slots, hardware=file_config.hardware if file_config else None)
try:
    if mode[0] == "mock":
        asyncio.run(_run_mock(_mock_config(), deck))
    elif mode[0] == "remote":
        asyncio.run(
            _run(
                file_config,
                deck,
                switch_profile=switch_profile,
                tick_interval=_resolve_tick_interval(file_config),
            )
        )
    else:
        asyncio.run(
            _run_local(
                mode[1],
                deck,
                file_config,
                switch_profile=switch_profile,
                tick_interval=_resolve_tick_interval(file_config),
            )
        )
finally:
    deck.close()
```

- [ ] **Step 4: Add brightness injection to drivers**

In `src/herdeck/driver/elgato.py`, change:

```python
def __init__(self, device=None, icon_provider=None, brightness: int = BRIGHTNESS):
    self._brightness = brightness
    self._dev = device if device is not None else self._open_device()
    self._icons = icon_provider
    self._callback: Callable[[int], None] | None = None
    if device is not None:
        self._dev.set_brightness(brightness)


def _open_device(self):
    deck = DeviceManager().enumerate()[0]
    deck.open()
    deck.reset()
    deck.set_brightness(self._brightness)
    return deck
```

In `src/herdeck/driver/d200.py`, change:

```python
def __init__(
    self,
    workdir: str | None = None,
    icon_provider=None,
    brightness: int = BRIGHTNESS,
    debounce: float = DEBOUNCE,
    keep_alive_interval: float = KEEP_ALIVE_INTERVAL,
):
    self.DEBOUNCE = debounce
    self.KEEP_ALIVE_INTERVAL = keep_alive_interval
    # Keep the rest of the existing initialization flow unchanged.
    with contextlib.redirect_stdout(io.StringIO()):
        self._dev.set_brightness(brightness, force=True)
        self._set_panel_background_mode()
```

In app default factories, pass hardware values:

```python
return D200Driver(
    brightness=hardware.brightness,
    debounce=hardware.debounce,
    keep_alive_interval=hardware.keep_alive_interval,
)

return ElgatoDriver(brightness=hardware.brightness)
```

- [ ] **Step 5: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_local_mode.py::test_make_deck_uses_hardware_web_bind_and_port tests/test_local_mode.py::test_runtime_startup_settings_prefer_env_over_local tests/test_local_mode.py::test_runtime_startup_settings_use_local_when_env_absent tests/test_driver_elgato.py::test_elgato_brightness_can_be_configured -v
```

Expected: PASS.

- [ ] **Step 6: Run local/driver suites**

Run:

```bash
.venv/bin/python -m pytest tests/test_local_mode.py tests/test_driver_elgato.py tests/test_d200_panel.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/herdeck/app.py src/herdeck/driver/d200.py src/herdeck/driver/elgato.py tests/test_local_mode.py tests/test_driver_elgato.py
git commit -m "feat(runtime): apply local hardware settings"
sha=$(git rev-parse HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

---

### Task 11: New Example Config and Docs

**Files:**
- Modify: `config.example.toml`
- Modify: `README.md`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Add failing config example test**

Update `tests/test_config.py::test_example_start_profiles_match_defaults` to assert new schema values:

```python
def test_example_config_uses_profile_schema(monkeypatch):
    monkeypatch.setenv("HERDECK_WORKBOX_TOKEN", "secret123")
    path = Path(__file__).resolve().parents[1] / "config.example.toml"
    cfg = load_config(path)

    assert cfg.meta.active_profile in cfg.meta.profile_names
    assert set(DEFAULT_START_PROFILES) <= set(cfg.start_profiles)
    assert cfg.view.management in {"launcher_menu", "bottom_row"}
    assert "blocked" in cfg.theme.colors
```

Remove or replace the old `test_example_start_profiles_match_defaults` if it duplicates this coverage.

- [ ] **Step 2: Run test to verify it fails if example is still legacy**

Run:

```bash
.venv/bin/python -m pytest tests/test_config.py::test_example_config_uses_profile_schema -v
```

Expected: FAIL until `config.example.toml` is migrated.

- [ ] **Step 3: Rewrite `config.example.toml`**

Replace the file with a new profile-based example that includes:

```toml
active_profile = "work"

[[servers]]
id = "workbox"
url = "ws://100.x.y.z:8788"
token_env = "HERDECK_WORKBOX_TOKEN"

[profiles.base]
servers = ["workbox"]
theme = "default"
view = "dense"
notifications = "normal"
safety = "standard"
macros = "default"
launcher = "default"

[profiles.work]
extends = "base"

[profiles.mobile]
extends = "work"
view = "management"
notifications = "phone"

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

[views.management]
management = "bottom_row"
bottom_row = ["profiles", "notifications", "safety", "theme", "new_agent"]
show_profile_on_panel = true
agent_slots = "max"
tile_fields = ["repo", "branch", "status", "time", "server"]

[notification_profiles.normal]
enabled = false
backends = ["macos"]
on = ["blocked"]
sound = true

[notification_profiles.phone]
enabled = false
backends = ["telegram"]
on = ["blocked"]
sound = false

[notification_profiles.phone.telegram]
token_env = "HERDECK_TELEGRAM_TOKEN"
chat_id = "123456789"

[safety.standard]
approve_always = true
require_confirm_for = []

[[macro_sets.default]]
label = "continue"
text = "continue"
[[macro_sets.default]]
label = "run tests"
text = "run the tests"
[[macro_sets.default]]
label = "commit"
text = "commit the changes"
[[macro_sets.default]]
label = "/compact"
text = "/compact"

[launchers.default]
claude = ["claude"]
codex = ["codex"]
cursor = ["cursor-agent"]
gemini = ["gemini"]
opencode = ["opencode"]
```

Keep security comments: tokens are env vars, not literal tokens.

- [ ] **Step 4: Update README**

Add a `Profiles and customization` section:

```markdown
## Profiles and customization

Herdeck supports a shareable `config.toml` and a device-local `local.toml`.
The shareable file defines profiles and reusable blocks for theme, view,
launcher, macros, notifications, and safety. The local file stores the active
profile and device-specific settings such as deck type, socket path, web bind,
and hardware tuning.

Switch profiles from the deck through `+ New` -> `Profiles`, or set
`HERDECK_PROFILE=mobile` to lock a process to a profile.
```

Update old "Adding an agent type" to point to `[launchers.*]` and answer profiles/legacy compatibility.

- [ ] **Step 5: Run docs/config tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_config.py tests/test_settings.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add config.example.toml README.md tests/test_config.py
git commit -m "docs: document customization profile config"
sha=$(git rev-parse HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

---

### Task 12: Full Verification and Polish

**Files:**
- Any files touched by previous tasks, only for fixes found by verification.

- [ ] **Step 1: Run full tests**

Run:

```bash
.venv/bin/python -m pytest
```

Expected: `226+ passed` with no failures. The exact count may be higher after new tests.

- [ ] **Step 2: Run Ruff**

Run:

```bash
.venv/bin/ruff check src tests
```

Expected: PASS.

- [ ] **Step 3: Run import smoke**

Run:

```bash
.venv/bin/python - <<'PY'
from herdeck.config import load_config
from herdeck.settings import load_settings, resolve_profile
from herdeck.app import make_deck, resolve_mode
print("ok")
PY
```

Expected: prints `ok`.

- [ ] **Step 4: Run example config smoke**

Run:

```bash
HERDECK_WORKBOX_TOKEN=secret .venv/bin/python - <<'PY'
from herdeck.config import load_config
cfg = load_config("config.example.toml")
assert cfg.meta.active_profile in cfg.meta.profile_names
assert cfg.start_profiles["codex"] == ["codex"]
print(cfg.meta.active_profile)
PY
```

Expected: prints the active profile, e.g. `work`.

- [ ] **Step 5: Commit any verification fixes**

Only if changes were needed, commit the exact files reported by `git status --short`.
For example, if Ruff changes `src/herdeck/settings.py` and a test changes
`tests/test_settings.py`, run:

```bash
git add src/herdeck/settings.py tests/test_settings.py
git commit -m "fix: polish customization profiles"
sha=$(git rev-parse HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

- [ ] **Step 6: Final status**

Run:

```bash
git status --short
git log --oneline --decorate -12
```

Expected: clean worktree except intentional local files such as `.venv/` ignored by git.

---

## Self-Review Checklist

- Spec coverage:
  - Shareable config and `local.toml`: Tasks 2-4, 10-11.
  - Profiles and inheritance: Tasks 2-3.
  - Runtime switch: Tasks 6-8.
  - Management row: Task 6.
  - Theme/tile/icon customization: Task 5.
  - Notifications/safety/macros/launcher: Tasks 2, 6, 8-9.
  - App-ready settings service: Task 8.
  - Docs/example: Task 11.
- No placeholders: all tasks include exact files, commands, expected results, and code snippets.
- Type consistency:
  - Runtime config customization lives on `Config.theme`, `Config.view`, `Config.safety`, `Config.hardware`, `Config.meta`.
  - Settings layer returns `ResolvedSettings(config=Config, local_path=Path | None, startup_only_changed=False)`.
  - Orchestrator emits `Command("switch_profile", name, text=name)`.
  - App accepts `switch_profile: Callable[[str], Config | None] | None`.
