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

    assert "brew install cairo" in macos_job
    assert 'CAIRO_LIB="$(brew --prefix cairo)/lib"' in freeze_step
    assert 'export DYLD_FALLBACK_LIBRARY_PATH="$CAIRO_LIB' in freeze_step
    assert '.venv/bin/python -c "import cairosvg"' in freeze_step
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
