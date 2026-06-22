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


class _Clk:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_blank_read_backs_off_then_allows_retry_after_window():
    from herdeck.elgato.runtime import ReadCorrelator
    from herdeck.elgato.session import ElgatoSession
    from herdeck.model import AgentKey, AgentState, Status

    clk = _Clk()
    sess = ElgatoSession(_cfg(), _icons(), clock=clk)
    k = AgentKey("dev", "p1")
    sess.apply_snapshot("dev", [AgentState(k, "claude", "api", Status.BLOCKED)])
    corr = ReadCorrelator(sess, blank_cooldown=2.0)
    corr.issued(k, "r1")
    # A blank read is not a real prompt: rejected, in-flight pending cleared,
    # but a cooldown opens so _proactive_reads() does NOT immediately re-read (no spin).
    assert corr.result(k, "r1", "   ") is False
    assert corr.has_pending(k) is False
    assert corr.in_cooldown(k) is True
    assert k in sess.blocked_without_detection()  # still needs a real read
    # Within the window the proactive reader skips it; after it elapses, retry is allowed
    # (so the agent is NOT stuck disabled forever — the ticker re-reads).
    clk.now = 2.5
    assert corr.in_cooldown(k) is False


def test_blank_read_cooldown_is_reset_by_reblock():
    from herdeck.elgato.runtime import ReadCorrelator
    from herdeck.elgato.session import ElgatoSession
    from herdeck.model import AgentKey, AgentState, Status

    clk = _Clk()
    sess = ElgatoSession(_cfg(), _icons(), clock=clk)
    k = AgentKey("dev", "p1")
    sess.apply_snapshot("dev", [AgentState(k, "claude", "api", Status.BLOCKED)])  # gen 1
    corr = ReadCorrelator(sess, blank_cooldown=10.0)
    corr.issued(k, "r1")
    corr.result(k, "r1", "")  # blank -> cooldown for gen 1
    assert corr.in_cooldown(k) is True
    sess.apply_event("dev", AgentState(k, "claude", "api", Status.WORKING))
    sess.apply_event("dev", AgentState(k, "claude", "api", Status.BLOCKED))  # gen 2
    assert corr.in_cooldown(k) is False  # a fresh block episode retries immediately


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


def _copy_assets_and_bake(tmp_path):
    """Stage a baked assets dir (svg + content-keyed png) like the build does."""
    import os
    import shutil

    import pytest

    pytest.importorskip("cairosvg")

    from herdeck.elgato import frozen

    src = "src/herdeck/assets"
    staged = tmp_path / "herdeck_assets"
    staged.mkdir()
    for name in os.listdir(src):
        if name.endswith(".svg"):
            shutil.copy(os.path.join(src, name), staged / name)
    frozen.prerasterize_assets(str(staged), str(staged), frozen.BAKE_SIZE)
    return str(staged)


def test_frozen_session_uses_png_rasterizer(tmp_path):
    import os

    from herdeck.elgato.runtime import _frozen_session

    baked = _copy_assets_and_bake(tmp_path)
    sess = _frozen_session(_cfg(), baked)
    icons = sess._icons
    # PNG-loading rasterizer + bundled assets dir + offline fetch.
    assert icons._assets_dir == baked
    assert icons._fetch("claude") is None  # no network when frozen
    # The bundled-asset agent (codex.svg -> baked PNG) renders without cairosvg.
    name = icons.icon_for("codex", "green")
    assert os.path.exists(os.path.join(icons._cache_dir, name))


def test_session_for_runtime_dispatches_on_frozen(monkeypatch, tmp_path):
    import sys

    from herdeck.elgato import runtime

    baked = _copy_assets_and_bake(tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(runtime, "_baked_assets_dir", lambda: baked)
    sess = runtime._session_for_runtime(_cfg())
    assert sess._icons._assets_dir == baked  # frozen branch taken


def test_session_for_runtime_uses_default_when_not_frozen(monkeypatch):
    import sys

    from herdeck.elgato import runtime
    from herdeck.icons import _ASSETS_DIR, _default_fetch

    monkeypatch.delattr(sys, "frozen", raising=False)
    sess = runtime._session_for_runtime(_cfg())
    # Dev path unchanged: package assets dir + real network fetch.
    assert sess._icons._assets_dir == _ASSETS_DIR
    assert sess._icons._fetch is _default_fetch


def test_serve_elgato_default_make_session_is_runtime_dispatcher():
    import inspect

    from herdeck.elgato.runtime import _session_for_runtime, serve_elgato

    assert inspect.signature(serve_elgato).parameters["make_session"].default is _session_for_runtime
