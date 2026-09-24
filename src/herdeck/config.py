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
    backend: str = "herdr"
    # T3 only: read this machine's T3 desktop "visited" state so opening a
    # finished thread in the T3 desktop clears the deck's Done (t3_desktop_seen).
    # HERDECK_T3_DESKTOP_READ_STATE=1 still enables it for every T3 server.
    desktop_read_state: bool = False


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


# Agent states that can trigger a notification ([notifications] `on`), and the
# default macOS system sound played for each (override via [notifications.sounds]).
NOTIFY_EVENTS: tuple[str, ...] = ("blocked", "done")
DEFAULT_EVENT_SOUNDS: dict[str, str] = {"blocked": "Glass", "done": "Hero"}
# Events that alert when `on` is omitted. Both: the editor always offers a
# per-event sound, and a "done" sound that silently never fires reads as broken.
# An explicit `on = ["blocked"]` still opts out of done alerts.
DEFAULT_NOTIFY_ON: tuple[str, ...] = ("blocked", "done")


@dataclass
class TelegramConfig:
    token_env: str  # env var holding the bot token (never the token itself)
    chat_id: str  # target chat (not secret)
    message_thread_id: int | None = None  # optional Telegram forum topic id
    interactive: bool = False
    allowed_user_ids: list[int] = field(default_factory=list)
    prompt_max_chars: int = 1200
    # Minutes (0 = off, always send): send only while the deck host's user has
    # been idle this long and the deck was not pressed in that window.
    only_when_away: int = 0


@dataclass
class Notifications:
    enabled: bool = False
    on: list[str] = field(default_factory=lambda: list(DEFAULT_NOTIFY_ON))
    sound: bool = True
    backends: list[str] = field(default_factory=lambda: ["macos"])
    # Per-event sound name (a macOS system sound played with the alert); the
    # `sound` switch stays the master toggle. Defaults are in
    # DEFAULT_EVENT_SOUNDS; an explicit [notifications.sounds] table merges
    # over them, so one key can be overridden without retyping the rest.
    sounds: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_EVENT_SOUNDS))
    telegram: TelegramConfig | None = None
    # Opt-in: blocked banners get Approve/Deny buttons (a plain permission
    # prompt) or an inline reply field (anything else), answered through the
    # runtime (desktop app on macOS only).
    banner_actions: bool = False
    # Opt-in: blocked alerts append a short, sanitized excerpt of the prompt.
    banner_prompt: bool = False
    # No alert for the pane herdr reports focused at the transition while the
    # user is at the deck host (you are looking at it). Noise-only: default on.
    skip_focused: bool = True
    # Minutes (0 = off): an agent still blocked this long after its episode
    # began alerts again, once per interval, at most REMIND_MAX times.
    remind_after: int = 0


DEFAULT_STATUS_COLORS: dict[str, str] = {
    "working": "green",
    "idle": "blue",
    "blocked": "amber",
    "done": "cyan",
    "waiting": "violet",  # pane held pending background work (herdwatch)
    "unknown": "grey",
    "offline": "red",
}

DEFAULT_SERVER_ACCENTS: list[str] = ["teal", "violet", "orange", "pink", "lime"]
DEFAULT_TILE_FIELDS: list[str] = ["repo", "title", "tab", "branch", "status", "time", "server"]
TILE_LINE_TOKENS: tuple[str, ...] = (
    "project",
    "repo",
    "title",
    "branch",
    "workspace",
    "tab",
    "agent",
    "source",
    "work_item",
    "run",
)
WORKING_ANIMATIONS: tuple[str, ...] = ("spin", "comet", "pulse", "sweep", "none")
AGENT_ORDERS: tuple[str, ...] = ("status", "herdr")
# How an agent tile is filled with its status colour:
#   none  = dark tile, colour only in the status word + bottom accent bar (default)
#   tint  = whole tile a darkened shade of the status colour + a bright bottom edge
#   solid = whole tile the full status colour (text contrast flips on bright colours)
TILE_FILLS: tuple[str, ...] = ("none", "tint", "solid")
# What an agent tile's logo box shows:
#   agent   = the agent's mark (default)
#   project = the project's favicon instead of the mark (a monogram when none)
#   both    = the agent's mark plus a small project badge on its corner
TILE_ICONS: tuple[str, ...] = ("agent", "project", "both")
DEFAULT_BOTTOM_ROW: list[str] = ["profiles", "notifications", "safety", "theme", "new_agent"]


@dataclass
class ThemeConfig:
    colors: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_STATUS_COLORS))
    server_accents: list[str] = field(default_factory=lambda: list(DEFAULT_SERVER_ACCENTS))


