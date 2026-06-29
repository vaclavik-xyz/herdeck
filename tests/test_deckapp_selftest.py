# tests/test_deckapp_selftest.py
import os
import subprocess
import sys


def test_selftest_imports_exits_zero():
    # Preserve the env and prepend the repo `src` to PYTHONPATH so `-m herdeck.deckapp`
    # resolves even in a clean checkout (package not installed editable).
    repo_src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    env = {**os.environ, "HERDECK_SELFTEST": "imports"}
    env["PYTHONPATH"] = repo_src + os.pathsep + env.get("PYTHONPATH", "")
    r = subprocess.run(
        [sys.executable, "-m", "herdeck.deckapp"],
        env=env,
        capture_output=True,
        timeout=30,
    )
    assert r.returncode == 0, r.stderr.decode()
