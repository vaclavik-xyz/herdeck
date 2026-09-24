"""Black-box harness for the legacy-consumer contract tests.

The contract tests pin what production consumers observe from the OUTSIDE:
the web cockpit (``python -m herdeck.web run`` behind a reverse proxy, embedded
in another app's iframe), the ``herdeck`` CLI, the Elgato plugin backend and
Telegram interactive alerts. They drive a real subprocess against:

* ``FakeBridge`` — a scripted herdr-bridge WebSocket server (snapshot, events,
  results, live-terminal frames), recording every message the runtime sends;
* ``FakeTelegram`` — a local Bot API (sendMessage / getUpdates / ...). The
  subprocess reaches it through a ``sitecustomize`` shim that rewrites
  ``https://api.telegram.org`` to this server, so no runtime code is patched.

Nothing here imports the runtime under test: the tests must keep passing
unchanged while the implementation behind these entry points is replaced.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import queue
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import websockets

BRIDGE_TOKEN = "contract-bridge-token"
WEB_TOKEN = "contract-web-token-0123456789"
TELEGRAM_TOKEN = "123456:contract-bot-token"
PROMPT = "Do you want to proceed?\n1. Yes\n2. Yes, and don't ask again\n3. No"

_SITECUSTOMIZE = '''
import os
import urllib.request

_base = os.environ.get("HERDECK_CONTRACT_TELEGRAM_BASE")
if _base:
    _real_urlopen = urllib.request.urlopen

    def _redirect(url, *args, **kwargs):
        prefix = "https://api.telegram.org/"
        if isinstance(url, str) and url.startswith(prefix):
            url = _base + "/" + url[len(prefix):]
        elif isinstance(url, urllib.request.Request) and url.full_url.startswith(prefix):
            url.full_url = _base + "/" + url.full_url[len(prefix):]
        return _real_urlopen(url, *args, **kwargs)

    urllib.request.urlopen = _redirect
'''


def pane(
    pane_id: str,
    status: str,
    *,
    agent_type: str = "claude",
    label: str | None = None,
    terminal_id: str | None = None,
    **extra,
) -> dict:
    record = {
        "pane_id": pane_id,
        "agent_type": agent_type,
        "label": label if label is not None else f"agent-{pane_id}",
        "status": status,
        "repo": f"repo-{pane_id}",
        "branch": "main",
        "terminal_id": terminal_id if terminal_id is not None else f"term-{pane_id}",
    }
    record.update(extra)
    return record


def wait_until(predicate, *, timeout: float = 10.0, interval: float = 0.05, message: str = ""):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {message or predicate}")


class FakeBridge:
    """Scripted herdr bridge on 127.0.0.1 (own asyncio loop thread)."""

    def __init__(self, panes: list[dict], *, server_id: str = "local", port: int = 0):
        self.server_id = server_id
        self._port = port
        self.panes = [dict(p) for p in panes]
        self.prompt = PROMPT
        self.received: list[dict] = []
        self.auth_headers: list[str] = []
        self._clients: set = set()
        self._lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        self._ready.wait(5)
        self.url = f"ws://127.0.0.1:{self.port}"

    # --- lifecycle -----------------------------------------------------------
    def _serve(self) -> None:
        asyncio.set_event_loop(self._loop)

        async def start():
            return await websockets.serve(self._handler, "127.0.0.1", self._port)

        self._server = self._loop.run_until_complete(start())
        self.port = self._server.sockets[0].getsockname()[1]
        self._ready.set()
        self._loop.run_forever()

    def close(self) -> None:
        async def shutdown():
            self._server.close()
            await self._server.wait_closed()

        try:
            asyncio.run_coroutine_threadsafe(shutdown(), self._loop).result(5)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)

    # --- protocol ------------------------------------------------------------
    def snapshot(self) -> dict:
        return {
            "type": "snapshot",
            "server_id": self.server_id,
            "panes": [dict(p) for p in self.panes],
            "protocol": 3,
            "capabilities": ["terminal_preview"],
        }

    async def _handler(self, ws) -> None:
        self.auth_headers.append(ws.request.headers.get("Authorization", ""))
        if ws.request.headers.get("Authorization") != f"Bearer {BRIDGE_TOKEN}":
            await ws.close(4401, "unauthorized")
            return
        self._clients.add(ws)
        try:
            async for raw in ws:
                msg = json.loads(raw)
                with self._lock:
                    self.received.append(msg)
                await self._answer(ws, msg)
        except websockets.WebSocketException:
            pass
        finally:
            self._clients.discard(ws)

    async def _answer(self, ws, msg: dict) -> None:
        kind = msg.get("type")
        req = msg.get("req")
        if kind == "list":
            await ws.send(json.dumps(self.snapshot()))
        elif kind == "read":
            await ws.send(
                json.dumps(
                    {
                        "type": "result",
                        "req": req,
                        "data": {"text": self.prompt, "pane_id": msg.get("pane_id")},
                    }
                )
            )
        elif kind in ("act", "send_text", "choose_if_blocked", "refresh_title"):
            await ws.send(json.dumps({"type": "result", "req": req, "data": {"sent": True}}))
        elif kind == "focus":
            await ws.send(json.dumps({"type": "result", "req": req, "data": {"focused": False}}))
        elif kind == "observe":
            await ws.send(
                json.dumps(
                    {
                        "type": "term_frame",
                        "req": req,
                        "seq": 0,
                        "full": True,
                        "cols": msg.get("cols", 80),
                        "rows": msg.get("rows", 24),
                        "data": base64.b64encode(b"hello from pane").decode(),
                    }
                )
            )

    def send_all(self, frame: dict) -> None:
        async def broadcast():
            for ws in list(self._clients):
                try:
                    await ws.send(json.dumps(frame))
                except websockets.WebSocketException:
                    pass

        asyncio.run_coroutine_threadsafe(broadcast(), self._loop).result(5)

    def push_event(self, record: dict) -> None:
        for index, existing in enumerate(self.panes):
            if existing["pane_id"] == record["pane_id"]:
                self.panes[index] = dict(record)
                break
        else:
            self.panes.append(dict(record))
        self.send_all({"type": "event", "server_id": self.server_id, "pane": dict(record)})

    def push_snapshot(self) -> None:
        self.send_all(self.snapshot())

    def messages(self, kind: str | None = None) -> list[dict]:
        with self._lock:
            return [m for m in self.received if kind is None or m.get("type") == kind]

    def wait_message(self, predicate, *, timeout: float = 10.0, message: str = "") -> dict:
        def find():
            with self._lock:
                for msg in self.received:
                    if predicate(msg):
                        return msg
            return None

        return wait_until(find, timeout=timeout, message=message or "bridge message")

    def wait_connected(self, *, timeout: float = 15.0) -> None:
        wait_until(lambda: self._clients, timeout=timeout, message="runtime connected to bridge")


class FakeTelegram:
    """Local Telegram Bot API: records calls and serves queued updates."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self._updates: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._next_message_id = 100
        harness = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode()
                fields = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
                match = re.fullmatch(r"/bot([^/]+)/(\w+)", self.path)
                if match is None or match.group(1) != TELEGRAM_TOKEN:
                    self._reply(404, {"ok": False, "error_code": 404, "description": "Not Found"})
                    return
                method = match.group(2)
                result = harness._handle(method, fields)
                self._reply(200, {"ok": True, "result": result})

            def _reply(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _handle(self, method: str, fields: dict):
        if method == "getUpdates":
            updates = []
            try:
                updates.append(self._updates.get(timeout=0.3))
                while True:
                    updates.append(self._updates.get_nowait())
            except queue.Empty:
                pass
            with self._lock:
                self.calls.append((method, fields))
            return updates
        with self._lock:
            self.calls.append((method, fields))
            if method == "sendMessage":
                self._next_message_id += 1
                return {"message_id": self._next_message_id}
        return True

    def queue_update(self, update: dict) -> None:
        self._updates.put(update)

    def sent(self, method: str) -> list[dict]:
        with self._lock:
            return [fields for name, fields in self.calls if name == method]

    def wait_call(self, method: str, predicate=lambda fields: True, *, timeout: float = 15.0):
        def find():
            for fields in self.sent(method):
                if predicate(fields):
                    return fields
            return None

        return wait_until(find, timeout=timeout, message=f"telegram {method}")

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def short_tmpdir() -> Path:
    """A short directory (unix socket paths are capped at ~104 bytes on macOS)."""
    return Path(tempfile.mkdtemp(prefix="hdc", dir="/tmp"))


def write_config(path: Path, bridge: FakeBridge, *, extra: str = "") -> Path:
    path.write_text(
        "\n".join(
            [
                "[[servers]]",
                'id = "local"',
                f'url = "{bridge.url}"',
                'token_env = "HERDECK_CONTRACT_BRIDGE_TOKEN"',
                "",
                "[deck]",
                'grid = "5x3"',
                'overview_order = ["local"]',
                "",
                extra,
            ]
        ),
        encoding="utf-8",
    )
    return path


def base_env(home: Path, *, telegram: FakeTelegram | None = None) -> dict[str, str]:
    """A clean environment: no inherited HERDECK_*/HERDR_* knobs, a private
    HOME/XDG tree, the bridge token in env and the Telegram redirect shim."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("HERDECK_", "HERDR_", "XDG_"))
    }
    shim = home / "shim"
    shim.mkdir(parents=True, exist_ok=True)
    (shim / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")
    src = str(Path(__file__).resolve().parents[1] / "src")
    env.update(
        {
            "HOME": str(home),
            "XDG_STATE_HOME": str(home / "state"),
            "XDG_CONFIG_HOME": str(home / "config"),
            "XDG_CACHE_HOME": str(home / "cache"),
            "PYTHONPATH": os.pathsep.join([str(shim), src]),
            "PYTHONUNBUFFERED": "1",
            "HERDECK_CONTRACT_BRIDGE_TOKEN": BRIDGE_TOKEN,
            "HERDECK_CONTRACT_TG_TOKEN": TELEGRAM_TOKEN,
            # never reach a real herdr socket on the host running the tests
            "HERDR_SOCKET_PATH": str(home / "no-herdr.sock"),
        }
    )
    if telegram is not None:
        env["HERDECK_CONTRACT_TELEGRAM_BASE"] = telegram.base
    return env


def seed_web_token(home: Path, token: str = WEB_TOKEN) -> None:
    path = home / "state" / "herdeck" / "web-token"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    path.chmod(0o600)


class RuntimeProcess:
    """A runtime entry point in a subprocess; stdout/stderr are collected."""

    def __init__(self, args: list[str], env: dict[str, str], *, cwd: Path | None = None):
        self.args = args
        self.proc = subprocess.Popen(
            [sys.executable, *args],
            env=env,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.lines: list[str] = []
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.append(line.rstrip("\n"))

    def output(self) -> str:
        return "\n".join(self.lines)

    def wait_line(self, pattern: str, *, timeout: float = 20.0) -> re.Match:
        regex = re.compile(pattern)

        def find():
            for line in list(self.lines):
                match = regex.search(line)
                if match:
                    return match
            if self.proc.poll() is not None:
                raise AssertionError(
                    f"runtime exited {self.proc.returncode}:\n{self.output()}"
                )
            return None

        return wait_until(find, timeout=timeout, message=f"output {pattern!r}")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        self._reader.join(2)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
