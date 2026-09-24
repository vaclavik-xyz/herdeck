"""herdeck-service maintenance tooling: --env, restart, status --json,
`install bridge --managed` (managed venv + release install + managed.json),
and the frozen sidecar's `service` subcommand dispatch."""

import hashlib
import importlib.util
import json
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _config(tmp_path, kind="runtime", **overrides):
    from herdeck.service import ServiceConfig

    values = {
        "kind": kind,
        "home": tmp_path,
        "python": "/opt/herdeck/venv/bin/python",
        "bind": "127.0.0.1",
        "port": 0 if kind == "runtime" else 8788,
        "socket_path": tmp_path / ".config/herdr/herdr.sock",
        "token_file": tmp_path / ".config/herdeck/bridge-token",
        "uid": 501,
    }
    values.update(overrides)
    return ServiceConfig(**values)


# --- --env ------------------------------------------------------------------------


def test_env_pairs_land_in_the_launch_agent_and_systemd_unit(tmp_path):
    from herdeck.service import render_launch_agent, render_systemd_unit

    env = (("HERDECK_D200_STANDARD_WRITER", "1"), ("HERDECK_T3_DESKTOP_READ_STATE", "1"))
    plist = plistlib.loads(render_launch_agent(_config(tmp_path, extra_env=env)))
    assert plist["EnvironmentVariables"] == {
        "HERDECK_D200_STANDARD_WRITER": "1",
        "HERDECK_T3_DESKTOP_READ_STATE": "1",
    }
    unit = render_systemd_unit(_config(tmp_path, extra_env=env, platform="linux"))
    assert 'Environment="HERDECK_D200_STANDARD_WRITER=1"' in unit


@pytest.mark.parametrize(
    "item",
    [
        "HERDECK_TOKEN=abc",
        "my_secret=1",
        "DB_PASSWORD=x",
        "NOEQUALS",
        "1BAD=x",
        "A B=x",
        "HERDECK_RUNTIME_MANAGED=1",
    ],
)
def test_env_rejects_secrets_and_malformed_names(item):
    from herdeck.service import parse_env_args

    with pytest.raises(ValueError):
        parse_env_args([item])


def test_env_rejects_duplicates_and_keys_the_unit_sets(tmp_path):
    from herdeck.service import parse_env_args, render_launch_agent

    with pytest.raises(ValueError, match="twice"):
        parse_env_args(["A=1", "A=2"])
    bridge = _config(tmp_path, kind="bridge", extra_env=(("HERDECK_PORT", "9"),))
    with pytest.raises(ValueError, match="set by herdeck-service"):
        render_launch_agent(bridge)


def test_cli_env_flag_reaches_the_config(tmp_path):
    from herdeck.service import _config_from_args, _parser

    args = _parser().parse_args(
        ["install", "runtime", "--home", str(tmp_path), "--python", "/p",
         "--env", "HERDECK_D200_STANDARD_WRITER=1", "--env", "X=a=b"]
    )
    config = _config_from_args(args)
    assert config.extra_env == (("HERDECK_D200_STANDARD_WRITER", "1"), ("X", "a=b"))

    args = _parser().parse_args(
        ["install", "runtime", "--home", str(tmp_path), "--python", "/p", "--env", "T3_TOKEN=x"]
    )
    with pytest.raises(SystemExit, match="secrets never go"):
        _config_from_args(args)


# --- restart / status --json ------------------------------------------------------------


def test_restart_uses_kickstart_or_systemctl(tmp_path):
    from herdeck.service import restart_service

    calls = []
    run = lambda command: calls.append(command) or 0  # noqa: E731
    assert restart_service(_config(tmp_path), runner=run) == 0
    restart_service(_config(tmp_path, kind="bridge"), runner=run)
    restart_service(_config(tmp_path, kind="bridge", system=True), runner=run)
    restart_service(_config(tmp_path, platform="linux"), runner=run)
    assert calls == [
        ["launchctl", "kickstart", "-k", "gui/501/dev.herdeck.runtime"],
        ["launchctl", "kickstart", "-k", "user/501/dev.herdeck.bridge"],
        ["sudo", "launchctl", "kickstart", "-k", "system/dev.herdeck.bridge"],
        ["systemctl", "--user", "restart", "herdeck-runtime.service"],
    ]


