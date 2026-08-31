#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import platform
import re
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE_PATH = ROOT / "src/herdeck/bridge.py"

PINNED_HERDR_VERSION = "0.8.2"
PINNED_PROTOCOL = 20
_RELEASE_BASE = f"https://github.com/herdrdev/herdr/releases/download/v{PINNED_HERDR_VERSION}"
PINNED_ASSETS: dict[tuple[str, str], tuple[str, str]] = {
    ("Linux", "x86_64"): (
        f"{_RELEASE_BASE}/herdr-linux-x86_64",
        "976150a14d490c94b243ea2e1a7eb2dfb67f12e36b182db90936f6728e6aecf4",
    ),
    ("Linux", "aarch64"): (
        f"{_RELEASE_BASE}/herdr-linux-aarch64",
        "f55610658e1c2e0d2aaef730b4b2ab885f7f8ba00285ab372bfb14f2e3d5b40d",
    ),
    ("Darwin", "arm64"): (
        f"{_RELEASE_BASE}/herdr-macos-aarch64",
        "a5d4f4d504d8b309c91f811050559300faba31258425f53c50852fc96f6ae574",
    ),
    ("Darwin", "x86_64"): (
        f"{_RELEASE_BASE}/herdr-macos-x86_64",
        "ab50262c8190cd7aa9056d249d255c08c328c3e8716de9cfa29db4f131b8e2c1",
    ),
}

# Current-protocol calls made by SocketHerdr. The protocol-16 agent.send and
# agent.start(argv=...) branches are deliberately excluded: a 0.8.2 schema no
# longer advertises that legacy surface, while Herdeck keeps it for old servers.
REQUIRED_METHOD_PARAMS: dict[str, set[str]] = {
    "agent.focus": {"target"},
    "agent.prompt": {"target", "text"},
    "agent.start": {"name", "kind", "pane_id", "args"},
    "events.subscribe": {"subscriptions"},
    "pane.list": set(),
    "pane.read": {"pane_id", "source"},
    "pane.send_keys": {"pane_id", "keys"},
    "pane.send_text": {"pane_id", "text"},
    "session.snapshot": set(),
    "tab.close": {"tab_id"},
    "tab.create": {"focus", "label"},
    "worktree.list": {"workspace_id"},
}

REQUIRED_RESPONSE_FIELDS: dict[str, set[str]] = {
    "SessionSnapshot": {"version", "protocol", "agents", "workspaces", "tabs"},
    "AgentInfo": {
        "pane_id",
        "terminal_id",
        "workspace_id",
        "tab_id",
        "agent",
        "agent_status",
        "cwd",
        "foreground_cwd",
        "tokens",
        "state_labels",
        "title",
        "terminal_title_stripped",
    },
    "WorkspaceInfo": {"workspace_id", "number", "label"},
    "TabInfo": {"tab_id", "workspace_id", "number", "label"},
    "PaneReadResult": {"text", "truncated"},
    "WorktreeInfo": {"path", "open_workspace_id", "label", "branch"},
}


class ContractError(RuntimeError):
    pass


def _literal_tuple(tree: ast.Module, name: str) -> tuple[str, ...]:
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            value = ast.literal_eval(node.value)
            if not isinstance(value, tuple) or any(not isinstance(item, str) for item in value):
                raise ContractError(f"{name} must be a literal tuple of strings")
            return value
    raise ContractError(f"could not find {name} in {BRIDGE_PATH.relative_to(ROOT)}")


def bridge_subscription_events(path: Path = BRIDGE_PATH) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    return {
        *_literal_tuple(tree, "_GLOBAL_EVENT_TYPES"),
        *_literal_tuple(tree, "_LABEL_EVENT_TYPES"),
        "pane.agent_status_changed",
    }


def _request_methods(schema: dict) -> dict[str, dict]:
    request = schema.get("schemas", {}).get("request", {})
    defs = request.get("$defs", {})
    methods: dict[str, dict] = {}
    for variant in request.get("oneOf", []):
        properties = variant.get("properties", {})
        method = properties.get("method", {}).get("const")
        params_ref = properties.get("params", {}).get("$ref", "")
        if not isinstance(method, str) or not isinstance(params_ref, str):
            continue
        methods[method] = defs.get(params_ref.rsplit("/", 1)[-1], {})
    return methods


def _subscription_events(schema: dict) -> set[str]:
    subscription = (
        schema.get("schemas", {})
        .get("request", {})
        .get("$defs", {})
        .get("Subscription", {})
    )
    events: set[str] = set()
    for variant in subscription.get("oneOf", []):
        value = variant.get("properties", {}).get("type", {}).get("const")
        if isinstance(value, str):
            events.add(value)
    return events


