from __future__ import annotations

import base64
import hmac
from collections.abc import Callable

from ..commands import Command
from .protocol import PROTOCOL_VERSION, ProtocolError, decode, encode
from .session import ElgatoSession, KeyRender


class IpcServer:
    def __init__(self, session: ElgatoSession, token: str, *, on_commands: Callable[[list[Command]], None]) -> None:
        self._session = session
        self._token = token
        self._on_commands = on_commands
        self._writer = None  # active TS connection (single client in v1)

    async def handle(self, reader, writer) -> None:
        authed = False
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                try:
                    msg = decode(line)
                except ProtocolError:
                    continue
                authed = await self._dispatch(msg, writer, authed)
                if authed is None:  # fatal (bad hello) or graceful bye
                    return
        finally:
            if self._writer is writer:
                self._writer = None
            writer.close()

    async def _dispatch(self, msg: dict, writer, authed: bool):
        kind = msg.get("type")
        if not authed:
            if kind != "hello":
                return authed
            if msg.get("protocol_version") != PROTOCOL_VERSION or not self._valid(msg.get("token", "")):
                await self._send(writer, {"type": "error", "reason": "auth or version mismatch"})
                return None
            self._writer = writer  # register the active connection ONLY after auth
            await self._send(writer, {"type": "ready"})
            await self._push(writer, self._session.render_all())
            self._session.take_render_diff()  # prime diff baseline
            return True

        if kind == "slots":
            self._session.set_slots([(s["instanceId"], (s["coord"]["col"], s["coord"]["row"]))
                                     for s in msg.get("slots", [])])
            await self._push(writer, self._session.take_render_diff())
        elif kind == "action_keys":
            self._session.set_action_keys([(s["instanceId"], s["type"], (s["coord"]["col"], s["coord"]["row"]))
                                           for s in msg.get("action_keys", [])])
            await self._push(writer, self._session.take_render_diff())
        elif kind == "keyUp":
            cmds = self._session.key_up(msg.get("instanceId", ""))
            if cmds:
                self._on_commands(cmds)
            await self._push(writer, self._session.take_render_diff())
        elif kind == "keyDown":
            pass
        elif kind == "bye":
            return None  # graceful close: handle() returns and clears the active writer
        return True

    def _valid(self, token) -> bool:
        return isinstance(token, str) and hmac.compare_digest(token.encode(), self._token.encode())

    async def push_diff(self) -> None:
        """Server-initiated render push, called by the runtime after herdr state
        changes so the deck updates without a key press."""
        if self._writer is not None:
            await self._push(self._writer, self._session.take_render_diff())

    async def _push(self, writer, renders: dict[str, KeyRender]) -> None:
        if not renders:
            return
        keys = {iid: {"image": base64.b64encode(r.image_png).decode(), "title": r.title}
                for iid, r in renders.items()}
        await self._send(writer, {"type": "render", "keys": keys})

    async def _send(self, writer, msg: dict) -> None:
        writer.write(encode(msg))
        await writer.drain()
