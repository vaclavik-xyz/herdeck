"""Runtime maintenance (deckapp/maintenance.py + the /maintenance routes):
D200 USB presence + location persistence, uhubctl power-cycle, deck restart."""

import json
import os
import plistlib
import subprocess
import threading
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from test_d200_sink import _RS, _held_driver, _Tile, _wait

from herdeck.config import HardwareConfig
from herdeck.deckapp import maintenance as mt
from herdeck.deckapp.device_lock import DeviceLock
from herdeck.deckapp.sinks import ReconnectingD200Sink, RenderFrame

D200 = {"vendor_id": 0x2207, "product_id": 0x0019, "path": b"x"}

LISTING = """\
Current status for hub 20-1 [2109:2817 VIA Labs, Inc. USB2.0 Hub, USB 2.10, 4 ports, ppps]
  Port 1: 0100 power
  Port 2: 0103 power enable connect [2207:0019 Ulanzi D200]
Current status for hub 20 [05ac:8104 Apple Inc. Root Hub]
  Port 1: 0503 power highspeed enable connect [2109:2817 VIA Labs USB2.0 Hub]
"""


# --- pure helpers -------------------------------------------------------------


def test_parse_uhubctl_listing_finds_the_d200_port():
    assert mt.parse_uhubctl_listing(LISTING) == ("20-1", 2)
    assert mt.parse_uhubctl_listing(LISTING.replace("2207:0019", "1234:5678")) is None
    assert mt.parse_uhubctl_listing("") is None


def test_sysfs_location_splits_the_last_port(tmp_path):
    def device(name, vid, pid):
        d = tmp_path / name
        d.mkdir()
        (d / "idVendor").write_text(vid + "\n")
        (d / "idProduct").write_text(pid + "\n")

    device("usb1", "1d6b", "0002")  # root hub
    device("1-1", "2109", "2817")  # the hub
    device("1-1.4", "2207", "0019")  # the D200
    (tmp_path / "1-1.4:1.0").mkdir()  # an interface
    assert mt.sysfs_d200_location(str(tmp_path)) == ("1-1", 4)


def test_sysfs_location_on_a_root_port(tmp_path):
    d = tmp_path / "3-12"
    d.mkdir()
    (d / "idVendor").write_text("2207")
    (d / "idProduct").write_text("0019")
    assert mt.sysfs_d200_location(str(tmp_path)) == ("3", 12)
    assert mt.sysfs_d200_location(str(tmp_path / "missing")) is None


def test_usb_location_persists_in_the_runtime_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HERDECK_RUNTIME_DIR", str(tmp_path))
    assert mt.usb_state_path() == tmp_path / "d200-usb.json"
    assert mt.load_usb_location() is None
    mt.save_usb_location("20-1", 2)
    saved = mt.load_usb_location()
    assert saved["hub"] == "20-1" and saved["port"] == 2
    assert isinstance(saved["seen_at"], int)


@pytest.mark.parametrize(
    "payload",
    [{"hub": "-a cycle", "port": 2}, {"hub": "1-1", "port": 0}, {"hub": "1", "port": True}, []],
)
def test_usb_location_ignores_a_tampered_file(tmp_path, payload):
    path = tmp_path / "d200-usb.json"
    path.write_text(json.dumps(payload))
    assert mt.load_usb_location(path) is None


def test_find_uhubctl_prefers_config_then_path_then_homebrew():
    exes = {"/custom/uhubctl", "/opt/homebrew/bin/uhubctl"}
    is_exe = exes.__contains__
    assert mt.find_uhubctl("/custom/uhubctl", which=lambda n: None, is_exe=is_exe) == "/custom/uhubctl"
    # a configured path that is not executable is not silently replaced
    assert mt.find_uhubctl("/nope", which=lambda n: "/usr/bin/uhubctl", is_exe=is_exe) is None
    assert mt.find_uhubctl("", which=lambda n: "/usr/bin/uhubctl", is_exe=is_exe) == "/usr/bin/uhubctl"
    # launchd's PATH lacks Homebrew
    assert mt.find_uhubctl("", which=lambda n: None, is_exe=is_exe) == "/opt/homebrew/bin/uhubctl"
    assert mt.find_uhubctl("", which=lambda n: None, is_exe=lambda p: False) is None


