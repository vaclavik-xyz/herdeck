import json

import pytest

from herdeck.config import ServerConfig
from herdeck.connector import Connector
from herdeck.protocol import Settings, decode_inbound


def _frame(**over):
    f = {
        "type": "settings",
        "server_id": "b",
        "revision": 3,
        "updated_at_ms": 1700000000000,
        "updated_by": "mbp",
        "settings": {"view": {"language": "cs"}},
    }
    f.update(over)
    return json.dumps(f)


def test_settings_frame_decodes():
    assert decode_inbound(_frame()) == Settings(
        "b", 3, 1700000000000, "mbp", {"view": {"language": "cs"}}
    )


def test_settings_null_and_updated_by_truncated():
    msg = decode_inbound(_frame(settings=None, updated_by="x" * 100))
    assert msg.settings is None
    assert msg.updated_by == "x" * 64


@pytest.mark.parametrize(
    "over",
    [
        {"revision": "3"},
        {"revision": True},
        {"revision": -1},
        {"updated_at_ms": 1.5},
        {"updated_at_ms": -5},
        {"settings": []},
        {"settings": "x"},
        {"server_id": 1},
        {"updated_by": 5},
    ],
)
def test_settings_malformed_raises(over):
    with pytest.raises(ValueError):
        decode_inbound(_frame(**over))


def test_connector_routes_settings_under_config_id():
    seen = []
    conn = Connector(
        ServerConfig(id="cfg", url="ws://x", token="t"),
        on_snapshot=lambda sid, states: None,
        on_event=lambda sid, state: None,
        on_connection=lambda sid, up: None,
        on_settings=lambda sid, s: seen.append((sid, s)),
    )
    conn._dispatch(_frame())
    assert seen[0][0] == "cfg"
    assert seen[0][1].revision == 3
