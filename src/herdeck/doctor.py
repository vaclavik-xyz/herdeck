from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
import tomllib
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .config import ConfigError, Notifications
from .secrets import get_secret

SOCKET_TIMEOUT = 1.0
SERVER_PROBE_TIMEOUT = 2.0


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def check_socket(path: str, exists: Callable[[str], bool], probe) -> Check:
    """probe(path) -> herdr session.snapshot response dict, or raises on failure."""
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
    snapshot = result.get("snapshot")
    if not isinstance(snapshot, dict):
        return Check("herdr socket", False, "malformed response (snapshot is not a dict)")
    agents = snapshot.get("agents")
    if agents is None:
        return Check("herdr socket", False, "malformed response (no agents)")
    if not isinstance(agents, list):
        return Check("herdr socket", False, "malformed response (agents is not a list)")
    return Check("herdr socket", True, f"responding, {len(agents)} agents")


def check_config(
    config_path: str | None,
    has_servers: bool,
    socket_exists: bool,
    token_envs=(),
    getenv=get_secret,
    token_sources=None,
) -> Check:
    if has_servers and token_sources is not None:
        # Per server: WHERE its token resolved (env / file / keychain), never the value.
        statuses = [
            f"{sid}={source}" if source else f"{sid}=missing ({error})"
            for sid, source, error in token_sources
        ]
        detail = f"config at {config_path}; tokens: {', '.join(statuses)}"
        return Check("configuration", all(source for _sid, source, _e in token_sources), detail)
    if has_servers:
        statuses = [f"{env}=present" if getenv(env) else f"{env}=missing" for env in token_envs]
        missing = [env for env in token_envs if not getenv(env)]
        detail = f"config at {config_path}; token envs: {', '.join(statuses)}"
        return Check("configuration", not missing, detail)
    if socket_exists:
        source = "no config" if config_path is None else f"config at {config_path}"
        return Check("configuration", True, f"{source}; local zero-config mode")
    if config_path is None:
        return Check(
            "configuration",
            False,
            "no config and no herdr socket (start herdr or create config.toml)",
        )
    return Check(
        "configuration",
        False,
        f"config at {config_path} has no servers and no herdr socket is available",
    )


def check_servers(servers, probe) -> list[Check]:
    """One connectivity check per configured [[servers]] entry.

    ``probe(url, token) -> error string, or None / a dict of bridge facts on
    success``. Without this, a bridge that is down, a wrong URL/port and a
    present-but-rejected token all passed doctor with green checkmarks —
    exactly the remote failure modes users hit."""
    checks = []
    for server in servers:
        try:
            if server.backend == "t3":
                from .t3 import T3Http, validate_shell
                snapshot = T3Http(server.url, server.token).get("/api/orchestration/shell")
                validate_shell(snapshot)
                result = None
            else:
                result = probe(server.url, server.token)
        except Exception as exc:
            result = str(exc) or type(exc).__name__
        name = f"server '{server.id}'"
        if isinstance(result, str):
            checks.append(Check(name, False, f"{server.url}: {result}"))
            continue
        ok, notes = _bridge_notes(result or {})
        detail = "; ".join([f"{server.url} answered with a snapshot", *notes])
        checks.append(Check(name, ok, detail))
    return checks


def _bridge_notes(info: dict, *, expected: str = __version__) -> tuple[bool, list[str]]:
    """Version/protocol/herdr facts of one bridge as report fragments, and
    whether they are healthy. A version other than ``expected`` is a
    mismatch: snapshot fields one side does not know render blank, silently."""
    from .protocol import WIRE_PROTOCOL

    ok, notes = True, []
    version = info.get("herdeck_version") or info.get("bridge_version")
    if version:
        if version != expected:
            ok = False
            notes.append(f"version mismatch: bridge {version} ≠ {expected}")
        else:
            notes.append(f"bridge {version}")
    elif info:
        notes.append("bridge version not reported")
    protocol = info.get("protocol")
    if isinstance(protocol, int) and protocol > WIRE_PROTOCOL:
        ok = False
        notes.append(f"unsupported wire protocol {protocol} (this install knows <= {WIRE_PROTOCOL})")
    if info.get("herdr_reachable") is False:
        ok = False
        notes.append("herdr socket not reachable from the bridge")
    elif info.get("herdr_reachable") is True:
        notes.append(f"herdr reachable, {info.get('clients', '?')} client(s)")
    return ok, notes


