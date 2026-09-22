"""Discovery and device-local selection of Herdr sessions.

Named Herdr sessions use ``~/.config/herdr/sessions/<name>/herdr.sock`` while
the default session keeps ``~/.config/herdr/herdr.sock``. Herdeck stores only
the selected session names in ``local.toml``; socket paths remain derived local
machine state and never enter the shareable config.
"""

from __future__ import annotations

import errno
import os
import socket
import stat
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class LocalSession:
    name: str
    server_id: str
    socket_path: str
    available: bool
    selected: bool

    def public(self) -> dict:
        return asdict(self)


_PROBE_TIMEOUT_S = 0.2
# connect() failures that prove nobody is listening on a socket file.
_DEAD_SOCKET_ERRNOS = {errno.ECONNREFUSED, errno.ENOENT, errno.ENOTSOCK}


def socket_alive(path: str | Path, *, timeout: float = _PROBE_TIMEOUT_S) -> bool:
    """Is a Herdr server actually listening at ``path``?

    A socket FILE outlives a crashed Herdr, so ``exists()`` reported a dead
    session as running. For a real socket file this does a short AF_UNIX
    connect (closed immediately, nothing is sent): refused = stale. A path
    that exists but is not a socket, or one the probe cannot judge (e.g. too
    long for sun_path), keeps the old existence answer.
    """
    target = os.path.expanduser(str(path))
    try:
        mode = os.stat(target).st_mode
    except OSError:
        return False
    if not stat.S_ISSOCK(mode):
        return True
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(timeout)
        probe.connect(target)
        return True
    except TimeoutError:
        # Accept queue full / server busy: something is there.
        return True
    except OSError as exc:
        return exc.errno not in _DEAD_SOCKET_ERRNOS
    finally:
        probe.close()


def _read_local(local_path: str | Path | None) -> dict:
    if local_path is None:
        return {}
    try:
        return tomllib.loads(Path(local_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return {}


def _configured_socket(local_data: dict) -> str | None:
    local = local_data.get("local")
    if not isinstance(local, dict):
        return None
    value = local.get("herdr_socket")
    return os.path.expanduser(value) if isinstance(value, str) and value else None


def _saved_selection(local_data: dict) -> list[str] | None:
    local = local_data.get("local")
    if not isinstance(local, dict) or "herdr_sessions" not in local:
        return None
    value = local.get("herdr_sessions")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _session_name(path: Path, *, root: Path, default: Path) -> str:
    if path == default:
        return "default"
    try:
        relative = path.relative_to(root)
    except ValueError:
        return "custom"
    if len(relative.parts) == 2 and relative.parts[1] == "herdr.sock":
        return relative.parts[0]
    return "custom"


def _server_id(name: str) -> str:
    return "local" if name in {"default", "custom"} else f"local:{name}"


def discover_local_sessions(
    local_path: str | Path | None = None,
    *,
    getenv=os.environ.get,
    home: str | Path | None = None,
) -> list[LocalSession]:
    """Return stable local session records, including selected unavailable ones.

    With no explicit ``herdr_sessions`` selection, the legacy single-socket
    precedence is preserved: explicit env socket/session, then
    ``[local].herdr_socket``, then the default Herdr socket.
    """

    home_path = Path(home or Path.home()).expanduser()
    herdr_dir = home_path / ".config" / "herdr"
    sessions_dir = herdr_dir / "sessions"
    default_socket = herdr_dir / "herdr.sock"
    local_data = _read_local(local_path)

    env_socket = getenv("HERDR_SOCKET") or getenv("HERDR_SOCKET_PATH")
    env_session = getenv("HERDR_SESSION")
    configured = _configured_socket(local_data)
    if env_socket:
        legacy_socket = Path(os.path.expanduser(env_socket))
    elif env_session:
        legacy_socket = sessions_dir / env_session / "herdr.sock"
    elif configured:
        legacy_socket = Path(configured)
    else:
        legacy_socket = default_socket

    # An explicit socket env is an exact process-level override, matching Herdr's
    # own precedence. Otherwise discover the conventional fleet around the
    # selected/default socket.
    candidates: list[Path] = [legacy_socket]
    if not env_socket:
        candidates.append(default_socket)
    if not env_socket and sessions_dir.is_dir():
        candidates.extend(sorted(sessions_dir.glob("*/herdr.sock")))

    unique_paths: list[Path] = []
    seen_paths: set[str] = set()
    for candidate in candidates:
        normalized = Path(os.path.abspath(os.path.expanduser(str(candidate))))
        key = str(normalized)
        if key not in seen_paths:
            seen_paths.add(key)
            unique_paths.append(normalized)

    saved = _saved_selection(local_data)
    legacy_name = _session_name(legacy_socket, root=sessions_dir, default=default_socket)
    selected_names = set(saved if saved is not None else [legacy_name])

    records: list[LocalSession] = []
    used_names: set[str] = set()
    for path in unique_paths:
        name = _session_name(path, root=sessions_dir, default=default_socket)
        if name in used_names:
            suffix = 2
            while f"{name}-{suffix}" in used_names:
                suffix += 1
            name = f"{name}-{suffix}"
        used_names.add(name)
        records.append(
            LocalSession(
                name=name,
                server_id=_server_id(name),
                socket_path=str(path),
                available=socket_alive(path),
                selected=name in selected_names,
            )
        )

    # Keep a saved named session visible even while its socket/session directory
    # is absent so the UI can explain the disconnected selection.
    for name in sorted(selected_names - used_names):
        path = default_socket if name == "default" else sessions_dir / name / "herdr.sock"
        records.append(
            LocalSession(
                name=name,
                server_id=_server_id(name),
                socket_path=str(path),
                available=socket_alive(path),
                selected=True,
            )
        )

    return sorted(records, key=lambda item: (item.name != "default", item.name))


def selected_local_sessions(
    local_path: str | Path | None = None,
    *,
    getenv=os.environ.get,
    home: str | Path | None = None,
) -> list[LocalSession]:
    return [
        session
        for session in discover_local_sessions(local_path, getenv=getenv, home=home)
        if session.selected and session.available
    ]


def has_explicit_local_session_selection(local_path: str | Path | None) -> bool:
    return _saved_selection(_read_local(local_path)) is not None
