"""Bridge-owned shared settings: the subset of config that follows the bridge.

Pure module (no IO). ``parse_shared`` is the strict validator used on every
write path (bridge store, editor route); ``to_raw`` is its inverse. Existing
config validators are reused so there is one validation code path.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from herdeck.config import (
    DEFAULT_MACROS,
    DEFAULT_NOTIFY_ON,
    DEFAULT_PROFILES,
    DEFAULT_REQUIRE_CONFIRM,
    DEFAULT_START_PROFILES,
    NOTIFY_EVENTS,
    AnswerProfile,
    Config,
    ConfigError,
    Macro,
    SafetyConfig,
    _parse_profile,
    normalize_notify_on,
    notification_flag,
    notification_minutes,
)

SHARED_NOTIFICATION_KEYS = ("on", "done_min_work", "done_short_delay", "remind_after",
                            "subagents_done")
SHARED_USAGE_KEYS = ("alert_at", "alert_reset")
SHARED_WHOLE_SECTIONS = ("answer_profiles", "safety", "macros", "start_profiles")
_SECTIONS = ("notifications", "usage", *SHARED_WHOLE_SECTIONS)
MAX_SHARED_BYTES = 64 * 1024

_PROFILE_KEYS = ("approve", "deny", "stop", "approve_always")


@dataclass
class SharedSettings:
    on: list[str]
    done_min_work: int
    done_short_delay: int
    remind_after: int
    subagents_done: bool
    answer_profiles: dict[str, AnswerProfile]
    safety: SafetyConfig
    macros: list[Macro]
    start_profiles: dict[str, list[str]]
    alert_at: list[int]
    alert_reset: bool


def _table(raw, name: str) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{name} must be a table")
    return raw


def _reject_unknown(raw: dict, allowed: tuple[str, ...], name: str) -> None:
    unknown = sorted(str(k) for k in raw if k not in allowed)
    if unknown:
        raise ConfigError(f"{name}: unknown or non-shared key(s) {unknown}")


def _str_list(value, name: str, *, non_empty_items: bool = True) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ConfigError(f"{name} must be a list of strings")
    if non_empty_items and any(not v for v in value):
        raise ConfigError(f"{name} must not contain empty strings")
    return list(value)


def _parse_on(raw: dict) -> list[str]:
    value = raw.get("on", list(DEFAULT_NOTIFY_ON))
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ConfigError("notifications.on must be a list of event names")
    on = normalize_notify_on(value)
    bad = [e for e in on if e not in NOTIFY_EVENTS]
    if bad:
        raise ConfigError(f"notifications.on: unknown event(s) {bad}; supported: {list(NOTIFY_EVENTS)}")
    return on


def _parse_profiles(raw) -> dict[str, AnswerProfile]:
    profiles = dict(DEFAULT_PROFILES)
    for name, p in _table(raw, "answer_profiles").items():
        if not isinstance(name, str) or not name:
            raise ConfigError("answer_profiles: profile names must be non-empty strings")
        p = _table(p, f"answer_profiles.{name}")
        _reject_unknown(p, _PROFILE_KEYS, f"answer_profiles.{name}")
        for key in _PROFILE_KEYS:
            if key in p:
                _str_list(p[key], f"answer_profiles.{name}.{key}", non_empty_items=False)
        profiles[name] = _parse_profile(name, p)
    return profiles


def _parse_safety(raw) -> SafetyConfig:
    raw = _table(raw, "safety")
    _reject_unknown(raw, ("approve_always", "require_confirm_for"), "safety")
    approve_always = raw.get("approve_always", True)
    if type(approve_always) is not bool:
        raise ConfigError("safety.approve_always must be true or false")
    confirm = _str_list(
        raw.get("require_confirm_for", list(DEFAULT_REQUIRE_CONFIRM)),
        "safety.require_confirm_for",
    )
    return SafetyConfig(approve_always=approve_always, require_confirm_for=confirm)


def _parse_macros(raw) -> list[Macro]:
    if raw is None:
        return list(DEFAULT_MACROS)
    if not isinstance(raw, list):
        raise ConfigError("macros must be a list of tables")
    out = []
    for i, m in enumerate(raw):
        if not isinstance(m, dict):
            raise ConfigError(f"macros[{i}] must be a table")
        _reject_unknown(m, ("label", "text"), f"macros[{i}]")
        label, text = m.get("label"), m.get("text")
        if not isinstance(label, str) or not label:
            raise ConfigError(f"macros[{i}].label must be a non-empty string")
        if not isinstance(text, str):
            raise ConfigError(f"macros[{i}].text must be a string")
        out.append(Macro(label=label, text=text))
    return out


def _parse_start_profiles(raw) -> dict[str, list[str]]:
    if raw is None:
        return dict(DEFAULT_START_PROFILES)
    if not isinstance(raw, dict):
        raise ConfigError("start_profiles must be a table")
    out = {}
    for name, argv in raw.items():
        if not isinstance(name, str) or not name:
            raise ConfigError("start_profiles: names must be non-empty strings")
        argv = _str_list(argv, f"start_profiles.{name}")
        if not argv:
            raise ConfigError(f"start_profiles.{name} must not be empty")
        out[name] = argv
    return out


def _parse_usage(raw) -> tuple[list[int], bool]:
    raw = _table(raw, "usage")
    _reject_unknown(raw, SHARED_USAGE_KEYS, "usage")
    alert_at = raw.get("alert_at", [])
    if (
        not isinstance(alert_at, list)
        or any(type(v) is not int or not 1 <= v <= 100 for v in alert_at)
    ):
        raise ConfigError("usage.alert_at must be a list of whole percentages 1-100")
    alert_reset = raw.get("alert_reset", False)
    if type(alert_reset) is not bool:
        raise ConfigError("usage.alert_reset must be true or false")
    return sorted(set(alert_at)), alert_reset


def parse_shared(raw: dict) -> SharedSettings:
    """Strictly validate a shared-settings document; raises ConfigError."""
    if not isinstance(raw, dict):
        raise ConfigError("settings must be a table")
    _reject_unknown(raw, _SECTIONS, "settings")
    n = _table(raw.get("notifications"), "notifications")
    _reject_unknown(n, SHARED_NOTIFICATION_KEYS, "notifications")
    alert_at, alert_reset = _parse_usage(raw.get("usage"))
    return SharedSettings(
        on=_parse_on(n),
        done_min_work=notification_minutes(n, "done_min_work"),
        done_short_delay=notification_minutes(n, "done_short_delay"),
        remind_after=notification_minutes(n, "remind_after"),
        subagents_done=notification_flag(n, "subagents_done", False),
        answer_profiles=_parse_profiles(raw.get("answer_profiles")),
        safety=_parse_safety(raw.get("safety")),
        macros=_parse_macros(raw.get("macros")),
        start_profiles=_parse_start_profiles(raw.get("start_profiles")),
        alert_at=alert_at,
        alert_reset=alert_reset,
    )


def to_raw(s: SharedSettings) -> dict:
    """TOML/JSON-able document in the shape ``parse_shared`` accepts."""
    return {
        "notifications": {
            "on": list(s.on),
            "done_min_work": s.done_min_work,
            "done_short_delay": s.done_short_delay,
            "remind_after": s.remind_after,
            "subagents_done": s.subagents_done,
        },
        "answer_profiles": {
            name: {
                "approve": list(p.approve),
                "deny": list(p.deny),
                "stop": list(p.stop),
                "approve_always": list(p.approve_always),
            }
            for name, p in s.answer_profiles.items()
        },
        "safety": {
            "approve_always": s.safety.approve_always,
            "require_confirm_for": list(s.safety.require_confirm_for),
        },
        "macros": [{"label": m.label, "text": m.text} for m in s.macros],
        "start_profiles": {k: list(v) for k, v in s.start_profiles.items()},
        "usage": {"alert_at": list(s.alert_at), "alert_reset": s.alert_reset},
    }


def extract_shared(base: dict) -> dict:
    """The shared part of a config.toml base table (only keys present)."""
    out: dict = {}
    for section, keys in (("notifications", SHARED_NOTIFICATION_KEYS),
                          ("usage", SHARED_USAGE_KEYS)):
        table = base.get(section)
        if isinstance(table, dict):
            picked = {k: table[k] for k in keys if k in table}
            if picked:
                out[section] = picked
    for section in SHARED_WHOLE_SECTIONS:
        if section in base:
            out[section] = base[section]
    return out


def apply_shared(config: Config, s: SharedSettings) -> Config:
    """A copy of ``config`` with the shared fields replaced (input untouched)."""
    return dataclasses.replace(
        config,
        profiles=dict(s.answer_profiles),
        safety=dataclasses.replace(s.safety, require_confirm_for=list(s.safety.require_confirm_for)),
        macros=list(s.macros),
        start_profiles={k: list(v) for k, v in s.start_profiles.items()},
        notifications=dataclasses.replace(
            config.notifications,
            on=list(s.on),
            done_min_work=s.done_min_work,
            done_short_delay=s.done_short_delay,
            remind_after=s.remind_after,
            subagents_done=s.subagents_done,
        ),
        usage=dataclasses.replace(
            config.usage, alert_at=list(s.alert_at), alert_reset=s.alert_reset
        ),
    )
