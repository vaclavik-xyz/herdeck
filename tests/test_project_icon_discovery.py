import os

import pytest

from herdeck import project_icon_discovery as disc
from herdeck.project_icon_discovery import (
    MAX_ICON_BYTES,
    ProjectIconIndex,
    find_icon_file,
    find_repo_root,
    icon_hash,
    mime_for_path,
    read_icon_file,
)

PNG = b"\x89PNG\r\n\x1a\nfake-png"


def _repo(tmp_path, name="shop", git_file=False):
    repo = tmp_path / name
    repo.mkdir(parents=True)
    if git_file:  # a linked worktree has a .git FILE
        (repo / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")
    else:
        (repo / ".git").mkdir()
    return repo


def _put(repo, rel, data=PNG):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_icon_hash_is_16_hex_of_sha256():
    import hashlib

    assert icon_hash(b"abc") == hashlib.sha256(b"abc").hexdigest()[:16]


def test_mime_for_path():
    assert mime_for_path("a/favicon.PNG") == "image/png"
    assert mime_for_path("favicon.ico") == "image/x-icon"
    assert mime_for_path("icon.svg") == "image/svg+xml"
    assert mime_for_path("icon.gif") is None


@pytest.mark.parametrize("git_file", [False, True])
def test_find_repo_root_detects_git_dir_or_file(tmp_path, git_file):
    repo = _repo(tmp_path, git_file=git_file)
    assert find_repo_root(str(repo / "src" / "deep"), home=str(tmp_path)) == str(repo)


def test_find_repo_root_walk_is_bounded(tmp_path):
    repo = _repo(tmp_path)
    deep = repo.joinpath(*[f"d{i}" for i in range(12)])  # 12 levels below the repo
    assert find_repo_root(str(deep), home=str(tmp_path)) is None
    assert find_repo_root(str(deep.parent), home=str(tmp_path)) == str(repo)


def test_find_repo_root_stops_at_home_parent(tmp_path):
    users = tmp_path / "users"
    (users / ".git").mkdir(parents=True)  # a repo AT home's parent is never used
    home = users / "me"
    assert find_repo_root(str(home / "proj"), home=str(home)) is None


@pytest.mark.parametrize("start", ["", "relative/path"])
def test_find_repo_root_rejects_empty_or_relative(start):
    assert find_repo_root(start) is None


def test_candidate_priority_raster_then_ico_then_svg(tmp_path):
    repo = _repo(tmp_path)
    _put(repo, "favicon.svg", b"<svg/>")
    _put(repo, "public/favicon.ico", b"ico-bytes")
    assert find_icon_file(str(repo))[0].endswith(os.path.join("public", "favicon.ico"))
    _put(repo, "app/icon.png")
    assert find_icon_file(str(repo))[0].endswith(os.path.join("app", "icon.png"))
    _put(repo, "favicon.png")
    assert find_icon_file(str(repo))[0] == os.path.realpath(repo / "favicon.png")


def test_size_cap_and_empty_files_are_skipped(tmp_path):
    repo = _repo(tmp_path)
    _put(repo, "favicon.png", b"x" * (MAX_ICON_BYTES + 1))
    _put(repo, "public/favicon.png", b"")
    _put(repo, "favicon.ico", b"ico")
    assert find_icon_file(str(repo))[0].endswith("favicon.ico")


def test_directory_named_like_a_candidate_is_skipped(tmp_path):
    repo = _repo(tmp_path)
    (repo / "favicon.png").mkdir()
    assert find_icon_file(str(repo)) is None


def test_symlink_escaping_the_repo_is_rejected(tmp_path):
    repo = _repo(tmp_path)
    outside = tmp_path / "secret.png"
    outside.write_bytes(PNG)
    (repo / "favicon.png").symlink_to(outside)
    assert find_icon_file(str(repo)) is None
    real = _put(repo, "branding/logo.png")
    (repo / "public").mkdir()
    (repo / "public" / "favicon.png").symlink_to(real)
    assert find_icon_file(str(repo))[0] == os.path.realpath(real)


def test_read_icon_file_enforces_extension_and_size(tmp_path):
    ok = tmp_path / "a.png"
    ok.write_bytes(PNG)
    assert read_icon_file(str(ok)) == ("image/png", PNG)
    big = tmp_path / "b.png"
    big.write_bytes(b"x" * (MAX_ICON_BYTES + 1))
    assert read_icon_file(str(big)) is None
    txt = tmp_path / "c.txt"
    txt.write_bytes(b"hello")
    assert read_icon_file(str(txt)) is None
    assert read_icon_file(str(tmp_path / "missing.png")) is None


def test_index_hashes_and_serves_the_blob(tmp_path):
    repo = _repo(tmp_path)
    _put(repo, "public/favicon.png")
    index = ProjectIconIndex(home=str(tmp_path))
    h = index.hash_for(cwd=str(repo / "src"))
    assert h == icon_hash(PNG)
    blob = index.blob(h)
    assert (blob.hash, blob.mime, blob.data) == (h, "image/png", PNG)


def test_index_prefers_the_worktree_path(tmp_path):
    repo = _repo(tmp_path)
    _put(repo, "favicon.ico", b"ico")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    index = ProjectIconIndex(home=str(tmp_path))
    assert index.hash_for(worktree_path=str(repo), cwd=str(elsewhere)) == icon_hash(b"ico")


def test_hash_for_outside_any_repo_is_empty(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _put(home, "favicon.png")  # an icon in $HOME itself is not a project icon
    index = ProjectIconIndex(home=str(home))
    assert index.hash_for(cwd=str(home)) == ""
    assert index.hash_for(cwd=str(home / "deleted" / "dir")) == ""
    assert index.hash_for() == ""


def test_index_rehashes_after_the_restat_interval(tmp_path):
    now = [100.0]
    repo = _repo(tmp_path)
    icon = _put(repo, "favicon.png", b"first")
    index = ProjectIconIndex(clock=lambda: now[0], home=str(tmp_path), restat_interval=60.0)
    first = index.hash_for(cwd=str(repo))
    icon.write_bytes(b"second-version")
    now[0] += 30.0
    assert index.hash_for(cwd=str(repo)) == first  # cached inside the window
    now[0] += 31.0
    second = index.hash_for(cwd=str(repo))
    assert second == icon_hash(b"second-version")
    assert index.blob(first) is None  # the stale blob is dropped
    assert index.blob(second).data == b"second-version"


def test_invalidate_forces_an_immediate_restat(tmp_path):
    repo = _repo(tmp_path)
    icon = _put(repo, "favicon.png", b"first")
    index = ProjectIconIndex(clock=lambda: 0.0, home=str(tmp_path))
    index.hash_for(cwd=str(repo))
    icon.write_bytes(b"changed!!")
    index.invalidate()
    assert index.hash_for(cwd=str(repo)) == icon_hash(b"changed!!")


def test_discovery_errors_never_raise(tmp_path, monkeypatch):
    repo = _repo(tmp_path)

    def boom(root):
        raise PermissionError("denied")

    monkeypatch.setattr(disc, "find_icon_file", boom)
    assert ProjectIconIndex(home=str(tmp_path)).hash_for(cwd=str(repo)) == ""


# --- fallback for a workspace folder that is not itself a repo ---------------


def _home_with_folder(tmp_path, name="diktato"):
    home = tmp_path / "home"
    folder = home / "projects" / name
    folder.mkdir(parents=True)
    return home, folder


def test_non_repo_folder_uses_its_own_favicon(tmp_path):
    home, folder = _home_with_folder(tmp_path)
    _put(folder, "favicon.png", b"parent-icon")
    child = _repo(folder, "app")
    _put(child, "favicon.png", b"child-icon")
    index = ProjectIconIndex(home=str(home))
    assert index.hash_for(cwd=str(folder)) == icon_hash(b"parent-icon")


def test_non_repo_folder_falls_back_to_child_repos_in_name_order(tmp_path):
    home, folder = _home_with_folder(tmp_path)
    app = _repo(folder, "dtt-app")
    cloud = _repo(folder, "dtt-cloud")
    _put(cloud, "src/app/icon.png", b"cloud-icon")
    index = ProjectIconIndex(home=str(home))
    assert index.hash_for(cwd=str(folder)) == icon_hash(b"cloud-icon")
    _put(app, "public/favicon.ico", b"app-icon")  # "dtt-app" sorts first
    index = ProjectIconIndex(home=str(home))
    assert index.hash_for(cwd=str(folder)) == icon_hash(b"app-icon")


def test_child_repo_detected_by_git_file(tmp_path):
    home, folder = _home_with_folder(tmp_path)
    wt = _repo(folder, "wt", git_file=True)
    _put(wt, "favicon.png", b"wt-icon")
    assert ProjectIconIndex(home=str(home)).hash_for(cwd=str(folder)) == icon_hash(b"wt-icon")


def test_non_repo_child_dir_is_ignored(tmp_path):
    home, folder = _home_with_folder(tmp_path)
    _put(folder / "notes", "favicon.png", b"notes-icon")  # no .git
    assert ProjectIconIndex(home=str(home)).hash_for(cwd=str(folder)) == ""
    repo = _repo(folder, "zeta")
    _put(repo, "favicon.png", b"zeta-icon")
    assert ProjectIconIndex(home=str(home)).hash_for(cwd=str(folder)) == icon_hash(b"zeta-icon")


def test_no_fallback_at_home_or_its_ancestors(tmp_path):
    home, _folder = _home_with_folder(tmp_path)
    _put(home, "favicon.png", b"home-icon")
    repo = _repo(home, "proj")
    _put(repo, "favicon.png", b"proj-icon")
    index = ProjectIconIndex(home=str(home))
    assert index.hash_for(cwd=str(home)) == ""
    assert index.hash_for(cwd=str(tmp_path)) == ""  # home's parent
    assert index.hash_for(cwd=os.path.sep) == ""


def test_child_scan_is_capped(tmp_path):
    home, folder = _home_with_folder(tmp_path)
    for i in range(disc.MAX_FALLBACK_CHILDREN):
        (folder / f"a{i:03d}").mkdir()  # plain dirs still count toward the cap
    late = _repo(folder, "zz")
    _put(late, "favicon.png", b"late")
    assert ProjectIconIndex(home=str(home)).hash_for(cwd=str(folder)) == ""
    (folder / "a000").rmdir()
    assert ProjectIconIndex(home=str(home)).hash_for(cwd=str(folder)) == icon_hash(b"late")


def test_fallback_symlink_escape_is_rejected(tmp_path):
    home, folder = _home_with_folder(tmp_path)
    outside = tmp_path / "secret.png"
    outside.write_bytes(PNG)
    (folder / "favicon.png").symlink_to(outside)
    child = _repo(folder, "app")
    sibling = _put(folder, "shared/logo.png", b"sibling")  # inside the folder, outside the child
    (child / "favicon.png").symlink_to(sibling)
    (folder / "linked").symlink_to(_repo(tmp_path, "elsewhere"))  # symlinked child skipped
    _put(tmp_path / "elsewhere", "favicon.png", b"elsewhere")
    assert ProjectIconIndex(home=str(home)).hash_for(cwd=str(folder)) == ""


def test_fallback_restat_picks_up_a_new_parent_favicon(tmp_path):
    now = [0.0]
    home, folder = _home_with_folder(tmp_path)
    child = _repo(folder, "app")
    _put(child, "favicon.png", b"child")
    index = ProjectIconIndex(clock=lambda: now[0], home=str(home), restat_interval=60.0)
    first = index.hash_for(cwd=str(folder))
    assert first == icon_hash(b"child")
    _put(folder, "favicon.png", b"parent")
    now[0] += 30.0
    assert index.hash_for(cwd=str(folder)) == first
    now[0] += 31.0
    assert index.hash_for(cwd=str(folder)) == icon_hash(b"parent")
    assert index.blob(first) is None


def test_fallback_folder_gaining_an_icon_from_nothing(tmp_path):
    now = [0.0]
    home, folder = _home_with_folder(tmp_path)
    index = ProjectIconIndex(clock=lambda: now[0], home=str(home))
    assert index.hash_for(cwd=str(folder)) == ""
    _put(folder, "favicon.png", b"new")
    now[0] += 61.0
    assert index.hash_for(cwd=str(folder)) == icon_hash(b"new")


def test_pane_inside_a_repo_does_not_use_the_parent_folder(tmp_path):
    home, folder = _home_with_folder(tmp_path)
    _put(folder, "favicon.png", b"parent")
    repo = _repo(folder, "app")  # repo without an icon
    assert ProjectIconIndex(home=str(home)).hash_for(cwd=str(repo / "src")) == ""
