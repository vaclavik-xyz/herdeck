import asyncio
import importlib.util
from pathlib import Path

from herdeck.driver.base import TileView


def _load_e2e_module(monkeypatch):
    def fake_run(coro):
        coro.close()
        return 0

    monkeypatch.setattr(asyncio, "run", fake_run)
    path = Path(__file__).resolve().parents[1] / "scripts" / "e2e_verify.py"
    spec = importlib.util.spec_from_file_location("e2e_verify_for_test", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except SystemExit:
        pass
    return module


def test_verify_capture_uses_explicit_connection_state(monkeypatch):
    module = _load_e2e_module(monkeypatch)
    tiles = [TileView(12, "+ New", "green")]

    ok, message = module._verify_capture(
        tiles=tiles,
        frames_seen=1,
        connected=False,
    )

    assert ok is False
    assert "not connected" in message
