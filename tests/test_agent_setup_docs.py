import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_agent_setup_runbook_is_linked_from_agent_and_user_docs():
    runbook = ROOT / "docs/agent-setup.md"

    assert runbook.exists()
    assert "docs/agent-setup.md" in (ROOT / "README.md").read_text()
    assert "docs/agent-setup.md" in (ROOT / "AGENTS.md").read_text()


def test_local_example_documents_multi_session_contract():
    data = tomllib.loads((ROOT / "local.example.toml").read_text())

    assert data["local"]["herdr_sessions"] == ["default", "review"]


def test_agent_runbook_covers_setup_completion_and_safety_contracts():
    text = (ROOT / "docs/agent-setup.md").read_text()

    required = (
        "herdr session list --json",
        "herdr_sessions",
        "HERDECK_MOCK",
        "onboarding.toml",
        "token_env",
        "keyring.set_password",
        "herdeck-service",
        "--system",
        "/Library/LaunchDaemons",
        "herdeck-doctor",
        "herdeck.deckapp",
        'CONFIG_PATH="${HERDECK_CONFIG',
        'HERDECK_LOCAL_CONFIG="$LOCAL_PATH"',
        ".result.snapshot.agents",
        "openssl rand -hex 32",
        "connections",
        "Rollback",
        "Never include token values",
    )
    assert all(term in text for term in required)
