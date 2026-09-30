import json
import os
import stat

from herdeck.bridge_settings import SETTINGS_CAPABILITY
from herdeck.config import Config, ServerConfig
from herdeck.deckapp import shared_view
from herdeck.deckapp.live import LiveSource
from herdeck.deckapp.shared_view import SharedSettingsView, default_cache_dir
from herdeck.model import AgentKey
from herdeck.protocol import Settings


def _raw(done_min_work=3):
    return {"notifications": {"done_min_work": done_min_work}, "macros": [{"label": "go", "text": "continue"}]}


def _frame(server_id="local:2", revision=5, settings=None, by="mac-a"):
    return Settings(server_id, revision, 1_700_000_000_000, by, settings)


def _files(path):
    return sorted(p.name for p in path.iterdir()) if path.exists() else []


def test_default_cache_dir_respects_runtime_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HERDECK_RUNTIME_DIR", str(tmp_path))
    assert default_cache_dir() == tmp_path / "bridge-settings"
    monkeypatch.delenv("HERDECK_RUNTIME_DIR")
    assert default_cache_dir() == __import__("pathlib").Path(
        os.path.expanduser("~/.cache/herdeck/bridge-settings")
    )


def test_frame_parses_and_writes_private_cache(tmp_path):
    view = SharedSettingsView(tmp_path)
    assert view.update("local:2", _frame(settings=_raw())) is True
    assert view.settings_for("local:2").done_min_work == 3
    (name,) = _files(tmp_path)
    assert "/" not in name and name.endswith(".json")
    path = tmp_path / name
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    data = json.loads(path.read_text())
    assert data == {
        "server_id": "local:2",
        "revision": 5,
        "updated_at_ms": 1_700_000_000_000,
        "updated_by": "mac-a",
        "settings": _raw(),
    }
    st = view.state("local:2")
    assert st == {
        "revision": 5,
        "updated_at_ms": 1_700_000_000_000,
        "updated_by": "mac-a",
        "set": True,
        "source": "bridge",
        "settings": _raw(),
    }
    # Same frame again is not a change.
    assert view.update("local:2", _frame(settings=_raw())) is False


def test_new_view_warm_starts_from_cache(tmp_path):
    SharedSettingsView(tmp_path).update("local:2", _frame(settings=_raw(7)))
    view = SharedSettingsView(tmp_path)
    assert view.settings_for("local:2").done_min_work == 7
    assert view.state("local:2")["source"] == "cache"
    assert view.state("local:2")["revision"] == 5


def test_unset_bridge_frame_replaces_cache_never_merges(tmp_path):
    SharedSettingsView(tmp_path).update("local:2", _frame(revision=5, settings=_raw()))
    view = SharedSettingsView(tmp_path)  # a Mac back online with an older cache
    assert view.settings_for("local:2") is not None
    assert view.update("local:2", _frame(revision=0, settings=None)) is True
    assert view.settings_for("local:2") is None
    assert _files(tmp_path) == []
    st = view.state("local:2")
    assert (st["set"], st["source"], st["revision"], st["settings"]) == (False, "none", 0, None)
    assert SharedSettingsView(tmp_path).settings_for("local:2") is None


def test_newer_bridge_frame_replaces_whole_document(tmp_path):
    view = SharedSettingsView(tmp_path)
    view.update("a", _frame("a", 5, {"notifications": {"done_min_work": 3, "remind_after": 9}}))
    assert view.update("a", _frame("a", 6, {"notifications": {"done_min_work": 4}})) is True
    s = view.settings_for("a")
    assert (s.done_min_work, s.remind_after) == (4, 0)  # remind_after not carried over


def test_invalid_settings_keep_previous(tmp_path, caplog):
    view = SharedSettingsView(tmp_path)
    view.update("a", _frame("a", 5, _raw(3)))
    with caplog.at_level("WARNING"):
        assert view.update("a", _frame("a", 6, {"notifications": {"done_min_work": True}})) is False
    assert view.settings_for("a").done_min_work == 3
    assert view.state("a")["revision"] == 5
    assert "invalid" in caplog.text.lower()
    assert SharedSettingsView(tmp_path).settings_for("a").done_min_work == 3


