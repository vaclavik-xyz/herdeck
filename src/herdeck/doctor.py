from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def check_socket(path: str, exists: Callable[[str], bool], probe) -> Check:
    """probe(path) -> herdr pane.list response dict, or raises on failure."""
    if not exists(path):
        return Check("herdr socket", False, f"not found at {path} (is herdr running?)")
    try:
        resp = probe(path)
    except Exception as exc:
        return Check("herdr socket", False, f"socket did not respond ({exc})")
    if not isinstance(resp, dict):
        return Check("herdr socket", False, "malformed response (not a dict)")
    result = resp.get("result")
    if not isinstance(result, dict):
        return Check("herdr socket", False, "malformed response (result is not a dict)")
    panes = result.get("panes")
    if panes is None:
        return Check("herdr socket", False, "malformed response (no panes)")
    if not isinstance(panes, list):
        return Check("herdr socket", False, "malformed response (panes is not a list)")
    return Check("herdr socket", True, f"responding, {len(panes)} panes")


def check_config(
    config_path: str | None,
    has_servers: bool,
    socket_exists: bool,
    token_envs=(),
    getenv=os.environ.get,
) -> Check:
    if has_servers:
        statuses = [
            f"{env}=present" if getenv(env) else f"{env}=missing"
            for env in token_envs
        ]
        missing = [env for env in token_envs if not getenv(env)]
        detail = f"config at {config_path}; token envs: {', '.join(statuses)}"
        return Check("configuration", not missing, detail)
    if socket_exists:
        source = "no config" if config_path is None else f"config at {config_path}"
        return Check("configuration", True, f"{source}; local zero-config mode")
    if config_path is None:
        return Check(
            "configuration",
            False,
            "no config and no herdr socket (start herdr or create config.toml)",
        )
    return Check(
        "configuration",
        False,
        f"config at {config_path} has no servers and no herdr socket is available",
    )


def check_optional_deps(is_available: Callable[[str], bool]) -> Check:
    modules = (
        ("PIL", "PIL"),
        ("cairosvg", "cairosvg"),
        ("strmdck", "strmdck"),
        ("streamdeck", "StreamDeck"),
    )
    statuses = [
        f"{label}=present" if is_available(import_name) else f"{label}=missing"
        for label, import_name in modules
    ]
    missing = [label for label, import_name in modules if not is_available(import_name)]
    detail = "; ".join(statuses)
    if missing:
        detail += '; optional hints: pip install ".[deck]" or ".[elgato]"'
    return Check("optional dependencies", True, detail)


def check_deck(lib_available: Callable[[str], bool]) -> Check:
    d200_ready = lib_available("strmdck") and lib_available("hid")
    elgato_ready = lib_available("StreamDeck")
    note = "device presence is not probed; Ulanzi Studio can hold the device"
    if d200_ready or elgato_ready:
        drivers = []
        if d200_ready:
            drivers.append("D200")
        if elgato_ready:
            drivers.append("Elgato")
        return Check("deck drivers", True, f"importable: {', '.join(drivers)}; {note}")
    return Check(
        "deck drivers",
        False,
        'no deck driver libraries importable; pip install ".[deck]" or ".[elgato]"; '
        f"{note}",
    )
