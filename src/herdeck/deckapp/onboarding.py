# src/herdeck/deckapp/onboarding.py
"""Persisted first-run onboarding choice. A tiny marker file next to the config:
`<config_dir>/onboarding.toml` with `choice = "local" | "demo"`. Absent = the user
has never onboarded. Remote is NOT recorded here — it is implied by a usable
config.toml; a successful remote connect clears this marker."""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

import tomli_w

_VALID = ("local", "demo")


def state_path(config_path: str | None) -> Path:
    base = (
        Path(config_path).expanduser().parent
        if config_path
        else Path(os.path.expanduser("~/.config/herdeck"))
    )
    return base / "onboarding.toml"


def read_choice(config_path) -> str | None:
    path = state_path(config_path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    choice = data.get("choice")
    return choice if choice in _VALID else None


def write_choice(config_path, choice: str) -> None:
    if choice not in _VALID:
        raise ValueError(f"invalid onboarding choice {choice!r}; want one of {_VALID}")
    path = state_path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(tomli_w.dumps({"choice": choice}), encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def clear_choice(config_path) -> None:
    state_path(config_path).unlink(missing_ok=True)
