"""GUI-facing config service: read/validate/write the unified TOML config plus
profile and keychain-secret management. A thin layer over `settings`/`config` —
no config resolution logic is reimplemented here.
"""
from __future__ import annotations

import copy
import os
import tomllib
import tomli_w
from pathlib import Path

from .. import secrets as secret_store
from ..config import ConfigError
from ..settings import SettingsSnapshot, load_settings, set_active_profile, validate_settings


class ConfigService:
    BASE_SECTIONS = (
        "servers",
        "deck",
        "answer_profiles",
        "macros",
        "start_profiles",
        "notifications",
        "theme",
        "view",
        "safety",
    )

    def __init__(self, config_path, local_path):
        self._config_path = Path(config_path)
        self._local_path = Path(local_path)

    def read(self) -> dict:
        if not self._config_path.exists():
            return {"base": {}, "profiles": {}, "local": {}, "secrets": {}}
        data = tomllib.loads(self._config_path.read_text(encoding="utf-8"))
        local = (
            tomllib.loads(self._local_path.read_text(encoding="utf-8"))
            if self._local_path.exists()
            else {}
        )
        base = {sec: data[sec] for sec in self.BASE_SECTIONS if sec in data}
        profiles = data.get("profiles", {})
        return {
            "base": base,
            "profiles": profiles,
            "local": local,
            "secrets": self._secret_flags(base, profiles),
        }

    @staticmethod
    def _collect_token_envs(obj, out=None) -> list[str]:
        """Every `token_env` value anywhere in a nested dict/list, in first-seen
        order. Covers server defs, base notifications, AND profile overlays
        (e.g. `[profiles.x.notifications.telegram].token_env`)."""
        if out is None:
            out = []
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key == "token_env" and isinstance(value, str):
                    if value not in out:
                        out.append(value)
                else:
                    ConfigService._collect_token_envs(value, out)
        elif isinstance(obj, list):
            for item in obj:
                ConfigService._collect_token_envs(item, out)
        return out

    def _snapshot_for(self, data: dict) -> SettingsSnapshot:
        toml_data = dict(data.get("base", {}))
        profiles = data.get("profiles") or {}
        if profiles:
            toml_data["profiles"] = profiles
        return SettingsSnapshot(
            config_path=self._config_path,
            local_path=self._local_path,
            data=toml_data,
            local_data=data.get("local", {}) or {},
            env_profile=os.environ.get("HERDECK_PROFILE"),
        )

    def validate(self, data: dict) -> list[str]:
        return validate_settings(self._snapshot_for(data))

    HEADER = "# Managed by herdeck-config — generated; manual comments are not preserved\n"

    def write(self, data: dict) -> list[str]:
        structural = [e for e in self.validate(data) if "is not set" not in e]
        if structural:
            return structural
        toml_data = dict(data.get("base", {}))
        profiles = data.get("profiles") or {}
        if profiles:
            toml_data["profiles"] = profiles
        self._atomic_write(self._config_path, self.HEADER + tomli_w.dumps(toml_data))
        local = data.get("local") or {}
        self._atomic_write(self._local_path, tomli_w.dumps(local) if local else "")
        return []

    def _atomic_write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def _secret_flags(self, base: dict, profiles: dict) -> dict:
        names = self._collect_token_envs(base)
        self._collect_token_envs(profiles, names)
        return {
            name: {"set": secret_store.has_secret(name), "source": secret_store.secret_source(name)}
            for name in names
        }

    def set_active(self, name: str) -> bool:
        snapshot = load_settings(self._config_path, self._local_path)
        return set_active_profile(snapshot, name)

    def create_profile(self, data: dict, name: str) -> dict:
        if name == "default":
            raise ConfigError("profile 'default' is reserved (it is the base config)")
        profiles = data.get("profiles") or {}
        if name in profiles:
            raise ConfigError(f"profile '{name}' already exists")
        out = copy.deepcopy(data)
        out.setdefault("profiles", {})[name] = {}
        return out

    def delete_profile(self, data: dict, name: str) -> dict:
        profiles = data.get("profiles") or {}
        if name not in profiles:
            raise ConfigError(f"unknown profile '{name}'")
        out = copy.deepcopy(data)
        del out["profiles"][name]
        return out

    def set_secret(self, name: str, value: str) -> None:
        secret_store.set_secret(name, value)

    def clear_secret(self, name: str) -> None:
        secret_store.clear_secret(name)
