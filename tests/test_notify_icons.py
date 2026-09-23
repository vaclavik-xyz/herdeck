import io
import os
import re

from PIL import Image

from herdeck.model import AgentKey, AgentState, Status
from herdeck.notify_icons import NotificationIconCache
from herdeck.project_icon_discovery import icon_hash
from herdeck.project_icons import ProjectIconStore


def _state(repo="shop", project_icon=""):
    return AgentState(
        key=AgentKey("local", "p1"),
        agent_type="claude",
        status=Status.DONE,
        label="p1",
        repo=repo,
        project_icon=project_icon,
    )


def _png(color, size=32):
    buf = io.BytesIO()
    Image.new("RGBA", (size, size), color).save(buf, format="PNG")
    return buf.getvalue()


def _center(path):
    with Image.open(path) as im:
        return im.convert("RGBA").getpixel((im.width // 2, im.height // 2))


def test_favicon_from_the_store_becomes_a_png_file(tmp_path):
    store = ProjectIconStore()
    data = _png((200, 30, 30, 255))
    digest = icon_hash(data)
    store.put(digest, "image/png", data)
    cache = NotificationIconCache(str(tmp_path), store=store)

    path = cache.path_for(_state(project_icon=digest))

    assert path is not None and os.path.isabs(path) and os.path.dirname(path) == str(tmp_path)
    assert _center(path)[:3] == (200, 30, 30)


def test_project_without_a_favicon_gets_the_monogram(tmp_path):
    cache = NotificationIconCache(str(tmp_path), store=ProjectIconStore())

    first = cache.path_for(_state(repo="shop"))
    other = cache.path_for(_state(repo="blog"))

    assert first and other and first != other
    assert _center(first)[3] == 255  # an opaque mark, not an empty file


def test_hash_whose_bytes_never_arrived_falls_back_to_the_monogram(tmp_path):
    cache = NotificationIconCache(str(tmp_path), store=ProjectIconStore())
    assert cache.path_for(_state(project_icon="0" * 16)) == cache.path_for(_state())


def test_config_override_wins(tmp_path):
    override = tmp_path / "shop.png"
    override.write_bytes(_png((20, 200, 40, 255)))
    cache = NotificationIconCache(str(tmp_path / "out"), store=ProjectIconStore())

    path = cache.path_for(_state(), {"shop": str(override)})

    assert _center(path)[:3] == (20, 200, 40)


def test_file_names_stay_in_the_shells_allowed_set(tmp_path):
    cache = NotificationIconCache(str(tmp_path), store=ProjectIconStore())
    path = cache.path_for(_state(repo="../../etc/ Weird name"))
    # lib.rs banner_image_path accepts only [a-z0-9-]+.png in this directory.
    assert re.fullmatch(r"[a-z0-9-]+\.png", os.path.basename(path))
    assert os.path.dirname(path) == str(tmp_path)


def test_existing_file_is_reused_not_rewritten(tmp_path):
    cache = NotificationIconCache(str(tmp_path), store=ProjectIconStore())
    path = cache.path_for(_state())
    os.utime(path, (1, 1))
    before = os.stat(path).st_size

    assert cache.path_for(_state()) == path
    assert os.stat(path).st_size == before
    assert os.stat(path).st_mtime > 1  # touched for LRU pruning


def test_prunes_least_recently_used_files(tmp_path):
    cache = NotificationIconCache(str(tmp_path), store=ProjectIconStore(), max_files=2)
    a = cache.path_for(_state(repo="a"))
    os.utime(a, (1, 1))
    b = cache.path_for(_state(repo="b"))
    c = cache.path_for(_state(repo="c"))

    assert not os.path.exists(a)
    assert os.path.exists(b) and os.path.exists(c)


def test_failure_returns_none_instead_of_raising(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a dir")
    cache = NotificationIconCache(str(blocker / "icons"), store=ProjectIconStore())
    assert cache.path_for(_state()) is None
