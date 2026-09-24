from __future__ import annotations

import contextlib
import contextvars
import json
import math
import os
import re
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .config import (
    AGENT_ORDERS,
    DEFAULT_EVENT_SOUNDS,
    DEFAULT_MACROS,
    DEFAULT_NOTIFY_ON,
    DEFAULT_PROFILES,
    DEFAULT_REQUIRE_CONFIRM,
    DEFAULT_START_PROFILES,
    TILE_FILLS,
    TILE_ICONS,
    TILE_LINE_TOKENS,
    WORKING_ANIMATIONS,
    Config,
    ConfigError,
    ConfigMeta,
    HardwareConfig,
    Macro,
    Notifications,
    SafetyConfig,
    ServerConfig,
    ThemeConfig,
    UsageConfig,
    ViewConfig,
    _parse_grid,
    _parse_profile,
    _parse_telegram_config,
    normalize_notify_on,
    notification_flag,
    notification_minutes,
    validate_event_sounds,
)
from .i18n import LANGUAGES

_METADATA_TILE_TOKEN_RE = re.compile(r"^\$[A-Za-z0-9_-]{1,32}$")


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


class TokenNotFoundError(ConfigError):
    """A server's bridge token resolved from none of its sources (env, token_file,
    keychain). Carries the server id so a deck can say which one — never a value."""

    def __init__(self, message: str, server_id: str):
        super().__init__(message)
        self.server_id = server_id


# Set while ConfigService validates a pending edit STRUCTURALLY: a token that
# does not resolve yet (not typed into the keychain, token file not created)
# must not block writing the config that references it.
_ASSUME_TOKENS_PRESENT: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "herdeck_assume_tokens_present", default=False
)


@contextlib.contextmanager
def assume_tokens_present():
    """Resolve every server token as a placeholder inside this block (structural
    validation only — the resulting Config must never be used to connect)."""
    reset = _ASSUME_TOKENS_PRESENT.set(True)
    try:
        yield
    finally:
        _ASSUME_TOKENS_PRESENT.reset(reset)


def _token_sources(raw: dict) -> tuple[str | None, Path | None]:
    """The validated ``(token_env, token_file)`` pair of a ``[[servers]]`` entry."""
    sid = raw.get("id")
    env = raw.get("token_env")
    if env is not None and (not isinstance(env, str) or not env):
        raise ConfigError(f"server '{sid}': token_env must be a non-empty name")
    value = raw.get("token_file")
    path = None
    if value is not None:
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"server '{sid}': token_file must be a non-empty path")
        path = Path(os.path.expanduser(value.strip()))
    if env is None and path is None:
        raise ConfigError(f"server '{sid}' needs token_env or token_file")
    return env, path