def test_forget_live_keeps_values(tmp_path):
    view = SharedSettingsView(tmp_path)
    view.update("a", _frame("a", 5, _raw(3)))
    view.forget_live("a")
    assert view.state("a")["source"] == "cache"
    assert view.settings_for("a").done_min_work == 3
    view.forget_live("unknown")
    assert view.state("unknown")["source"] == "none"


def test_garbage_cache_file_is_ignored(tmp_path):
    (tmp_path / "x.json").write_text("{nope")
    (tmp_path / "y.json").write_text(json.dumps({"server_id": "y", "revision": 2, "updated_at_ms": 0,
                                                 "updated_by": "", "settings": {"theme": {}}}))
    view = SharedSettingsView(tmp_path)
    assert view.settings_for("x") is None and view.settings_for("y") is None


def test_filenames_are_distinct_and_safe(tmp_path):
    view = SharedSettingsView(tmp_path)
    for sid in ("local:2", "local_2", "../evil", "a/b"):
        view.update(sid, _frame(sid, 1, _raw()))
    assert len(_files(tmp_path)) == 4
    assert not (tmp_path.parent / "evil.json").exists()
    fresh = SharedSettingsView(tmp_path)
    for sid in ("local:2", "local_2", "../evil", "a/b"):
        assert fresh.settings_for(sid) is not None


def test_no_cache_dir_is_memory_only():
    view = SharedSettingsView(None)
    assert view.update("a", _frame("a", 1, _raw())) is True
    assert view.settings_for("a").done_min_work == 3


# --- LiveSource.config_for ---------------------------------------------------


def _two_server_config():
    cfg = Config(
        servers=[ServerConfig(id="a", url="ws://a", token="t"), ServerConfig(id="b", url="ws://b", token="t")],
        profiles={},
        overview_order=["a", "b"],
        grid=(5, 3),
    )
    cfg.notifications.enabled = False
    cfg.notifications.sound = False
    cfg.notifications.done_min_work = 1
    return cfg


def _source(tmp_path, config=None):
    return LiveSource(config or _two_server_config(), shared_view=SharedSettingsView(tmp_path))


def test_config_for_applies_that_bridges_settings(tmp_path):
    src = _source(tmp_path)
    src._on_settings("a", _frame("a", 5, _raw(3)))
    cfg = src.config_for("a")
    assert cfg.notifications.done_min_work == 3
    assert cfg.notifications.sound is False  # local delivery kept
    assert src.config.notifications.done_min_work == 1  # local config untouched
    assert src.config_for("a") is cfg  # memoized


def test_config_for_unset_or_unknown_server_is_local(tmp_path):
    src = _source(tmp_path)
    assert src.config_for("b") is src.config
    assert src.config_for("nope") is src.config
    src._on_settings("b", _frame("b", 0, None))
    assert src.config_for("b") is src.config


def test_two_bridges_different_rules(tmp_path):
    src = _source(tmp_path)
    src._on_settings("a", _frame("a", 5, _raw(3)))
    src._on_settings("b", _frame("b", 2, _raw(9)))
    assert src.config_for("a").notifications.done_min_work == 3
    assert src.config_for("b").notifications.done_min_work == 9


def test_config_for_follows_updates_and_reset(tmp_path):
    src = _source(tmp_path)
    src._on_settings("a", _frame("a", 5, _raw(3)))
    assert src.config_for("a").notifications.done_min_work == 3
    src._on_settings("a", _frame("a", 6, _raw(4)))
    assert src.config_for("a").notifications.done_min_work == 4
    src._on_settings("a", _frame("a", 0, None))
    assert src.config_for("a") is src.config


def test_live_source_warm_starts_from_cache(tmp_path):
    SharedSettingsView(tmp_path).update("a", _frame("a", 5, _raw(3)))
    src = _source(tmp_path)
    assert src.config_for("a").notifications.done_min_work == 3
    assert src.shared_state()["a"]["source"] == "cache"


def test_live_source_default_view_uses_default_cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(shared_view, "default_cache_dir", lambda: tmp_path / "bs")
    SharedSettingsView(tmp_path / "bs").update("a", _frame("a", 5, _raw(3)))
    src = LiveSource(_two_server_config())
    assert src.config_for("a").notifications.done_min_work == 3


