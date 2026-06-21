from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


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
    label: str          # short tile label
    text: str           # text sent to the agent (via herdr agent.send)


@dataclass
class Notifications:
    enabled: bool = False
    on: list[str] = field(default_factory=lambda: ["blocked"])
    sound: bool = True


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
        default_factory=lambda: dict(DEFAULT_START_PROFILES))
    notifications: Notifications = field(default_factory=Notifications)


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


def load_config(path: str | Path) -> Config:
    data = tomllib.loads(Path(path).read_text())

    servers = []
    for s in data.get("servers", []):
        env = s["token_env"]
        token = os.environ.get(env)
        if not token:
            raise ConfigError(f"env var '{env}' for server '{s['id']}' is not set")
        servers.append(ServerConfig(id=s["id"], url=s["url"], token=token))
    # Empty servers is allowed; the remote run path requires >=1 itself.

    deck = data.get("deck", {})
    grid = _parse_grid(deck.get("grid", "5x3"))
    overview_order = deck.get("overview_order", [s.id for s in servers])

    profiles = dict(DEFAULT_PROFILES)
    for name, raw in data.get("answer_profiles", {}).items():
        profiles[name] = _parse_profile(name, raw)

    # An explicit (even empty) section disables defaults; only a MISSING section
    # falls back to the built-ins.
    if "macros" in data:
        macros = [Macro(label=m["label"], text=m["text"]) for m in data["macros"]]
    else:
        macros = list(DEFAULT_MACROS)

    if "start_profiles" in data:
        start_profiles = {k: list(v) for k, v in data["start_profiles"].items()}
    else:
        start_profiles = dict(DEFAULT_START_PROFILES)

    n = data.get("notifications", {})
    notifications = Notifications(
        enabled=n.get("enabled", False),
        on=list(n.get("on", ["blocked"])),
        sound=n.get("sound", True),
    )

    return Config(servers=servers, profiles=profiles,
                  overview_order=overview_order, grid=grid, macros=macros,
                  start_profiles=start_profiles, notifications=notifications)
