"""The usage agent (usage_agent.py): the file it writes in the login session,
and the bridge's composite source that prefers that file over its own poller."""

import json
import logging
import os
import stat

import pytest

import herdeck.usage_agent as ua
from herdeck.usage import ProviderUsage, UsageWindow, usage_to_wire


def _codex(used=40):
    return ProviderUsage("codex", [UsageWindow("5h", used, "2026-09-24T18:00:00Z")], "paid", "pro")


class FakePoller:
    def __init__(self, data=None):
        self.data = list(data or [])
        self.started = 0
        self.closed = 0
        self.fail = False

    def start(self):
        self.started += 1

    def close(self):
        self.closed += 1

    def snapshot(self):
        if self.fail:
            raise RuntimeError("poller broke")
        return list(self.data)


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


# --- paths & file ---------------------------------------------------------------


def test_default_path_follows_xdg_state_home(tmp_path):
    assert ua.default_path({"XDG_STATE_HOME": "/xdg"}, tmp_path) == ua.Path(
        "/xdg/herdeck/bridge-usage.json"
    )
    assert ua.default_path({}, tmp_path) == tmp_path / ".local/state/herdeck/bridge-usage.json"


def test_write_file_is_atomic_private_and_versioned(tmp_path):
    path = tmp_path / "state" / "herdeck" / "bridge-usage.json"
    ua.write_file(path, usage_to_wire([_codex()]), 60.0, 123.5)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    doc = json.loads(path.read_text())
    assert doc == {
        "version": 1,
        "written_at": 123.5,
        "refresh_secs": 60.0,
        "providers": usage_to_wire([_codex()]),
    }
    inode = path.stat().st_ino
    ua.write_file(path, [], 60.0, 124.0)
    assert path.stat().st_ino != inode  # replaced by rename, never rewritten in place
    assert [p.name for p in path.parent.iterdir()] == ["bridge-usage.json"]  # no temp left


def test_write_file_cleans_up_its_temp_file_on_failure(tmp_path, monkeypatch):
    path = tmp_path / "bridge-usage.json"

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(ua.os, "replace", boom)
    with pytest.raises(OSError):
        ua.write_file(path, [], 60.0, 1.0)
    assert list(tmp_path.iterdir()) == []


def test_read_file_rejects_garbage(tmp_path):
    path = tmp_path / "f.json"
    assert ua.read_file(path) is None
    path.write_text("{nope")
    assert ua.read_file(path) is None
    path.write_text(json.dumps({"version": 2, "written_at": 1}))
    assert ua.read_file(path) is None
    path.write_text(json.dumps([1]))
    assert ua.read_file(path) is None


def test_freshness_window():
    doc = {"written_at": 1000.0, "refresh_secs": 60}
    assert ua.max_age(60) == 300  # never below 5 minutes
    assert ua.max_age(600) == 1800  # 3 refreshes
    assert ua.is_fresh(doc, 1300.0) and not ua.is_fresh(doc, 1300.5)
    assert ua.file_age(doc, 1010.0) == 10.0
    assert not ua.is_fresh({"written_at": 5000.0, "refresh_secs": 60}, 1000.0)  # future garbage
    assert not ua.is_fresh({"written_at": "soon"}, 1000.0)
    assert not ua.is_fresh({"written_at": True}, 1000.0)
    assert not ua.is_fresh(None, 1000.0)
    # a missing/absurd refresh_secs cannot keep the data fresh forever
    assert not ua.is_fresh({"written_at": 0.0, "refresh_secs": 1e12}, 10 * 24 * 3600.0)


# --- the agent --------------------------------------------------------------------