def check_optional_deps(is_available: Callable[[str], bool]) -> Check:
    modules = (
        ("PIL", "PIL"),
        ("resvg_py", "resvg_py"),  # SVG agent marks + project favicons
        ("strmdck", "strmdck"),
        ("streamdeck", "StreamDeck"),
        # The converged runtime imports these unconditionally at startup
        # (config write / keychain tokens) yet they live only in extras — a
        # missing tomli_w crashed the deployed runtime with a raw
        # ImportError while doctor showed all green.
        ("tomli_w", "tomli_w"),
        ("keyring", "keyring"),
    )
    statuses = [
        f"{label}=present" if is_available(import_name) else f"{label}=missing"
        for label, import_name in modules
    ]
    missing = [label for label, import_name in modules if not is_available(import_name)]
    detail = "; ".join(statuses)
    if missing:
        detail += '; optional hints: pip install ".[deck]" or ".[elgato]"'
        if "tomli_w" in missing or "keyring" in missing:
            detail += "; the desktop/converged runtime needs tomli_w + keyring"
    return Check("optional dependencies", True, detail)


def check_deck(lib_available: Callable[[str], bool]) -> Check:
    d200_ready = lib_available("strmdck") and lib_available("hid")
    elgato_ready = lib_available("StreamDeck")
    note = "device presence is not probed; Ulanzi Studio can hold the device"
    if d200_ready or elgato_ready:
        drivers = []
        if d200_ready:
            drivers.append("D200")
        if elgato_ready:
            drivers.append("Elgato")
        return Check("deck drivers", True, f"importable: {', '.join(drivers)}; {note}")
    return Check(
        "deck drivers",
        False,
        f'no deck driver libraries importable; pip install ".[deck]" or ".[elgato]"; {note}',
    )


def check_web_service(base_url: str, probe) -> Check:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return Check("web service", False, "invalid web URL")
    base_path = parsed.path.rstrip("/")
    health_url = f"{parsed.scheme}://{parsed.netloc}{base_path}/healthz"
    try:
        payload = probe(health_url)
    except Exception as exc:
        return Check("web service", False, f"health probe failed ({exc})")
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return Check("web service", False, "health probe returned a malformed response")
    if payload.get("service") != "herdeck-web":
        return Check("web service", False, "health probe reached a different service")
    release = str(payload.get("version") or "unknown")
    build = str(payload.get("build") or "unknown")
    return Check("web service", True, f"healthy; version={release}; build={build}")


