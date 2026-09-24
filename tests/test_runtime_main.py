"""herdeck.runtime.main() as a real process: started, stopped by SIGTERM.

The deck app and the D200 sink are replaced by recorders (no bridge, no USB,
no HTTP port), everything else — signal handlers, runtime.json discovery,
the shutdown order — is the real entry point.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Runs inside the child. Each close prints one JSON event line, and records
# whether runtime.json still existed at that moment.
_HARNESS = r"""
import json, os, sys
from herdeck import runtime
from herdeck.deckapp.discovery import runtime_file_path

def event(name):
    print(json.dumps({"event": name, "runtime_json": os.path.exists(runtime_file_path())}), flush=True)

class App:
    host, port, token, source_name, slots, config = "127.0.0.1", 8123, "tok", "mock", 13, None
    def add_sink(self, sink): pass
    def press(self, index): pass
    def close(self): event("app.close")

class Sink:
    def close(self): event("sink.close")

runtime.create_app = lambda host, port: App()
runtime._build_d200_sink = lambda app, driver_factory: Sink()
sys.exit(runtime.main())
"""


def _env(tmp_path):
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("HERDECK_SELFTEST", "HERDECK_RUNTIME_MANAGED", "HERDECK_PARENT_WATCH")
    }
    env["HERDECK_RUNTIME_DIR"] = str(tmp_path)
    env["PYTHONPATH"] = os.path.join(ROOT, "src") + os.pathsep + env.get("PYTHONPATH", "")
    return env


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_runtime_main_publishes_discovery_and_stops_cleanly_on_signal(tmp_path, sig):
    runtime_json = tmp_path / "runtime.json"
    proc = subprocess.Popen(
        [sys.executable, "-c", _HARNESS],
        env=_env(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # The first stdout line is the discovery fallback; runtime.json is
        # written before it is printed.
        discovery = json.loads(proc.stdout.readline())
        assert discovery == {
            "url": "http://127.0.0.1:8123",
            "host": "127.0.0.1",
            "port": 8123,
            "token": "tok",
            "source": "mock",
        }
        assert json.loads(runtime_json.read_text()) == discovery
        assert (runtime_json.stat().st_mode & 0o777) == 0o600

        proc.send_signal(sig)
        out, err = proc.communicate(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    assert proc.returncode == 0, err
    events = [json.loads(line) for line in out.splitlines() if line.strip()]
    # runtime.json goes first (an attaching window must not find a dying
    # runtime), then the D200 sink, then the app.
    assert events == [
        {"event": "sink.close", "runtime_json": False},
        {"event": "app.close", "runtime_json": False},
    ]
    assert not runtime_json.exists()


def test_managed_runtime_main_leaves_discovery_alone(tmp_path):
    """A runtime spawned by the desktop host (HERDECK_RUNTIME_MANAGED=1) never
    writes or deletes the shared runtime.json."""
    runtime_json = tmp_path / "runtime.json"
    runtime_json.write_text('{"url": "http://127.0.0.1:1"}')
    env = _env(tmp_path)
    env["HERDECK_RUNTIME_MANAGED"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-c", _HARNESS],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert json.loads(proc.stdout.readline())["port"] == 8123
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode == 0, err
    assert [json.loads(line)["event"] for line in out.splitlines()] == ["sink.close", "app.close"]
    assert runtime_json.read_text() == '{"url": "http://127.0.0.1:1"}'
