"""Opt-in live pilot against an ISOLATED T3 data directory, never user's history.

PYTHONPATH=src python tests/t3_live_probe.py --binary /path/to/t3 --base-dir /tmp/pilot --url http://127.0.0.1:13773
The token is issued into memory and never printed. --execute sends one harmless
prompt to the first thread in this disposable instance.
"""
import argparse
import asyncio
import json
import subprocess
import uuid
from datetime import UTC, datetime

from herdeck.config import ServerConfig
from herdeck.t3 import T3Connector


async def main(args):
    token = subprocess.check_output([args.binary, "auth", "session", "issue", "--base-dir",
        args.base_dir, "--token-only", "--ttl", "1h", "--label", "Herdeck disposable pilot"],
        text=True, stderr=subprocess.DEVNULL, timeout=30).strip()
    results = []
    c = T3Connector(ServerConfig("t3-pilot", args.url, token, "t3"),
        on_snapshot=lambda *a: None, on_event=lambda *a: None, on_connection=lambda *a: None,
        on_result=lambda req, data: results.append(data))
    await c.refresh()
    print(json.dumps({"connected": True, "threads": len(c.states)}))
    if not args.execute:
        return
    state = next(iter(c.states.values()))
    if args.scenario != "reply":
        for kind, key, value in [
            ("thread.runtime-mode.set", "runtimeMode", "approval-required"),
            ("thread.interaction-mode.set", "interactionMode", "plan" if args.scenario == "question" else "default"),
        ]:
            c.http.dispatch({"type": kind, "commandId": uuid.uuid4().hex,
                "threadId": state.key.pane_id, key: value, "createdAt": datetime.now(UTC).isoformat()})
        await c.refresh()
        state = c.states[state.key.pane_id]
    prompt = {
        "reply": "Reply exactly HERDECK_PILOT_OK. Do not use tools or modify any files.",
        "stop": "Use a shell tool to sleep for 20 seconds, then reply PILOT_FINISHED. Do not modify files.",
        "approval": "Use a shell tool with escalated permission to run: printf HERDECK_APPROVAL_OK . This is an integration test requiring a real approval prompt. Do not modify files.",
        "question": "Before doing anything, call request_user_input with exactly one single-choice question: Choose pilot colour; options Blue and Green. Do not use other tools or modify files.",
    }[args.scenario]
    await c.send({"type": "backend_action", "pane_id": state.key.pane_id,
        "revision": state.backend_revision, "action": "continue", "req": "pilot",
        "text": prompt})
    print(json.dumps({"dispatch": results[-1]}))
    if not results[-1].get("accepted"):
        raise SystemExit(1)
    seen = set()
    for _ in range(90):
        await asyncio.sleep(1)
        await c.refresh()
        state = c.states[state.key.pane_id]
        seen.add(state.status.value)
        if args.scenario == "stop" and "stop" in state.capabilities:
            await c.send({"type": "backend_action", "pane_id": state.key.pane_id,
                "revision": state.backend_revision, "action": "stop", "req": "stop"})
            for _ in range(15):
                await asyncio.sleep(1)
                await c.refresh()
                if "stop" not in c.states[state.key.pane_id].capabilities:
                    print(json.dumps({"stop_verified": True, "dispatch": results[-1]}))
                    return
            break
        if args.scenario in ("approval", "question") and state.backend_actions:
            option = state.backend_actions[0]
            expected = "answer" if args.scenario == "question" else "approve"
            if option["id"] != expected:
                continue
            await c.send({"type": "backend_action", "pane_id": state.key.pane_id,
                "revision": state.backend_revision, "action": option["id"],
                "payload": option["payload"], "req": "answer"})
            for _ in range(20):
                await asyncio.sleep(1)
                await c.refresh()
                pending = c.states[state.key.pane_id].backend_actions
                if not any(a["payload"].get("requestId") == option["payload"]["requestId"] for a in pending):
                    print(json.dumps({"request_resolution_verified": True, "scenario": args.scenario,
                        "dispatch": results[-1]}))
                    return
            break
        if "HERDECK_PILOT_OK" in state.preview and state.status.value == "idle":
            messages = c._threads[state.key.pane_id].get("messages", [])
            if any(m.get("role") == "assistant" and "HERDECK_PILOT_OK" in m.get("text", "") for m in messages):
                print(json.dumps({"assistant_reply_verified": True, "states": sorted(seen)}))
                return
    print(json.dumps({"assistant_reply_verified": False, "states": sorted(seen)}))
    raise SystemExit(1)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--binary", required=True)
    p.add_argument("--base-dir", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--execute", action="store_true")
    p.add_argument("--scenario", choices=("reply", "stop", "approval", "question"), default="reply")
    asyncio.run(main(p.parse_args()))
