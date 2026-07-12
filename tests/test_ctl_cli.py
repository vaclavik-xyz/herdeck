import json

import pytest

from herdeck.config import DEFAULT_PROFILES, Config, ServerConfig
from herdeck.ctl import (
    EXIT_OK,
    EXIT_SKIPPED,
    EXIT_TARGET,
    EXIT_USAGE,
    build_parser,
    dispatch,
)
from herdeck.model import AgentKey, AgentState, Status


def _config():
    return Config(servers=[ServerConfig("dev", "ws://x", "tok")],
                  profiles=dict(DEFAULT_PROFILES), overview_order=[], grid=(5, 3))


class StubSession:
    """Stands in for an opened CtlSession for dispatch-level tests."""
    def __init__(self, agents):
        self.config = _config()
        self.agents = {a.key: a for a in agents}
        self.acted = None

    def resolve_target(self, spec):
        from herdeck.ctl import CtlSession
        return CtlSession.resolve_target(self, spec)

    async def act(self, action, agent, **kw):
        self.acted = (action, agent.key.pane_id, kw)
        return {"result": "sent", "settled": True}

    async def request(self, cmd, *, timeout):
        self.acted = ("request", cmd.kind, cmd.pane_id)
        return {"sent": True}

    async def wait(self, predicate, *, timeout):
        return predicate()  # already-satisfied path


def test_parser_rejects_unknown_status():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["wait", "--any", "--until", "bogus"])


def test_wait_has_independent_unlimited_timeout():
    args = build_parser().parse_args(["wait", "--any", "--until", "blocked"])
    assert args.wait_timeout is None       # wait waits forever by default (N1)
    assert args.timeout == 10.0            # global connect/request default unchanged


@pytest.mark.asyncio
async def test_dispatch_wait_requires_exactly_one_selector(capsys):
    args = build_parser().parse_args(["wait", "--until", "blocked"])  # neither agent nor --any
    assert await dispatch(args, StubSession([])) == EXIT_USAGE  # N2
    assert "exactly one" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_dispatch_ls_json(capsys):
    a = AgentState(AgentKey("dev", "p1"), "claude", "auth", Status.BLOCKED, repo="herdeck")
    args = build_parser().parse_args(["ls", "--json"])
    rc = await dispatch(args, StubSession([a]))
    assert rc == EXIT_OK
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["pane_id"] == "p1" and rows[0]["status"] == "blocked"


@pytest.mark.asyncio
async def test_dispatch_approve_calls_act():
    a = AgentState(AgentKey("dev", "p1"), "claude", "auth", Status.BLOCKED)
    args = build_parser().parse_args(["approve", "dev:p1"])
    sess = StubSession([a])
    assert await dispatch(args, sess) == EXIT_OK
    assert sess.acted[0] == "approve"


@pytest.mark.asyncio
async def test_dispatch_unknown_target_exit4(capsys):
    args = build_parser().parse_args(["approve", "ghost"])
    assert await dispatch(args, StubSession([])) == EXIT_TARGET
    assert "no agent" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_dispatch_skipped_exit3(capsys):
    a = AgentState(AgentKey("dev", "p1"), "claude", "auth", Status.IDLE)

    class SkipSession(StubSession):
        async def act(self, action, agent, **kw):
            return {"result": "skipped", "settled": True}

    args = build_parser().parse_args(["approve", "dev:p1"])
    assert await dispatch(args, SkipSession([a])) == EXIT_SKIPPED


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["send", "focus"])
async def test_dispatch_direct_command_surfaces_identity_skip(command, capsys):
    a = AgentState(
        AgentKey("dev", "p1"),
        "claude",
        "auth",
        Status.BLOCKED,
        terminal_id="term-old",
    )

    class SkipRequestSession(StubSession):
        async def request(self, cmd, *, timeout):
            return {"skipped": True, "message": "agent identity changed"}

    argv = [command, "dev:p1"]
    if command == "send":
        argv.append("hello")
    args = build_parser().parse_args(argv)

    assert await dispatch(args, SkipRequestSession([a])) == EXIT_SKIPPED
    assert "agent identity changed" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_dispatch_wait_any_returns_matched_agent(capsys):
    a = AgentState(AgentKey("dev", "p1"), "claude", "auth", Status.BLOCKED)
    args = build_parser().parse_args(["wait", "--any", "--until", "blocked", "--json"])
    rc = await dispatch(args, StubSession([a]))
    assert rc == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out == {"agent": "dev:p1", "status": "blocked"}


def test_common_options_parse_in_either_position():
    """`--json` / `--server` / `--timeout` must work before AND after the
    subcommand (audit: ctl-arg-order — the README documented the trap)."""
    p = build_parser()
    assert p.parse_args(["ls", "--json"]).json is True
    assert p.parse_args(["--json", "ls"]).json is True
    assert p.parse_args(["ls"]).json is False
    assert p.parse_args(["--server", "a", "ls"]).server == "a"
    assert p.parse_args(["ls", "--server", "a"]).server == "a"
    assert p.parse_args(["approve", "x", "--timeout", "5"]).timeout == 5.0
    assert p.parse_args(["--timeout", "5", "approve", "x"]).timeout == 5.0
    assert p.parse_args(["ls"]).timeout == 10.0
    # wait keeps its OWN --timeout (max wait), independent of the connect knob
    w = p.parse_args(["wait", "--any", "--until", "blocked", "--timeout", "60"])
    assert w.wait_timeout == 60.0
    assert w.timeout == 10.0


@pytest.mark.asyncio
async def test_amain_resolves_socket_from_the_loaded_config(tmp_path, monkeypatch):
    """herdeck-ctl must honour [hardware].herdr_socket like the deck does
    (roborev 5d162de)."""
    import herdeck.bootstrap as bootstrap_mod
    from herdeck.ctl import _amain

    sock = tmp_path / "custom.sock"
    config = tmp_path / "config.toml"
    config.write_text("")  # serverless config; the socket override lives in local.toml
    (tmp_path / "local.toml").write_text(f'[local]\nherdr_socket = "{sock}"\n')
    monkeypatch.delenv("HERDR_SOCKET", raising=False)
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    monkeypatch.delenv("HERDR_SESSION", raising=False)
    monkeypatch.delenv("HERDECK_LOCAL_CONFIG", raising=False)
    seen = {}
    real = bootstrap_mod.resolve_socket_path

    def recording(cfg=None, **kw):
        seen["hardware_socket"] = cfg.hardware.herdr_socket if cfg else None
        return real(cfg, **kw)

    monkeypatch.setattr(bootstrap_mod, "resolve_socket_path", recording)
    args = build_parser().parse_args(["--config", str(config), "ls"])
    rc = await _amain(args)  # no socket file + no servers -> clean error exit
    assert seen["hardware_socket"] == str(sock)  # the CONFIG override reached the resolver
    assert rc != 0


def test_parser_accepts_waiting_status():
    # WAITING is a real state the protocol can produce (herdwatch-held panes),
    # so `ls --status waiting` / `wait --until waiting` must be accepted.
    args = build_parser().parse_args(["ls", "--status", "waiting"])
    assert args.status == "waiting"
    args = build_parser().parse_args(["wait", "--any", "--until", "waiting"])
    assert args.until == "waiting"
