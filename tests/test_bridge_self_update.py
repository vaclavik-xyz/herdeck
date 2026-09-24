"""Bridge self-update (self_update.py + the bridge's `update` message).

No real pip, network or process exit: the installer, the downloader and the
exit seam are all fakes."""

import asyncio
import contextlib
import hashlib
import json
from pathlib import Path

import pytest
import websockets

import herdeck.bridge as bridge_mod
from herdeck import __version__
from herdeck import self_update as su
from herdeck.bridge import StubHerdr, _serve_connection

TARGET = "7.8.9"
WHEEL = su.wheel_name(TARGET)
WHEEL_BYTES = b"PK\x03\x04 fake wheel"


def _sums(data=WHEEL_BYTES, name=WHEEL):
    return f"{hashlib.sha256(data).hexdigest()}  {name}\nabc  other.tar.gz\n".encode()


# --- managed-environment probe -------------------------------------------------


def _venv(tmp_path, marker=None):
    venv = tmp_path / "bridge-venv"
    (venv / "bin").mkdir(parents=True)
    pkg = venv / "lib" / "site-packages" / "herdeck"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    if marker is not None:
        (venv / su.MARKER_NAME).write_text(
            marker if isinstance(marker, str) else json.dumps(marker)
        )
    return venv, pkg / "__init__.py"


def _probe(venv, pkg_init, **kw):
    args = dict(
        prefix=str(venv),
        base_prefix="/usr",
        executable=str(venv / "bin" / "python"),
        package_file=str(pkg_init),
        editable=lambda: False,
    )
    args.update(kw)
    return su.probe_managed_env(**args)


def test_probe_accepts_a_managed_venv(tmp_path):
    venv, pkg = _venv(tmp_path, {"version": "1.0.0", "source": "wheel"})
    env, reason = _probe(venv, pkg)
    assert reason == ""
    assert env.prefix == venv and env.marker["source"] == "wheel"
    assert env.python == str(venv / "bin" / "python")


def test_probe_reads_the_marker_from_sys_prefix_by_default(tmp_path, monkeypatch):
    """The documented M1 contract: Path(sys.prefix) / "managed.json"."""
    venv, pkg = _venv(tmp_path, {"version": "1.0.0", "source": "wheel"})
    monkeypatch.setattr(su.sys, "prefix", str(venv))
    monkeypatch.setattr(su.sys, "base_prefix", "/usr")
    monkeypatch.setattr(su.sys, "executable", str(venv / "bin" / "python"))
    env, reason = su.probe_managed_env(package_file=str(pkg), editable=lambda: False)
    assert env is not None, reason


@pytest.mark.parametrize(
    "marker, kw, reason",
    [
        (None, {}, "no managed.json marker"),
        ("{not json", {}, "unreadable managed.json"),
        ("[1]", {}, "malformed managed.json"),
        ({"prefix": "/elsewhere"}, {}, "different environment"),
        ({}, {"base_prefix": "SELF"}, "does not run from a virtualenv"),
        ({}, {"executable": "/usr/bin/python3"}, "interpreter is not inside"),
        ({}, {"package_file": "/src/herdeck/src/herdeck/__init__.py"}, "source checkout"),
        ({}, {"editable": lambda: True}, "editable install"),
    ],
)
def test_probe_refuses_anything_but_a_managed_install(tmp_path, marker, kw, reason):
    venv, pkg = _venv(tmp_path, marker)
    if kw.get("base_prefix") == "SELF":
        kw["base_prefix"] = str(venv)
    env, why = _probe(venv, pkg, **kw)
    assert env is None and reason in why


def test_probe_accepts_a_marker_that_names_this_venv(tmp_path):
    venv, pkg = _venv(tmp_path, {})
    (venv / su.MARKER_NAME).write_text(json.dumps({"prefix": str(venv)}))
    assert _probe(venv, pkg)[0] is not None


