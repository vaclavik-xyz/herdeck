import pytest

from herdeck.deckapp import onboarding


def test_absent_is_none(tmp_path):
    cfg = str(tmp_path / "config.toml")
    assert onboarding.read_choice(cfg) is None


def test_round_trip_local_then_demo(tmp_path):
    cfg = str(tmp_path / "config.toml")
    onboarding.write_choice(cfg, "local")
    assert onboarding.read_choice(cfg) == "local"
    onboarding.write_choice(cfg, "demo")
    assert onboarding.read_choice(cfg) == "demo"
    # the marker lives next to the config, not at the config path itself
    assert (tmp_path / "onboarding.toml").exists()


def test_clear_is_idempotent(tmp_path):
    cfg = str(tmp_path / "config.toml")
    onboarding.clear_choice(cfg)  # absent: no error
    onboarding.write_choice(cfg, "local")
    onboarding.clear_choice(cfg)
    assert onboarding.read_choice(cfg) is None


def test_invalid_choice_rejected(tmp_path):
    cfg = str(tmp_path / "config.toml")
    with pytest.raises(ValueError):
        onboarding.write_choice(cfg, "remote")


def test_none_config_path_uses_xdg_default():
    # state_path(None) points under ~/.config/herdeck/
    assert onboarding.state_path(None).name == "onboarding.toml"
    assert "herdeck" in str(onboarding.state_path(None))
