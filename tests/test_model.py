from herdeck.model import AgentKey, AgentState, Status, WorkContext


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
    assert s.work == WorkContext()


def test_work_context_accepts_bounded_labels_and_https_urls():
    context = WorkContext.from_state_labels(
        {
            "work.source": "github",
            "work.item": "vaclavik-xyz/persOS#123",
            "work.run": "run-42",
            "work.url": "https://github.com/vaclavik-xyz/persOS/issues/123",
        }
    )

    assert context == WorkContext(
        source="github",
        item="vaclavik-xyz/persOS#123",
        run="run-42",
        url="https://github.com/vaclavik-xyz/persOS/issues/123",
    )

    rejected = WorkContext.from_state_labels(
        {
            "work.source": "x" * 100,
            "work.item": "i" * 300,
            "work.run": "r" * 300,
            "work.url": "http://insecure.example/run",
        }
    )
    assert rejected.source == "x" * 64
    assert rejected.item == "i" * 160
    assert rejected.run == "r" * 160
    assert rejected.url == ""


def test_work_context_rejects_malformed_https_url_without_breaking_snapshot():
    assert WorkContext.from_state_labels({"work.url": "https://["}).url == ""
