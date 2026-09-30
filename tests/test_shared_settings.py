import dataclasses

import pytest

from herdeck.config import Config, ConfigError, Macro
from herdeck.shared_settings import apply_shared, extract_shared, parse_shared, to_raw

RAW = {
    "notifications": {"on": ["blocked", "done"], "done_min_work": 3, "done_short_delay": 10,
                      "remind_after": 5, "subagents_done": True},
    "answer_profiles": {"claude": {"approve": ["1"], "deny": ["3"], "stop": ["escape"],
                                   "approve_always": ["2"]}},
    "safety": {"approve_always": False, "require_confirm_for": ["stop"]},
    "macros": [{"label": "go", "text": "continue"}],
    "start_profiles": {"claude": ["claude", "--x"]},
    "usage": {"alert_at": [80, 95], "alert_reset": True},
}


def test_round_trip():
    s = parse_shared(RAW)
    assert s.done_min_work == 3 and s.macros == [Macro("go", "continue")]
    assert parse_shared(to_raw(s)) == s


def test_empty_gives_todays_defaults():
    from herdeck.config import DEFAULT_MACROS, DEFAULT_START_PROFILES
    s = parse_shared({})
    assert s.macros == list(DEFAULT_MACROS) and s.start_profiles == dict(DEFAULT_START_PROFILES)
    assert (s.done_min_work, s.remind_after, s.subagents_done) == (0, 0, False)


@pytest.mark.parametrize("bad", [
    {"notifications": {"done_min_work": True}},
    {"notifications": {"remind_after": -1}},
    {"notifications": {"enabled": True}},            # local key is not shared
    {"notifications": {"on": ["nope"]}},
    {"notifications": {"on": "done"}},
    {"macros": [{"label": "x"}]},                    # missing text
    {"macros": "nope"},
    {"start_profiles": {"claude": ["claude", 3]}},
    {"start_profiles": {"claude": []}},
    {"safety": {"approve_always": "yes"}},
    {"safety": {"require_confirm_for": "stop"}},
    {"answer_profiles": {"x": {"approve": "1", "deny": ["2"], "stop": ["3"]}}},
    {"answer_profiles": {"x": {"approve": ["1"]}}},
    {"usage": {"alert_at": [0]}},
    {"usage": {"alert_at": [True]}},
    {"usage": {"source": "local"}},                  # local key
    {"theme": {}},                                   # unknown section
])
def test_invalid_is_rejected(bad):
    with pytest.raises(ConfigError):
        parse_shared(bad)


def test_extract_takes_only_shared_keys():
    base = {**RAW, "notifications": {**RAW["notifications"], "enabled": True, "sound": False},
            "usage": {"alert_at": [90], "source": "local"}, "view": {"language": "cs"}}
    out = extract_shared(base)
    assert out["notifications"] == RAW["notifications"]
    assert out["usage"] == {"alert_at": [90]}
    assert "view" not in out


def test_apply_replaces_shared_and_keeps_local_delivery():
    cfg = Config(servers=[], profiles={}, overview_order=[], grid=(5, 3))
    cfg.notifications.enabled = True
    cfg.notifications.sound = False
    cfg.usage.source = "local"
    out = apply_shared(cfg, parse_shared(RAW))
    assert out.notifications.enabled is True and out.notifications.sound is False
    assert out.notifications.done_min_work == 3 and out.usage.source == "local"
    assert out.usage.alert_at == [80, 95] and out.safety.approve_always is False
    assert cfg.notifications.done_min_work == 0  # original untouched
    assert out is not cfg and dataclasses.is_dataclass(out)


@pytest.mark.parametrize("bad", [
    {"macros": [{"label": "go", "text": "\ud800"}]},
    {"macros": [{"label": "\udfff", "text": "x"}]},
    {"start_profiles": {"claude": ["claude", "\ud800"]}},
    {"start_profiles": {"\ud800": ["claude"]}},
    {"answer_profiles": {"claude": {"approve": ["\ud800"]}}},
    {"safety": {"require_confirm_for": ["\ud800"]}},
])
def test_lone_surrogates_are_rejected(bad):
    with pytest.raises(ConfigError):
        parse_shared(bad)


def test_non_strict_ignores_unknown_top_level_section_with_warning(caplog):
    raw = {"future": {"x": 1}, "macros": [{"label": "go", "text": "continue"}]}
    with caplog.at_level("WARNING", logger="herdeck.shared_settings"):
        s = parse_shared(raw, strict=False)
    assert [m.label for m in s.macros] == ["go"]
    assert "future" in caplog.text


def test_strict_still_rejects_unknown_top_level_section():
    with pytest.raises(ConfigError):
        parse_shared({"future": {}, "macros": []})
    with pytest.raises(ConfigError):
        parse_shared({"future": {}}, strict=True)


def test_unknown_key_inside_known_section_rejected_in_both_modes():
    for strict in (True, False):
        with pytest.raises(ConfigError):
            parse_shared({"notifications": {"bogus": 1}}, strict=strict)
