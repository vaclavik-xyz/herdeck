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




@pytest.mark.parametrize("keys", [6, 15, 32])
def test_elgato_usb_deck_uses_its_own_key_layout(keys, tmp_path, monkeypatch):
    """Mini (6), MK.2 (15), XL (32): tiles fill key_count - 2 keys, the last two
    keys are the panel and their presses reach the runtime — whatever the
    configured grid, and across a config reload."""
    from test_driver_elgato import FakeDeck, FakeIcons, _wait_until

    from herdeck.driver.elgato import ElgatoDriver
    from herdeck.host import Host, _Front

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[[servers]]\nid = "local"\nurl = "ws://bridge"\ntoken_env = "HD_TOKEN"\n'
        '[deck]\ngrid = "4x4"\noverview_order = ["local"]\n'
    )
    monkeypatch.setenv("HD_TOKEN", "tok")
    device = FakeDeck(key_count=keys)
    driver = ElgatoDriver(device=device, icon_provider=FakeIcons())
    monkeypatch.setattr(driver, "_to_native", lambda image: image.tobytes())
    host = Host(
        make_config(),  # grid 5x3: 13 tiles if the grid decided
        _Front("elgato", deck=driver),
        mode="remote",
        config_path=str(config_path),
        local_path=str(tmp_path / "local.toml"),
        source_factory=lambda cfg: live(cfg)[0],
    )
    host.start()
    try:
        slots = keys - 2
        assert host.app.slots == slots
        assert _wait_until(lambda: set(device.images) == set(range(keys)))
        presses = []
        host.app._source.press = lambda index: presses.append(index) or []
        device.callback(device, slots, True)  # left panel key
        device.callback(device, slots + 1, True)  # right panel key
        assert presses == [slots, slots + 1]
        host.app.reload()  # the 4x4 grid of the file must not reshape the deck
        assert host.app.slots == slots and host.app._orch.slots == slots
    finally:
        host.close()
