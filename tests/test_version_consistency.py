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
