"""Bridge self-update: a *managed* bridge installs a herdeck release into its
own virtualenv, verifies it, answers, and exits so its service restarts it.

Wire contract (full token only; a read-only token is refused by the bridge's
``READONLY_MESSAGES`` allowlist before this module is reached)::

    -> {"type": "update", "req": "u1", "version": "X.Y.Z"}
    <- {"type": "progress", "req": "u1", "stage": "download", "message": "..."}  (0..n)
    <- {"type": "result", "req": "u1", "data": {"updated": "X.Y.Z", "source": "wheel",
                                               "restarting": true}}
    <- {"type": "result", "req": "u1", "data": {"updated": null, "error": {
           "code": "not_managed" | "invalid_version" | "busy" | "failed",
           "message": "...", "output": "<installer output tail>"}}}

Managed-install contract (shared with ``herdeck-service install bridge
--managed``): the service tooling creates a dedicated venv and writes a marker
``managed.json`` (``{"version": ..., "source": ...}``) at the venv root, i.e.
``Path(sys.prefix) / "managed.json"``. Only a bridge whose interpreter runs
from that venv, whose ``herdeck`` package is installed inside it (not an
editable/source checkout), and that carries the marker may update itself. An
optional ``prefix`` (or ``venv``) key in the marker must then name this venv.

Install source, in order:

1. the release wheel ``herdeck-X-py3-none-any.whl`` from the GitHub release
   ``vX``, verified against that release's ``SHA256SUMS`` asset before pip or
   uv ever sees it (a wheel without a matching checksum is refused);
2. only when the release has *no wheel asset at all* (releases before the
   Python assets were published): ``git+https://github.com/vaclavik-xyz/herdeck@vX``.
   That path has no checksum — it trusts TLS and the tag — and needs ``git``.

The installed version is then read back in a fresh interpreter. Any failure
leaves the running (old) bridge serving and replies with the output tail; the
bridge only exits after the new version verified. No shell is ever involved:
every subprocess is an argv list with a timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import importlib.util
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .protocol import encode

log = logging.getLogger(__name__)

MARKER_NAME = "managed.json"
REPO = "vaclavik-xyz/herdeck"
RELEASE_ASSET_URL = "https://github.com/{repo}/releases/download/v{version}/{name}"
GIT_SOURCE = "git+https://github.com/{repo}@v{version}"
SUMS_NAME = "SHA256SUMS"

# A strict subset of PEP 440 (release, optional pre/post/dev): the version goes
# into a URL and a git ref, so nothing else is accepted.
_VERSION_RE = re.compile(
    r"^\d{1,4}\.\d{1,4}\.\d{1,4}(?:(?:a|b|rc)\d{1,4})?(?:\.post\d{1,4})?(?:\.dev\d{1,4})?$"
)

DOWNLOAD_TIMEOUT_S = 60.0
INSTALL_TIMEOUT_S = 600.0
VERIFY_TIMEOUT_S = 60.0
WHEEL_MAX_BYTES = 64 * 2**20
SUMS_MAX_BYTES = 2**20
OUTPUT_TAIL_LINES = 40
PROGRESS_MAX_CHARS = 300
# Delay between the result frame and the exit, so the reply flushes first.
EXIT_GRACE_S = 0.5


def valid_version(version: object) -> bool:
    return isinstance(version, str) and bool(_VERSION_RE.fullmatch(version))


def wheel_name(version: str) -> str:
    return f"herdeck-{version}-py3-none-any.whl"


def asset_url(version: str, name: str) -> str:
    return RELEASE_ASSET_URL.format(repo=REPO, version=version, name=name)


def git_source(version: str) -> str:
    return GIT_SOURCE.format(repo=REPO, version=version)


class UpdateError(Exception):
    """A refused or failed update; ``code`` is stable for the runtime/UI."""

    def __init__(self, code: str, message: str, output: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.output = output


@dataclass(frozen=True)
class ManagedEnv:
    prefix: Path
    python: str
    marker: dict


def _editable_install() -> bool:
    """True when the installed herdeck distribution is an editable install
    (PEP 610 ``direct_url.json`` with ``dir_info.editable``)."""
    try:
        from importlib.metadata import PackageNotFoundError, distribution

        raw = distribution("herdeck").read_text("direct_url.json")
    except PackageNotFoundError:
        return False
    except Exception:
        return False
    if not raw:
        return False
    try:
        info = json.loads(raw)
    except ValueError:
        return False
    dir_info = info.get("dir_info") if isinstance(info, dict) else None
    return isinstance(dir_info, dict) and dir_info.get("editable") is True


def probe_managed_env(
    *,
    prefix: str | None = None,
    base_prefix: str | None = None,
    executable: str | None = None,
    package_file: str | None = None,
    editable: Callable[[], bool] = _editable_install,
) -> tuple[ManagedEnv | None, str]:
    """``(env, "")`` when this bridge may update itself, else ``(None, reason)``."""
    prefix = prefix if prefix is not None else sys.prefix
    base_prefix = base_prefix if base_prefix is not None else sys.base_prefix
    executable = executable if executable is not None else sys.executable
    if package_file is None:
        from . import __file__ as package_file
    root = Path(prefix)
    try:
        resolved = root.resolve()
        if resolved == Path(base_prefix).resolve():
            return None, "the bridge does not run from a virtualenv"
    except OSError as exc:
        return None, f"cannot resolve the bridge environment: {exc}"
    marker_path = root / MARKER_NAME
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"no {MARKER_NAME} marker in {root} (not a managed install)"
    except (OSError, ValueError) as exc:
        return None, f"unreadable {MARKER_NAME} marker: {exc}"
    if not isinstance(marker, dict):
        return None, f"malformed {MARKER_NAME} marker"
    declared = marker.get("prefix", marker.get("venv"))
    if declared is not None:
        try:
            if not isinstance(declared, str) or Path(declared).expanduser().resolve() != resolved:
                return None, f"{MARKER_NAME} names a different environment"
        except OSError:
            return None, f"{MARKER_NAME} names an unresolvable environment"
    if not os.path.abspath(executable).startswith(os.path.abspath(prefix) + os.sep):
        return None, "the bridge interpreter is not inside the managed environment"
    try:
        if not Path(package_file).resolve().is_relative_to(resolved):
            return None, "herdeck is imported from outside the managed environment (source checkout)"
    except OSError:
        return None, "cannot locate the running herdeck package"
    if editable():
        return None, "herdeck is an editable install"
    return ManagedEnv(prefix=root, python=executable, marker=marker), ""


# --- default I/O seams (tests inject fakes) ----------------------------------


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """GitHub answers an asset URL with a redirect to its CDN; follow it only
    while it stays on https."""

    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            raise urllib.error.URLError(f"refusing a redirect off https: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_url(url: str, max_bytes: int, timeout: float) -> bytes | None:
    """GET ``url`` (HTTPS only, following GitHub's asset redirect). ``None`` on
    404 — the asset does not exist; other failures raise."""
    if not url.startswith("https://"):
        raise ValueError("only https downloads are allowed")
    opener = urllib.request.build_opener(_HttpsOnlyRedirects())
    request = urllib.request.Request(url, headers={"User-Agent": f"herdeck-bridge/{__version__}"})
    try:
        with opener.open(request, timeout=timeout) as response:
            data = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    if len(data) > max_bytes:
        raise ValueError(f"download exceeds {max_bytes} bytes")
    return data


async def run_installer(
    argv: list[str],
    timeout: float,
    on_line: Callable[[str], Awaitable[None]],
    *,
    env: dict | None = None,
    cwd: str | None = None,
) -> tuple[int | None, str]:
    """Run ``argv`` (never a shell), forwarding each output line. Returns
    ``(returncode, output_tail)``; returncode ``None`` means it timed out and
    was killed."""
    tail: deque[str] = deque(maxlen=OUTPUT_TAIL_LINES)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
        cwd=cwd,
    )
    assert proc.stdout is not None

    async def pump() -> None:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                tail.append(text)
                await on_line(text)
        await proc.wait()

    try:
        await asyncio.wait_for(pump(), timeout=timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=5)
        return None, "\n".join(tail)
    return proc.returncode, "\n".join(tail)


def _child_env() -> dict:
    """The installer/verifier environment: this process's, minus anything that
    would make the child import code from somewhere other than the venv."""
    env = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PIP_TARGET", "PIP_PREFIX"):
        env.pop(name, None)
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["NO_COLOR"] = "1"
    return env


def _find_uv(which: Callable[[str], str | None]) -> str | None:
    found = which("uv")
    if found:
        return found
    for path in (
        os.path.expanduser("~/.local/bin/uv"),
        os.path.expanduser("~/.cargo/bin/uv"),
        "/opt/homebrew/bin/uv",
        "/usr/local/bin/uv",
    ):
        if os.access(path, os.X_OK):
            return path
    return None


def _has_pip() -> bool:
    return importlib.util.find_spec("pip") is not None


def parse_sums(text: str, name: str) -> str | None:
    """The hex SHA-256 recorded for ``name`` in a ``sha256sum``-format file."""
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) != 2:
            continue
        digest, filename = parts
        if filename.lstrip("*") == name and re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            return digest.lower()
    return None


def _write_marker(env: ManagedEnv, version: str, source: str) -> None:
    marker = {**env.marker, "version": version, "source": source}
    path = env.prefix / MARKER_NAME
    fd, temporary = tempfile.mkstemp(prefix=".managed.", dir=env.prefix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(marker, handle, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


Send = Callable[[str], Awaitable[bool]]


class BridgeUpdater:
    """Serves ``update`` messages for one bridge process (one update at a time,
    across all connections). Every side effect is an injectable seam."""

    def __init__(
        self,
        *,
        request_exit: Callable[[], None],
        probe: Callable[[], tuple[ManagedEnv | None, str]] = probe_managed_env,
        fetch: Callable[[str, int, float], bytes | None] = fetch_url,
        run: Callable[..., Awaitable[tuple[int | None, str]]] = run_installer,
        which: Callable[[str], str | None] = shutil.which,
        has_pip: Callable[[], bool] = _has_pip,
        write_marker: Callable[[ManagedEnv, str, str], None] = _write_marker,
        current_version: str = __version__,
    ):
        self._request_exit = request_exit
        self._probe = probe
        self._fetch = fetch
        self._run = run
        self._which = which
        self._has_pip = has_pip
        self._write_marker = write_marker
        self._current = current_version
        self._busy = False
        self._tasks: set[asyncio.Task] = set()
        self.exit_requested = False

    @property
    def managed(self) -> bool:
        return self._probe()[0] is not None

    @property
    def busy(self) -> bool:
        return self._busy

    def start(self, msg: dict, send: Send) -> asyncio.Task:
        """Handle one ``update`` message in the background (the connection
        keeps serving meanwhile). The busy check runs before the task's first
        await, so two back-to-back requests can never both start."""
        task = asyncio.create_task(self.handle(msg, send))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def handle(self, msg: dict, send: Send) -> None:
        req = msg.get("req")
        req = req if isinstance(req, str) else ""
        version = msg.get("version")
        try:
            if not valid_version(version):
                raise UpdateError("invalid_version", f"invalid version: {version!r}")
            if self._busy:
                raise UpdateError("busy", "an update is already running")
            env, reason = self._probe()
            if env is None:
                raise UpdateError("not_managed", reason)
        except UpdateError as exc:
            await send(_error_result(req, exc))
            return
        if version == self._current:
            await send(
                encode(
                    {
                        "type": "result",
                        "req": req,
                        "data": {"updated": version, "unchanged": True, "restarting": False},
                    }
                )
            )
            return
        self._busy = True
        succeeded = False
        try:
            source = await self._install(req, version, env, send)
            succeeded = True
        except UpdateError as exc:
            log.warning("bridge self-update to %s failed: %s", version, exc.message)
            await send(_error_result(req, exc))
        except Exception as exc:  # never let an update kill the serving bridge
            log.exception("bridge self-update to %s failed", version)
            await send(_error_result(req, UpdateError("failed", str(exc) or type(exc).__name__)))
        finally:
            if not succeeded:
                self._busy = False
        if not succeeded:
            return
        # Stays busy: this process is about to exit into the new version.
        try:
            self._write_marker(env, version, source)
        except Exception as exc:
            log.warning("could not update %s: %s", MARKER_NAME, exc)
        await send(
            encode(
                {
                    "type": "result",
                    "req": req,
                    "data": {"updated": version, "source": source, "restarting": True},
                }
            )
        )
        log.info("bridge updated to herdeck %s (%s); exiting for a service restart", version, source)
        self.exit_requested = True
        self._request_exit()

    async def _progress(self, send: Send, req: str, stage: str, message: str) -> None:
        await send(
            encode(
                {
                    "type": "progress",
                    "req": req,
                    "stage": stage,
                    "message": message[:PROGRESS_MAX_CHARS],
                }
            )
        )

    async def _download(self, url: str, max_bytes: int) -> bytes | None:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._fetch, url, max_bytes, DOWNLOAD_TIMEOUT_S),
                timeout=DOWNLOAD_TIMEOUT_S + 5,
            )
        except TimeoutError as exc:
            raise UpdateError("failed", f"download timed out: {url}") from exc
        except UpdateError:
            raise
        except Exception as exc:
            raise UpdateError("failed", f"download failed: {url}: {exc}") from exc

    async def _install(self, req: str, version: str, env: ManagedEnv, send: Send) -> str:
        with tempfile.TemporaryDirectory(prefix="herdeck-update-") as tmp:
            name = wheel_name(version)
            url = asset_url(version, name)
            await self._progress(send, req, "download", f"downloading {url}")
            wheel = await self._download(url, WHEEL_MAX_BYTES)
            if wheel is None:
                target = git_source(version)
                source = "git"
                await self._progress(
                    send, req, "download", f"release v{version} has no wheel; using {target}"
                )
            else:
                sums = await self._download(asset_url(version, SUMS_NAME), SUMS_MAX_BYTES)
                if sums is None:
                    raise UpdateError(
                        "failed",
                        f"release v{version} has a wheel but no {SUMS_NAME}; refusing an "
                        "unverified install",
                    )
                expected = parse_sums(sums.decode("utf-8", "replace"), name)
                if expected is None:
                    raise UpdateError("failed", f"{SUMS_NAME} has no entry for {name}")
                actual = hashlib.sha256(wheel).hexdigest()
                if not hmac.compare_digest(actual, expected):
                    raise UpdateError(
                        "failed", f"SHA-256 mismatch for {name}: expected {expected}, got {actual}"
                    )
                await self._progress(send, req, "download", f"verified SHA-256 of {name}")
                path = Path(tmp) / name
                path.write_bytes(wheel)
                target = str(path)
                source = "wheel"
            argv = self._installer_argv(env, target)
            await self._progress(send, req, "install", f"installing with {Path(argv[0]).name}")

            async def on_line(line: str) -> None:
                await self._progress(send, req, "install", line)

            code, output = await self._run(
                argv, INSTALL_TIMEOUT_S, on_line, env=_child_env(), cwd=tmp
            )
            if code is None:
                raise UpdateError(
                    "failed", f"installer timed out after {INSTALL_TIMEOUT_S:.0f}s", output
                )
            if code != 0:
                raise UpdateError("failed", f"installer exited with {code}", output)
            await self._progress(send, req, "verify", f"checking the installed version is {version}")

            async def ignore(_line: str) -> None:
                return None

            code, output = await self._run(
                [env.python, "-I", "-c", "import herdeck; print(herdeck.__version__)"],
                VERIFY_TIMEOUT_S,
                ignore,
                env=_child_env(),
                cwd=tmp,
            )
            installed = output.strip().splitlines()[-1].strip() if output.strip() else ""
            if code != 0 or installed != version:
                raise UpdateError(
                    "failed",
                    f"installed version check failed (got {installed or 'nothing'}, "
                    f"expected {version})",
                    output,
                )
        return source

    def _installer_argv(self, env: ManagedEnv, target: str) -> list[str]:
        if self._has_pip():
            return [
                env.python,
                "-m",
                "pip",
                "install",
                "--no-input",
                "--progress-bar",
                "off",
                target,
            ]
        uv = _find_uv(self._which)
        if uv is not None:
            return [uv, "pip", "install", "--python", env.python, target]
        raise UpdateError("failed", "no installer: neither pip nor uv is available")


def _error_result(req: str, exc: UpdateError) -> str:
    error = {"code": exc.code, "message": exc.message}
    if exc.output:
        error["output"] = exc.output
    return encode({"type": "result", "req": req, "data": {"updated": None, "error": error}})


def not_managed_result(req: object, reason: str) -> str:
    """The reply of a bridge with no updater (the runtime's embedded one)."""
    return _error_result(req if isinstance(req, str) else "", UpdateError("not_managed", reason))
