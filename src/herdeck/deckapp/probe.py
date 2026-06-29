"""One-shot server probe for the remote onboarding flow (test-before-commit).

Connects ws(s)://… with the Authorization: Bearer <token> header the bridge
expects. A first frame (the snapshot) means the token was accepted -> ok. The
bridge closes 4401 for a bad token. Anything else (refused/timeout/bad url) ->
unreachable. No Connector/backoff; a single attempt."""
from __future__ import annotations

import asyncio
import dataclasses

import websockets


@dataclasses.dataclass(frozen=True)
class ProbeResult:
    ok: bool
    reason: str  # "ok" | "bad_token" | "unreachable"


def _close_code(exc) -> int | None:
    rcvd = getattr(exc, "rcvd", None)
    return getattr(rcvd, "code", None) if rcvd is not None else getattr(exc, "code", None)


async def probe_server(url: str, token: str, *, timeout: float = 4.0) -> ProbeResult:
    try:
        async with asyncio.timeout(timeout):
            async with websockets.connect(
                url, additional_headers={"Authorization": f"Bearer {token}"}
            ) as ws:
                await ws.recv()  # first frame = snapshot -> token accepted
        return ProbeResult(True, "ok")
    except websockets.ConnectionClosed as exc:
        return ProbeResult(False, "bad_token" if _close_code(exc) == 4401 else "unreachable")
    except (OSError, TimeoutError, websockets.InvalidURI, websockets.InvalidHandshake):
        return ProbeResult(False, "unreachable")
    except Exception:
        return ProbeResult(False, "unreachable")