def _read_token_file(server_id: str, path: Path) -> str | None:
    """The stripped token in ``path``; None when the file does not exist.

    Same rules as the bridge's own token file: a regular file readable by its
    owner only (0600 or stricter). Anything else is refused loudly — a token
    other users can read is a leaked token."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ConfigError(
            f"server '{server_id}': cannot read token_file '{path}' ({exc.strerror})"
        ) from None
    if not stat.S_ISREG(st.st_mode):
        raise ConfigError(f"server '{server_id}': token_file '{path}' is not a regular file")
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        raise ConfigError(
            f"server '{server_id}': token_file '{path}' is readable by group/others "
            f"(mode {mode:04o}); run chmod 600 on it"
        )
    try:
        token = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        reason = getattr(exc, "strerror", None) or "not UTF-8 text"
        raise ConfigError(
            f"server '{server_id}': cannot read token_file '{path}' ({reason})"
        ) from None
    if not token:
        raise ConfigError(f"server '{server_id}': token_file '{path}' is empty")
    return token


def resolve_server_token(raw: dict) -> tuple[str, str]:
    """``(token, source)`` for a ``[[servers]]`` entry; source is ``"env"``,
    ``"file"`` or ``"keychain"``.

    Order: the ``token_env`` environment variable, then ``token_file``, then the
    OS keychain entry named ``token_env``. A launchd/systemd service has no
    shell env (and refuses TOKEN env names by design), so ``token_file`` is how
    a service gets a token the keychain does not hold. Raises ConfigError —
    ``TokenNotFoundError`` when no source has it; the message never carries a
    token value."""
    from . import secrets

    sid = str(raw.get("id"))
    env, path = _token_sources(raw)
    if env and os.environ.get(env):
        return os.environ[env], "env"
    if path is not None:
        token = _read_token_file(sid, path)
        if token:
            return token, "file"
    if env:
        # The env var is known unset here, so get_secret answers from the keychain
        # (and never raises when there is no keychain backend).
        token = secrets.get_secret(env)
        if token:
            return token, "keychain"
    tried = []
    if env:
        tried.append(f"env var '{env}' is not set")
    if path is not None:
        tried.append(f"token_file '{path}' does not exist")
    if env:
        tried.append(f"no keychain entry '{env}'")
    raise TokenNotFoundError(
        f"bridge token for server '{sid}' not found ({'; '.join(tried)})", sid
    )


def _server_config(raw: dict) -> ServerConfig:
    if _ASSUME_TOKENS_PRESENT.get():
        _token_sources(raw)  # a malformed token_env/token_file is still structural
        token = "x"
    else:
        token, _source = resolve_server_token(raw)
    backend = raw.get("backend", "herdr")
    if backend not in ("herdr", "t3"):
        raise ConfigError("unsupported server backend")
    desktop_read_state = raw.get("desktop_read_state", False)
    if not isinstance(desktop_read_state, bool):
        raise ConfigError(f"server '{raw['id']}': desktop_read_state must be true or false")
    if desktop_read_state and backend != "t3":
        raise ConfigError(
            f"server '{raw['id']}': desktop_read_state is an option of T3 servers only"
        )
    if backend == "t3":
        from .t3 import T3Http
        try:
            T3Http(raw["url"], token)
        except ValueError as exc:
            raise ConfigError(str(exc)) from None
    return ServerConfig(raw["id"], raw["url"], token, backend, desktop_read_state)


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
    if "agent_order" in raw:
        val = raw["agent_order"]
        if val not in AGENT_ORDERS:
            raise ConfigError(f"unknown view.agent_order '{val}'; want one of {AGENT_ORDERS}")
        view.agent_order = val
    if "bottom_row" in raw:
        view.bottom_row = list(raw["bottom_row"])
    if "tile_fields" in raw:
        view.tile_fields = list(raw["tile_fields"])
    for key in ("tile_primary", "tile_secondary"):
        if key in raw:
            tokens = list(raw[key])
            for tok in tokens:
                if tok not in TILE_LINE_TOKENS and not (
                    isinstance(tok, str) and _METADATA_TILE_TOKEN_RE.fullmatch(tok)
                ):
                    raise ConfigError(f"unknown tile token '{tok}' in view.{key}")
            setattr(view, key, tokens)
    if "working_animation" in raw:
        val = raw["working_animation"]
        if val not in WORKING_ANIMATIONS:
            raise ConfigError(
                f"unknown view.working_animation '{val}'; want one of {WORKING_ANIMATIONS}"
            )
        view.working_animation = val
    if "tile_fill" in raw:
        val = raw["tile_fill"]
        if val not in TILE_FILLS:
            raise ConfigError(f"unknown view.tile_fill '{val}'; want one of {TILE_FILLS}")
        view.tile_fill = val
    if "tile_icon" in raw:
        val = raw["tile_icon"]
        if val not in TILE_ICONS:
            raise ConfigError(f"unknown view.tile_icon '{val}'; want one of {TILE_ICONS}")
        view.tile_icon = val
    if "project_icons" in raw:
        view.project_icons = _project_icons(raw["project_icons"])
    if "show_profile_on_panel" in raw:
        view.show_profile_on_panel = bool(raw["show_profile_on_panel"])
    if "collapse_idle" in raw:
        if not isinstance(raw["collapse_idle"], bool):
            raise ConfigError("view.collapse_idle must be true or false")
        view.collapse_idle = raw["collapse_idle"]
    if "language" in raw:
        val = raw["language"]
        if val not in LANGUAGES:
            raise ConfigError(f"unknown view.language '{val}'; want one of {LANGUAGES}")
        view.language = val
    return view


def _project_icons(raw) -> dict[str, str]:
    """[view.project_icons]: repo name -> icon path. Paths stay as written (the
    runtime expands "~" when it reads the file) so the editor round-trips them."""
    if not isinstance(raw, dict):
        raise ConfigError('view.project_icons must be a table of repo = "path" entries')
    out: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            raise ConfigError("view.project_icons keys must be non-empty repo names")
        if isinstance(value, dict):
            # An unquoted dotted key (vaclavik.xyz = "...") parses as a nested
            # table; rebuild the dotted name for the hint.
            dotted = key
            while isinstance(value, dict) and len(value) == 1:
                sub, value = next(iter(value.items()))
                dotted = f"{dotted}.{sub}"
            raise ConfigError(
                f"view.project_icons.{key} is a table, not a path; quote the repo name "
                f'if it contains a dot, e.g. "{dotted}" = "~/icons/{dotted}.png"'
            )
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"view.project_icons.{key} must be a non-empty path string")
        out[key] = value
    return out


def _notifications_config(raw: dict | None) -> Notifications:
    raw = raw or {}
    telegram = None
    tg_raw = raw.get("telegram")
    if isinstance(tg_raw, dict):
        telegram = _parse_telegram_config(tg_raw)
    return Notifications(
        enabled=raw.get("enabled", False),
        on=normalize_notify_on(raw.get("on", DEFAULT_NOTIFY_ON)),
        sound=raw.get("sound", True),
        backends=list(raw.get("backends", ["macos"])),
        sounds={**DEFAULT_EVENT_SOUNDS, **validate_event_sounds(raw.get("sounds"))},
        telegram=telegram,
        banner_actions=notification_flag(raw, "banner_actions", False),
        banner_prompt=notification_flag(raw, "banner_prompt", False),
        skip_focused=notification_flag(raw, "skip_focused", True),
        remind_after=notification_minutes(raw, "remind_after"),
    )


def resolve_notifications(snapshot: SettingsSnapshot) -> Notifications:
    active = _active_profile_name(snapshot)
    merged, _selection = _merged_sections(snapshot.data, active)
    return _notifications_config(merged.get("notifications"))


def _safety_config(raw: dict | None) -> SafetyConfig:
    raw = raw or {}
    return SafetyConfig(
        approve_always=raw.get("approve_always", True),
        # An explicit empty list in the file opts out; only an ABSENT key falls
        # back to the confirm-Stop default.
        require_confirm_for=list(raw.get("require_confirm_for", DEFAULT_REQUIRE_CONFIRM)),
    )


def _usage_config(raw: dict | None) -> UsageConfig:
    raw = raw or {}
    usage = UsageConfig()
    if "providers" in raw:
        providers = raw["providers"]
        if not isinstance(providers, list) or any(not isinstance(p, str) for p in providers):
            raise ConfigError("usage.providers must be a list of provider ids (strings)")
        for p in providers:
            # Ids become one comma-joined CLI argument: a blank id would turn
            # the poller ON with a garbage `--provider ""` call (the editor's
            # list Add button seeds an empty row), a comma corrupts the join.
            if not re.fullmatch(r"[A-Za-z0-9_-]+", p):
                raise ConfigError(
                    f"usage.providers entry {p!r} is not a provider id"
                    " (letters, digits, '-', '_')"
                )
        if len(set(providers)) != len(providers):
            raise ConfigError("usage.providers must not contain duplicate provider ids")
        usage.providers = list(providers)
    if "paid_only" in raw:
        paid_only = raw["paid_only"]
        if type(paid_only) is not bool:
            raise ConfigError("usage.paid_only must be true or false")
        usage.paid_only = paid_only
    if "refresh_secs" in raw:
        secs = raw["refresh_secs"]
        if type(secs) is not int or secs < 30:
            raise ConfigError("usage.refresh_secs must be an integer >= 30")
        usage.refresh_secs = secs
    for key in ("codex_path", "claude_cache_path", "codexbar_path"):
        if key not in raw:
            continue
        path = raw[key]
        if not isinstance(path, str) or (key != "codexbar_path" and not path.strip()):
            raise ConfigError(f"usage.{key} must be a non-empty string")
        setattr(usage, key, path)
    if "alert_at" in raw:
        levels = raw["alert_at"]
        if not isinstance(levels, list) or any(
            type(v) is not int or not 1 <= v <= 100 for v in levels
        ):
            raise ConfigError("usage.alert_at must be a list of integer percentages 1-100")
        usage.alert_at = sorted(set(levels))
    if "alert_reset" in raw:
        alert_reset = raw["alert_reset"]
        if type(alert_reset) is not bool:
            raise ConfigError("usage.alert_reset must be true or false")
        usage.alert_reset = alert_reset
    return usage


def _macro_set(raw) -> list[Macro]:
    if raw is None:
        return list(DEFAULT_MACROS)
    return [Macro(label=m["label"], text=m["text"]) for m in raw]


def _launcher(raw) -> dict[str, list[str]]:
    if raw is None:
        return dict(DEFAULT_START_PROFILES)
    return {k: list(v) for k, v in raw.items()}


# A uhubctl hub location: bus, then an optional dotted port chain ("1", "1-1",
# "20-1.4"). Validated so a config value can never smuggle an option into argv.
_USB_HUB_RE = re.compile(r"[0-9]+(-[0-9]+(\.[0-9]+)*)?")


def _hardware_config(local_data: dict) -> HardwareConfig:
    raw = local_data.get("local", {})
    hw = local_data.get("hardware", {})
    if not isinstance(raw, dict):
        raise ConfigError("local must be a table")
    if not isinstance(hw, dict):
        raise ConfigError("hardware must be a table")

    web_port = raw.get("web_port")
    if web_port is not None and (
        not isinstance(web_port, int) or isinstance(web_port, bool) or not 0 <= web_port <= 65535
    ):
        raise ConfigError("local.web_port must be an integer from 0 to 65535")

    terminal_app = raw.get("terminal_app", "")
    if not isinstance(terminal_app, str):
        raise ConfigError("local.terminal_app must be an app name string (empty = off)")

    brightness = hw.get("brightness", 80)
    if (
        not isinstance(brightness, int)
        or isinstance(brightness, bool)
        or not 0 <= brightness <= 100
    ):
        raise ConfigError("hardware.brightness must be an integer from 0 to 100")

    intervals = {}
    for key, default, maximum in (
        ("debounce", 0.25, 60.0),
        ("keep_alive_interval", 5.0, 86400.0),
        ("tick_interval", 0.4, 60.0),
    ):
        value = hw.get(key, default)
        if (
            not isinstance(value, int | float)
            or isinstance(value, bool)
            or value <= 0
            or value > maximum
            or not math.isfinite(float(value))
        ):
            raise ConfigError(
                f"hardware.{key} must be a positive number no greater than {maximum:g}"
            )
        intervals[key] = float(value)

    d200_standard_writer = hw.get("d200_standard_writer", False)
    if not isinstance(d200_standard_writer, bool):
        raise ConfigError("hardware.d200_standard_writer must be true or false")
    uhubctl = hw.get("uhubctl", "")
    if not isinstance(uhubctl, str):
        raise ConfigError("hardware.uhubctl must be a path string (empty = auto-detect)")
    usb_hub = hw.get("usb_hub", "")
    if not isinstance(usb_hub, str) or (usb_hub and not _USB_HUB_RE.fullmatch(usb_hub)):
        raise ConfigError(
            "hardware.usb_hub must be a uhubctl hub location such as 1-1 or 20-1.4"
        )
    usb_port = hw.get("usb_port")
    if usb_port is not None and (
        not isinstance(usb_port, int) or isinstance(usb_port, bool) or not 1 <= usb_port <= 127
    ):
        raise ConfigError("hardware.usb_port must be an integer from 1 to 127")

    return HardwareConfig(
        deck=raw.get("deck"),
        herdr_socket=raw.get("herdr_socket"),
        web_bind=raw.get("web_bind"),
        web_port=web_port,
        icons_dir=raw.get("icons_dir"),
        terminal_app=terminal_app.strip(),
        brightness=brightness,
        debounce=intervals["debounce"],
        keep_alive_interval=intervals["keep_alive_interval"],
        tick_interval=intervals["tick_interval"],
        d200_standard_writer=d200_standard_writer,
        uhubctl=uhubctl.strip(),
        usb_hub=usb_hub,
        usb_port=usb_port,
    )


def load_local_hardware(path: str | Path | None) -> HardwareConfig:
    """Load only device-local hardware, independent of servers and their tokens."""
    if path is None:
        return HardwareConfig()
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        return _hardware_config(data)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ConfigError):
        return HardwareConfig()


_OVERLAY_SECTIONS = (
    "deck",
    "answer_profiles",
    "macros",
    "start_profiles",
    "notifications",
    "theme",
    "view",
    "safety",
    "usage",
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
        usage=_usage_config(merged.get("usage")),
        hardware=_hardware_config(local_data),
        meta=ConfigMeta(
            active_profile=profile_name,
            profile_names=["default"] + sorted(data.get("profiles", {})),
            env_locked_profile=env_profile is not None,
        ),
    )
