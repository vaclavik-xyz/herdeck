"""OS-keychain-backed secret store for herdeck token values.

Resolution is env-first: an environment variable named `token_env` always wins;
the OS keychain (via `keyring`, service "herdeck") is the fallback store the
config editor writes to. TOML never holds secret values — only the env-var name.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

SERVICE = "herdeck"


def _service() -> str:
    """Keyring namespace, isolated for disposable desktop build channels."""
    return os.environ.get("HERDECK_KEYRING_SERVICE") or SERVICE


def _keyring():
    """Return the keyring module, or raise if it is unavailable.

    Indirected through a function so tests can swap it and so an absent backend
    degrades gracefully (callers that read catch the failure).
    """
    import keyring

    return keyring


def get_secret(name: str) -> str | None:
    """The secret value for `name`: the env var if set, else the keychain entry,
    else None. Never raises if the keychain backend is missing."""
    env = os.environ.get(name)
    if env:
        return env
    try:
        return _keyring().get_password(_service(), name)
    except Exception:
        return None


def secret_source(name: str) -> str | None:
    """Where the secret comes from: "env", "keychain", or None if unset."""
    if os.environ.get(name):
        return "env"
    try:
        if _keyring().get_password(_service(), name) is not None:
            return "keychain"
    except Exception:
        return None
    return None


def has_secret(name: str) -> bool:
    return get_secret(name) is not None


def set_secret(name: str, value: str) -> None:
    """Store `value` in the OS keychain under `name`. Raises if no backend."""
    _keyring().set_password(_service(), name, value)


def clear_secret(name: str) -> None:
    """Remove the keychain entry for `name`. No-op if it is absent."""
    try:
        _keyring().delete_password(_service(), name)
    except Exception:
        pass


def peek_keychain(name: str) -> str | None:
    """The keychain value for `name` (keychain only, ignoring env — unlike get_secret,
    which is env-first), or None if ABSENT. Unlike the other readers this does NOT swallow
    backend errors — it RAISES — so a caller can distinguish 'missing' (None) from
    'unreadable' (exception) and never erase a token it failed to snapshot."""
    return _keyring().get_password(_service(), name)


def write_token_file(path: Path, token: str) -> None:
    """Atomically write ``token`` to ``path`` with mode 0600 (the runtime
    refuses a token file others can read). Creates the parent directory.

    A symlinked ``path`` keeps its link: the file it points to is replaced,
    since ``os.replace`` onto the link itself would swap it for a regular file."""
    path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".")
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, (token + "\n").encode())
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(tmp, path)
    except BaseException:
        if fd is not None:
            os.close(fd)
        Path(tmp).unlink(missing_ok=True)
        raise