def test_version_validation_is_strict():
    for good in ("1.2.3", "0.10.0", "1.2.3rc1", "1.2.3.post1", "1.2.3.dev4"):
        assert su.valid_version(good), good
    for bad in ("", "1.2", "v1.2.3", "1.2.3; rm -rf /", "1.2.3 ", "../1.2.3", "1.2.3/x", 3, None):
        assert not su.valid_version(bad), bad


def test_parse_sums_finds_the_named_file():
    text = _sums().decode()
    assert su.parse_sums(text, WHEEL) == hashlib.sha256(WHEEL_BYTES).hexdigest()
    assert su.parse_sums(f"{'a' * 64} *{WHEEL}\n", WHEEL) == "a" * 64  # binary-mode marker
    assert su.parse_sums("garbage\n", WHEEL) is None


def test_release_urls():
    assert su.asset_url(TARGET, WHEEL) == (
        f"https://github.com/vaclavik-xyz/herdeck/releases/download/v{TARGET}/"
        f"herdeck-{TARGET}-py3-none-any.whl"
    )
    assert su.git_source(TARGET) == f"git+https://github.com/vaclavik-xyz/herdeck@v{TARGET}"


# --- the updater with fake network + installer ---------------------------------


class Fakes:
    def __init__(self, tmp_path, *, assets=None, install_rc=0, verify_out=TARGET, has_pip=True):
        self.env = su.ManagedEnv(
            prefix=tmp_path, python=str(tmp_path / "bin" / "python"), marker={"source": "x"}
        )
        self.assets = (
            assets
            if assets is not None
            else {su.asset_url(TARGET, WHEEL): WHEEL_BYTES, su.asset_url(TARGET, "SHA256SUMS"): _sums()}
        )
        self.install_rc = install_rc
        self.verify_out = verify_out
        self.fetched: list[str] = []
        self.runs: list[list[str]] = []
        self.run_kwargs: list[dict] = []
        self.exits = 0
        self.markers: list[tuple] = []
        self.gate: asyncio.Event | None = None
        self.pip = has_pip
        self.managed = True

    def probe(self):
        return (self.env, "") if self.managed else (None, "no managed.json marker")

    def fetch(self, url, max_bytes, timeout):
        self.fetched.append(url)
        value = self.assets.get(url)
        if isinstance(value, Exception):
            raise value
        return value

    async def run(self, argv, timeout, on_line, **kw):
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
        self.runs.append(argv)
        self.run_kwargs.append(kw)
        if "-c" in argv:  # the fresh-interpreter version check
            return (0, self.verify_out + "\n") if self.verify_out is not None else (1, "boom")
        if self.gate is not None:
            await self.gate.wait()
        await on_line("Processing herdeck")
        await on_line("Successfully installed herdeck")
        if self.install_rc is None:
            return None, "Processing herdeck"
        return self.install_rc, "line 1\nERROR: no matching distribution"

    def updater(self, **kw):
        return su.BridgeUpdater(
            request_exit=self.request_exit,
            probe=self.probe,
            fetch=self.fetch,
            run=self.run,
            which=lambda name: "/opt/uv" if name == "uv" else None,
            has_pip=lambda: self.pip,
            write_marker=lambda env, version, source: self.markers.append((version, source)),
            current_version="1.0.0",
            **kw,
        )

    def request_exit(self):
        self.exits += 1


class Sink:
    def __init__(self):
        self.frames: list[dict] = []

    async def __call__(self, raw):
        self.frames.append(json.loads(raw))
        return True

    @property
    def result(self):
        results = [f for f in self.frames if f["type"] == "result"]
        assert len(results) == 1, self.frames
        return results[0]

    @property
    def progress(self):
        return [f for f in self.frames if f["type"] == "progress"]


def _msg(version=TARGET, req="u1"):
    return {"type": "update", "req": req, "version": version}


