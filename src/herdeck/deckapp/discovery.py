"""runtime.json discovery file. The headless runtime publishes its localhost
HTTP address + token here (perms 0600) so the Tauri desktop window can ATTACH
to a running runtime instead of spawning its own sidecar. The file is the same
{url,host,port,token,source} shape the sidecar prints to stdout. Deleted on a
clean exit; a stale file is detected by the window's /health ping failing."""

from __future__ import annotations

import json
import os


def runtime_file_path() -> str:
    base = os.environ.get("HERDECK_RUNTIME_DIR") or os.path.expanduser("~/.cache/herdeck")
    return os.path.join(base, "runtime.json")


def write_runtime_file(path: str, info: dict) -> None:
    """Atomically write `info` as JSON with 0600 perms (create parent dirs)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(info, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)  # atomic on POSIX


def read_runtime_file(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def clear_runtime_file(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