def _telegram_get_me(token: str) -> str | None:
    """None if the Bot API accepts the token, else a short SANITIZED reason —
    urllib errors can embed the request URL (which contains the token), so
    only known-safe fields are ever echoed."""
    import json
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/getMe", timeout=2
        ) as r:
            data = json.loads(r.read().decode())
        return None if data.get("ok") else str(data.get("description", "rejected"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return "token rejected (401 Unauthorized)"
        return f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        return str(reason) if reason is not None else "unreachable"
    except Exception as exc:
        return type(exc).__name__  # e.g. a malformed token breaking the URL


def check_notifications(
    notifications: Notifications, getenv=get_secret, telegram_probe=None
) -> Check:
    if not notifications.enabled:
        return Check("notifications", True, "disabled")
    supported = {"macos", "telegram"}
    parts = [f"backends={','.join(notifications.backends) or '(none)'}"]
    ok = True
    usable = 0
    unknown = [b for b in notifications.backends if b not in supported]
    if unknown:
        parts.append(f"unknown={','.join(unknown)}")
        ok = False
    if "macos" in notifications.backends:
        usable += 1
    if "telegram" in notifications.backends:
        tg = notifications.telegram
        if tg is None:
            parts.append("telegram=no usable [notifications.telegram] (need token_env + chat_id)")
            ok = False
        else:
            token_present = bool(getenv(tg.token_env))
            chat_present = bool(tg.chat_id)
            parts.append(f"token_env={'present' if token_present else 'missing'}")
            parts.append(f"chat_id={'present' if chat_present else 'missing'}")
            if token_present and telegram_probe is not None:
                # a PRESENT token says nothing about a wrong/revoked one — the
                # user finds out only when an agent sat blocked for an hour
                probe_error = telegram_probe(getenv(tg.token_env))
                if probe_error is None:
                    parts.append("telegram=reachable")
                else:
                    parts.append(f"telegram={probe_error}")
                    ok = False
            if token_present and chat_present:
                usable += 1
                if tg.interactive:
                    if tg.allowed_user_ids:
                        parts.append("interactive=ready")
                        parts.append(
                            "topic=present" if tg.message_thread_id is not None else "topic=absent"
                        )
                    else:
                        parts.append("interactive=missing allowed_user_ids")
                        ok = False
            else:
                ok = False
    if usable == 0:
        parts.append("no usable backend (nothing will fire)")
        ok = False
    return Check("notifications", ok, "; ".join(parts))


def _read_notifications(config_path: str | None) -> Notifications:
    if config_path is None:
        return Notifications()
    try:
        from .bootstrap import _discover_local_config_path
        from .settings import load_settings, resolve_notifications

        snapshot = load_settings(config_path, _discover_local_config_path(config_path))
        return resolve_notifications(snapshot)
    except ConfigError:
        raise
    except Exception:
        return Notifications()


def _check_configured_notifications(config_path: str | None) -> Check:
    try:
        return check_notifications(
            _read_notifications(config_path), telegram_probe=_telegram_get_me
        )
    except ConfigError as exc:
        return Check("notifications", False, f"invalid config ({exc})")


def check_subagent_hooks(status: Callable[[str], dict]) -> Check:
    """Whether ``herdeck-subagent-hook`` is wired into Claude Code and Codex on
    this machine (hooks_install.py), and the OpenCode plugin. Not installed is
    fine (optional); a hook file that cannot be read, a partial install, or
    Codex hooks without ``[features] hooks`` are not."""
    from .hooks_install import AGENT_NAMES, AGENTS

    ok = True
    parts = []
    for agent in AGENTS:
        name = AGENT_NAMES[agent]
        info = status(agent)
        if info.get("error"):
            ok = False
            parts.append(f"{name}: {info['error']}")
        elif info.get("installed"):
            note = f"{name}: installed"
            if agent == "codex":
                if info.get("features_hooks_enabled") is not True:
                    ok = False
                    note += " but [features] hooks is not enabled in Codex's config.toml"
                else:
                    note += " (trust it once with /hooks in a Codex session)"
            parts.append(note)
        elif info.get("events"):
            ok = False
            parts.append(
                f"{name}: partly installed ({', '.join(info['events'])}); "
                "run herdeck-service hooks install"
            )
        else:
            parts.append(f"{name}: not installed (optional: herdeck-service hooks install)")
    return Check("subagent hooks", ok, "; ".join(parts))


def _subagent_hook_status(agent: str) -> dict:
    from .hooks_install import agent_status

    return agent_status(agent, Path.home())


def format_report(checks: Iterable[Check]) -> str:
    lines = ["herdeck doctor"]
    for check in checks:
        mark = "✓" if check.ok else "✗"
        lines.append(f"{mark} {check.name}: {check.detail}")
    return "\n".join(lines)


async def _socket_snapshot(path: str) -> dict:
    from .bridge import SocketHerdr

    return await SocketHerdr(path)._rpc("session.snapshot", {})


def _probe_socket(path: str) -> dict:
    return asyncio.run(asyncio.wait_for(_socket_snapshot(path), timeout=SOCKET_TIMEOUT))


async def _probe_server_ws(url: str, token: str) -> str | dict:
    """Connect to a bridge and wait for its greeting snapshot. On success a
    dict of what the bridge announced (version, protocol, and — from a bridge
    that answers ``health`` — herdr reachability and client count), else a
    human-readable reason (reusing the connector's classification, so a
    rejected token reads as 'token rejected', not as a generic close)."""
    import websockets

    from .connector import _describe_connect_error
    from .protocol import Snapshot, decode_inbound

    try:
        async with websockets.connect(
            url,
            additional_headers={"Authorization": f"Bearer {token}"},
            open_timeout=SERVER_PROBE_TIMEOUT,
            close_timeout=1,
        ) as ws:
            raw = await asyncio.wait_for(ws.recv(), timeout=SERVER_PROBE_TIMEOUT)
            # Any WebSocket service can answer a frame; only a decodable
            # herdeck snapshot proves this is a herdeck-bridge.
            try:
                msg = decode_inbound(raw)
            except Exception:
                return "answered, but not with a herdeck snapshot (is this a herdeck-bridge?)"
            if not isinstance(msg, Snapshot):
                return f"answered with {type(msg).__name__}, not a snapshot"
            info = {"herdeck_version": msg.herdeck_version, "protocol": msg.protocol}
            health = await _bridge_health(ws)
            if health is not None:
                info.update(health)
            return info
    except TimeoutError:
        return f"connected but no snapshot within {SERVER_PROBE_TIMEOUT:g}s"
    except (OSError, websockets.WebSocketException) as exc:
        return _describe_connect_error(exc)


async def _bridge_health(ws) -> dict | None:
    """Ask a connected bridge for its health (``{"type": "health"}``). None
    from a bridge that predates the message, closes, or does not answer."""
    import websockets

    try:
        await ws.send(json.dumps({"type": "health", "req": "doctor-health"}))
        async with asyncio.timeout(SERVER_PROBE_TIMEOUT):
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("type") == "error":
                    return None  # an older bridge: "unknown client message"
                if msg.get("type") == "result" and msg.get("req") == "doctor-health":
                    data = msg.get("data")
                    return data if isinstance(data, dict) else None
    except (TimeoutError, ValueError, AttributeError, websockets.WebSocketException):
        return None


def _probe_server(url: str, token: str) -> str | dict | None:
    return asyncio.run(_probe_server_ws(url, token))


def _runtime_health(url: str, token: str) -> dict | None:
    """The runtime's token-authed /health JSON, or None when it does not
    answer within 1s."""
    import urllib.request

    try:
        with urllib.request.urlopen(f"{url}/health?token={token}", timeout=1) as r:
            if r.status != 200:
                return None
            data = json.loads(r.read())
            return data if isinstance(data, dict) else {}
    except Exception:
        return None


def _runtime_notes(health: dict) -> tuple[bool, list[str]]:
    """Runtime version vs this install, and each bridge vs the runtime."""
    runtime = health.get("version")
    if not runtime:
        return True, []  # a pre-0.8.1 runtime: nothing to compare
    ok, notes = True, [f"runtime {runtime}"]
    if runtime != __version__:
        ok = False
        notes.append(f"version mismatch: runtime {runtime} ≠ installed {__version__} — restart it")
    servers = health.get("servers")
    for sid, facts in (servers.items() if isinstance(servers, dict) else ()):
        if not isinstance(facts, dict):
            continue
        bridge_ok, bridge_notes = _bridge_notes(facts, expected=runtime)
        ok = ok and bridge_ok
        state = "connected" if facts.get("connected") else f"down ({facts.get('last_error') or '?'})"
        notes.append(f"'{sid}' {state}" + (f": {', '.join(bridge_notes)}" if bridge_notes else ""))
    return ok, notes


def check_runtime(read_file, health) -> Check:
    """State of the converged runtime's discovery file (runtime.json).

    A stale file (crash leftover) makes the desktop window spawn its own
    sidecar in confusing silence — this was previously diagnosable only by
    reverse-engineering Tauri behavior."""
    from .deckapp.discovery import runtime_file_path

    path = runtime_file_path()
    info = read_file(path)
    if info is None:
        return Check("runtime", True, "no runtime.json (no headless runtime running)")
    url, token = info.get("url"), info.get("token")
    if not url or not token:
        return Check("runtime", False, f"malformed runtime.json at {path}")
    answer = health(url, token)
    if answer is not None and answer is not False:
        ok, notes = _runtime_notes(answer) if isinstance(answer, dict) else (True, [])
        return Check("runtime", ok, "; ".join([f"headless runtime answering at {url}", *notes]))
    return Check(
        "runtime",
        False,
        f"stale runtime.json at {path} (runtime not responding) — the desktop "
        "window will spawn its own sidecar; delete the file or restart the runtime",
    )


def _module_available(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def token_sources(config_path: str | None) -> list[tuple[str, str | None, str | None]]:
    """``(server_id, source, error)`` per ``[[servers]]`` entry: source is where
    its token resolves ("env" / "file" / "keychain"), or None with the reason.
    Never carries a token value."""
    from .settings import resolve_server_token

    if config_path is None:
        return []
    try:
        data = tomllib.loads(Path(config_path).read_text())
    except Exception:
        return []
    out = []
    for raw in data.get("servers", []):
        if not isinstance(raw, dict):
            continue
        try:
            _token, source = resolve_server_token(raw)
            out.append((str(raw.get("id")), source, None))
        except ConfigError as exc:
            out.append((str(raw.get("id")), None, str(exc)))
    return out


def _read_config_facts(
    config_path: str | None,
) -> tuple[bool, list[str], Check | None, list]:
    if config_path is None:
        return False, [], None, []
    try:
        data = tomllib.loads(Path(config_path).read_text())
        servers = data.get("servers", [])
        token_envs = [
            server["token_env"]
            for server in servers
            if isinstance(server, dict) and "token_env" in server
        ]
    except Exception as exc:
        return (
            False,
            [],
            Check("configuration", False, f"cannot read config at {config_path} ({exc})"),
            [],
        )

    from .config import ConfigError, load_config

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        # A token that resolves from no source is reported by the configuration
        # line (per server, with the reason); anything else is a real config error.
        if any(source is None for _sid, source, _err in token_sources(config_path)):
            return bool(servers), token_envs, None, []
        return (
            bool(servers),
            token_envs,
            Check("configuration", False, f"invalid config at {config_path} ({exc})"),
            [],
        )
    except Exception as exc:
        return (
            bool(servers),
            token_envs,
            Check("configuration", False, f"invalid config at {config_path} ({exc})"),
            [],
        )
    return bool(config.servers), token_envs, None, list(config.servers)


def collect_checks(web_url: str | None = None) -> list[Check]:
    from .bootstrap import _discover_config_path

    config_path = _discover_config_path()
    has_servers, token_envs, config_error, servers = _read_config_facts(config_path)
    # Resolve the socket like the deck does (env > [hardware].herdr_socket >
    # default) — a diagnostic that contradicts the working app is the worst
    # kind of doctor output.
    from .bootstrap import resolve_socket_path

    file_config = None
    if config_path:
        try:
            from .config import load_config

            file_config = load_config(config_path)
        except Exception:
            file_config = None
    socket_path = resolve_socket_path(file_config)
    socket_exists = os.path.exists(socket_path)
    socket_check = (
        Check("herdr socket", True, "remote config present; local socket not required")
        if has_servers
        else check_socket(socket_path, os.path.exists, _probe_socket)
    )
    checks = [
        config_error
        if config_error is not None
        else check_config(
            config_path,
            has_servers,
            socket_exists,
            token_envs=token_envs,
            token_sources=token_sources(config_path),
        ),
    ]
    # Actually contact each configured server — token presence alone said
    # nothing about a dead bridge, a wrong URL or a rejected token.
    checks.extend(check_servers(servers, _probe_server))
    from .deckapp.discovery import read_runtime_file

    checks.extend(
        [
            socket_check,
            check_optional_deps(_module_available),
            check_deck(_module_available),
            check_runtime(read_runtime_file, _runtime_health),
            _check_configured_notifications(config_path),
            check_subagent_hooks(_subagent_hook_status),
        ]
    )
    if web_url:
        checks.append(check_web_service(web_url, _probe_web_health))
    return checks


def _probe_web_health(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=SERVER_PROBE_TIMEOUT) as response:
        return json.loads(response.read())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="herdeck-doctor")
    parser.add_argument("--web-url", help="optional Herdeck web base URL to probe")
    args = parser.parse_args(argv)
    checks = collect_checks(args.web_url)
    print(format_report(checks))
    if any(not check.ok for check in checks):
        sys.exit(1)


if __name__ == "__main__":
    main()
