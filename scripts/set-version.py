#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"
VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _match(path: Path, pattern: str) -> str:
    match = re.search(pattern, path.read_text(), flags=re.MULTILINE)
    if not match:
        raise RuntimeError(f"could not read version from {path.relative_to(ROOT)}")
    return match.group(1)


def _replace(path: Path, pattern: str, replacement: str) -> None:
    text = path.read_text()
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise RuntimeError(f"could not update version in {path.relative_to(ROOT)}")
    path.write_text(updated)


def versions() -> dict[str, str]:
    desktop_package = _read_json(ROOT / "desktop/package.json")
    desktop_lock = _read_json(ROOT / "desktop/package-lock.json")
    tauri = _read_json(ROOT / "desktop/src-tauri/tauri.conf.json")
    streamdeck_package = _read_json(ROOT / "streamdeck/package.json")
    streamdeck_lock = _read_json(ROOT / "streamdeck/package-lock.json")
    streamdeck_manifest = _read_json(
        ROOT / "streamdeck/xyz.vaclavik.herdeck.sdPlugin/manifest.json"
    )
    return {
        "src/herdeck/__init__.py": _match(
            ROOT / "src/herdeck/__init__.py", r'^__version__ = "([^"]+)"$'
        ),
        "pyproject.toml": _match(ROOT / "pyproject.toml", r'^version = "([^"]+)"$'),
        "desktop/package.json": desktop_package["version"],
        "desktop/package-lock.json": desktop_lock["version"],
        "desktop/package-lock.json#root": desktop_lock["packages"][""]["version"],
        "desktop/src-tauri/Cargo.toml": _match(
            ROOT / "desktop/src-tauri/Cargo.toml", r'^version = "([^"]+)"$'
        ),
        "desktop/src-tauri/Cargo.lock": _match(
            ROOT / "desktop/src-tauri/Cargo.lock",
            r'\[\[package\]\]\nname = "herdeck-desktop"\nversion = "([^"]+)"',
        ),
        "desktop/src-tauri/tauri.conf.json": tauri["version"],
        "streamdeck/package.json": streamdeck_package["version"],
        "streamdeck/package-lock.json": streamdeck_lock["version"],
        "streamdeck/package-lock.json#root": streamdeck_lock["packages"][""]["version"],
        "streamdeck/xyz.vaclavik.herdeck.sdPlugin/manifest.json": streamdeck_manifest[
            "Version"
        ].removesuffix(".0"),
    }


def set_version(version: str) -> None:
    if not VERSION_RE.fullmatch(version):
        raise SystemExit("version must be stable SemVer in MAJOR.MINOR.PATCH form")
    VERSION_FILE.write_text(version + "\n")
    _replace(
        ROOT / "src/herdeck/__init__.py",
        r'^__version__ = "[^"]+"$',
        f'__version__ = "{version}"',
    )
    _replace(ROOT / "pyproject.toml", r'^version = "[^"]+"$', f'version = "{version}"')
    _replace(
        ROOT / "desktop/src-tauri/Cargo.toml",
        r'^version = "[^"]+"$',
        f'version = "{version}"',
    )
    _replace(
        ROOT / "desktop/src-tauri/Cargo.lock",
        r'(\[\[package\]\]\nname = "herdeck-desktop"\nversion = ")[^"]+',
        rf"\g<1>{version}",
    )

    for relative in ("desktop/package.json", "desktop/package-lock.json"):
        path = ROOT / relative
        payload = _read_json(path)
        payload["version"] = version
        if relative.endswith("package-lock.json"):
            payload["packages"][""]["version"] = version
        _write_json(path, payload)

    tauri_path = ROOT / "desktop/src-tauri/tauri.conf.json"
    tauri = _read_json(tauri_path)
    tauri["version"] = version
    _write_json(tauri_path, tauri)

    for relative in ("streamdeck/package.json", "streamdeck/package-lock.json"):
        path = ROOT / relative
        payload = _read_json(path)
        payload["version"] = version
        if relative.endswith("package-lock.json"):
            payload["packages"][""]["version"] = version
        _write_json(path, payload)

    manifest_path = ROOT / "streamdeck/xyz.vaclavik.herdeck.sdPlugin/manifest.json"
    manifest = _read_json(manifest_path)
    manifest["Version"] = f"{version}.0"
    _write_json(manifest_path, manifest)


def check() -> int:
    expected = VERSION_FILE.read_text().strip()
    mismatches = {path: value for path, value in versions().items() if value != expected}
    if mismatches:
        for path, value in mismatches.items():
            print(f"{path}: {value} != {expected}")
        return 1
    print(f"all manifests match {expected}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Synchronize Herdeck release versions")
    parser.add_argument("version", nargs="?", help="new stable MAJOR.MINOR.PATCH version")
    parser.add_argument("--check", action="store_true", help="verify without writing")
    args = parser.parse_args(argv)
    if args.check:
        if args.version:
            parser.error("--check does not accept a version")
        return check()
    if not args.version:
        parser.error("provide a version or use --check")
    set_version(args.version)
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
