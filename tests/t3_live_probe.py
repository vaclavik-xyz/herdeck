"""Opt-in live probe of T3 orchestration protocol 2 against an ISOLATED T3 instance.

PYTHONPATH=src:tests python tests/t3_live_probe.py --binary /path/to/t3 --base-dir /tmp/pilot --url http://127.0.0.1:13773

Never point it at your real T3 data directory: it flips the read state of the
first thread (thread.mark-unread, then thread.visit). The least-privilege token
(orchestration:read + orchestration:operate) is issued into memory and never
printed. --execute additionally sends one harmless prompt to the first thread
and polls until it is DONE.
"""
import argparse
import asyncio
import json
import time
import uuid
from datetime import UTC, datetime

from t3_probe_auth import descriptor_summary, session_token

from herdeck.config import ServerConfig
from herdeck.model import Status
from herdeck.t3 import T3Connector
from herdeck.t3_client import T3Client


def out(**kv):
    print(json.dumps(kv, ensure_ascii=False, default=str))


def command(kind, **fields):
    return {"type": kind, "commandId": str(uuid.uuid4()), **fields}


def shell_thread(client, tid):
    return next(t for t in client.shell()["threads"] if t["id"] == tid)


async def read_state_round_trip(client, tid):
    before = shell_thread(client, tid)
    out(step="read-state-before", lastVisitedAt=before.get("lastVisitedAt"),
        latestRunCompletedAt=before.get("latestRunCompletedAt"))
    await client.dispatch(command("thread.mark-unread", threadId=tid))
    unread = shell_thread(client, tid)
    out(step="after-mark-unread", lastVisitedAt=unread.get("lastVisitedAt"))
    visited_at = before.get("latestRunCompletedAt") or datetime.now(UTC).isoformat()
    await client.dispatch(command("thread.visit", threadId=tid, visitedAt=visited_at))
    visited = shell_thread(client, tid)
    out(step="after-visit", lastVisitedAt=visited.get("lastVisitedAt"), visitedAt=visited_at)
    if not visited.get("lastVisitedAt"):
        raise SystemExit("thread.visit did not set lastVisitedAt")


async def execute(client, connector, tid, timeout):
    message = command(
        "message.dispatch", threadId=tid, messageId=str(uuid.uuid4()),
        text="Reply exactly HERDECK_PILOT_OK. Do not use tools or modify any files.",
        createdBy="user", creationSource="web", attachments=[],
        dispatchMode={"type": "start_immediately"}, deliveryIntent="auto")
    out(step="dispatch", result=await client.dispatch(message))
    seen, deadline = None, time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(1)
        await connector.refresh()
        status = connector.states[tid].status
        if status != seen:
            out(step="transition", status=status.value)
            seen = status
        if status == Status.DONE:
            out(done=True)
            return
    out(done=False, last=seen.value if seen else None)
    raise SystemExit(1)


async def pilot(args, token):
    client = T3Client(args.url, token)
    desc = client.descriptor()
    out(**descriptor_summary(desc))
    connector = T3Connector(ServerConfig("t3-probe", args.url, token, "t3"),
                            on_snapshot=lambda *a: None, on_event=lambda *a: None,
                            on_connection=lambda *a: None, client=client)
    await connector.refresh()
    out(connected=True, threads=len(connector.states))
    for tid, state in connector.states.items():
        out(thread=tid, status=state.status.value, attention=getattr(state, "attention", None))
    if not connector.states:
        raise SystemExit("no threads in the disposable instance; create one first")
    tid = next(iter(connector.states))
    detail = client.thread_detail(tid)
    out(step="detail", has_requests=bool(detail.get("runtimeRequests")), has_plan=bool(detail.get("plans")))
    await read_state_round_trip(client, tid)
    if args.execute:
        await execute(client, connector, tid, args.timeout)


async def main(args):
    with session_token(args.binary, args.base_dir) as token:
        await pilot(args, token)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--binary", required=True)
    p.add_argument("--base-dir", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--execute", action="store_true")
    p.add_argument("--timeout", type=int, default=90)
    asyncio.run(main(p.parse_args()))