def test_status_info_reports_install_program_and_from_app(tmp_path):
    from herdeck.service import install_service, service_status_info

    config = _config(tmp_path)
    info = service_status_info(config, runner=lambda command: 1)
    assert info == {
        "kind": "runtime",
        "label": "dev.herdeck.runtime",
        "installed": False,
        "unit_path": str(tmp_path / "Library/LaunchAgents/dev.herdeck.runtime.plist"),
        "loaded": False,
        "program": None,
        "from_app": False,
    }
    app = tmp_path / "Applications/herdeck.app"
    binary = app / "Contents/Resources/herdeck-deckapp/herdeck-deckapp"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    install_service(_config(tmp_path, from_app=app), runner=lambda command: 0)
    probes = []
    info = service_status_info(config, runner=lambda command: probes.append(command) or 0)
    assert info["installed"] and info["loaded"] and info["from_app"]
    assert info["program"] == str(binary)
    assert probes == [["launchctl", "print", "gui/501/dev.herdeck.runtime"]]


def test_status_info_on_systemd(tmp_path):
    from herdeck.service import install_service, service_status_info

    config = _config(tmp_path, platform="linux")
    install_service(config, runner=lambda command: 0)
    info = service_status_info(config, runner=lambda command: 0)
    assert info["label"] == "herdeck-runtime.service"
    assert info["program"] == "/opt/herdeck/venv/bin/python"


def test_cli_status_json_and_restart(monkeypatch, tmp_path, capsys):
    import herdeck.service as service

    monkeypatch.setattr(service, "_quiet_run", lambda command: 1)
    service.main(["status", "runtime", "--home", str(tmp_path), "--json"])
    assert json.loads(capsys.readouterr().out)["installed"] is False

    calls = []
    monkeypatch.setattr(service, "_run", lambda command: calls.append(command) or 0)
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(SystemExit) as exit_info:
        service.main(["restart", "runtime", "--home", str(tmp_path), "--uid", "501"])
    assert exit_info.value.code == 0
    assert calls == [["launchctl", "kickstart", "-k", "gui/501/dev.herdeck.runtime"]]


# --- managed bridge install -----------------------------------------------------------


class FakeTools:
    """Plays uv/pip/python for ManagedInstaller: creates the venv interpreter
    on `venv`, records installs, answers the version check."""

    def __init__(self, reported_version="0.9.1", install_code=0):
        self.calls = []
        self.reported_version = reported_version
        self.install_code = install_code

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        if "venv" in argv:
            venv = Path(argv[-1] if "--python" not in argv else argv[argv.index("venv") + 2])
            (venv / "bin").mkdir(parents=True, exist_ok=True)
            (venv / "bin/python").write_text("")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "install" in argv:
            return subprocess.CompletedProcess(argv, self.install_code, "", "ERROR: no wheel")
        if argv[1:2] == ["-c"]:
            return subprocess.CompletedProcess(argv, 0, self.reported_version + "\n", "")
        raise AssertionError(argv)


def _downloader(files):
    def download(url, dest):
        name = url.rsplit("/", 1)[1]
        if name not in files:
            raise OSError(f"404 {url}")
        dest.write_bytes(files[name])

    return download


WHEEL = b"PK fake wheel"
WHEEL_NAME = "herdeck-0.9.1-py3-none-any.whl"


def _installer(tools, files, uv=None):
    from herdeck.managed import ManagedInstaller

    return ManagedInstaller(
        runner=tools,
        download=_downloader(files),
        which=lambda name: uv if name == "uv" else None,
        base_python="/usr/bin/python3",
        log=lambda message: None,
    )


