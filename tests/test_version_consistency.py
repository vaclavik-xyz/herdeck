from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
VERSIONED_FILES = (
    "VERSION",
    "scripts/set-version.py",
    "src/herdeck/__init__.py",
    "pyproject.toml",
    "desktop/package.json",
    "desktop/package-lock.json",
    "desktop/src-tauri/Cargo.toml",
    "desktop/src-tauri/Cargo.lock",
    "desktop/src-tauri/tauri.conf.json",
    "streamdeck/package.json",
    "streamdeck/package-lock.json",
    "streamdeck/xyz.vaclavik.herdeck.sdPlugin/manifest.json",
)


def test_all_release_manifests_match_canonical_version():
    result = subprocess.run(
        [sys.executable, "scripts/set-version.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_release_workflow_rejects_a_tag_that_does_not_match_version():
    workflow = (ROOT / ".github/workflows/release.yml").read_text()

    assert 'test "$GITHUB_REF_NAME" = "v$(cat VERSION)"' in workflow


def test_version_script_synchronizes_a_real_version_bump(tmp_path):
    for relative in VERSIONED_FILES:
        source = ROOT / relative
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    result = subprocess.run(
        [sys.executable, "scripts/set-version.py", "9.8.7"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "VERSION").read_text() == "9.8.7\n"
    assert 'version = "9.8.7"' in (tmp_path / "pyproject.toml").read_text()
    manifest = (tmp_path / "streamdeck/xyz.vaclavik.herdeck.sdPlugin/manifest.json")
    assert '"Version": "9.8.7.0"' in manifest.read_text()


# --- no source file may hard-code the version -------------------------------
#
# The manifest check above is an allowlist: it proves the twelve files it knows
# about agree, and is blind to a version written by hand anywhere else. That
# blind spot shipped — the desktop settings sidebar said "v0.1.1" through the
# whole 0.2.0 release. These pin the scan that closes it.


def _run(*args, cwd=ROOT):
    return subprocess.run(
        [sys.executable, "scripts/set-version.py", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def test_check_rejects_a_source_file_that_hard_codes_the_current_version(tmp_path):
    for relative in VERSIONED_FILES:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
    component = tmp_path / "desktop/src/Widget.svelte"
    component.parent.mkdir(parents=True, exist_ok=True)
    version = (ROOT / "VERSION").read_text().strip()
    component.write_text(f"<span>v{version}</span>\n")

    result = _run("--check", cwd=tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "desktop/src/Widget.svelte:1" in result.stdout
    assert f"hard-codes {version}" in result.stdout


def test_check_ignores_versions_that_are_not_ours(tmp_path):
    for relative in VERSIONED_FILES:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
    component = tmp_path / "desktop/src/Widget.svelte"
    component.parent.mkdir(parents=True, exist_ok=True)
    # herdr's version, an IP, and our version embedded in a longer number: none
    # of these is a claim about what this build is.
    version = (ROOT / "VERSION").read_text().strip()
    component.write_text(
        f"<!-- needs herdr 0.7.4 -->\n<span>127.0.0.1</span>\n<span>10.{version}</span>\n"
    )

    result = _run("--check", cwd=tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr


def test_check_ignores_version_fixtures_in_tests(tmp_path):
    for relative in VERSIONED_FILES:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
    version = (ROOT / "VERSION").read_text().strip()
    spec = tmp_path / "desktop/src/Widget.test.ts"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text(f'const current = "{version}";\n')

    result = _run("--check", cwd=tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr


def test_bump_refuses_to_strand_a_literal_of_the_outgoing_version(tmp_path):
    for relative in VERSIONED_FILES:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
    outgoing = (ROOT / "VERSION").read_text().strip()
    component = tmp_path / "desktop/src/Widget.svelte"
    component.parent.mkdir(parents=True, exist_ok=True)
    component.write_text(f"<span>v{outgoing}</span>\n")

    result = _run("9.8.7", cwd=tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert f"hard-codes the outgoing {outgoing}" in result.stdout
    # A refused bump must not leave the tree half-written.
    assert (tmp_path / "VERSION").read_text().strip() == outgoing