async def test_successful_update_verifies_the_wheel_installs_checks_and_exits(tmp_path):
    fakes, sink = Fakes(tmp_path), Sink()
    updater = fakes.updater()
    await updater.handle(_msg(), sink)
    assert sink.result == {
        "type": "result",
        "req": "u1",
        "data": {"updated": TARGET, "source": "wheel", "restarting": True},
    }
    assert fakes.fetched == [su.asset_url(TARGET, WHEEL), su.asset_url(TARGET, "SHA256SUMS")]
    install, verify = fakes.runs
    assert install[:4] == [fakes.env.python, "-m", "pip", "install"]
    assert install[-1].endswith(WHEEL) and Path(install[-1]).name == WHEEL
    # the bridge module itself must import in the new install, not just the package
    assert verify == [
        fakes.env.python,
        "-I",
        "-c",
        "import herdeck.bridge, herdeck; print(herdeck.__version__)",
    ]
    assert "PYTHONPATH" not in fakes.run_kwargs[0]["env"]
    stages = [p["stage"] for p in sink.progress]
    assert stages[0] == "download" and "install" in stages and stages[-1] == "verify"
    assert all(p["req"] == "u1" for p in sink.progress)
    assert any(p["message"] == "Successfully installed herdeck" for p in sink.progress)
    # the result goes out BEFORE the exit is requested
    assert fakes.exits == 1 and updater.exit_requested
    assert fakes.markers == [
        (
            TARGET,
            {
                "source": "wheel",
                "source_url": su.asset_url(TARGET, WHEEL),
                "sha256": hashlib.sha256(WHEEL_BYTES).hexdigest(),
                "verified": True,
            },
        )
    ]
    assert updater.busy  # the process is exiting: no second update


async def test_uv_is_used_when_the_venv_has_no_pip(tmp_path):
    fakes, sink = Fakes(tmp_path, has_pip=False), Sink()
    await fakes.updater().handle(_msg(), sink)
    assert fakes.runs[0][:5] == ["/opt/uv", "pip", "install", "--python", fakes.env.python]
    assert sink.result["data"]["updated"] == TARGET


async def test_no_wheel_asset_falls_back_to_the_git_tag_only_for_pre_wheel_releases(
    tmp_path, monkeypatch
):
    # Today both floors are 0.10.0, so the fallback is unreachable; move them
    # apart to exercise it.
    monkeypatch.setattr(su, "FIRST_SELF_UPDATE_VERSION", "0.0.1")
    monkeypatch.setattr(su, "WHEELS_SINCE_VERSION", "9.0.0")
    fakes, sink = Fakes(tmp_path, assets={}), Sink()
    await fakes.updater().handle(_msg(), sink)
    assert fakes.markers[0][1]["verified"] is False and fakes.markers[0][1]["sha256"] is None
    assert fakes.runs[0][-1] == f"git+https://github.com/vaclavik-xyz/herdeck@v{TARGET}"
    assert sink.result["data"] == {"updated": TARGET, "source": "git", "restarting": True}
    assert fakes.exits == 1


def _failure(sink):
    data = sink.result["data"]
    assert data["updated"] is None
    return data["error"]


async def test_a_missing_wheel_of_a_wheel_era_release_is_an_error(tmp_path):
    fakes, sink = Fakes(tmp_path, assets={}), Sink()
    updater = fakes.updater()
    await updater.handle(_msg(), sink)
    error = _failure(sink)
    assert error["code"] == "failed" and "no wheel asset" in error["message"]
    assert fakes.runs == [] and fakes.exits == 0 and not updater.busy


@pytest.mark.parametrize(
    "version, allow",
    [
        ("0.99.0", False),  # older than the running 1.0.0
        ("1.0.0rc1", False),
        ("0.9.5", True),  # below the first self-updating release: never
    ],
)
async def test_downgrades_are_refused(tmp_path, version, allow):
    fakes, sink = Fakes(tmp_path), Sink()
    msg = {**_msg(version), **({"allow_downgrade": True} if allow else {})}
    await fakes.updater().handle(msg, sink)
    assert _failure(sink)["code"] == "downgrade"
    assert fakes.fetched == [] and fakes.runs == [] and fakes.exits == 0


