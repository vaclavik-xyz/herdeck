from herdeck.commands import Command, build_action_command, command_to_msg, profile_for
from herdeck.config import DEFAULT_PROFILES, Config
from herdeck.model import AgentKey, AgentState, Status


def _agent(server="dev", pane="p1", agent_type="claude"):
    return AgentState(AgentKey(server, pane), agent_type, "lbl", Status.BLOCKED)


def _config(profiles=None):
    return Config(servers=[], profiles=profiles or dict(DEFAULT_PROFILES),
                  overview_order=[], grid=(5, 3))


def test_command_to_msg_list_has_no_req():
    assert command_to_msg(Command("list", "dev"), None) == {"type": "list"}


def test_command_to_msg_read():
    m = command_to_msg(Command("read", "dev", "p1", source="detection"), "r1")
    assert m == {"type": "read", "req": "r1", "pane_id": "p1", "source": "detection"}


def test_command_to_msg_focus():
    assert command_to_msg(Command("focus", "dev", "p1"), "r2") == {
        "type": "focus", "req": "r2", "pane_id": "p1"}


def test_command_to_msg_send_text():
    m = command_to_msg(Command("send_text", "dev", "p1", text="hi"), "r3")
    assert m == {"type": "send_text", "req": "r3", "pane_id": "p1", "text": "hi"}


def test_command_to_msg_guarded_choice():
    command = Command(
        "choose_if_blocked",
        "dev",
        "p1",
        text="2",
        terminal_id="term-1",
        decision_revision="a" * 64,
    )

    assert command_to_msg(command, "r4") == {
        "type": "choose_if_blocked",
        "req": "r4",
        "pane_id": "p1",
        "choice": "2",
        "terminal_id": "term-1",
        "decision_revision": "a" * 64,
    }


def test_command_to_msg_start():
    m = command_to_msg(Command("start", "dev", text="claude", keys=["claude"]), "r4")
    assert m == {"type": "start", "req": "r4", "name": "claude", "argv": ["claude"]}


def test_command_to_msg_act_guard_flags():
    assert command_to_msg(Command("act_if_blocked", "dev", "p1", keys=["1"]), "r5")["guard"] is True
    assert command_to_msg(Command("act_force", "dev", "p1", keys=["ctrl+c"]), "r6")["guard"] is False


def test_action_command_carries_expected_terminal_identity():
    from herdeck.config import AnswerProfile
    from herdeck.model import AgentKey, AgentState, Status

    agent = AgentState(
        AgentKey("dev", "p1"),
        "claude",
        "api",
        Status.BLOCKED,
        terminal_id="term-123",
    )
    profile = AnswerProfile(["1"], ["2"], ["ctrl+c"], ["3"])

    command = build_action_command("approve", agent, profile, force=False, always=False)

    assert command.terminal_id == "term-123"
    assert command_to_msg(command, "r7")["terminal_id"] == "term-123"


def test_command_to_msg_unknown_raises():
    import pytest
    with pytest.raises(ValueError):
        command_to_msg(Command("bogus", "dev"), "r7")


def test_profile_for_known_and_fallback():
    cfg = _config()
    assert profile_for(cfg, "codex").approve == ["y", "enter"]
    assert profile_for(cfg, "nonexistent") is cfg.profiles["default"]


def test_build_action_command_approve_guarded_default():
    cmd = build_action_command("approve", _agent(), profile_for(_config(), "claude"),
                               force=False, always=False)
    assert cmd.kind == "act_if_blocked"
    assert cmd.keys == ["1", "enter"]
    assert cmd.pane_id == "p1" and cmd.server_id == "dev"


def test_build_action_command_approve_always_and_force():
    cmd = build_action_command("approve", _agent(), profile_for(_config(), "claude"),
                               force=True, always=True)
    assert cmd.kind == "act_force"
    assert cmd.keys == ["2", "enter"]


def test_build_action_command_deny_and_stop():
    p = profile_for(_config(), "claude")
    assert build_action_command("deny", _agent(), p, force=False, always=False).kind == "act_if_blocked"
    stop = build_action_command("stop", _agent(), p, force=False, always=False)
    assert stop.kind == "act_force" and stop.keys == ["ctrl+c"]
