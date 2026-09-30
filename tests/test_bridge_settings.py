import os
import stat

from herdeck.bridge_settings import BridgeSettingsStore, default_path

GOOD = {"notifications": {"done_min_work": 3}, "macros": [{"label": "go", "text": "continue"}]}


def test_unset_store_frame(tmp_path):
    st = BridgeSettingsStore(tmp_path / "b.toml")
    assert st.revision == 0 and st.raw is None
    assert st.frame("m4") == {"type": "settings", "server_id": "m4", "revision": 0,
                              "updated_at_ms": 0, "updated_by": "", "settings": None}


def test_put_persists_and_bumps(tmp_path):
    p = tmp_path / "b.toml"
    st = BridgeSettingsStore(p, clock=lambda: 1000.0)
    res = st.put(0, GOOD, "herdeck@macbench")
    assert res.ok and res.revision == 1
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    again = BridgeSettingsStore(p)
    assert again.revision == 1 and again.updated_by == "herdeck@macbench"
    assert again.frame("m4")["settings"]["notifications"]["done_min_work"] == 3


def test_stale_invalid_too_large_leave_file_untouched(tmp_path):
    p = tmp_path / "b.toml"
    st = BridgeSettingsStore(p)
    st.put(0, GOOD, "a")
    before = p.read_bytes()
    assert st.put(0, GOOD, "b").error == "stale_revision"
    bad = st.put(1, {"macros": [{"label": "x"}]}, "b")
    assert bad.error == "invalid" and bad.messages
    huge = st.put(1, {"macros": [{"label": "x", "text": "y" * 70000}]}, "b")
    assert huge.error == "too_large"
    assert st.put(1, "not a dict", "b").error == "invalid"
    assert p.read_bytes() == before and st.revision == 1


def test_corrupt_file_is_served_unset_with_error(tmp_path):
    p = tmp_path / "b.toml"
    p.write_text("revision = [", encoding="utf-8")
    st = BridgeSettingsStore(p)
    assert st.raw is None and st.revision == 0 and st.error
    assert st.put(0, GOOD, "a").ok  # adoption overwrites it


def test_default_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("HERDECK_BRIDGE_SETTINGS", str(tmp_path / "x.toml"))
    assert default_path() == tmp_path / "x.toml"
    monkeypatch.delenv("HERDECK_BRIDGE_SETTINGS")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_path() == tmp_path / ".config/herdeck/bridge-settings.toml"
    assert default_path("work") == tmp_path / ".config/herdeck/local-bridge-settings-work.toml"


def test_negative_updated_at_is_served_unset_with_error(tmp_path):
    p = tmp_path / "b.toml"
    p.write_text('revision = 2\nupdated_at_ms = -5\nupdated_by = "a"\n', encoding="utf-8")
    st = BridgeSettingsStore(p)
    assert st.raw is None and st.revision == 0 and st.updated_at_ms == 0 and st.error
