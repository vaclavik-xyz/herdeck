#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"
VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")

# versions() is an allowlist: it proves the manifests it knows about agree with
# VERSION, but it cannot see a version written by hand anywhere else. That blind
# spot is not hypothetical — the desktop settings sidebar hard-coded "v0.1.1" and
# went on saying so through the 0.2.0 release, because a string inside a .svelte
# file was outside both this script and the test that backs it.
#
# So scan the source trees too, and fail on any literal of the *current* version
# that is not sanctioned below. Matching only the current version is deliberate:
# it keeps IP addresses and references to other projects' versions (herdr 0.7.4,
# herdr 0.8.2) out of the results, and it fires on the commit that introduces the
# literal, which is the moment worth catching.
SOURCE_ROOTS = ("src", "desktop/src", "desktop/src-tauri/src", "streamdeck/src")
SOURCE_SUFFIXES = frozenset({".py", ".ts", ".js", ".svelte", ".rs"})
# Tests state versions as fixtures ("update 0.2.0 is available", current 0.1.0);
# those are arbitrary values, not claims about what this build is.
TEST_MARKERS = (".test.", ".spec.")
SKIP_DIRECTORIES = frozenset({"node_modules", "build", "dist", "target", "__pycache__"})
# Third-party code vendored into our own tree: xterm and its addons ship as
# single-line minified bundles, neither ours to edit nor sane to scan.
SKIP_RELATIVE_DIRECTORIES = ("src/herdeck/assets/web",)
# Sanctioned homes for a literal version inside a source tree. Each one must also
# be covered by versions(), so it cannot drift.
VERSION_LITERAL_ALLOWLIST = frozenset({"src/herdeck/__init__.py"})


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

    previous = VERSION_FILE.read_text().strip()
    # --check only ever looks for the current version, so it catches a literal on
    # the commit that introduces it but goes blind the moment the version moves
    # past it. A bump is where that blindness begins, so pre-flight BOTH
    # directions here, before a single file is written: the outgoing scan stops a
    # literal being stranded where --check would no longer look for it, and the
    # incoming one stops a bump that would rewrite twelve manifests and only then
    # fail the --check it runs itself.
    # Only on a real bump. Re-running with the current version is the repair
    # after a bad merge, where writing the manifests is the whole point — there
    # the incoming version IS the current one, and check() reports any stray
    # literal afterwards without blocking the repair.
    blockers = []
    if previous != version:
        blockers.append((f"the outgoing {previous}", stray_version_literals(previous)))
        blockers.append((f"the incoming {version}", stray_version_literals(version)))
    reported = [(label, hits) for label, hits in blockers if hits]
    if reported:
        for label, hits in reported:
            for path, number, line in hits:
                print(f"{path}:{number}: hard-codes {label}: {line}")
        raise SystemExit(
            f"refusing to bump {previous} -> {version}: nothing was written, so fix "
            f"the lines above and run it again"
        )

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


def _source_files():
    for relative in SOURCE_ROOTS:
        root = ROOT / relative
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.suffix not in SOURCE_SUFFIXES or not path.is_file():
                continue
            relative_path = path.relative_to(ROOT)
            if SKIP_DIRECTORIES.intersection(relative_path.parts):
                continue
            posix = relative_path.as_posix()
            if any(posix.startswith(f"{d}/") for d in SKIP_RELATIVE_DIRECTORIES):
                continue
            if any(marker in path.name for marker in TEST_MARKERS):
                continue
            yield path


def _excerpt(line: str, at: int, before: int = 40, after: int = 80) -> str:
    """A short window around `at`, so the reported line is still evidence.

    Cutting the first N characters instead would, on the very long line this
    exists for, reliably print everything except the literal being complained
    about.
    """
    text = line.strip()
    if len(text) <= before + after:
        return text  # already fits; windowing would only lose the beginning
    start = max(0, at - before)
    end = min(len(line), at + after)
    text = line[start:end].strip()
    return ("..." if start else "") + text + ("..." if end < len(line) else "")


def stray_version_literals(expected: str) -> list[tuple[str, int, str]]:
    r"""Source lines that hard-code `expected` outside the sanctioned manifests.

    The optional `v` matters: the literal this check exists for was written
    `<span>v0.1.1</span>`, and a lookbehind placed after the prefix would treat
    the `v` as the preceding word character and miss it.

    The boundaries are digits and dots rather than word characters, because the
    other place a version gets hand-written here is a release artifact —
    `herdeck_0.2.0_x64.dmg`, `Herdeck_0.2.0_amd64.AppImage`, a download URL — and
    `\w` would swallow the surrounding `_` as part of a longer word and skip it.
    The lookahead rejects only a following digit or dot-digit, so `0.2.01` and the
    address `10.0.2.0` are not versions while `herdeck-0.2.0.dmg` still is.
    """
    pattern = re.compile(rf"(?<![\d.])v?{re.escape(expected)}(?!\d|\.\d)")
    found: list[tuple[str, int, str]] = []
    for path in _source_files():
        relative = path.relative_to(ROOT).as_posix()
        if relative in VERSION_LITERAL_ALLOWLIST:
            continue
        for number, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            match = pattern.search(line)
            if match:
                found.append((relative, number, _excerpt(line, match.start())))
    return found


def check() -> int:
    expected = VERSION_FILE.read_text().strip()
    mismatches = {path: value for path, value in versions().items() if value != expected}
    strays = stray_version_literals(expected)
    if mismatches or strays:
        for path, value in mismatches.items():
            print(f"{path}: {value} != {expected}")
        for path, number, line in strays:
            print(f"{path}:{number}: hard-codes {expected}: {line}")
        if strays:
            print(
                "\nRead the version from a manifest instead of writing it here — the"
                "\nfrontend injects __APP_VERSION__ from package.json, and Python has"
                "\nherdeck.__version__. Add the path to VERSION_LITERAL_ALLOWLIST and"
                "\nversions() only if it genuinely has to be a literal."
            )
        return 1
    print(f"all manifests match {expected}, and no source file hard-codes it")
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
