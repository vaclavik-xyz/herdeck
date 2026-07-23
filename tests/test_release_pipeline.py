import base64
import json
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
    freeze_step = workflow.split(
        "- name: Freeze + smoke the bundled sidecar", maxsplit=1
    )[1].split("- uses: dtolnay/rust-toolchain@stable", maxsplit=1)[0]
    spec = (ROOT / "desktop/herdeck-deckapp.spec").read_text()
    build_script = (ROOT / "desktop/scripts/build-sidecar.sh").read_text()

    assert "APPLE_SIGNING_IDENTITY:" in freeze_step
    assert "APPLE_TEAM_ID: ${{ secrets.APPLE_TEAM_ID }}" in freeze_step
    assert 'os.environ.get("APPLE_SIGNING_IDENTITY")' in spec
    assert "codesign_identity=CODESIGN_IDENTITY" in spec
    assert "verify-macos-sidecar-signing.sh" in build_script
    assert "--force --options runtime --timestamp" in build_script
    assert 'PYTHON_LINK="$DIST/herdeck-deckapp/_internal/Python"' in build_script
    assert 'Python.framework" -depth -delete' in build_script