# --- Maintenance facade over a fake app -----------------------------------------


class FakeSink:
    def __init__(self, restart_result=None, health=None):
        self.restart_result = restart_result or {"outcome": "reopened"}
        self.restarts = 0
        self.kicks = 0
        self._health = health or {
            "connected": True, "since": 1, "last_frame_at": 2, "last_error": None,
            "lock_owner": None,
        }

    def deliver(self, frame):
        pass

    def health(self):
        return dict(self._health)

    def restart(self, timeout):
        self.restarts += 1
        return dict(self.restart_result)

    def kick(self):
        self.kicks += 1

    def close(self):
        pass


def _fake_app(hardware=None, sinks=None, servers=None):
    source = types.SimpleNamespace(
        server_health=lambda: servers or {"box": {"connected": True, "bridge_version": "0.9.0"}}
    )
    app = types.SimpleNamespace(
        config=types.SimpleNamespace(hardware=hardware or HardwareConfig()),
        _sinks=sinks if sinks is not None else [FakeSink()],
        _source=source,
        _started_at=0.0,
        _lock=threading.Lock(),
        refreshes=[],
    )
    app._refresh_locked = lambda **kw: app.refreshes.append(kw)
    return app


class Runner:
    def __init__(self, results=None):
        self.calls = []
        self.results = results or {}

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        key = "list" if len(argv) == 1 else "cycle"
        result = self.results.get(key, (0, "", ""))
        if isinstance(result, BaseException):
            raise result
        code, out, err = result
        return subprocess.CompletedProcess(argv, code, out, err)


def _maintenance(tmp_path, *, app=None, present=True, runner=None, uhubctl="/opt/bin/uhubctl",
                 platform="darwin"):
    return mt.Maintenance(
        app or _fake_app(),
        enumerate_usb=lambda: [D200] if present else ([] if present is False else None),
        runner=runner or Runner({"list": (0, LISTING, "")}),
        which=lambda name: uhubctl,
        is_exe=lambda p: p == uhubctl,
        sysfs_root=str(tmp_path / "no-sysfs"),
        home=tmp_path,
        platform=platform,
        state_path=tmp_path / "d200-usb.json",
    )


def test_status_reports_versions_service_logs_d200_and_servers(tmp_path):
    agents = tmp_path / "Library/LaunchAgents"
    agents.mkdir(parents=True)
    program = "/Applications/herdeck.app/Contents/Resources/herdeck-deckapp/herdeck-deckapp"
    (agents / "dev.herdeck.runtime.plist").write_bytes(plistlib.dumps({
        "Label": "dev.herdeck.runtime",
        "ProgramArguments": [program],
        "StandardOutPath": str(tmp_path / "Library/Logs/herdeck-runtime.log"),
    }))
    status = _maintenance(tmp_path).status()

    assert status["pid"] == os.getpid()
    assert set(status) == {"version", "pid", "uptime_s", "process", "service", "logs", "d200", "servers"}
    assert status["service"] == {
        "installed": True,
        "label": "dev.herdeck.runtime",
        "unit_path": str(agents / "dev.herdeck.runtime.plist"),
        "program": program,
        "from_app": True,
    }
    assert status["logs"] == {
        "runtime": str(tmp_path / "Library/Logs/herdeck-runtime.log"),
        "app": str(tmp_path / "Library/Logs/herdeck/herdeck.log"),
    }
    assert set(status["process"]) == {"frozen", "executable", "spawned_by_app", "is_service"}
    d200 = status["d200"]
    assert d200["state"] == "connected" and d200["supervised"] is True
    assert d200["usb_present"] is True
    assert d200["usb_location"] == "20-1:2"  # discovered via the uhubctl listing
    assert d200["power_cycle"] == {
        "available": True, "reason": None, "uhubctl": "/opt/bin/uhubctl",
        "hub": "20-1", "port": 2, "source": "last_seen",
    }
    assert status["servers"] == {
        "box": {"self_update": False, "managed": None, "connected": True, "bridge_version": "0.9.0"}
    }
    # persisted for when the device is gone
    assert mt.load_usb_location(tmp_path / "d200-usb.json")["hub"] == "20-1"