async def test_an_explicit_downgrade_is_allowed_above_the_floor(tmp_path):
    fakes, sink = Fakes(tmp_path), Sink()
    fakes.verify_out = "0.99.0"
    fakes.assets = {
        su.asset_url("0.99.0", su.wheel_name("0.99.0")): WHEEL_BYTES,
        su.asset_url("0.99.0", "SHA256SUMS"): _sums(name=su.wheel_name("0.99.0")),
    }
    await fakes.updater().handle({**_msg("0.99.0"), "allow_downgrade": True}, sink)
    assert sink.result["data"]["updated"] == "0.99.0" and fakes.exits == 1


def test_version_ordering():
    ordered = [
        "1.0.0.dev1",
        "1.0.0a1",
        "1.0.0b2",
        "1.0.0rc1",
        "1.0.0",
        "1.0.0.post1",
        "1.0.1",
        "1.10.0",
    ]
    keys = [su.version_key(v) for v in ordered]
    assert keys == sorted(keys) and len(set(keys)) == len(keys)
    assert su.compare_versions("0.10.0", "0.9.0") == 1
    assert su.compare_versions("0.9.0", "0.9.0") == 0
    assert su.compare_versions("x", "0.9.0") is None


@pytest.mark.parametrize(
    "assets, message",
    [
        ({su.asset_url(TARGET, WHEEL): WHEEL_BYTES}, "no SHA256SUMS"),
        (
            {
                su.asset_url(TARGET, WHEEL): WHEEL_BYTES,
                su.asset_url(TARGET, "SHA256SUMS"): _sums(b"other bytes"),
            },
            "SHA-256 mismatch",
        ),
        (
            {
                su.asset_url(TARGET, WHEEL): WHEEL_BYTES,
                su.asset_url(TARGET, "SHA256SUMS"): _sums(name="herdeck-other.whl"),
            },
            "no entry for",
        ),
        ({su.asset_url(TARGET, WHEEL): OSError("network down")}, "download failed"),
    ],
)
async def test_an_unverifiable_wheel_is_never_installed(tmp_path, assets, message):
    fakes, sink = Fakes(tmp_path, assets=assets), Sink()
    updater = fakes.updater()
    await updater.handle(_msg(), sink)
    error = _failure(sink)
    assert error["code"] == "failed" and message in error["message"]
    assert fakes.runs == [] and fakes.exits == 0 and not updater.busy


async def test_installer_failure_keeps_the_old_bridge_and_reports_the_tail(tmp_path):
    fakes, sink = Fakes(tmp_path, install_rc=1), Sink()
    updater = fakes.updater()
    await updater.handle(_msg(), sink)
    error = _failure(sink)
    assert error == {
        "code": "failed",
        "message": "installer exited with 1",
        "output": "line 1\nERROR: no matching distribution",
    }
    assert fakes.exits == 0 and fakes.markers == [] and not updater.busy
    assert len(fakes.runs) == 1  # no version check after a failed install


async def test_installer_timeout_is_a_failure(tmp_path):
    fakes, sink = Fakes(tmp_path, install_rc=None), Sink()
    await fakes.updater().handle(_msg(), sink)
    assert "timed out" in _failure(sink)["message"] and fakes.exits == 0


@pytest.mark.parametrize("verify_out", ["1.0.0", None])
async def test_the_bridge_only_exits_after_the_new_version_verified(tmp_path, verify_out):
    fakes, sink = Fakes(tmp_path, verify_out=verify_out), Sink()
    updater = fakes.updater()
    await updater.handle(_msg(), sink)
    assert "installed version check failed" in _failure(sink)["message"]
    assert fakes.exits == 0 and not updater.busy


