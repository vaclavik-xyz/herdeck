import os
import stat

from herdeck.deckapp.discovery import (
    clear_runtime_file,
    read_runtime_file,
    runtime_file_path,
    write_runtime_file,
)

INFO = {"url": "http://127.0.0.1:8800", "host": "127.0.0.1", "port": 8800, "token": "t0ken", "source": "live"}


def test_write_then_read_round_trips(tmp_path):
    p = str(tmp_path / "runtime.json")
    write_runtime_file(p, INFO)
    assert read_runtime_file(p) == INFO


def test_written_file_is_0600(tmp_path):
    p = str(tmp_path / "runtime.json")
    write_runtime_file(p, INFO)
    mode = stat.S_IMODE(os.stat(p).st_mode)
    assert mode == 0o600


def test_write_creates_missing_parent_dir(tmp_path):
    p = str(tmp_path / "nested" / "dir" / "runtime.json")
    write_runtime_file(p, INFO)
    assert read_runtime_file(p) == INFO


def test_read_missing_returns_none(tmp_path):
    assert read_runtime_file(str(tmp_path / "absent.json")) is None


def test_read_malformed_returns_none(tmp_path):
    p = tmp_path / "runtime.json"
    p.write_text("{not json")
    assert read_runtime_file(str(p)) is None


def test_clear_removes_file_and_is_idempotent(tmp_path):
    p = str(tmp_path / "runtime.json")
    write_runtime_file(p, INFO)
    clear_runtime_file(p)
    assert not os.path.exists(p)
    clear_runtime_file(p)  # second call must not raise


def test_runtime_file_path_honors_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERDECK_RUNTIME_DIR", str(tmp_path))
    assert runtime_file_path() == str(tmp_path / "runtime.json")


def test_runtime_file_path_default(monkeypatch):
    monkeypatch.delenv("HERDECK_RUNTIME_DIR", raising=False)
    assert runtime_file_path().endswith("/.cache/herdeck/runtime.json")