def test_status_without_a_service_and_on_linux(tmp_path):
    status = _maintenance(tmp_path, platform="linux").status()
    assert status["service"]["installed"] is False
    assert status["service"]["label"] == "herdeck-runtime.service"
    assert status["logs"]["runtime"] is None


def test_linux_service_unit_program_is_read_from_execstart(tmp_path):
    unit = tmp_path / ".config/systemd/user/herdeck-runtime.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\nExecStart=/opt/venv/bin/python -m herdeck.runtime\n")
    info = mt.runtime_service_info(tmp_path, "linux")
    assert info["installed"] and info["program"] == "/opt/venv/bin/python"
    assert info["from_app"] is False


@pytest.mark.parametrize(
    ("health", "present", "state"),
    [
        ({"connected": False, "lock_owner": 4242}, True, "locked"),
        ({"connected": False, "lock_owner": None}, False, "not_on_usb"),
        ({"connected": False, "lock_owner": None}, True, "disconnected"),
        ({"connected": False, "lock_owner": None}, None, "disconnected"),
    ],
)
def test_d200_state_codes(tmp_path, health, present, state):
    app = _fake_app(sinks=[FakeSink(health=health)])
    assert _maintenance(tmp_path, app=app, present=present).d200_status()["state"] == state


def test_d200_state_without_a_d200_sink(tmp_path):
    d200 = _maintenance(tmp_path, app=_fake_app(sinks=[])).d200_status()
    assert d200["state"] == "unsupervised" and d200["supervised"] is False


def test_usb_presence_is_cached_briefly(tmp_path):
    calls = []
    clock = [0.0]
    m = mt.Maintenance(
        _fake_app(), enumerate_usb=lambda: calls.append(1) or [D200], clock=lambda: clock[0],
        home=tmp_path, state_path=tmp_path / "s.json",
    )
    assert m.usb_present() is True
    assert m.usb_present() is True
    assert len(calls) == 1
    clock[0] = 3.0
    m.usb_present()
    assert len(calls) == 2


def test_power_cycle_unavailable_reasons(tmp_path):
    assert _maintenance(tmp_path, uhubctl=None).power_cycle() == {
        "ok": False, "outcome": "unavailable", "reason": "uhubctl_missing",
    }
    configured = _fake_app(hardware=HardwareConfig(uhubctl="/not/there"))
    assert _maintenance(tmp_path, app=configured).power_cycle()["reason"] == "uhubctl_not_executable"
    # D200 gone and its location never seen
    runner = Runner()
    assert _maintenance(tmp_path, present=False, runner=runner).power_cycle() == {
        "ok": False, "outcome": "unavailable", "reason": "location_unknown",
    }
    assert runner.calls == []


def test_power_cycle_runs_uhubctl_argv_without_a_shell_and_kicks_the_sink(tmp_path):
    runner = Runner({"list": (0, LISTING, ""), "cycle": (0, "Sent power cycle", "")})
    sink = FakeSink()
    m = _maintenance(tmp_path, app=_fake_app(sinks=[sink]), runner=runner)
    result = m.power_cycle()
    assert result == {
        "ok": True, "outcome": "cycled", "hub": "20-1", "port": 2,
        "command": "/opt/bin/uhubctl -l 20-1 -p 2 -a cycle -d 2",
    }
    argv, timeout = runner.calls[-1]
    assert argv == ["/opt/bin/uhubctl", "-l", "20-1", "-p", "2", "-a", "cycle", "-d", "2"]
    assert timeout == mt.UHUBCTL_CYCLE_TIMEOUT_S
    assert sink.kicks == 1


def test_power_cycle_uses_the_last_seen_location_when_the_d200_is_gone(tmp_path):
    mt.save_usb_location("20-1.4", 3, tmp_path / "d200-usb.json")
    runner = Runner({"cycle": (0, "", "")})
    result = _maintenance(tmp_path, present=False, runner=runner).power_cycle()
    assert result["ok"] and runner.calls[0][0][1:5] == ["-l", "20-1.4", "-p", "3"]


