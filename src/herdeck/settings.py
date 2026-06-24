from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .config import (
    DEFAULT_MACROS,
    DEFAULT_PROFILES,
    DEFAULT_START_PROFILES,
    TILE_LINE_TOKENS,
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
    _parse_grid,
    _parse_profile,
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
    locked = snapshot.env_profile is not None
    active = _active_profile_name(snapshot)
    names = ["default"] + sorted(snapshot.data.get("profiles", {}))
    return [{"name": n, "active": n == active, "locked": locked} for n in names]


def resolve_profile(snapshot: SettingsSnapshot, name: str | None = None) -> ResolvedSettings:
    active = name or _active_profile_name(snapshot)
    merged, selection = _merged_sections(snapshot.data, active)
    config = _build_config(
        snapshot.data,
        merged,
        selection,
        snapshot.local_data,
        profile_name=active,
        env_profile=snapshot.env_profile,
    )
    return ResolvedSettings(config=config, local_path=snapshot.local_path)


def set_active_profile(snapshot: SettingsSnapshot, name: str, *, persist: bool = True) -> bool:
    if name != "default" and name not in snapshot.data.get("profiles", {}):
        raise ConfigError(f"unknown profile '{name}'")
    if snapshot.env_profile is not None:
        return False
    resolve_profile(snapshot, name)  # validate it builds (incl. the base for "default")
    if not persist:
        return True
    local_path = snapshot.local_path
    if local_path is None:
        return False
    local_path.parent.mkdir(parents=True, exist_ok=True)
    existing = snapshot.local_data
    lines = [f"active_profile = {_toml_value(name)}"]
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


def validate_settings(snapshot: SettingsSnapshot) -> list[str]:
    errors: list[str] = []
    if "default" in snapshot.data.get("profiles", {}):
        errors.append("profile 'default' is reserved (it is the base config)")
    try:
        resolve_profile(snapshot)
    except ConfigError as exc:
        errors.append(f"active: {exc}")
    for name in sorted(snapshot.data.get("profiles", {})):
        if name == "default":
            continue
        try:
            resolve_profile(snapshot, name)
        except ConfigError as exc:
            errors.append(f"{name}: {exc}")
    return errors


def _toml_line(key: str, value) -> str:
    if isinstance(value, bool):
        return f"{key} = {'true' if value else 'false'}"
    if isinstance(value, int | float):
        return f"{key} = {value}"
    if isinstance(value, list):
        rendered = ", ".join(_toml_value(item) for item in value)
        return f"{key} = [{rendered}]"
    return f"{key} = {_toml_value(value)}"


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(str(value))


def _active_profile_name(snapshot: SettingsSnapshot) -> str:
    return (
        snapshot.env_profile
        or snapshot.local_data.get("active_profile")
        or snapshot.data.get("active_profile")
        or "default"
    )


def _profile_overlays(profiles: dict, name: str) -> list[dict]:
    """Overlay dicts from the base-most parent down to `name` (inclusive)."""
    chain: list[str] = []
    seen: set[str] = set()
    cur: str | None = name
    while cur and cur != "default":
        if cur in seen:
            raise ConfigError("profile inheritance cycle: " + " -> ".join(chain + [cur]))
        if cur not in profiles:
            raise ConfigError(f"unknown profile '{cur}'")
        seen.add(cur)
        chain.append(cur)
        cur = profiles[cur].get("extends")
    return [profiles[n] for n in reversed(chain)]


def _server_config(raw: dict) -> ServerConfig:
    env = raw["token_env"]
    token = os.environ.get(env)
    if not token:
        raise ConfigError(f"env var '{env}' for server '{raw['id']}' is not set")
    return ServerConfig(raw["id"], raw["url"], token)


def _theme_config(raw: dict | None) -> ThemeConfig:
    raw = raw or {}
    theme = ThemeConfig()
    colors = raw.get("colors", {})
    if colors:
        theme.colors.update({k: str(v) for k, v in colors.items()})
    if "server_accents" in raw:
        theme.server_accents = list(raw["server_accents"])
    return theme


def _view_config(raw: dict | None) -> ViewConfig:
    raw = raw or {}
    view = ViewConfig()
    for key in ("management", "agent_slots"):
        if key in raw:
            setattr(view, key, raw[key])
    if "bottom_row" in raw:
        view.bottom_row = list(raw["bottom_row"])
    if "tile_fields" in raw:
        view.tile_fields = list(raw["tile_fields"])
    for key in ("tile_primary", "tile_secondary"):
        if key in raw:
            tokens = list(raw[key])
            for tok in tokens:
                if tok not in TILE_LINE_TOKENS:
                    raise ConfigError(f"unknown tile token '{tok}' in view.{key}")
            setattr(view, key, tokens)
    if "show_profile_on_panel" in raw:
        view.show_profile_on_panel = bool(raw["show_profile_on_panel"])
    return view


def _notifications_config(raw: dict | None) -> Notifications:
    raw = raw or {}
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


def _safety_config(raw: dict | None) -> SafetyConfig:
    raw = raw or {}
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


_OVERLAY_SECTIONS = (
    "deck",
    "answer_profiles",
    "macros",
    "start_profiles",
    "notifications",
    "theme",
    "view",
    "safety",
)


def _merged_sections(data: dict, profile_name: str | None) -> tuple[dict, list[str] | None]:
    merged = {sec: data.get(sec) for sec in _OVERLAY_SECTIONS}
    selection: list[str] | None = None
    if profile_name and profile_name != "default":
        for overlay in _profile_overlays(data.get("profiles", {}), profile_name):
            for sec in _OVERLAY_SECTIONS:
                if sec in overlay:
                    merged[sec] = _merge_section(merged.get(sec), overlay[sec])
            if "servers" in overlay:
                selection = list(overlay["servers"])
    return merged, selection


def _merge_section(base, overlay):
    """Overlay a config section onto a base: tables merge field-by-field
    (recursively), scalars and lists replace wholesale."""
    if isinstance(base, dict) and isinstance(overlay, dict):
        out = dict(base)
        for key, value in overlay.items():
            out[key] = _merge_section(out.get(key), value)
        return out
    return overlay


def _build_config(
    data: dict,
    merged: dict,
    selection: list[str] | None,
    local_data: dict,
    *,
    profile_name: str,
    env_profile: str | None,
) -> Config:
    servers_by_id = {s["id"]: s for s in data.get("servers", [])}
    if selection is None:
        deck_sel = merged.get("deck") or {}
        if "overview_order" in deck_sel:
            selection = list(deck_sel["overview_order"])
        else:
            selection = list(servers_by_id)
    servers = []
    for sid in selection:
        if sid not in servers_by_id:
            raise ConfigError(f"unknown server '{sid}'")
        servers.append(_server_config(servers_by_id[sid]))

    deck = merged.get("deck") or {}
    grid = _parse_grid(deck.get("grid", "5x3"))

    answer_profiles = dict(DEFAULT_PROFILES)
    for name, raw in (merged.get("answer_profiles") or {}).items():
        answer_profiles[name] = _parse_profile(name, raw)

    return Config(
        servers=servers,
        profiles=answer_profiles,
        overview_order=selection,
        grid=grid,
        macros=_macro_set(merged.get("macros")),
        start_profiles=_launcher(merged.get("start_profiles")),
        notifications=_notifications_config(merged.get("notifications")),
        theme=_theme_config(merged.get("theme")),
        view=_view_config(merged.get("view")),
        safety=_safety_config(merged.get("safety")),
        hardware=_hardware_config(local_data),
        meta=ConfigMeta(
            active_profile=profile_name,
            profile_names=["default"] + sorted(data.get("profiles", {})),
            env_locked_profile=env_profile is not None,
        ),
    )