async def test_refusals_touch_nothing(tmp_path):
    fakes = Fakes(tmp_path)
    fakes.managed = False
    sink = Sink()
    await fakes.updater().handle(_msg(), sink)
    assert _failure(sink)["code"] == "not_managed"
    fakes.managed = True
    for bad in ("latest", "1.2.3 && echo", None):
        sink = Sink()
        await fakes.updater().handle(_msg(bad), sink)
        assert _failure(sink)["code"] == "invalid_version"
    assert fakes.fetched == [] and fakes.runs == [] and fakes.exits == 0


async def test_the_current_version_is_a_no_op(tmp_path):
    fakes, sink = Fakes(tmp_path), Sink()
    await fakes.updater().handle(_msg("1.0.0"), sink)
    assert sink.result["data"] == {"updated": "1.0.0", "unchanged": True, "restarting": False}
    assert fakes.fetched == [] and fakes.exits == 0


async def test_one_update_at_a_time(tmp_path):
    fakes = Fakes(tmp_path)
    fakes.gate = asyncio.Event()
    updater = fakes.updater()
    first, second = Sink(), Sink()
    task = updater.start(_msg(req="a"), first)
    await asyncio.sleep(0.05)
    assert updater.busy
    await updater.handle(_msg(req="b"), second)
    assert _failure(second) == {"code": "busy", "message": "an update is already running"}
    fakes.gate.set()
    await task
    assert first.result["data"]["updated"] == TARGET and fakes.exits == 1


async def test_real_installer_runner_streams_lines_and_times_out(tmp_path):
    """run_installer against a tiny python child (no shell, no pip)."""
    import sys

    lines: list[str] = []

    async def on_line(line):
        lines.append(line)

    code, tail = await su.run_installer(
        [sys.executable, "-c", "print('a'); print('b')"], 10, on_line
    )
    assert code == 0 and lines == ["a", "b"] and tail == "a\nb"
    code, _ = await su.run_installer(
        [sys.executable, "-c", "import time; time.sleep(30)"], 0.3, on_line
    )
    assert code is None


def test_fetch_refuses_plain_http():
    with pytest.raises(ValueError):
        su.fetch_url("http://example.com/x", 10, 1)


# --- over the wire ---------------------------------------------------------------


@contextlib.asynccontextmanager
async def _bridge(updater):
    clients: dict = {}
    herdr = StubHerdr(panes=[])

    async def handler(ws):
        await _serve_connection(
            ws,
            herdr,
            "s",
            "full-token",
            clients,
            "/unused.sock",
            readonly_token="view-token",
            updater=updater,
        )

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


async def _frames_until_result(ws, req):
    frames = []
    while True:
        frame = json.loads(await asyncio.wait_for(ws.recv(), 3))
        frames.append(frame)
        if frame.get("req") == req and frame["type"] in ("result", "error"):
            return frames


async def test_full_token_update_streams_progress_then_result(tmp_path):
    fakes = Fakes(tmp_path)
    async with _bridge(fakes.updater()) as url:
        headers = {"Authorization": "Bearer full-token"}
        async with websockets.connect(url, additional_headers=headers) as ws:
            assert json.loads(await ws.recv())["type"] == "snapshot"
            await ws.send(json.dumps(_msg()))
            frames = await _frames_until_result(ws, "u1")
    assert frames[-1]["data"]["updated"] == TARGET
    assert {f["type"] for f in frames[:-1]} == {"progress"}
    assert fakes.exits == 1


async def test_readonly_token_cannot_update(tmp_path):
    fakes = Fakes(tmp_path)
    async with _bridge(fakes.updater()) as url:
        headers = {"Authorization": "Bearer view-token"}
        async with websockets.connect(url, additional_headers=headers) as ws:
            await ws.recv()
            await ws.send(json.dumps(_msg()))
            frames = await _frames_until_result(ws, "u1")
    assert frames == [
        {"type": "error", "req": "u1", "message": "read-only token: 'update' is not allowed"}
    ]
    assert fakes.fetched == [] and fakes.runs == [] and fakes.exits == 0


