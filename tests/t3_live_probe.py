"""Opt-in live pilot against an ISOLATED T3 data directory, never user's history.

PYTHONPATH=src python tests/t3_live_probe.py --binary /path/to/t3 --base-dir /tmp/pilot --url http://127.0.0.1:13773
The token is issued into memory and never printed. --execute sends one harmless
prompt to the first thread in this disposable instance.
"""
import argparse
import asyncio
import json
import subprocess

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
    await c.send({"type": "backend_action", "pane_id": state.key.pane_id,
        "revision": state.backend_revision, "action": "continue", "req": "pilot",
        "text": "Reply exactly HERDECK_PILOT_OK. Do not use tools or modify any files."})
    print(json.dumps({"dispatch": results[-1]}))
    if not results[-1].get("accepted"):
        raise SystemExit(1)
    seen = set()
    for _ in range(90):
        await asyncio.sleep(1)
        await c.refresh()
        state = c.states[state.key.pane_id]
        seen.add(state.status.value)
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
    asyncio.run(main(p.parse_args()))