def test_managed_install_verifies_the_wheel_hash_and_writes_the_marker(tmp_path):
    from herdeck.managed import read_marker, wheel_url

    tools = FakeTools()
    sums = f"{hashlib.sha256(WHEEL).hexdigest()}  {WHEEL_NAME}\n".encode()
    venv = tmp_path / "venv"
    marker = _installer(tools, {WHEEL_NAME: WHEEL, "SHA256SUMS": sums}).install(venv, "v0.9.1")

    assert tools.calls[0] == ["/usr/bin/python3", "-m", "venv", str(venv)]
    install = tools.calls[1]
    assert install[:5] == [str(venv / "bin/python"), "-m", "pip", "install", "--upgrade"]
    assert install[5].endswith(WHEEL_NAME)
    assert tools.calls[2] == [str(venv / "bin/python"), "-c",
                              "import herdeck; print(herdeck.__version__)"]
    assert marker["version"] == "0.9.1" and marker["verified"] is True
    assert marker["source"] == wheel_url("0.9.1")
    assert wheel_url("0.9.1") == (
        "https://github.com/vaclavik-xyz/herdeck/releases/download/v0.9.1/" + WHEEL_NAME
    )
    assert read_marker(venv) == marker


def test_managed_install_uses_uv_when_present(tmp_path):
    tools = FakeTools()
    venv = tmp_path / "venv"
    _installer(tools, {WHEEL_NAME: WHEEL}, uv="/bin/uv").install(venv, "0.9.1")
    assert tools.calls[0] == ["/bin/uv", "venv", "--seed", str(venv), "--python", "/usr/bin/python3"]
    assert tools.calls[1][:6] == ["/bin/uv", "pip", "install", "--python",
                                  str(venv / "bin/python"), "--upgrade"]


def test_managed_install_falls_back_to_git_without_a_release_wheel(tmp_path):
    from herdeck.managed import read_marker

    tools = FakeTools()
    venv = tmp_path / "venv"
    _installer(tools, {}).install(venv, "0.9.1")
    assert tools.calls[1][-1] == "git+https://github.com/vaclavik-xyz/herdeck@v0.9.1"
    assert read_marker(venv)["source"] == "git+https://github.com/vaclavik-xyz/herdeck@v0.9.1"
    assert read_marker(venv)["verified"] is False


def test_managed_install_refuses_a_tampered_wheel(tmp_path):
    from herdeck.managed import ManagedInstallError

    sums = f"{'0' * 64}  {WHEEL_NAME}\n".encode()
    with pytest.raises(ManagedInstallError, match="hash mismatch"):
        _installer(FakeTools(), {WHEEL_NAME: WHEEL, "SHA256SUMS": sums}).install(
            tmp_path / "venv", "0.9.1"
        )


def test_managed_install_failures_leave_no_marker(tmp_path):
    from herdeck.managed import ManagedInstallError, read_marker

    venv = tmp_path / "venv"
    with pytest.raises(ManagedInstallError, match="no wheel"):
        _installer(FakeTools(install_code=1), {WHEEL_NAME: WHEEL}).install(venv, "0.9.1")
    with pytest.raises(ManagedInstallError, match="reports herdeck 0.9.0"):
        _installer(FakeTools(reported_version="0.9.0"), {WHEEL_NAME: WHEEL}).install(venv, "0.9.1")
    assert read_marker(venv) is None


@pytest.mark.parametrize("version", ["latest", "0.9.1; rm -rf /", "../0.9", ""])
def test_managed_install_rejects_non_release_versions(tmp_path, version):
    from herdeck.managed import ManagedInstallError

    with pytest.raises(ManagedInstallError):
        _installer(FakeTools(), {}).install(tmp_path / "venv", version)


def test_install_bridge_managed_points_the_unit_at_the_venv(tmp_path):
    from herdeck.service import install_managed_bridge

    tools = FakeTools()
    calls = []
    path = install_managed_bridge(
        _config(tmp_path, kind="bridge"),
        "0.9.1",
        installer=_installer(tools, {WHEEL_NAME: WHEEL}),
        runner=lambda command: calls.append(command) or 0,
        token_factory=lambda: "generated",
    )
    venv = tmp_path / ".local/share/herdeck/bridge-venv"
    plist = plistlib.loads(path.read_bytes())
    assert plist["ProgramArguments"] == [str(venv / "bin/python"), "-m", "herdeck.bridge"]
    assert json.loads((venv / "managed.json").read_text())["version"] == "0.9.1"
    assert calls == [["launchctl", "bootstrap", "user/501", str(path)]]