async def test_health_reports_whether_the_bridge_is_managed(tmp_path):
    fakes = Fakes(tmp_path)
    async with _bridge(fakes.updater()) as url:
        headers = {"Authorization": "Bearer full-token"}
        async with websockets.connect(url, additional_headers=headers) as ws:
            await ws.recv()
            await ws.send(json.dumps({"type": "health", "req": "h"}))
            reply = json.loads(await asyncio.wait_for(ws.recv(), 3))
    assert reply["data"]["managed"] is True


async def test_a_bridge_without_an_updater_answers_not_managed():
    async with _bridge(None) as url:
        headers = {"Authorization": "Bearer full-token"}
        async with websockets.connect(url, additional_headers=headers) as ws:
            await ws.recv()
            await ws.send(json.dumps(_msg()))
            frames = await _frames_until_result(ws, "u1")
    assert frames[-1]["data"]["error"]["code"] == "not_managed"


async def test_serve_returns_true_when_the_updater_requests_the_exit(monkeypatch):
    """The exit seam: serve() returns (main() then exits 0 for the service
    manager to restart it) instead of running forever."""
    monkeypatch.setattr(bridge_mod, "EXIT_GRACE_S", 0.01)
    seams = []

    def factory(request_exit):
        seams.append(request_exit)
        return su.BridgeUpdater(request_exit=request_exit, probe=lambda: (None, "test"))

    task = asyncio.create_task(
        bridge_mod.serve(
            "/nonexistent/herdr.sock", "127.0.0.1", 0, "s", "tok", updater_factory=factory
        )
    )
    for _ in range(100):
        if seams:
            break
        await asyncio.sleep(0.02)
    assert not task.done()
    seams[0]()
    # exit_requested is set by the updater before it calls the seam
    assert await asyncio.wait_for(task, 3) is False


def test_main_exits_cleanly_after_a_restart_request(monkeypatch):
    async def fake_serve(*args, **kwargs):
        return True

    monkeypatch.setattr(bridge_mod, "serve", fake_serve)
    monkeypatch.setattr(bridge_mod, "resolve_herdr_socket_path", lambda: "/tmp/h.sock")
    monkeypatch.setenv("HERDECK_TOKEN", "t")
    monkeypatch.delenv("HERDECK_READONLY_TOKEN_FILE", raising=False)
    monkeypatch.delenv("HERDECK_TOKEN_FILE", raising=False)
    monkeypatch.setenv("HERDECK_BIND", "127.0.0.1")
    assert bridge_mod.main([]) is None  # returns -> exit status 0


def test_marker_is_rewritten_atomically_describing_the_new_artifact(tmp_path, monkeypatch):
    # managed.py's schema: stale artifact keys never survive an update
    marker = {
        "version": "1.0.0",
        "source": "git+https://github.com/vaclavik-xyz/herdeck@v1.0.0",
        "sha256": None,
        "verified": False,
        "venv": str(tmp_path),
        "installed_at": 1,
    }
    (tmp_path / su.MARKER_NAME).write_text(json.dumps(marker))
    monkeypatch.setattr(su.time, "time", lambda: 1234)
    installed = {
        "source": "wheel",
        "source_url": su.asset_url(TARGET, WHEEL),
        "sha256": "ab" * 32,
        "verified": True,
    }
    su._write_marker(su.ManagedEnv(prefix=tmp_path, python="py", marker=marker), TARGET, installed)
    assert json.loads((tmp_path / su.MARKER_NAME).read_text()) == {
        "version": TARGET,
        "source": su.asset_url(TARGET, WHEEL),
        "sha256": "ab" * 32,
        "verified": True,
        "venv": str(tmp_path),
        "installed_at": 1234,
    }
    assert [p.name for p in tmp_path.iterdir()] == [su.MARKER_NAME]


def test_update_is_a_wire_capability():
    assert "self_update" in bridge_mod._WIRE_CAPABILITIES
    assert __version__  # the runtime sends its own version as the target
