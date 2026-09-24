import base64
import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_desktop_updater_uses_signed_https_github_channel():
    config = json.loads((ROOT / "desktop/src-tauri/tauri.conf.json").read_text())

    assert config["bundle"]["createUpdaterArtifacts"] is True
    updater = config["plugins"]["updater"]
    assert updater["endpoints"] == [
        "https://github.com/vaclavik-xyz/herdeck/releases/latest/download/latest.json"
    ]
    public_key = base64.b64decode(updater["pubkey"]).decode()
    assert "minisign public key" in public_key


def test_tag_workflow_builds_macos_updater_and_publishes_after_all_builds():
    workflow = (ROOT / ".github/workflows/release.yml").read_text()

    assert "build-macos:" in workflow
    assert "runs-on: macos-14" in workflow
    assert "tauri-apps/tauri-action@v0" in workflow
    assert "TAURI_SIGNING_PRIVATE_KEY:" in workflow
    assert "APPLE_SIGNING_IDENTITY:" in workflow
    assert "APPLE_ID: ${{ secrets.APPLE_ID }}" in workflow
    assert "APPLE_PASSWORD: ${{ secrets.APPLE_PASSWORD }}" in workflow
    assert "APPLE_TEAM_ID: ${{ secrets.APPLE_TEAM_ID }}" in workflow
    assert "releaseDraft: true" in workflow
    assert "*.AppImage*" in workflow
    assert "name: herdeck-macos" in workflow
    assert "scripts/generate-update-manifest.py" in workflow
    assert "dist/latest.json" in workflow
    assert "publish-release:" in workflow
    assert "needs: [build-linux, build-macos]" in workflow
    assert "if: startsWith(github.ref, 'refs/tags/v')" in workflow
    assert 'gh release upload "$GITHUB_REF_NAME"' in workflow
    assert "dist/herdeck-linux-x86_64/appimage/*" in workflow
    assert "dist/herdeck-linux-arm64/appimage/*" in workflow
    assert "dist/herdeck-macos/*" in workflow
    assert 'gh release edit "$GITHUB_REF_NAME"' in workflow
    assert "--draft=false" in workflow


def test_macos_release_signs_and_verifies_the_frozen_sidecar():
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    macos_job = workflow.split("build-macos:", maxsplit=1)[1].split(
        "publish-release:", maxsplit=1
    )[0]
    freeze_step = workflow.split(
        "- name: Freeze + smoke the bundled sidecar", maxsplit=1
    )[1].split("- uses: dtolnay/rust-toolchain@stable", maxsplit=1)[0]
    spec = (ROOT / "desktop/herdeck-deckapp.spec").read_text()
    build_script = (ROOT / "desktop/scripts/build-sidecar.sh").read_text()

    # resvg-py is a self-contained wheel: no native cairo library to install.
    assert "cairo" not in macos_job
    assert '.venv/bin/python -c "import resvg_py"' in freeze_step
    assert "xcrun notarytool submit" in macos_job
    assert "xcrun stapler staple" in macos_job
    assert "xcrun stapler validate" in macos_job
    assert "spctl -a -t open --context context:primary-signature" in macos_job
    assert 'gh release upload "$GITHUB_REF_NAME" "$dmg"' in macos_job
    assert "--clobber" in macos_job
    assert "APPLE_SIGNING_IDENTITY:" in freeze_step
    assert "APPLE_TEAM_ID: ${{ secrets.APPLE_TEAM_ID }}" in freeze_step
    assert 'os.environ.get("APPLE_SIGNING_IDENTITY")' in spec
    assert "codesign_identity=CODESIGN_IDENTITY" in spec
    assert "verify-macos-sidecar-signing.sh" in build_script
    assert "--force --options runtime --timestamp" in build_script
    assert 'PYTHON_LINK="$DIST/herdeck-deckapp/_internal/Python"' in build_script
    assert 'Python.framework" -depth -delete' in build_script


