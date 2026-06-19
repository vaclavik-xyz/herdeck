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


# Quick-send macros shown when drilling into a non-blocked agent.
DEFAULT_MACROS: list[Macro] = [
    Macro("continue", "continue"),
    Macro("run tests", "run the tests"),
    Macro("commit", "commit the changes"),
    Macro("/compact", "/compact"),
]

# Agent types startable from the deck -> the argv herdr runs in a new pane.
DEFAULT_START_PROFILES: dict[str, list[str]] = {
    "claude": ["claude"],
    "codex": ["codex"],
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
    if not servers:
        raise ConfigError("no [[servers]] configured")

    deck = data.get("deck", {})
    grid = _parse_grid(deck.get("grid", "5x3"))
    overview_order = deck.get("overview_order", [s.id for s in servers])

    profiles = {
        name: _parse_profile(name, raw)
        for name, raw in data.get("answer_profiles", {}).items()
    }
    if "default" not in profiles:
        raise ConfigError("answer_profiles.default is required")

    raw_macros = data.get("macros", [])
    macros = ([Macro(label=m["label"], text=m["text"]) for m in raw_macros]
              if raw_macros else list(DEFAULT_MACROS))

    raw_starts = data.get("start_profiles", {})
    start_profiles = ({k: list(v) for k, v in raw_starts.items()}
                      if raw_starts else dict(DEFAULT_START_PROFILES))

    return Config(servers=servers, profiles=profiles,
                  overview_order=overview_order, grid=grid, macros=macros,
                  start_profiles=start_profiles)
