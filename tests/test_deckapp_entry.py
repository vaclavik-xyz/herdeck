import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENTRY = REPO / "desktop" / "scripts" / "deckapp-entry.py"


def test_entry_emits_a_discovery_line():
    assert ENTRY.exists(), "deckapp entry script missing"
    env = {**os.environ, "PYTHONPATH": str(REPO / "src"), "HERDECK_MOCK": "1"}
    proc = subprocess.Popen(
        [sys.executable, str(ENTRY)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    try:
        line = proc.stdout.readline()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    d = json.loads(line)
    assert set(d) >= {"url", "host", "port", "token", "source"}
    assert d["source"] == "mock"
