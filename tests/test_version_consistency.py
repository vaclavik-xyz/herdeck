from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
VERSION = (ROOT / "VERSION").read_text().strip()
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


@pytest.fixture
def sandbox(tmp_path):
    """A tree holding just the versioned manifests, safe to bump and to break."""
    for relative in VERSIONED_FILES:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
    return tmp_path


def run_script(*args, cwd):
    return subprocess.run(
        [sys.executable, "scripts/set-version.py", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_all_release_manifests_match_canonical_version():
    result = run_script("--check", cwd=ROOT)

    assert result.returncode == 0, result.stdout + result.stderr


def test_release_workflow_rejects_a_tag_that_does_not_match_version():
    workflow = (ROOT / ".github/workflows/release.yml").read_text()

    assert 'test "$GITHUB_REF_NAME" = "v$(cat VERSION)"' in workflow


def test_version_script_synchronizes_a_real_version_bump(sandbox):
    result = run_script("9.8.7", cwd=sandbox)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (sandbox / "VERSION").read_text() == "9.8.7\n"
    assert 'version = "9.8.7"' in (sandbox / "pyproject.toml").read_text()
    manifest = sandbox / "streamdeck/xyz.vaclavik.herdeck.sdPlugin/manifest.json"
    assert '"Version": "9.8.7.0"' in manifest.read_text()


# --- no source file may hard-code the version -------------------------------
#
# The manifest check above is an allowlist: it proves the twelve files it knows
# about agree, and is blind to a version written by hand anywhere else. That
# blind spot shipped — the desktop settings sidebar said "v0.1.1" through the
# whole 0.2.0 release. These pin the scan that closes it.


def test_check_rejects_a_source_file_that_hard_codes_the_current_version(sandbox):
    write(sandbox, "desktop/src/Widget.svelte", f"<span>v{VERSION}</span>\n")

    result = run_script("--check", cwd=sandbox)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "desktop/src/Widget.svelte:1" in result.stdout
    assert f"hard-codes {VERSION}" in result.stdout


@pytest.mark.parametrize(
    "text",
    [
        f"const artifact = 'herdeck_{VERSION}_x64.dmg';",
        f"const artifact = 'herdeck-{VERSION}.dmg';",
        f"const artifact = 'Herdeck_{VERSION}_amd64.AppImage';",
        f"const url = 'https://example.test/v{VERSION}/herdeck.tar.gz';",
    ],
)
def test_check_rejects_a_version_written_into_a_release_artifact_name(sandbox, text):
    # The word-boundary form of this scan missed every one of these: `_` is a
    # word character, so `herdeck_0.2.0_x64` read as one long word.
    write(sandbox, "desktop/src/Widget.ts", text + "\n")

    result = run_script("--check", cwd=sandbox)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "desktop/src/Widget.ts:1" in result.stdout


@pytest.mark.parametrize(
    "text",
    [
        "// needs herdr 0.7.4",  # another project's version
        "const host = '127.0.0.1';",  # an address
        f"const other = '10.{VERSION}';",  # ours inside a longer number
        f"const other = '{VERSION}.5';",  # a four-component number
        f"const other = '{VERSION}1';",  # a longer number
    ],
)
def test_check_ignores_numbers_that_are_not_our_version(sandbox, text):
    write(sandbox, "desktop/src/Widget.ts", text + "\n")

    result = run_script("--check", cwd=sandbox)

    assert result.returncode == 0, result.stdout + result.stderr


def test_check_ignores_version_fixtures_in_tests(sandbox):
    write(sandbox, "desktop/src/Widget.test.ts", f'const current = "{VERSION}";\n')

    result = run_script("--check", cwd=sandbox)

    assert result.returncode == 0, result.stdout + result.stderr


def test_check_ignores_vendored_bundles(sandbox):
    write(sandbox, "src/herdeck/assets/web/xterm.js", f"var v='{VERSION}';\n")

    result = run_script("--check", cwd=sandbox)

    assert result.returncode == 0, result.stdout + result.stderr


def test_check_truncates_an_enormous_matching_line_around_the_match(sandbox):
    # Truncating from the start of the line would, on exactly the line this
    # guards against, print everything except the literal being reported.
    write(sandbox, "desktop/src/Widget.ts", f"// {'x' * 5000} v{VERSION} {'y' * 5000}\n")

    result = run_script("--check", cwd=sandbox)

    assert result.returncode == 1, result.stdout + result.stderr
    reported = next(li for li in result.stdout.splitlines() if "Widget.ts:1" in li)
    assert len(reported) < 200
    assert VERSION in reported.split("hard-codes", 1)[1].split(":", 1)[1]


def test_rerunning_the_current_version_repairs_manifests_despite_a_stray_literal(
    sandbox,
):
    # Re-running with the current version is the repair after a bad merge, so a
    # stray literal must not block it — check() still reports the literal after.
    write(sandbox, "desktop/src/Widget.svelte", f"<span>v{VERSION}</span>\n")
    (sandbox / "pyproject.toml").write_text(
        (sandbox / "pyproject.toml").read_text().replace(
            f'version = "{VERSION}"', 'version = "0.0.1"'
        )
    )

    result = run_script(VERSION, cwd=sandbox)

    assert f'version = "{VERSION}"' in (sandbox / "pyproject.toml").read_text()
    assert result.returncode == 1, result.stdout + result.stderr
    assert "refusing to bump" not in result.stdout
    assert f"hard-codes {VERSION}" in result.stdout


def test_bump_refuses_to_strand_a_literal_of_the_outgoing_version(sandbox):
    write(sandbox, "desktop/src/Widget.svelte", f"<span>v{VERSION}</span>\n")

    result = run_script("9.8.7", cwd=sandbox)

    assert result.returncode == 1, result.stdout + result.stderr
    assert f"hard-codes the outgoing {VERSION}" in result.stdout
    # A refused bump must not leave the tree half-written.
    assert (sandbox / "VERSION").read_text().strip() == VERSION


def test_bump_refuses_a_literal_of_the_incoming_version_before_writing(sandbox):
    # Without this, the bump rewrites twelve manifests and only then fails the
    # --check it runs itself — the opposite of the contract above.
    write(sandbox, "desktop/src/Widget.svelte", "<span>v9.8.7</span>\n")

    result = run_script("9.8.7", cwd=sandbox)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "hard-codes the incoming 9.8.7" in result.stdout
    assert (sandbox / "VERSION").read_text().strip() == VERSION
    assert f'version = "{VERSION}"' in (sandbox / "pyproject.toml").read_text()
