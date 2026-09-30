"""The bridge's one answer path: episode guard -> herdr -> settle the episode.

A deck/client answer (``_serve_connection``) and a bridge-side answer (the
Telegram control) both go through ``answer_frame`` / ``execute_answer``, so a
prompt is answered exactly once whoever gets there first: the loser is refused
with ``{"skipped": true, "message": "stale"}`` by ``EventHub.begin_answer``.
"""

from __future__ import annotations

import json

from .events import STALE, EventHub, client_label
from .protocol import encode


def answer_sent(out: str) -> bool:
    """Did an answer's reply say it went out to the pane?"""
    try:
        data = json.loads(out).get("data")
    except (ValueError, AttributeError):
        return False
    return isinstance(data, dict) and data.get("sent") is True


async def answer_frame(
    herdr,
    server_id: str,
    raw: str,
    msg: object,
    label: str,
    *,
    events: EventHub | None,
    icons=None,
    status_since=None,
    extra_capabilities: tuple[str, ...] = (),
    subagents=None,
) -> str:
    """Run one client message through the episode guard; returns the reply frame."""
    # handle_client_message lives in bridge.py, which imports this module.
    from .bridge import handle_client_message

    ticket = (
        await events.begin_answer(msg, label)
        if events is not None and isinstance(msg, dict)
        else None
    )
    if ticket == STALE:
        assert isinstance(msg, dict)
        req = msg.get("req")
        return encode(
            {
                "type": "result",
                "req": req if isinstance(req, str) else "",
                "data": {"skipped": True, "message": STALE},
            }
        )
    try:
        out = await handle_client_message(
            herdr,
            server_id,
            raw,
            icons,
            status_since,
            extra_capabilities,
            subagents=subagents,
            events=events,
        )
    except Exception as exc:
        out = encode({"type": "error", "message": str(exc)})
    if ticket is not None:
        events.end_answer(ticket, sent=answer_sent(out))
    return out


async def execute_answer(
    herdr,
    server_id: str,
    msg: dict,
    by: str,
    *,
    events: EventHub | None,
    icons=None,
    status_since=None,
    extra_capabilities: tuple[str, ...] = (),
    subagents=None,
) -> dict:
    """A bridge-side answer (e.g. Telegram) with the same guard as a client's.

    Returns the result ``data`` (``{"sent": True}``, ``{"skipped": True[,
    "message": "stale"]}``...) or ``{"error": <message>}``. The ``answered``
    event carries ``by=<by>`` and, unless ``msg`` names one, ``via=<by>``."""
    msg = {"via": by, **msg}
    out = await answer_frame(
        herdr,
        server_id,
        json.dumps(msg),
        msg,
        client_label(by),
        events=events,
        icons=icons,
        status_since=status_since,
        extra_capabilities=extra_capabilities,
        subagents=subagents,
    )
    frame = json.loads(out)
    if frame.get("type") == "result" and isinstance(frame.get("data"), dict):
        return frame["data"]
    return {"error": str(frame.get("message", "error"))}
