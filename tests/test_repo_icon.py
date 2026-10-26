from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_repo_favicon_matches_app_icon_master():
    # favicon.png exists only so tools that discover a project icon at conventional
    # repo paths (Herdeck's own project_icon_discovery, T3 Code) show the app icon;
    # the Tauri icon set under desktop/src-tauri/icons/ is the master.
    master = ROOT / "desktop" / "src-tauri" / "icons" / "128x128@2x.png"
    copy = ROOT / "favicon.png"
    assert copy.read_bytes() == master.read_bytes(), (
        "copy desktop/src-tauri/icons/128x128@2x.png to favicon.png"
    )
