"""Contract: console scripts, ``python -m`` entries and the Elgato backend.

Pins what shells, launchd units and the Stream Deck plugin invoke: the script
names in pyproject, ``python -m herdeck.app`` / ``herdeck.web``, their argument
parsing and error exits, the headless ``herdeck`` runtime connecting to a
bridge, and the frozen-backend entry script the Elgato plugin spawns
(``HERDECK_DECK=elgato-plugin`` IPC handshake + import self-test).
"""

from __future__ import annotations

import importlib
import json
import socket
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest
from contract_support import (
    FakeBridge,
    RuntimeProcess,
    base_env,
    pane,
    short_tmpdir,
    wait_until,
    write_config,
)

ROOT = Path(__file__).resolve().parents[1]
BACKEND_ENTRY = ROOT / "streamdeck" / "scripts" / "herdeck-backend-entry.py"
SCRIPTS = {
    "herdeck",
    "herdeck-bridge",
    "herdeck-doctor",
    "herdeck-ctl",
    "herdeck-web",
    "herdeck-service",
    "herdeck-usage",
    # the login-session usage agent for a --system bridge (README "Usage agent")
    "herdeck-usage-agent",
    "herdeck-t3-connect",
    # invoked by Claude Code / Codex hook configs (README "Subagent tracking")
    "herdeck-subagent-hook",
}


