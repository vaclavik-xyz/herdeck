import pytest


@pytest.fixture(autouse=True)
def _isolated_notification_icons(tmp_path, monkeypatch):
    """Banner icon PNGs (notify_icons) land in a per-test dir, never ~/.cache."""
    monkeypatch.setattr(
        "herdeck.notify_icons.default_dir", lambda: str(tmp_path / "notification-icons")
    )


@pytest.fixture(autouse=True)
def _isolated_status_since_state(tmp_path, monkeypatch):
    """The bridge's persisted status-since table never touches ~/.local/state."""
    monkeypatch.setattr(
        "herdeck.status_since.default_state_path",
        lambda name="bridge-status-since.json": str(tmp_path / "state" / name),
    )
