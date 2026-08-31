import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts/check-herdr-api.py"
SPEC = importlib.util.spec_from_file_location("check_herdr_api", SCRIPT_PATH)
assert SPEC and SPEC.loader
contract = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(contract)


def _schema() -> dict:
    request_defs = {}
    variants = []
    for index, (method, fields) in enumerate(contract.REQUIRED_METHOD_PARAMS.items()):
        definition = f"Params{index}"
        request_defs[definition] = {
            "type": "object",
            "properties": {field: {"type": "string"} for field in fields},
            "required": sorted(fields),
        }
        variants.append(
            {
                "properties": {
                    "method": {"const": method},
                    "params": {"$ref": f"#/schemas/request/$defs/{definition}"},
                }
            }
        )
    request_defs["Subscription"] = {
        "oneOf": [
            {"properties": {"type": {"const": event}}}
            for event in sorted(contract.bridge_subscription_events())
        ]
    }
    response_defs = {
        definition: {
            "properties": {field: {"type": "string"} for field in fields}
        }
        for definition, fields in contract.REQUIRED_RESPONSE_FIELDS.items()
    }
    return {
        "protocol": contract.PINNED_PROTOCOL,
        "schemas": {
            "request": {"$defs": request_defs, "oneOf": variants},
            "success_response": {"$defs": response_defs},
        },
    }


def test_contract_accepts_complete_schema():
    message = contract.validate_schema(_schema())

    assert "12 methods" in message
    assert "response fields" in message


def test_contract_reports_missing_method_and_response_field():
    schema = _schema()
    schema["schemas"]["request"]["oneOf"] = [
        item
        for item in schema["schemas"]["request"]["oneOf"]
        if item["properties"]["method"]["const"] != "agent.prompt"
    ]
    del schema["schemas"]["success_response"]["$defs"]["AgentInfo"]["properties"][
        "state_labels"
    ]

    with pytest.raises(contract.ContractError) as exc:
        contract.validate_schema(schema)

    assert "missing method agent.prompt" in str(exc.value)
    assert "AgentInfo is missing: state_labels" in str(exc.value)


def test_contract_reports_bridge_subscription_drift():
    schema = _schema()
    subscriptions = schema["schemas"]["request"]["$defs"]["Subscription"]["oneOf"]
    subscriptions[:] = [
        item
        for item in subscriptions
        if item["properties"]["type"]["const"] != "pane.agent_status_changed"
    ]

    with pytest.raises(contract.ContractError, match="pane.agent_status_changed"):
        contract.validate_schema(schema)


def test_pinned_download_metadata_is_immutable_and_sha256_sized():
    assert set(contract.PINNED_ASSETS) == {
        ("Linux", "x86_64"),
        ("Linux", "aarch64"),
        ("Darwin", "arm64"),
        ("Darwin", "x86_64"),
    }
    for url, sha256 in contract.PINNED_ASSETS.values():
        assert f"/v{contract.PINNED_HERDR_VERSION}/" in url
        assert len(sha256) == 64
        int(sha256, 16)