def run(args, env, *, timeout=30):
    return subprocess.run(
        [sys.executable, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_console_scripts_exist_and_resolve():
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    assert set(scripts) == SCRIPTS
    for name, target in scripts.items():
        module, _, attr = target.partition(":")
        assert callable(getattr(importlib.import_module(module), attr)), name


def test_herdeck_version_and_module_entry(tmp_path):
    env = base_env(tmp_path)
    for args in (["-m", "herdeck.app", "--version"], ["-c", "from herdeck.app import main; main()",
                                                     "--version"]):
        result = run(args, env)
        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith("herdeck ")


def test_herdeck_without_config_or_herdr_exits_2(tmp_path):
    env = base_env(tmp_path)
    env["HERDECK_DECK"] = "fake"
    result = run(["-m", "herdeck.app"], env)
    assert result.returncode == 2
    assert "No herdr socket at" in result.stderr
    assert "no [[servers]] config" in result.stderr


def test_herdeck_web_argument_parsing(tmp_path):
    env = base_env(tmp_path)
    result = run(["-m", "herdeck.web", "run", "--help"], env)
    assert result.returncode == 0
    for option in (
        "--host",
        "--port",
        "--base-path",
        "--public-origin",
        "--frame-ancestor",
        "--allow-query-token",
    ):
        assert option in result.stdout
    result = run(["-m", "herdeck.web", "url"], env)
    assert result.returncode != 0
    assert "legacy query-token URL is disabled" in result.stderr

    result = run(["-m", "herdeck.web", "run", "--host", "8.8.8.8"], env)
    assert result.returncode != 0
    assert "HERDECK_WEB_BIND must be loopback or a Tailscale address" in result.stderr

    result = run(["-m", "herdeck.web", "run", "--base-path", "herdeck/"], env)
    assert result.returncode != 0
    assert "web base path must look like /herdeck" in result.stderr

    bad = dict(env, HERDECK_MOCK="1", HERDECK_WEB_FRAME_ANCESTORS="https://embedder.example")
    result = run(["-m", "herdeck.web", "run"], bad)
    assert result.returncode != 0
    assert "frame ancestors require an explicit HTTPS public origin" in result.stderr

    token_file = tmp_path / "web-token"
    result = run(
        [
            "-m",
            "herdeck.web",
            "url",
            "--allow-query-token",
            "--token-file",
            str(token_file),
            "--host",
            "127.0.0.1",
            "--port",
            "8801",
            "--base-path",
            "/herdeck",
        ],
        env,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == (
        f"http://127.0.0.1:8801/herdeck/?token={token_file.read_text()}"
    )


def test_headless_herdeck_connects_to_the_configured_bridge(tmp_path):
    bridge = FakeBridge([pane("p1", "blocked")])
    env = base_env(tmp_path)
    config = write_config(tmp_path / "config.toml", bridge)
    env.update({"HERDECK_CONFIG": str(config), "HERDECK_DECK": "fake"})
    proc = RuntimeProcess(["-m", "herdeck.app"], env, cwd=tmp_path)
    try:
        bridge.wait_connected()
        bridge.wait_message(lambda m: m.get("type") == "list")
        assert bridge.auth_headers[0] == "Bearer contract-bridge-token"
        assert proc.proc.poll() is None
    finally:
        proc.stop()
        bridge.close()


def _ipc(sock_path, token, *, timeout=15.0):
    deadline = time.monotonic() + timeout
    while True:
        try:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(sock_path))
            break
        except OSError:
            client.close()
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)
    client.settimeout(timeout)
    client.sendall(
        (json.dumps({"type": "hello", "protocol_version": 1, "token": token}) + "\n").encode()
    )
    reader = client.makefile("rb")
    return client, reader


@pytest.mark.parametrize(
    "entry",
    [["-m", "herdeck.app"], [str(BACKEND_ENTRY)]],
    ids=["module", "frozen-entry-script"],
)
def test_elgato_plugin_backend_serves_its_ipc_socket(tmp_path, entry):
    bridge = FakeBridge([pane("p1", "blocked"), pane("p2", "working")])
    sockdir = short_tmpdir()
    sock_path = sockdir / "e.sock"
    env = base_env(tmp_path)
    config = write_config(tmp_path / "config.toml", bridge)
    env.update(
        {
            "HERDECK_CONFIG": str(config),
            "HERDECK_DECK": "elgato-plugin",
            "HERDECK_ELGATO_SOCK": str(sock_path),
            "HERDECK_ELGATO_TOKEN": "elgato-secret",
        }
    )
    proc = RuntimeProcess(entry, env, cwd=tmp_path)
    try:
        bridge.wait_connected()
        wait_until(sock_path.exists, message="elgato socket")
        client, reader = _ipc(sock_path, "wrong")
        assert json.loads(reader.readline()) == {
            "type": "error",
            "reason": "auth or version mismatch",
        }
        client.close()
        client, reader = _ipc(sock_path, "elgato-secret")
        assert json.loads(reader.readline()) == {"type": "ready"}
        client.sendall(
            (
                json.dumps(
                    {
                        "type": "slots",
                        "slots": [{"instanceId": "k0", "coord": {"col": 0, "row": 0}}],
                    }
                )
                + "\n"
            ).encode()
        )
        render = json.loads(reader.readline())
        assert render["type"] == "render"
        assert set(render["keys"]["k0"]) == {"image", "title"}
        client.close()
    finally:
        proc.stop()
        bridge.close()
        sock_path.unlink(missing_ok=True)
        sockdir.rmdir()


def test_elgato_backend_entry_import_selftest(tmp_path):
    env = base_env(tmp_path)
    env["HERDECK_SELFTEST"] = "imports"
    result = run([str(BACKEND_ENTRY)], env, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


def test_elgato_plugin_without_ipc_env_fails_loudly(tmp_path):
    bridge = FakeBridge([])
    env = base_env(tmp_path)
    config = write_config(tmp_path / "config.toml", bridge)
    env.update({"HERDECK_CONFIG": str(config), "HERDECK_DECK": "elgato-plugin"})
    try:
        result = run(["-m", "herdeck.app"], env)
        assert result.returncode != 0
        assert "HERDECK_ELGATO_SOCK and HERDECK_ELGATO_TOKEN must both be set" in result.stderr
    finally:
        bridge.close()


def test_service_installer_renders_the_web_unit(tmp_path):
    env = base_env(tmp_path)
    result = run(["-m", "herdeck.service", "--help"], env)
    assert result.returncode == 0
    from herdeck.service import ServiceConfig, _program_and_environment

    config = ServiceConfig(
        kind="web",
        home=tmp_path,
        python="/venv/bin/python3",
        bind="100.86.178.12",
        port=8801,
        base_path="/herdeck",
        public_origin="https://cockpit.example.test",
        frame_ancestors=("https://cockpit.example.test",),
    )
    arguments, environment = _program_and_environment(config)
    assert arguments == ["/venv/bin/python3", "-m", "herdeck.web", "run"]
    assert environment["HERDECK_WEB_BIND"] == "100.86.178.12"
    assert environment["HERDECK_WEB_PORT"] == "8801"
    assert environment["HERDECK_WEB_BASE_PATH"] == "/herdeck"
    assert environment["HERDECK_WEB_PUBLIC_ORIGIN"] == "https://cockpit.example.test"
    assert environment["HERDECK_WEB_FRAME_ANCESTORS"] == "https://cockpit.example.test"
