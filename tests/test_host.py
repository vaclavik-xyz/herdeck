"""Unit tests for ``herdeck.host`` (the ``herdeck``/``herdeck-web`` entry of
the one runtime): the D200 front under ``d200.lock``, config reload and its
failure panel. End-to-end behaviour: tests/test_contract_*.py."""

from __future__ import annotations

import time

import pytest
from test_one_runtime import live, make_config

# --- host -------------------------------------------------------------------------------


def test_host_drives_an_explicit_d200_only_while_it_holds_the_lock(tmp_path, monkeypatch):
    from herdeck.deckapp.device_lock import DeviceLock, d200_lock_path
    from herdeck.driver.fake import FakeRenderer
    from herdeck.host import Host, _Front

    monkeypatch.setenv("HERDECK_RUNTIME_DIR", str(tmp_path))
    other = DeviceLock(d200_lock_path())
    assert other.acquire()  # another runtime owns the D200
    opened = []

    def driver_factory(hardware):
        opened.append(hardware)
        return FakeRenderer(13)

    config = make_config()
    host = Host(
        config,
        _Front("d200"),
        mode="remote",
        source_factory=lambda cfg: live(cfg)[0],
        d200_driver_factory=driver_factory,
    )
    host.start()
    try:
        sink = host.app._sinks[0]
        sink._lock_retry_interval = 0.05
        time.sleep(0.3)
        assert opened == []
        other.release()
        deadline = time.monotonic() + 5
        while not opened and time.monotonic() < deadline:
            time.sleep(0.02)
        assert opened, "the D200 opens once the lock is free"
    finally:
        host.close()


def test_host_reload_failure_holds_a_status_panel(tmp_path):
    from herdeck.driver.fake import FakeRenderer
    from herdeck.host import Host, _Front

    config_path = tmp_path / "config.toml"
    config_path.write_text("this is = = not toml")
    host = Host(
        make_config(),
        _Front("fake", deck=FakeRenderer(13)),
        mode="remote",
        config_path=str(config_path),
        local_path=str(tmp_path / "local.toml"),
        source_factory=lambda cfg: live(cfg)[0],
    )
    host.start()
    try:
        host.app.reload()
        assert host.front.deck.last_panel.title == "reload failed"
        assert host.app._source.config is host.config  # the old config stays
    finally:
        host.close()


def test_host_reload_swaps_in_a_wired_source(tmp_path):
    from herdeck.driver.fake import FakeRenderer
    from herdeck.host import Host, _Front

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[[servers]]\nid = "local"\nurl = "ws://bridge"\ntoken_env = "HD_TOKEN"\n'
        '[deck]\ngrid = "5x3"\noverview_order = ["local"]\n'
    )
    import os

    os.environ["HD_TOKEN"] = "tok"
    built = []

    def factory(cfg):
        source = live(cfg)[0]
        built.append(source)
        return source

    host = Host(
        make_config(),
        _Front("fake", deck=FakeRenderer(13)),
        mode="remote",
        config_path=str(config_path),
        local_path=str(tmp_path / "local.toml"),
        source_factory=factory,
    )
    host.start()
    try:
        host.app.reload()
        assert host.app._source is built[-1] and len(built) == 2
        assert built[-1]._result_tap == host.services.claim_result
    finally:
        host.close()
        os.environ.pop("HD_TOKEN", None)


def test_unknown_deck_kinds_are_rejected():
    from herdeck.host import open_front

    with pytest.raises(ValueError):
        open_front("nope", 13)




def test_local_mode_reload_keeps_the_embedded_bridge(tmp_path):
    """A profile switch / reload in local mode applies the file settings but
    keeps talking to the embedded bridge the host started."""
    from herdeck.bootstrap import local_config
    from herdeck.driver.fake import FakeRenderer
    from herdeck.host import Host, _Front

    config_path = tmp_path / "config.toml"
    config_path.write_text('[deck]\ngrid = "4x4"\n[theme.colors]\nblocked = "pink"\n')
    runtime = local_config(7654, "secret")
    host = Host(
        runtime,
        _Front("fake", deck=FakeRenderer(14)),
        mode="local",
        config_path=str(config_path),
        local_path=str(tmp_path / "local.toml"),
        source_factory=lambda cfg: live(cfg)[0],
    )
    host.start()
    try:
        host.app.reload()
        config = host.app._source.config
        assert [(s.id, s.url, s.token) for s in config.servers] == [
            ("local", "ws://127.0.0.1:7654", "secret")
        ]
        assert config.grid == (4, 4)
        assert config.theme.colors["blocked"] == "pink"
        assert host.app.slots == 14
    finally:
        host.close()
