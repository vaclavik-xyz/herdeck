import logging
import os

from herdeck.config import ViewConfig
from herdeck.model import AgentKey, AgentState, Status
from herdeck.project_icon_discovery import MAX_ICON_BYTES, icon_hash
from herdeck.project_icons import (
    ProjectIconStore,
    StoredIcon,
    default_store,
    ingest_project_icon,
    tile_icon_fields,
)
from herdeck.protocol import ProjectIcon


def _put(store, data, mime="image/png"):
    h = icon_hash(data)
    store.put(h, mime, data)
    return h


def test_put_get_and_duplicate():
    store = ProjectIconStore()
    h = icon_hash(b"a")
    assert store.put(h, "image/png", b"a") is True
    assert store.put(h, "image/png", b"a") is False  # already there: no re-render
    assert store.get(h) == StoredIcon("image/png", b"a")
    assert h in store and "0" * 16 not in store


def test_lru_bounds_by_entries_and_recency():
    store = ProjectIconStore(max_entries=2)
    a, b = _put(store, b"a"), _put(store, b"b")
    store.get(a)  # a is now most recent
    c = _put(store, b"c")
    assert a in store and c in store and b not in store


def test_lru_bounds_by_bytes():
    store = ProjectIconStore(max_bytes=10)
    a = _put(store, b"x" * 6)
    b = _put(store, b"y" * 6)
    assert b in store and a not in store


def test_put_rejects_empty_or_oversized():
    store = ProjectIconStore()
    assert store.put("0" * 16, "image/png", b"") is False
    assert store.put("1" * 16, "image/png", b"x" * (MAX_ICON_BYTES + 1)) is False


def test_resolve_uses_wire_hash_only_when_bytes_are_present():
    store = ProjectIconStore()
    h = icon_hash(b"icon")
    assert store.resolve("shop", h, {}) is None  # frame not arrived yet -> monogram
    store.put(h, "image/png", b"icon")
    assert store.resolve("shop", h, {}) == h
    assert store.resolve("shop", "", {}) is None


def test_override_takes_precedence_over_discovery(tmp_path):
    store = ProjectIconStore()
    wire = _put(store, b"discovered")
    override = tmp_path / "shop.png"
    override.write_bytes(b"override-bytes")
    h = store.resolve("shop", wire, {"shop": str(override)})
    assert h == icon_hash(b"override-bytes")
    assert store.get(h) == StoredIcon("image/png", b"override-bytes")
    assert store.resolve("blog", wire, {"shop": str(override)}) == wire  # other repos unaffected


def test_override_path_expands_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "icons").mkdir()
    (tmp_path / "icons" / "shop.ico").write_bytes(b"ico-bytes")
    store = ProjectIconStore()
    h = store.resolve("shop", "", {"shop": "~/icons/shop.ico"})
    assert h == icon_hash(b"ico-bytes")
    assert store.get(h).mime == "image/x-icon"


def test_override_is_rechecked_after_the_interval(tmp_path):
    now = [0.0]
    store = ProjectIconStore(clock=lambda: now[0], override_recheck_s=5.0)
    path = tmp_path / "shop.png"
    path.write_bytes(b"v1")
    first = store.resolve("shop", "", {"shop": str(path)})
    path.write_bytes(b"version-2")
    os.utime(path, ns=(1, 1))  # a different mtime_ns as well as a different size
    assert store.resolve("shop", "", {"shop": str(path)}) == first
    now[0] = 6.0
    assert store.resolve("shop", "", {"shop": str(path)}) == icon_hash(b"version-2")


def test_resolve_follows_a_changed_override_map(tmp_path):
    store = ProjectIconStore()
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    a.write_bytes(b"aaa")
    b.write_bytes(b"bbbb")
    assert store.resolve("shop", "", {"shop": str(a)}) == icon_hash(b"aaa")
    assert store.resolve("shop", "", {"shop": str(b)}) == icon_hash(b"bbbb")  # edited config
    assert store.resolve("shop", "", {}) is None  # override removed


def test_missing_override_falls_back_to_discovery_and_warns_once(tmp_path, caplog):
    store = ProjectIconStore()
    wire = _put(store, b"discovered")
    missing = {"shop": str(tmp_path / "nope.png"), "blog": str(tmp_path / "notes.txt")}
    (tmp_path / "notes.txt").write_text("not an icon")
    with caplog.at_level(logging.WARNING, logger="herdeck.project_icons"):
        assert store.resolve("shop", wire, missing) == wire
        assert store.resolve("shop", wire, missing) == wire
        assert store.resolve("blog", "", missing) is None
    warnings = [r for r in caplog.records if "project icon override" in r.getMessage()]
    assert len(warnings) == 2  # one per bad path, not per render


def test_ingest_project_icon_uses_the_given_or_default_store():
    store = ProjectIconStore()
    icon = ProjectIcon("dev", icon_hash(b"z"), "image/png", b"z")
    assert ingest_project_icon(icon, store) is True
    assert ingest_project_icon(icon, store) is False
    default_store().clear()
    try:
        assert ingest_project_icon(icon) is True
        assert icon.hash in default_store()
    finally:
        default_store().clear()


def test_tile_icon_fields_agent_mode_is_inert():
    state = AgentState(AgentKey("dev", "p"), "claude", "api", Status.IDLE, repo="shop")
    assert tile_icon_fields(ViewConfig(), state, ProjectIconStore()) == {
        "tile_icon": "agent",
        "project_icon": None,
        "project_name": "",
    }


def test_tile_icon_fields_project_mode_resolves_hash_and_name():
    store = ProjectIconStore()
    h = _put(store, b"shop-icon")
    view = ViewConfig(tile_icon="both")
    state = AgentState(
        AgentKey("dev", "p"), "claude", "api", Status.IDLE, repo="shop", project_icon=h
    )
    assert tile_icon_fields(view, state, store) == {
        "tile_icon": "both",
        "project_icon": h,
        "project_name": "shop",
    }
    no_repo = AgentState(AgentKey("dev", "q"), "claude", "label-only", Status.IDLE)
    assert tile_icon_fields(view, no_repo, store)["project_name"] == "label-only"


def test_override_keys_with_surrounding_whitespace_still_match(tmp_path):
    store = ProjectIconStore()
    path = tmp_path / "shop.png"
    path.write_bytes(b"padded")
    assert store.resolve("shop", "", {" shop ": str(path)}) == icon_hash(b"padded")
