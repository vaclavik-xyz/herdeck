from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def check_socket(path: str, exists: Callable[[str], bool], probe) -> Check:
    """probe(path) -> herdr pane.list response dict, or raises on failure."""
    if not exists(path):
        return Check("herdr socket", False, f"not found at {path} (is herdr running?)")
    try:
        resp = probe(path)
    except Exception as exc:
        return Check("herdr socket", False, f"socket did not respond ({exc})")
    if not isinstance(resp, dict):
        return Check("herdr socket", False, "malformed response (not a dict)")
    result = resp.get("result")
    if not isinstance(result, dict):
        return Check("herdr socket", False, "malformed response (result is not a dict)")
    panes = result.get("panes")
    if panes is None:
        return Check("herdr socket", False, "malformed response (no panes)")
    if not isinstance(panes, list):
        return Check("herdr socket", False, "malformed response (panes is not a list)")
    return Check("herdr socket", True, f"responding, {len(panes)} panes")
