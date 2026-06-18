from __future__ import annotations

import asyncio
import json
import os
from typing import Protocol

from .protocol import encode


class HerdrClient(Protocol):
    async def list_panes(self) -> list[dict]: ...
    async def get_pane(self, pane_id: str) -> dict: ...
    async def read_pane(self, pane_id: str, source: str) -> str: ...
    async def send_keys(self, pane_id: str, keys: list[str]) -> None: ...


class StubHerdr:
    """In-memory herdr for tests."""

    def __init__(self, panes: list[dict]):
        self.panes = panes
        self.detection: dict[str, str] = {}
        self.sent: list[tuple[str, list[str]]] = []

    async def list_panes(self) -> list[dict]:
        return self.panes

    async def get_pane(self, pane_id: str) -> dict:
        return next(p for p in self.panes if p["pane_id"] == pane_id)

    async def read_pane(self, pane_id: str, source: str) -> str:
        return self.detection.get(pane_id, "")

    async def send_keys(self, pane_id: str, keys: list[str]) -> None:
        self.sent.append((pane_id, keys))


async def handle_client_message(herdr: HerdrClient, server_id: str, raw: str) -> str:
    msg = json.loads(raw)
    kind = msg["type"]
    if kind == "list":
        panes = await herdr.list_panes()
        return encode({"type": "snapshot", "server_id": server_id, "panes": panes})
    if kind == "read":
        text = await herdr.read_pane(msg["pane_id"], msg.get("source", "detection"))
        return encode({"type": "result", "req": msg["req"], "data": {"text": text}})
    if kind == "act":
        pane = await herdr.get_pane(msg["pane_id"])
        if pane.get("status") != "blocked":
            return encode({"type": "result", "req": msg["req"],
                           "data": {"skipped": True}})
        await herdr.send_keys(msg["pane_id"], msg["keys"])
        return encode({"type": "result", "req": msg["req"], "data": {"sent": True}})
    raise ValueError(f"unknown client message: {kind}")
