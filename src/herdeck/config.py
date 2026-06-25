from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("herdeck.config")


class ConfigError(Exception):
    pass


@dataclass
class ServerConfig:
    id: str
    url: str
    token: str


@dataclass
class AnswerProfile:
    approve: list[str]
    deny: list[str]
    stop: list[str]
    approve_always: list[str]


@dataclass
class Macro:
    label: str  # short tile label
    text: str  # text sent to the agent (via herdr agent.send)


@dataclass
class TelegramConfig:
    token_env: str  # env var holding the bot token (never the token itself)
    chat_id: str  # target chat (not secret)


@dataclass
class Notifications:
    enabled: bool = False
    on: list[str] = field(default_factory=lambda: ["blocked"])
    sound: bool = True
    backends: list[str] = field(default_factory=lambda: ["macos"])
    telegram: TelegramConfig | None = None


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
TILE_LINE_TOKENS: tuple[str, ...] = ("repo", "branch", "workspace", "tab", "agent")
DEFAULT_BOTTOM_ROW: list[str] = ["profiles", "notifications", "safety", "theme", "new_agent"]


@dataclass
class ThemeConfig:
    colors: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_STATUS_COLORS))
    server_accents: list[str] = field(default_factory=lambda: list(DEFAULT_SERVER_ACCENTS))


@dataclass
class ViewConfig:
    management: str = "launcher_menu"
    bottom_row: list[str] = field(default_factory=lambda: list(DEFAULT_BOTTOM_ROW))
    show_profile_on_panel: bool = False
    agent_slots: str = "max"
    tile_fields: list[str] = field(default_factory=lambda: list(DEFAULT_TILE_FIELDS))
    # None = key absent (each render path supplies its own fallback);
    # [] = explicitly empty (that text line is off). A non-empty list is a
    # token list rendered by layout.compose_line.
    tile_primary: list[str] | None = None
    tile_secondary: list[str] | None = None


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


# Quick-send macros shown when drilling into a non-blocked agent.
DEFAULT_MACROS: list[Macro] = [
    Macro("continue", "continue"),
    Macro("run tests", "run the tests"),
    Macro("commit", "commit the changes"),
    Macro("/compact", "/compact"),
]

# Agent types startable from the deck -> the argv herdr runs in a new pane.
# Override these commands in config [start_profiles] for local CLI variants.
DEFAULT_START_PROFILES: dict[str, list[str]] = {
    "claude": ["claude"],
    "codex": ["codex"],
    "cursor": ["cursor-agent"],
    "gemini": ["gemini"],
    "opencode": ["opencode"],
}

# Built-in answer profiles used when a config omits them (and by local mode).
DEFAULT_PROFILES: dict[str, AnswerProfile] = {
    "claude": AnswerProfile(["1", "enter"], ["esc"], ["ctrl+c"], ["2", "enter"]),
    "codex": AnswerProfile(["y", "enter"], ["n", "enter"], ["ctrl+c"], ["y", "enter"]),
    "default": AnswerProfile(["enter"], ["esc"], ["ctrl+c"], ["enter"]),
}


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


def _parse_grid(value: str) -> tuple[int, int]:
    try:
        cols, rows = value.lower().split("x")
        return int(cols), int(rows)
    except (ValueError, AttributeError) as exc:
        raise ConfigError(f"invalid grid '{value}', expected e.g. '5x3'") from exc


def _parse_profile(name: str, raw: dict) -> AnswerProfile:
    for key in ("approve", "deny", "stop"):
        if key not in raw:
            raise ConfigError(f"profile '{name}' missing '{key}'")
    return AnswerProfile(
        approve=raw["approve"],
        deny=raw["deny"],
        stop=raw["stop"],
        approve_always=raw.get("approve_always", raw["approve"]),
    )


def parse_notifications(n: dict) -> Notifications:
    tg_raw = n.get("telegram")
    telegram = None
    if isinstance(tg_raw, dict):
        if "token_env" in tg_raw and "chat_id" in tg_raw:
            telegram = TelegramConfig(token_env=tg_raw["token_env"], chat_id=str(tg_raw["chat_id"]))
        else:
            log.warning(
                "[notifications.telegram] needs both token_env and "
                "chat_id; ignoring telegram config"
            )
    return Notifications(
        enabled=n.get("enabled", False),
        on=list(n.get("on", ["blocked"])),
        sound=n.get("sound", True),
        backends=list(n.get("backends", ["macos"])),
        telegram=telegram,
    )


def load_config(path: str | Path) -> Config:
    from .bootstrap import _discover_local_config_path
    from .settings import load_settings, resolve_profile

    return resolve_profile(load_settings(path, _discover_local_config_path(str(path)))).config
