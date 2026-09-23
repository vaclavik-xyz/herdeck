import pytest


@pytest.fixture(autouse=True)
def _isolated_notification_icons(tmp_path, monkeypatch):
    """Banner icon PNGs (notify_icons) land in a per-test dir, never ~/.cache."""
    monkeypatch.setattr(
        "herdeck.notify_icons.default_dir", lambda: str(tmp_path / "notification-icons")
    )
