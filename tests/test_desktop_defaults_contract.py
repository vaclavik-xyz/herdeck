import json
from dataclasses import asdict
from pathlib import Path

from herdeck.config import (
    DEFAULT_MACROS,
    DEFAULT_PROFILES,
    DEFAULT_START_PROFILES,
    HardwareConfig,
    Notifications,
    SafetyConfig,
    TelegramConfig,
    ThemeConfig,
    UsageConfig,
    ViewConfig,
    _parse_telegram_optional_int,
)
from herdeck.settings import _build_config

DEFAULTS_PATH = Path(__file__).parents[1] / "desktop" / "src" / "lib" / "configDefaults.json"


def _defaults():
    return json.loads(DEFAULTS_PATH.read_text(encoding="utf-8"))


def _assert_subset(actual: dict, expected_subset: dict):
    assert {key: actual[key] for key in expected_subset} == expected_subset


def test_desktop_defaults_match_backend_contract():
    defaults = _defaults()

    runtime = _build_config({}, {}, None, {}, profile_name="default", env_profile=None)
    assert defaults["grid"] == f"{runtime.grid[0]}x{runtime.grid[1]}"
    assert defaults["theme"] == asdict(ThemeConfig())
    _assert_subset(asdict(ViewConfig()), defaults["view"])
    assert defaults["safety"] == asdict(SafetyConfig())
    assert defaults["usage"] == asdict(UsageConfig())
    _assert_subset(asdict(HardwareConfig()), defaults["hardware"])

    notifications = asdict(Notifications())
    _assert_subset(notifications, {k: v for k, v in defaults["notifications"].items() if k != "telegram"})
    telegram = asdict(TelegramConfig("", ""))
    telegram.pop("token_env")
    telegram.pop("chat_id")
    telegram["message_thread_id"] = 0  # serializable sentinel for runtime None
    assert defaults["notifications"]["telegram"] == telegram
    assert (
        _parse_telegram_optional_int(
            "message_thread_id", defaults["notifications"]["telegram"]["message_thread_id"]
        )
        is None
    )

    assert defaults["macros"] == [asdict(macro) for macro in DEFAULT_MACROS]
    assert defaults["start_profiles"] == DEFAULT_START_PROFILES
    assert defaults["answer_profiles"] == {
        name: asdict(profile) for name, profile in DEFAULT_PROFILES.items()
    }