@dataclass
class ViewConfig:
    management: str = "launcher_menu"
    agent_order: str = "status"
    bottom_row: list[str] = field(default_factory=lambda: list(DEFAULT_BOTTOM_ROW))
    show_profile_on_panel: bool = False
    agent_slots: str = "max"
    tile_fields: list[str] = field(default_factory=lambda: list(DEFAULT_TILE_FIELDS))
    # None = key absent (each render path supplies its own fallback);
    # [] = explicitly empty (that text line is off). A non-empty list is a
    # token list rendered by layout.compose_line.
    tile_primary: list[str] | None = None
    tile_secondary: list[str] | None = None
    working_animation: str = "spin"
    tile_fill: str = "none"
    tile_icon: str = "agent"
    # Repo name -> icon file on the RUNTIME machine ("~" is expanded when the
    # file is read); overrides the favicon the bridge discovers in the repo.
    project_icons: dict[str, str] = field(default_factory=dict)
    # Language of RENDERED deck text (tiles, panel, websim) and the desktop UI.
    language: str = "en"
    # Fold IDLE agents into one "+N idle" overview tile (press = unfold). The
    # Elgato plugin keeps sticky per-key slots and ignores it.
    collapse_idle: bool = False


# Actions that need a second confirming press by default. Stop (act_force) is an
# unconditional interrupt sitting one key away from Back on the drill view, so a
# single mis-aimed press must not kill an agent's work; the Elgato plugin's Stop
# has always been two-step. Set `require_confirm_for = []` to opt out.
DEFAULT_REQUIRE_CONFIRM: tuple[str, ...] = ("act_force",)


@dataclass
class SafetyConfig:
    approve_always: bool = True
    require_confirm_for: list[str] = field(default_factory=lambda: list(DEFAULT_REQUIRE_CONFIRM))


@dataclass
class UsageConfig:
    # Provider ids to poll; empty keeps the usage panel off. This is also the
    # explicit per-provider allow-list used by the settings UI.
    providers: list[str] = field(default_factory=list)
    # Hide providers unless their native source confirms a paid subscription.
    # Disabled by default so existing CodexBar-only configurations keep working.
    paid_only: bool = False
    refresh_secs: int = 300
    codex_path: str = "codex"
    claude_cache_path: str = "~/.cache/herdeck/claude-usage.json"
    # Compatibility fallback for missing native providers. Empty disables it.
    codexbar_path: str = "codexbar"
    # Opt-in usage notifications (usage_alerts.py), delivered through the
    # [notifications] backends. `alert_at` = used-% thresholds (1-100, stored
    # sorted + deduped); each fires once per window period. `alert_reset`
    # announces the reset of a window that reached max(alert_at) (100 when
    # alert_at is empty).
    alert_at: list[int] = field(default_factory=list)
    alert_reset: bool = False


@dataclass
class HardwareConfig:
    deck: str | None = None
    herdr_socket: str | None = None
    web_bind: str | None = None
    web_port: int | None = None
    icons_dir: str | None = None
    # App to bring forward after a tile press focused a pane ("" = off). Only
    # useful when the herdr client runs on the deck machine (see terminal_app.py).
    terminal_app: str = ""
    brightness: int = 80
    debounce: float = 0.25
    keep_alive_interval: float = 5.0
    tick_interval: float = 0.4
    # Use strmdck's disk-backed ZIP writer for the D200 (a firmware
    # compatibility path, see docs/agent-setup.md). HERDECK_D200_STANDARD_WRITER=1
    # still enables it when this is false.
    d200_standard_writer: bool = False
    # USB power-cycle of the D200's hub port (deckapp/maintenance.py). uhubctl =
    # path to the tool ("" = look on PATH and the Homebrew prefixes); usb_hub +
    # usb_port pin the location, otherwise the last location the D200 was seen
    # at is used.
    uhubctl: str = ""
    usb_hub: str = ""
    usb_port: int | None = None


@dataclass
class ConfigMeta:
    active_profile: str = "default"
    profile_names: list[str] = field(default_factory=lambda: ["default"])
    env_locked_profile: bool = False


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
    usage: UsageConfig = field(default_factory=UsageConfig)
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


def _parse_telegram_bool(name: str, value: bool) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"notifications.telegram.{name} must be a boolean")
    return value


def _parse_telegram_int(name: str, value) -> int:
    if type(value) is not int:
        raise ConfigError(f"notifications.telegram.{name} must be an integer")
    return value


def _parse_telegram_optional_int(name: str, value) -> int | None:
    # TOML has no null. Zero is the persisted sentinel for an explicit
    # profile override that disables an inherited forum topic.
    if value is None or (type(value) is int and value == 0):
        return None
    return _parse_telegram_int(name, value)


