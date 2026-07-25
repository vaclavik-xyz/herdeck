from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]


def _signed(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"artifact")
    Path(f"{path}.sig").write_text(f"signature-for-{path.name}")


def test_generates_one_manifest_for_macos_and_both_linux_architectures(tmp_path):
    artifacts = tmp_path / "artifacts"
    _signed(artifacts / "herdeck-macos/herdeck.app.tar.gz")
    _signed(
        artifacts
        / "herdeck-linux-x86_64/appimage/herdeck_0.1.0_amd64.AppImage.tar.gz"
    )
    _signed(
        artifacts
        / "herdeck-linux-arm64/appimage/herdeck_0.1.0_arm64.AppImage.tar.gz"
    )
    version = tmp_path / "VERSION"
    version.write_text("0.1.0\n")
    output = tmp_path / "latest.json"

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/generate-update-manifest.py"),
            "--artifacts",
            str(artifacts),
            "--repo",
            "vaclavik-xyz/herdeck",
            "--tag",
            "v0.1.0",
            "--version-file",
            str(version),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads(output.read_text())
    assert manifest["version"] == "0.1.0"
    assert set(manifest["platforms"]) == {
        "darwin-aarch64",
        "linux-x86_64",
        "linux-aarch64",
    }
    assert manifest["platforms"]["darwin-aarch64"]["url"].endswith(
        "/herdeck.app.tar.gz"
    )
    assert manifest["platforms"]["linux-aarch64"]["signature"].startswith(
        "signature-for-"
    )


def test_rejects_a_tag_that_does_not_match_version(tmp_path):
    version = tmp_path / "VERSION"
    version.write_text("0.1.0\n")
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/generate-update-manifest.py"),
            "--artifacts",
            str(tmp_path),
            "--repo",
            "vaclavik-xyz/herdeck",
            "--tag",
            "v0.2.0",
            "--version-file",
            str(version),
            "--output",
            str(tmp_path / "latest.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "does not match version" in result.stderr