def test_desktop_bundle_contains_the_converged_d200_runtime():
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    spec = (ROOT / "desktop/herdeck-deckapp.spec").read_text()
    entry = (ROOT / "desktop/scripts/runtime-entry.py").read_text()
    smoke = (ROOT / "desktop/scripts/smoke-sidecar.sh").read_text()
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert '.venv/bin/pip install -e ".[packaging,deck]"' in workflow
    assert "runtime-entry.py" in spec
    assert "from herdeck.runtime import main" in entry
    assert '"herdeck.driver.d200"' in spec
    assert '"strmdck"' in spec
    assert '"hid"' in spec
    excludes = spec.split("excludes=", maxsplit=1)[1].split("noarchive=", maxsplit=1)[0]
    assert '"strmdck"' not in excludes
    assert '"hid"' not in excludes
    assert "frozen D200 runtime imports reachable" in smoke
    assert 'token=<redacted>' in smoke
    assert 'echo "discovery: $DISCOVERY"' not in smoke

    deck_dependencies = pyproject["project"]["optional-dependencies"]["deck"]
    assert "strmdck" in deck_dependencies
    assert "hidapi" in deck_dependencies


def _ci_job(workflow: str, name: str) -> str:
    """The text of one top-level job in a workflow (up to the next job)."""
    body = workflow.split(f"\n  {name}:\n", maxsplit=1)[1]
    lines = []
    for line in body.splitlines():
        if line.startswith("  ") and not line.startswith("   ") and line.strip().endswith(":"):
            break  # next job
        lines.append(line)
    return "\n".join(lines)


def test_ci_freezes_and_smokes_both_bundles_on_prs():
    """Freezing used to run only on release tags / manual dev builds, so a broken
    spec or missing hidden import surfaced at release time. CI now freezes both
    PyInstaller bundles and runs their import selftests, path-filtered."""
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    changes = _ci_job(workflow, "freeze-changes")
    job = _ci_job(workflow, "freeze-smoke")

    for path in (
        "src/**",
        "pyproject.toml",
        "desktop/herdeck-deckapp.spec",
        "desktop/scripts/**",
        "streamdeck/herdeck-backend.spec",
        "streamdeck/scripts/**",
    ):
        assert f"- '{path}'" in changes
    assert "dorny/paths-filter@v3" in changes
    assert "needs: freeze-changes" in job
    assert "if: needs.freeze-changes.outputs.freeze == 'true'" in job

    assert "runs-on: ubuntu-latest" in job
    assert "cache: pip" in job
    # Same Linux system packages as the release build that freezes the sidecar.
    release = (ROOT / ".github/workflows/release.yml").read_text()
    for pkg in ("libwebkit2gtk-4.1-dev", "libgtk-3-dev", "patchelf", "desktop-file-utils"):
        assert pkg in job and pkg in release
    assert '.venv/bin/pip install -e ".[packaging,deck]"' in job

    # Desktop sidecar: freeze desktop/herdeck-deckapp.spec + smoke (which runs
    # HERDECK_SELFTEST=imports on the frozen binary).
    assert "bash desktop/scripts/build-sidecar.sh" in job
    assert "bash desktop/scripts/smoke-sidecar.sh" in job
    assert "$DESKTOP/herdeck-deckapp.spec" in (ROOT / "desktop/scripts/build-sidecar.sh").read_text()
    assert "HERDECK_SELFTEST=imports" in (ROOT / "desktop/scripts/smoke-sidecar.sh").read_text()

    # Elgato backend: freeze its spec and run the entry's import selftest.
    assert "-m PyInstaller streamdeck/herdeck-backend.spec" in job
    assert "HERDECK_SELFTEST=imports build/elgato-dist/herdeck-backend/herdeck-backend" in job
    entry = (ROOT / "streamdeck/scripts/herdeck-backend-entry.py").read_text()
    assert 'os.environ.get("HERDECK_SELFTEST") == "imports"' in entry
