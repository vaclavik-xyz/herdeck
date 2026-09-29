import json
import time

from herdeck.config import ServerConfig
from herdeck.connector import Connector
from herdeck.deckapp.live import LiveSource
from herdeck.protocol import Presence, decode_inbound
from tests.test_deckapp_live import notify_config


class FakeIdle:
    def __init__(self, idle):
        self.idle = idle

    def idle_seconds(self):
        return self.idle


def test_presence_frame_decodes():
    msg = decode_inbound(json.dumps({"type": "presence", "server_id": "b", "idle_s": 12.5, "clients": 2}))
    assert msg == Presence("b", 12.5, 2)
    msg = decode_inbound(json.dumps({"type": "presence", "server_id": "b", "idle_s": None, "clients": 0}))
    assert msg.idle_s is None


def test_connector_routes_presence_under_config_id():
    seen = []
    conn = Connector(
        ServerConfig(id="cfg", url="ws://x", token="t"),
        on_snapshot=lambda sid, states: None,
        on_event=lambda sid, state: None,
        on_connection=lambda sid, up: None,
        on_presence=lambda sid, idle: seen.append((sid, idle)),
    )
    conn._dispatch(json.dumps({"type": "presence", "server_id": "b", "idle_s": 3.0, "clients": 1}))
    assert seen == [("cfg", 3.0)]


def _live(local_idle, now):
    config, server = notify_config()
    src = LiveSource(config, server, idle_probe=FakeIdle(local_idle))
    src._presence_clock = lambda: now[0]
    return src, server


def test_remote_activity_means_not_away():
    now = [1000.0]
    src, server = _live(3600.0, now)  # this Mac idle for an hour
    assert src._user_away(300) is True
    src._on_presence(server.id, 10.0)  # but the user types on another Mac
    assert src._user_away(300) is False
    now[0] += 400  # the report is older than PRESENCE_STALE_S: ignored, local 3600 s remains
    assert src._user_away(300) is True


def test_remote_idle_ages_and_nothing_known_is_away():
    now = [0.0]
    src, server = _live(None, now)
    assert src._user_away(60) is True
    src._on_presence(server.id, 30.0)
    now[0] = 20.0
    assert src._user_away(60) is False  # 50 s
    now[0] = 40.0
    assert src._user_away(60) is True  # 70 s
    src._on_presence(server.id, None)
    assert src._user_away(60) is True


def test_disconnect_forgets_remote_presence():
    now = [0.0]
    src, server = _live(3600.0, now)
    src._on_presence(server.id, 1.0)
    src._on_connection(server.id, False)
    assert src._user_away(300) is True


class Runner:
    def __init__(self, caps):
        self.sent = []
        self.connector = type("C", (), {"capabilities": frozenset(caps)})()

    def send(self, msg):
        self.sent.append(msg)

    def close(self):
        pass


def test_report_goes_only_to_connected_bridges_offering_presence():
    config, server = notify_config()
    src = LiveSource(config, server, idle_probe=FakeIdle(12.0))
    old, new = Runner([]), Runner(["presence"])
    src._runners = {"old": old, "new": new}
    src._connected = {"old": True, "new": True}
    assert src.report_presence() == 1
    assert new.sent == [{"type": "presence", "idle_s": 12.0}]
    assert old.sent == []
    src._connected["new"] = False
    assert src.report_presence() == 0


def test_local_idle_is_min_of_hid_and_deck_press():
    config, server = notify_config()
    src = LiveSource(config, server, idle_probe=FakeIdle(None))
    assert src._local_idle() is None
    src._last_deck_press = time.monotonic() - 5
    assert 4 <= src._local_idle() <= 6
