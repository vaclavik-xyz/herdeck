"""Desktop agent card for T3 agents: options sharing an action id."""

from test_agent_card import blocked, make


def t3_blocked(**kw):
    agent = blocked(**kw)
    agent.backend = "t3"
    agent.backend_revision = "rev-1"
    agent.backend_actions = [
        {"id": "answer", "label": "Red", "payload": {"requestId": "r", "answers": ["red"]}},
        {"id": "answer", "label": "Blue", "payload": {"requestId": "r", "answers": ["blue"]}},
        {"id": "deny", "label": "Decline", "payload": {"requestId": "r", "decision": "decline"}},
    ]
    return agent


def backend_msgs(runner):
    return [m for m in runner.sent if m["type"] == "backend_action"]


def test_t3_answer_sends_the_chosen_options_own_payload():
    app, src, runner, _ = make(reply=lambda m: {"ok": True, "sent": True, "accepted": True})
    src._on_snapshot("prod", [t3_blocked()])
    detail = src.card_detail("prod", "p0")
    assert len({o["key"] for o in detail["options"]}) == 3
    blue = next(o for o in detail["options"] if o["label"] == "Blue")
    assert src.card_answer("prod", "p0", blue["key"], detail["revision"])["code"] == "sent"
    (msg,) = backend_msgs(runner)
    assert msg["action"] == "answer"
    assert msg["payload"] == {"requestId": "r", "answers": ["blue"]}