def _parse_telegram_int_list(name: str, value) -> list[int]:
    if not isinstance(value, list):
        raise ConfigError(f"notifications.telegram.{name} must be a list")
    parsed = []
    for item in value:
        try:
            parsed.append(_parse_telegram_int(name, item))
        except ConfigError as exc:
            raise ConfigError(f"notifications.telegram.{name} must contain integers") from exc
    return parsed


def _parse_telegram_config(tg_raw: dict) -> TelegramConfig | None:
    if "token_env" not in tg_raw or "chat_id" not in tg_raw:
        log.warning(
            "[notifications.telegram] needs both token_env and chat_id; ignoring telegram config"
        )
        return None
    thread = tg_raw.get("message_thread_id")
    return TelegramConfig(
        token_env=tg_raw["token_env"],
        chat_id=str(tg_raw["chat_id"]),
        message_thread_id=_parse_telegram_optional_int("message_thread_id", thread),
        interactive=_parse_telegram_bool("interactive", tg_raw.get("interactive", False)),
        allowed_user_ids=_parse_telegram_int_list(
            "allowed_user_ids", tg_raw.get("allowed_user_ids", [])
        ),
        prompt_max_chars=_parse_telegram_int(
            "prompt_max_chars", tg_raw.get("prompt_max_chars", 1200)
        ),
        only_when_away=notification_minutes(
            tg_raw, "only_when_away", section="notifications.telegram"
        ),
    )


def validate_event_sounds(raw) -> dict[str, str]:
    """Validate an explicit [notifications.sounds] table (event → sound name).

    Returns only the explicitly configured entries; callers merge them over
    DEFAULT_EVENT_SOUNDS. Shared by the TOML loader and the GUI config service
    so both reject the same malformed payloads.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("notifications.sounds must be a table of event = sound name")
    unknown = sorted(k for k in raw if k not in NOTIFY_EVENTS)
    if unknown:
        raise ConfigError(
            f"notifications.sounds has unknown event(s) {unknown}; want {list(NOTIFY_EVENTS)}"
        )
    bad = [k for k, v in raw.items() if not isinstance(v, str) or not v.strip()]
    if bad:
        raise ConfigError(
            f"notifications.sounds.{bad[0]} must be a non-empty sound name"
            " (a macOS system sound, e.g. Glass, Hero, Basso)"
        )
    return dict(raw)


def notification_flag(raw: dict, name: str, default: bool) -> bool:
    """A boolean [notifications] option (shared by both config loaders)."""
    value = raw.get(name, default)
    if type(value) is not bool:
        raise ConfigError(f"notifications.{name} must be true or false")
    return value


def notification_minutes(raw: dict, name: str, section: str = "notifications") -> int:
    """A whole-minutes option, 0 = off (shared by both config loaders)."""
    value = raw.get(name, 0)
    if type(value) is not int or not 0 <= value <= 1440:
        raise ConfigError(f"{section}.{name} must be whole minutes 0-1440 (0 = off)")
    return value


def normalize_notify_on(raw) -> list[str]:
    """`[notifications].on` with surrounding whitespace stripped from each event,
    so a hand-written " done" still fires (the app matches with `in`)."""
    return [e.strip() if isinstance(e, str) else e for e in raw]


def parse_notifications(n: dict) -> Notifications:
    tg_raw = n.get("telegram")
    telegram = _parse_telegram_config(tg_raw) if isinstance(tg_raw, dict) else None
    on = normalize_notify_on(n.get("on", DEFAULT_NOTIFY_ON))
    unknown_events = [e for e in on if e not in NOTIFY_EVENTS]
    if unknown_events:
        log.warning(
            "unknown [notifications] on event(s) %s; supported: %s",
            unknown_events,
            list(NOTIFY_EVENTS),
        )
    return Notifications(
        enabled=n.get("enabled", False),
        on=on,
        sound=n.get("sound", True),
        backends=list(n.get("backends", ["macos"])),
        sounds={**DEFAULT_EVENT_SOUNDS, **validate_event_sounds(n.get("sounds"))},
        telegram=telegram,
        banner_actions=notification_flag(n, "banner_actions", False),
        banner_prompt=notification_flag(n, "banner_prompt", False),
        skip_focused=notification_flag(n, "skip_focused", True),
        remind_after=notification_minutes(n, "remind_after"),
    )


def load_config(path: str | Path) -> Config:
    from .bootstrap import _discover_local_config_path
    from .settings import load_settings, resolve_profile

    return resolve_profile(load_settings(path, _discover_local_config_path(str(path)))).config