def validate_schema(schema: dict, *, bridge_path: Path = BRIDGE_PATH) -> str:
    errors: list[str] = []
    if schema.get("protocol") != PINNED_PROTOCOL:
        errors.append(f"protocol is {schema.get('protocol')!r}, expected {PINNED_PROTOCOL}")

    methods = _request_methods(schema)
    for method, sent_fields in sorted(REQUIRED_METHOD_PARAMS.items()):
        params = methods.get(method)
        if params is None:
            errors.append(f"missing method {method}")
            continue
        accepted = set(params.get("properties", {}))
        missing_fields = sent_fields - accepted
        if missing_fields:
            errors.append(f"{method} rejects fields: {', '.join(sorted(missing_fields))}")
        required = set(params.get("required", []))
        unsent_required = required - sent_fields
        if unsent_required:
            errors.append(
                f"{method} newly requires fields Herdeck does not send: "
                + ", ".join(sorted(unsent_required))
            )

    schema_events = _subscription_events(schema)
    missing_events = bridge_subscription_events(bridge_path) - schema_events
    if missing_events:
        errors.append("unsupported subscriptions: " + ", ".join(sorted(missing_events)))

    response_defs = schema.get("schemas", {}).get("success_response", {}).get("$defs", {})
    checked_fields = 0
    for definition, required_fields in REQUIRED_RESPONSE_FIELDS.items():
        properties = set(response_defs.get(definition, {}).get("properties", {}))
        missing_fields = required_fields - properties
        checked_fields += len(required_fields)
        if missing_fields:
            errors.append(f"{definition} is missing: {', '.join(sorted(missing_fields))}")

    if errors:
        raise ContractError("Herdr API contract failed:\n- " + "\n- ".join(errors))
    return (
        f"Herdr API protocol {PINNED_PROTOCOL} satisfies {len(REQUIRED_METHOD_PARAMS)} methods, "
        f"{len(bridge_subscription_events(bridge_path))} subscriptions, and "
        f"{checked_fields} response fields"
    )


def _schema_from_binary(binary: Path) -> dict:
    version_result = subprocess.run(
        [str(binary), "--version"], text=True, capture_output=True, check=True
    )
    match = re.search(r"\b(\d+\.\d+\.\d+)\b", version_result.stdout)
    actual = match.group(1) if match else "unknown"
    if actual != PINNED_HERDR_VERSION:
        raise ContractError(f"Herdr binary is {actual}, expected {PINNED_HERDR_VERSION}")
    result = subprocess.run(
        [str(binary), "api", "schema", "--json"], text=True, capture_output=True, check=True
    )
    return json.loads(result.stdout)


def _download_pinned_binary(directory: Path) -> Path:
    destination = directory / "herdr"
    machine = platform.machine()
    if platform.system() == "Linux" and machine in {"aarch64", "arm64"}:
        machine = "aarch64"
    key = (platform.system(), machine)
    try:
        url, expected_sha256 = PINNED_ASSETS[key]
    except KeyError as exc:
        raise ContractError(f"no pinned Herdr API asset for {key[0]} {key[1]}") from exc
    request = urllib.request.Request(url, headers={"User-Agent": "herdeck-ci"})
    digest = hashlib.sha256()
    with urllib.request.urlopen(request) as response, destination.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != expected_sha256:
        raise ContractError(
            f"downloaded Herdr SHA-256 is {actual}, expected {expected_sha256}"
        )
    destination.chmod(0o755)
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check Herdeck against the pinned Herdr API")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--herdr", type=Path, help="path to a pinned Herdr binary")
    source.add_argument("--schema", type=Path, help="path to an exported Herdr schema")
    source.add_argument(
        "--download", action="store_true", help="download and verify the pinned Linux binary"
    )
    args = parser.parse_args(argv)

    try:
        if args.schema:
            schema = json.loads(args.schema.read_text())
        elif args.download:
            with tempfile.TemporaryDirectory(prefix="herdeck-herdr-api-") as temp:
                schema = _schema_from_binary(_download_pinned_binary(Path(temp)))
        else:
            resolved = args.herdr or (Path(found) if (found := shutil.which("herdr")) else None)
            if resolved is None:
                parser.error("herdr is not on PATH; pass --herdr, --schema, or --download")
            schema = _schema_from_binary(resolved)
        print(validate_schema(schema))
    except (ContractError, json.JSONDecodeError, OSError, subprocess.CalledProcessError) as exc:
        print(exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