def test_power_cycle_config_pin_wins(tmp_path):
    mt.save_usb_location("20-1", 2, tmp_path / "d200-usb.json")
    app = _fake_app(hardware=HardwareConfig(usb_hub="1-1", usb_port=7))
    runner = Runner({"list": (0, LISTING, ""), "cycle": (0, "", "")})
    m = _maintenance(tmp_path, app=app, runner=runner)
    assert m.d200_status()["power_cycle"]["source"] == "config"
    assert m.power_cycle()["command"].endswith("-l 1-1 -p 7 -a cycle -d 2")


def test_power_cycle_reports_needs_admin_with_the_exact_command(tmp_path):
    runner = Runner({
        "list": (0, LISTING, ""),
        "cycle": (1, "", "Permission denied. Try running as root!"),
    })
    result = _maintenance(tmp_path, runner=runner).power_cycle()
    assert result["outcome"] == "needs_admin" and result["ok"] is False
    assert result["command"] == "sudo /opt/bin/uhubctl -l 20-1 -p 2 -a cycle -d 2"


def test_power_cycle_failure_and_timeout(tmp_path):
    runner = Runner({"list": (0, LISTING, ""), "cycle": (1, "", "No compatible devices")})
    result = _maintenance(tmp_path, runner=runner).power_cycle()
    assert result["outcome"] == "failed" and "No compatible devices" in result["error"]

    runner = Runner({
        "list": (0, LISTING, ""),
        "cycle": subprocess.TimeoutExpired(["uhubctl"], 30),
    })
    assert _maintenance(tmp_path, runner=runner).power_cycle()["outcome"] == "timeout"


def test_power_cycle_is_not_run_twice_at_once(tmp_path):
    m = _maintenance(tmp_path)
    assert m._cycle_lock.acquire()
    try:
        assert m.power_cycle() == {"ok": False, "outcome": "busy"}
    finally:
        m._cycle_lock.release()


def test_restart_deck_outcomes(tmp_path):
    app = _fake_app()
    result = _maintenance(tmp_path, app=app).restart_deck()
    assert result == {"ok": True, "outcome": "reopened"}
    assert app.refreshes == [{"working": None, "full": True}]  # full redraw

    failing = _fake_app(sinks=[FakeSink({"outcome": "failed", "error": "No openable"})])
    result = _maintenance(tmp_path, app=failing, present=False).restart_deck()
    assert result == {"ok": False, "outcome": "not_present", "error": "No openable", "usb_present": False}
    result = _maintenance(tmp_path, app=failing, present=True).restart_deck()
    assert result["outcome"] == "failed" and result["usb_present"] is True

    locked = _fake_app(sinks=[FakeSink({"outcome": "locked_by", "pid": 77})])
    assert _maintenance(tmp_path, app=locked).restart_deck() == {
        "ok": False, "outcome": "locked_by", "pid": 77,
    }
    assert _maintenance(tmp_path, app=_fake_app(sinks=[])).restart_deck() == {
        "ok": False, "outcome": "unsupported",
    }


# --- ReconnectingD200Sink.restart against fake devices ---------------------------


def test_sink_restart_reopens_and_repaints_the_full_frame(tmp_path):
    drivers = []

    def factory():
        driver = _held_driver()
        drivers.append(driver)
        return driver

    lock = DeviceLock(str(tmp_path / "d200.lock"))
    sink = ReconnectingD200Sink(factory, on_press=lambda i: None, slots=13, retry_interval=0.01,
                                device_lock=lock)
    try:
        sink.deliver(RenderFrame(render=_RS([_Tile(0), _Tile(1)], panel="P"), working=None, full=True))
        assert _wait(lambda: drivers and drivers[0].frames)
        assert sink.restart(timeout=2.0) == {"outcome": "reopened"}
        assert drivers[0].closed is True
        assert len(drivers) == 2
        assert drivers[1].frames == [([0, 1], "P")]
        assert lock.held  # never let go of the deck mid-restart
    finally:
        sink.close()


