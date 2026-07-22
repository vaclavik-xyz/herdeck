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