def test_settings_change_bumps_semantic_generation(tmp_path):
    src = _source(tmp_path)
    key_a, key_b = AgentKey("a", "p1"), AgentKey("b", "p1")
    src._agents[key_a] = object()
    src._agents[key_b] = object()
    src._bump_semantic_locked([key_a, key_b])  # both have their own generation
    ga, gb = src.semantic_generation("a", "p1"), src.semantic_generation("b", "p1")
    src._on_settings("a", _frame("a", 5, _raw(3)))
    assert src.semantic_generation("a", "p1") > ga
    assert src.semantic_generation("b", "p1") == gb
    ga2 = src.semantic_generation("a", "p1")
    src._on_settings("a", _frame("a", 5, _raw(3)))  # unchanged: no bump
    assert src.semantic_generation("a", "p1") == ga2


def test_settings_change_schedules_refresh(tmp_path):
    import threading

    src = _source(tmp_path)
    refreshes = []
    src.attach(object(), lock=threading.Lock(), refresh_locked=lambda: refreshes.append(1))
    src._on_settings("a", _frame("a", 5, _raw(3)))
    assert refreshes == [1]
    src._on_settings("a", _frame("a", 5, _raw(3)))
    assert refreshes == [1]


class _Conn:
    def __init__(self, caps):
        self.capabilities = frozenset(caps)


class _Runner:
    def __init__(self, caps):
        self.connector = _Conn(caps)


def test_shared_state_and_connection_loss(tmp_path):
    src = _source(tmp_path)
    src._runners["a"] = _Runner({SETTINGS_CAPABILITY})
    src._runners["b"] = _Runner(set())
    src._on_connection("a", True)
    src._on_settings("a", _frame("a", 5, _raw(3)))
    state = src.shared_state()
    assert set(state) == {"a", "b"}
    assert state["a"]["offered"] is True and state["a"]["connected"] is True
    assert state["a"]["source"] == "bridge" and state["a"]["set"] is True
    assert state["b"] == {
        "revision": 0,
        "updated_at_ms": 0,
        "updated_by": "",
        "set": False,
        "source": "none",
        "settings": None,
        "offered": False,
        "connected": False,
    }
    src._on_connection("a", False)
    state = src.shared_state()
    assert state["a"]["source"] == "cache" and state["a"]["connected"] is False
    assert src.config_for("a").notifications.done_min_work == 3


def test_clear_drops_entry_and_cache(tmp_path):
    view = SharedSettingsView(tmp_path)
    view.update("a", _frame("a", 5, _raw(3)))
    assert view.clear("a") is True
    assert view.settings_for("a") is None and _files(tmp_path) == []
    assert view.clear("a") is False


def test_old_bridge_without_capability_drops_cached_settings(tmp_path):
    """Review Focus 3: server "a" once had a settings-capable bridge; its id now
    points at an old bridge that never sends a settings frame."""
    SharedSettingsView(tmp_path).update("a", _frame("a", 5, _raw(3)))
    src = _source(tmp_path)
    assert src.config_for("a").notifications.done_min_work == 3  # warm start
    src._runners["a"] = _Runner({"usage"})  # capabilities of the old bridge
    src._agents[AgentKey("a", "p1")] = object()
    src._bump_semantic_locked([AgentKey("a", "p1")])
    gen = src.semantic_generation("a", "p1")
    src._on_snapshot("a", [])
    assert src.config_for("a") is src.config
    assert _files(tmp_path) == []
    assert src.shared_state()["a"]["source"] == "none"
    assert src.semantic_generation("a", "p1") > gen


def test_bridge_with_capability_keeps_cache_until_frame(tmp_path):
    SharedSettingsView(tmp_path).update("a", _frame("a", 5, _raw(3)))
    src = _source(tmp_path)
    src._runners["a"] = _Runner({SETTINGS_CAPABILITY})
    src._on_snapshot("a", [])
    assert src.config_for("a").notifications.done_min_work == 3
    assert len(_files(tmp_path)) == 1
    src._on_settings("a", _frame("a", 6, _raw(4)))
    assert src.config_for("a").notifications.done_min_work == 4


def test_snapshot_without_known_capabilities_keeps_cache(tmp_path):
    SharedSettingsView(tmp_path).update("a", _frame("a", 5, _raw(3)))
    src = _source(tmp_path)  # no runner attached: capabilities unknown
    src._on_snapshot("a", [])
    assert src.config_for("a").notifications.done_min_work == 3