def test_sink_restart_reports_a_failed_reopen(tmp_path):
    attempts = []

    def factory():
        attempts.append(1)
        raise RuntimeError("No openable Ulanzi D200 control interface")

    sink = ReconnectingD200Sink(factory, on_press=lambda i: None, slots=13, retry_interval=30)
    try:
        assert _wait(lambda: attempts)
        # the 30 s retry sleep is cut short by the restart request
        result = sink.restart(timeout=2.0)
        assert result == {"outcome": "failed", "error": "No openable Ulanzi D200 control interface"}
        assert len(attempts) == 2
    finally:
        sink.close()


def test_sink_restart_does_not_fight_another_runtime_for_the_lock(tmp_path):
    path = str(tmp_path / "d200.lock")
    owner = DeviceLock(path)
    assert owner.acquire()
    opens = []
    sink = ReconnectingD200Sink(lambda: opens.append(1) or _held_driver(), on_press=lambda i: None,
                                slots=13, device_lock=DeviceLock(path), lock_retry_interval=30)
    try:
        assert sink.restart(timeout=1.0) == {"outcome": "locked_by", "pid": os.getpid()}
        assert opens == []
    finally:
        sink.close()
        owner.release()


# --- HTTP routes ------------------------------------------------------------------


@pytest.fixture
def served(tmp_path):
    from test_deckapp import _serving_app

    app = _serving_app()
    app._maintenance = _maintenance(tmp_path, app=_fake_app())
    yield app
    app.close()


def _url(app, path):
    return f"http://{app.host}:{app.port}{path}"


def _post(app, path, token=None, body=b"{}"):
    req = urllib.request.Request(_url(app, path), data=body, method="POST")
    req.add_header("X-Herdeck-Token", app.token if token is None else token)
    return urllib.request.urlopen(req, timeout=5)


def test_maintenance_routes_require_the_token(served):
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(_url(served, "/maintenance?token=wrong"), timeout=2)
    assert err.value.code == 403
    for path in (
        "/maintenance/deck/restart",
        "/maintenance/deck/power-cycle",
        "/maintenance/servers/box/update",
    ):
        with pytest.raises(urllib.error.HTTPError) as err:
            _post(served, path, token="wrong")
        assert err.value.code == 403


def test_get_maintenance_returns_the_status_json(served):
    with urllib.request.urlopen(_url(served, f"/maintenance?token={served.token}"), timeout=5) as r:
        body = json.loads(r.read())
    assert body["d200"]["state"] == "connected"
    assert "servers" in body and "service" in body


def test_post_deck_actions_return_outcomes(served):
    with _post(served, "/maintenance/deck/restart") as r:
        assert json.loads(r.read()) == {"ok": True, "outcome": "reopened"}
    with _post(served, "/maintenance/deck/power-cycle") as r:
        assert json.loads(r.read())["outcome"] == "cycled"
    with pytest.raises(urllib.error.HTTPError) as err:
        _post(served, "/maintenance/deck/restart", body=b"[1]")
    assert err.value.code == 400


def test_bridge_update_route_needs_a_live_source(served):
    # The route is served by deckapp/bridge_update.py (tests/test_bridge_update_route.py);
    # a source without bridges (the demo) has nothing to update.
    with pytest.raises(urllib.error.HTTPError) as err:
        _post(served, "/maintenance/servers/box/update")
    assert err.value.code == 404


def test_runtime_default_maintenance_uses_the_real_app(served, monkeypatch):
    monkeypatch.setattr(mt, "_default_enumerate", lambda: None)  # no real USB in tests
    del served._maintenance
    status = served.maintenance.status()
    assert status["d200"]["supervised"] is False  # the mock app has no D200 sink
    assert Path(status["logs"]["app"]).name == "herdeck.log"


def test_toggling_the_standard_writer_reopens_the_d200():
    from herdeck.deckapp.server import DeckApp

    sig = DeckApp._d200_hardware_signature
    assert sig(HardwareConfig()) != sig(HardwareConfig(d200_standard_writer=True))