def test_agent_writes_on_change_and_as_a_heartbeat(tmp_path):
    path = tmp_path / "bridge-usage.json"
    clock = Clock()
    poller = FakePoller([_codex(40)])
    agent = ua.UsageAgent(poller, path, 60, clock=clock)
    assert agent.tick() is True  # the first check always writes
    first = json.loads(path.read_text())
    assert first["written_at"] == clock.now and first["refresh_secs"] == 60.0
    clock.now += 5
    assert agent.tick() is False  # unchanged, heartbeat not due
    poller.data = [_codex(55)]
    assert agent.tick() is True  # changed -> written at once
    assert json.loads(path.read_text())["providers"][0]["windows"][0]["used_percent"] == 55
    clock.now += 59
    assert agent.tick() is False
    clock.now += 1
    assert agent.tick() is True  # refresh_secs since the last write: heartbeat
    assert json.loads(path.read_text())["written_at"] == clock.now


def test_agent_clamps_refresh_and_survives_poller_and_disk_errors(tmp_path, monkeypatch):
    path = tmp_path / "bridge-usage.json"
    poller = FakePoller([_codex()])
    agent = ua.UsageAgent(poller, path, 1, clock=Clock())
    assert agent.refresh_secs == 30.0  # the poller's own floor
    poller.fail = True
    assert agent.tick() is False and not path.exists()
    poller.fail = False
    real, broken = ua.write_file, [True]

    def write(*args):
        if broken[0]:
            raise OSError("read-only")
        real(*args)

    monkeypatch.setattr(ua, "write_file", write)
    assert agent.tick() is False
    broken[0] = False
    assert agent.tick() is True  # retried on the next check


def test_agent_run_stops_on_the_event(tmp_path):
    import threading

    stop = threading.Event()
    poller = FakePoller([_codex()])
    agent = ua.UsageAgent(poller, tmp_path / "f.json", 60, check_s=0.01)
    thread = threading.Thread(target=agent.run, args=(stop,))
    thread.start()
    stop.set()
    thread.join(2)
    assert not thread.is_alive()
    assert (tmp_path / "f.json").exists()


def test_main_polls_writes_and_closes_the_poller(tmp_path, monkeypatch):
    import herdeck.usage as usage_mod

    poller = FakePoller([_codex()])
    monkeypatch.setattr(usage_mod, "poller_from_config", lambda cfg, on_alert=None: poller)
    monkeypatch.delenv("HERDECK_USAGE_CONFIG", raising=False)
    handlers = {}
    monkeypatch.setattr(ua.signal, "signal", lambda sig, fn: handlers.setdefault(sig, fn))

    def run_once(self, stop):
        self.tick()
        handlers[ua.signal.SIGTERM](ua.signal.SIGTERM, None)  # launchd stops it
        assert stop.is_set()

    monkeypatch.setattr(ua.UsageAgent, "run", run_once)
    path = tmp_path / "out.json"
    assert ua.main(["--file", str(path)]) == 0
    assert (poller.started, poller.closed) == (1, 1)
    assert json.loads(path.read_text())["providers"] == usage_to_wire([_codex()])
    assert set(handlers) == {ua.signal.SIGTERM, ua.signal.SIGINT}


def test_main_default_file_honours_xdg_state_home(tmp_path, monkeypatch):
    import herdeck.usage as usage_mod

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(usage_mod, "poller_from_config", lambda cfg, on_alert=None: FakePoller())
    monkeypatch.setattr(ua.signal, "signal", lambda sig, fn: None)
    monkeypatch.setattr(ua.UsageAgent, "run", lambda self, stop: self.tick())
    assert ua.main([]) == 0
    written = tmp_path / "xdg" / "herdeck" / "bridge-usage.json"
    assert json.loads(written.read_text())["providers"] == []


# --- the bridge's composite source ------------------------------------------------


class Own:
    """Records own pollers the composite builds."""

    def __init__(self, data=None):
        self.data = data
        self.built: list[FakePoller] = []

    def __call__(self):
        poller = FakePoller(self.data)
        self.built.append(poller)
        return poller


