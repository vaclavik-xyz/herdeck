from herdeck.model import Status, AgentKey, AgentState


def test_status_values():
    assert Status.BLOCKED.value == "blocked"
    assert Status("working") is Status.WORKING


def test_agent_key_is_hashable_and_equal():
    a = AgentKey("workbox", "w1:p1")
    b = AgentKey("workbox", "w1:p1")
    assert a == b
    assert {a, b} == {a}


def test_agent_state_defaults():
    s = AgentState(
        key=AgentKey("workbox", "w1:p1"),
        agent_type="claude",
        label="api",
        status=Status.BLOCKED,
    )
    assert s.project == ""
    assert s.status is Status.BLOCKED
