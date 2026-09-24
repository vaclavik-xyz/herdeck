"""scripts/deploy-host.sh: syntax, usage, and the remote (declare -f) payload."""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/deploy-host.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _run(*args):
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True)


def test_script_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_help_documents_flags():
    result = _run("--help")
    assert result.returncode == 0
    for flag in ("--role", "--host", "--ref", "--root", "--venv", "--rollback"):
        assert flag in result.stdout


def test_rejects_unknown_role():
    result = _run("--role", "web")
    assert result.returncode == 2
    assert "--role must be runtime or bridge" in result.stderr


def test_remote_payload_survives_declare_f():
    # Over ssh the target half is shipped as `declare -f target_main`; bash
    # mangles heredocs followed by `|| ...` when re-printing a function, so the
    # re-printed body must still parse.
    body = subprocess.run(
        ["sed", "-n", "/^target_main() {/,/^}/p", str(SCRIPT)],
        capture_output=True, text=True, check=True,
    ).stdout
    printed = subprocess.run(
        ["bash", "-c", 'eval "$BODY" && declare -f target_main'],
        env={"BODY": body, "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, check=True,
    )
    assert "target_main" in printed.stdout
    assert subprocess.run(["bash", "-n"], input=printed.stdout, text=True).returncode == 0