def test_install_bridge_managed_stops_before_the_unit_on_failure(tmp_path):
    from herdeck.service import install_managed_bridge

    calls = []
    with pytest.raises(SystemExit, match="managed bridge install failed"):
        install_managed_bridge(
            _config(tmp_path, kind="bridge"),
            "0.9.1",
            installer=_installer(FakeTools(install_code=1), {}),
            runner=lambda command: calls.append(command) or 0,
        )
    assert calls == []
    assert not (tmp_path / "Library/LaunchAgents/dev.herdeck.bridge.plist").exists()


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["install", "runtime", "--managed"], "only for the bridge"),
        (["install", "bridge", "--version", "0.9.1"], "--version needs --managed"),
        (["install", "bridge", "--managed", "--python", "/p"], "drop --python"),
    ],
)
def test_cli_managed_flag_validation(tmp_path, argv, message):
    from herdeck.service import _config_from_args, _parser

    args = _parser().parse_args(argv + ["--home", str(tmp_path)])
    with pytest.raises(SystemExit, match=message):
        _config_from_args(args)


def test_cli_install_bridge_managed_calls_the_installer(monkeypatch, tmp_path, capsys):
    import herdeck.service as service

    seen = {}

    def fake_install(config, version):
        seen["config"], seen["version"] = config, version
        return tmp_path / "unit.plist"

    monkeypatch.setattr(service, "install_managed_bridge", fake_install)
    service.main(["install", "bridge", "--managed", "--version", "0.9.1", "--home", str(tmp_path)])
    assert seen["version"] == "0.9.1" and seen["config"].kind == "bridge"
    assert capsys.readouterr().out.strip() == str(tmp_path / "unit.plist")


# --- frozen sidecar -------------------------------------------------------------------------


def test_frozen_install_needs_from_app_or_managed(monkeypatch, tmp_path):
    from herdeck.service import _config_from_args, _parser

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    args = _parser().parse_args(["install", "bridge", "--home", str(tmp_path)])
    with pytest.raises(SystemExit, match="runtime --from-app"):
        _config_from_args(args)
    args = _parser().parse_args(["install", "runtime", "--home", str(tmp_path), "--from-app", "/x.app"])
    assert _config_from_args(args).from_app == Path("/x.app").resolve()


def test_frozen_from_app_defaults_to_the_running_bundle(monkeypatch, tmp_path):
    import herdeck.service as service

    binary = tmp_path / "My herdeck.app/Contents/Resources/herdeck-deckapp/herdeck-deckapp"
    binary.parent.mkdir(parents=True)
    binary.write_text("")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(binary))
    assert service._default_from_app() == (tmp_path / "My herdeck.app").resolve()
    monkeypatch.setattr(sys, "executable", str(tmp_path / "elsewhere"))
    assert service._default_from_app() == service.DEFAULT_APP


def _runtime_entry():
    spec = importlib.util.spec_from_file_location(
        "runtime_entry", ROOT / "desktop/scripts/runtime-entry.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_entry_dispatches_the_service_subcommand(monkeypatch):
    import herdeck.runtime
    import herdeck.service

    seen = []
    monkeypatch.setattr(herdeck.service, "main", lambda argv: seen.append(("service", argv)))
    monkeypatch.setattr(herdeck.runtime, "main", lambda: seen.append(("runtime",)) or 0)
    entry = _runtime_entry()
    assert entry._dispatch(["service", "install", "runtime", "--from-app"]) == 0
    assert entry._dispatch([]) == 0
    assert seen == [("service", ["install", "runtime", "--from-app"]), ("runtime",)]


def test_sidecar_spec_bundles_the_service_cli():
    spec = (ROOT / "desktop/herdeck-deckapp.spec").read_text()
    for module in ('"herdeck.service"', '"herdeck.managed"', '"herdeck.deckapp.maintenance"'):
        assert module in spec
