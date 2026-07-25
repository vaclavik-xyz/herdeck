import json
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_dev_build_has_an_isolated_unsigned_macos_artifact():
    workflow = (ROOT / ".github/workflows/dev-build.yml").read_text()
    config = json.loads(
        (ROOT / "desktop/src-tauri/tauri.dev.conf.json").read_text()
    )

    assert "workflow_dispatch:" in workflow
    assert "runs-on: macos-14" in workflow
    assert "HERDECK_BUILD_CHANNEL: dev" in workflow
    assert "HERDECK_BUILD_SHA: ${{ github.sha }}" in workflow
    assert "bash desktop/scripts/build-sidecar.sh" in workflow
    assert "bash desktop/scripts/smoke-sidecar.sh" in workflow
    assert "--bundles app" in workflow
    assert "--config src-tauri/tauri.dev.conf.json" in workflow
    assert "HERDECK_SELFTEST=imports" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert "retention-days: 14" in workflow
    assert "APPLE_SIGNING_IDENTITY" not in workflow
    assert "notarytool" not in workflow
    assert "gh release" not in workflow

    assert config["productName"] == "Herdeck Dev"
    assert config["identifier"] == "xyz.vaclavik.herdeck.desktop.dev"
    assert config["bundle"]["createUpdaterArtifacts"] is False


def test_readme_explains_how_dev_builds_differ_from_releases():
    readme = (ROOT / "README.md").read_text()

    assert "### macOS dev build" in readme
    assert "Herdeck Dev.app" in readme
    assert "herdeck-dev-macos-arm64" in readme
    assert "~/.config/herdeck-dev" in readme
    assert "does not use the stable updater" in readme
