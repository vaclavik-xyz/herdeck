#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

TARGETS = {
    "darwin-aarch64": ("herdeck-macos", "*.app.tar.gz"),
    "linux-x86_64": ("herdeck-linux-x86_64/appimage", "*.AppImage.tar.gz"),
    "linux-aarch64": ("herdeck-linux-arm64/appimage", "*.AppImage.tar.gz"),
}


def _single_artifact(root: Path, relative: str, pattern: str) -> Path:
    matches = sorted((root / relative).glob(pattern))
    if len(matches) != 1:
        raise SystemExit(
            f"expected one {relative}/{pattern} updater artifact, found {len(matches)}"
        )
    signature = Path(f"{matches[0]}.sig")
    if not signature.is_file() or not signature.read_text().strip():
        raise SystemExit(f"missing updater signature for {matches[0]}")
    return matches[0]


def build_manifest(*, root: Path, repo: str, tag: str, version: str) -> dict:
    if tag != f"v{version}":
        raise SystemExit(f"tag {tag!r} does not match version {version!r}")
    platforms = {}
    for target, (relative, pattern) in TARGETS.items():
        artifact = _single_artifact(root, relative, pattern)
        signature = Path(f"{artifact}.sig").read_text().strip()
        filename = quote(artifact.name)
        platforms[target] = {
            "signature": signature,
            "url": f"https://github.com/{repo}/releases/download/{quote(tag)}/{filename}",
        }
    return {
        "version": version,
        "notes": f"Herdeck {version}",
        "pub_date": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "platforms": platforms,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a unified signed Tauri update manifest")
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--version-file", type=Path, default=Path("VERSION"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    version = args.version_file.read_text().strip()
    manifest = build_manifest(
        root=args.artifacts,
        repo=args.repo,
        tag=args.tag,
        version=version,
    )
    args.output.write_text(json.dumps(manifest, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
