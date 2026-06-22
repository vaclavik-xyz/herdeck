import pytest

from herdeck.config import ConfigError
from herdeck.elgato.runtime import discover_ipc


def test_discover_ipc_reads_env(monkeypatch):
    monkeypatch.setenv("HERDECK_ELGATO_SOCK", "/tmp/h.sock")
    monkeypatch.setenv("HERDECK_ELGATO_TOKEN", "abc")
    assert discover_ipc() == ("/tmp/h.sock", "abc")


def test_discover_ipc_requires_both(monkeypatch):
    monkeypatch.delenv("HERDECK_ELGATO_SOCK", raising=False)
    monkeypatch.setenv("HERDECK_ELGATO_TOKEN", "abc")
    with pytest.raises(ConfigError):
        discover_ipc()


def test_session_commands_route_to_connectors():
    # The runtime hands session commands to a sender keyed by server_id.
    from herdeck.commands import Command
    from herdeck.elgato.runtime import build_command_sender

    sent = []
    sender = build_command_sender(send=lambda cmd: sent.append((cmd.kind, cmd.server_id)))
    sender([Command("focus", "dev", "p1"), Command("act_force", "dev", "p1", keys=["ctrl+c"])])
    assert sent == [("focus", "dev"), ("act_force", "dev")]


def _icons():
    class Icons:
        def render_tile_bytes(self, tile):
            return b""

    return Icons()


def _cfg(servers=("dev",)):
    from herdeck.config import DEFAULT_PROFILES, Config, ServerConfig

    return Config(
        servers=[ServerConfig(s, f"ws://{s}", "t") for s in servers],
        profiles=dict(DEFAULT_PROFILES),
        overview_order=list(servers),
        grid=(5, 3),
    )


def test_read_correlator_rejects_stale_read_after_reblock():
    from herdeck.elgato.runtime import ReadCorrelator
    from herdeck.elgato.session import ElgatoSession
    from herdeck.model import AgentKey, AgentState, Status

    sess = ElgatoSession(_cfg(), _icons())
    k = AgentKey("dev", "p1")
    sess.apply_snapshot("dev", [AgentState(k, "claude", "api", Status.BLOCKED)])  # gen 1
    corr = ReadCorrelator(sess)
    corr.issued(k, "r1")
    sess.apply_event("dev", AgentState(k, "claude", "api", Status.WORKING))
    sess.apply_event("dev", AgentState(k, "claude", "api", Status.BLOCKED))  # gen 2
    assert corr.result(k, "r1", "old prompt") is False  # stale read rejected


def test_read_correlator_is_keyed_per_server_not_pane():
    from herdeck.elgato.runtime import ReadCorrelator
    from herdeck.elgato.session import ElgatoSession
    from herdeck.model import AgentKey, AgentState, Status

    sess = ElgatoSession(_cfg(servers=("a", "b")), _icons())
    ka, kb = AgentKey("a", "p1"), AgentKey("b", "p1")  # identical pane id, two servers
    sess.apply_snapshot("a", [AgentState(ka, "claude", "api", Status.BLOCKED)])
    sess.apply_snapshot("b", [AgentState(kb, "claude", "api", Status.BLOCKED)])
    corr = ReadCorrelator(sess)
    corr.issued(ka, "r1")
    corr.issued(kb, "r2")
    assert corr.result(kb, "r2", "B prompt") is True  # server a's read never clobbers b


def test_read_correlator_clear_server_drops_pending():
    from herdeck.elgato.runtime import ReadCorrelator
    from herdeck.elgato.session import ElgatoSession
    from herdeck.model import AgentKey, AgentState, Status

    sess = ElgatoSession(_cfg(), _icons())
    k = AgentKey("dev", "p1")
    sess.apply_snapshot("dev", [AgentState(k, "claude", "api", Status.BLOCKED)])
    corr = ReadCorrelator(sess)
    corr.issued(k, "r1")
    assert corr.has_pending(k) is True
    corr.clear_server("dev")
    assert corr.has_pending(k) is False  # so reconnect can issue a fresh read


def test_default_session_passes_icons_dir_override(tmp_path):
    from herdeck.config import HardwareConfig
    from herdeck.elgato.runtime import _default_session

    cfg = _cfg()
    cfg.hardware = HardwareConfig(icons_dir=str(tmp_path / "icons"))
    sess = _default_session(cfg)
    assert sess._icons._overrides_dir == str(tmp_path / "icons")  # honors custom icon dir