def _composite(tmp_path, clock=None, data=None):
    own = Own(data if data is not None else [ProviderUsage("claude", [UsageWindow("5h", 3, None)])])
    path = tmp_path / "bridge-usage.json"
    closed = []
    comp = ua.CompositeUsagePoller(
        own, path, clock=clock or Clock(), closer=lambda p: (p.close(), closed.append(p))
    )
    return comp, own, path, closed


def test_fresh_file_is_used_and_the_own_poller_never_starts(tmp_path):
    clock = Clock()
    comp, own, path, _closed = _composite(tmp_path, clock)
    ua.write_file(path, usage_to_wire([_codex()]), 60, clock.now - 10)
    comp.start()
    assert comp.snapshot() == [_codex()]
    assert own.built == [] and comp.mode == "agent"


def test_stale_file_gives_no_usage_and_still_no_own_poller(tmp_path, caplog):
    clock = Clock()
    comp, own, path, _closed = _composite(tmp_path, clock)
    ua.write_file(path, usage_to_wire([_codex()]), 60, clock.now)
    comp.start()
    assert comp.snapshot() == [_codex()]
    clock.now += 301
    with caplog.at_level(logging.INFO, logger="herdeck.usage_agent"):
        assert comp.snapshot() == []
        assert comp.snapshot() == []
    assert own.built == []
    assert sum("stale" in r.getMessage() for r in caplog.records) == 1  # on the transition only
    ua.write_file(path, usage_to_wire([_codex(70)]), 60, clock.now)  # the agent is back
    assert comp.snapshot() == [_codex(70)]


def test_unreadable_file_counts_as_present_but_empty(tmp_path):
    comp, own, path, _closed = _composite(tmp_path)
    path.write_text("garbage")
    comp.start()
    assert comp.snapshot() == [] and own.built == []


def test_absent_file_starts_the_own_poller_lazily(tmp_path):
    comp, own, path, _closed = _composite(tmp_path)
    assert comp.snapshot() == [] and own.built == []  # not started yet: nothing built
    comp.start()
    assert len(own.built) == 1 and own.built[0].started == 1
    assert comp.snapshot()[0].provider == "claude"
    comp.snapshot()
    assert len(own.built) == 1  # one poller, not one per snapshot
    comp.close()
    assert own.built[0].closed == 1


def test_file_appearing_closes_the_own_poller_and_vanishing_restarts_one(tmp_path, caplog):
    clock = Clock()
    comp, own, path, closed = _composite(tmp_path, clock)
    comp.start()
    first = own.built[0]
    with caplog.at_level(logging.INFO, logger="herdeck.usage_agent"):
        ua.write_file(path, usage_to_wire([_codex()]), 60, clock.now)
        assert comp.snapshot() == [_codex()]
        assert closed == [first] and first.closed == 1 and comp.mode == "agent"
        os.unlink(path)  # `herdeck-service uninstall usage`
        assert comp.snapshot()[0].provider == "claude"
    assert len(own.built) == 2 and own.built[1].started == 1  # a fresh poller
    messages = [r.getMessage() for r in caplog.records]
    assert any("found" in m for m in messages) and any("gone" in m for m in messages)
    comp.close()
    assert own.built[1].closed == 1


def test_file_is_reread_only_when_it_changes(tmp_path, monkeypatch):
    clock = Clock()
    comp, own, path, _closed = _composite(tmp_path, clock)
    ua.write_file(path, usage_to_wire([_codex()]), 60, clock.now)
    reads = []
    real = ua.read_file
    monkeypatch.setattr(ua, "read_file", lambda p: reads.append(p) or real(p))
    comp.snapshot()
    comp.snapshot()
    assert len(reads) == 1
    ua.write_file(path, usage_to_wire([_codex(80)]), 60, clock.now)
    assert comp.snapshot() == [_codex(80)] and len(reads) == 2


def test_default_closer_does_not_block_the_caller():
    import threading

    gate = threading.Event()

    class Slow:
        def close(self):
            gate.wait(2)

    ua._close_in_background(Slow())  # returns while close() is still blocked
    gate.set()
