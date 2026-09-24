"""Managed bridge install: a herdeck release in its own virtualenv.

``herdeck-service install bridge --managed [--version X]`` builds
``~/.local/share/herdeck/bridge-venv`` (uv when present, else ``python -m
venv``), installs the herdeck release there — the wheel published on the
GitHub release, falling back to ``git+https://github.com/vaclavik-xyz/herdeck@vX``
— checks that the venv imports exactly that version, and writes
``managed.json`` into the venv. That marker is what later tells the bridge it
runs from an install it may update itself (never a dev/editable checkout).

Network and subprocesses go through injectable ``download`` / ``runner``
callables so tests never touch either.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

REPO = "vaclavik-xyz/herdeck"
RELEASE_BASE = f"https://github.com/{REPO}/releases/download/v{{version}}"
WHEEL_NAME = "herdeck-{version}-py3-none-any.whl"
SUMS_NAME = "SHA256SUMS"
GIT_SOURCE = f"git+https://github.com/{REPO}@v{{version}}"
MARKER_NAME = "managed.json"
INSTALL_TIMEOUT_S = 600.0
DOWNLOAD_TIMEOUT_S = 120.0
# PEP 440-ish release strings only: the version lands in a URL and in argv.
_VERSION_RE = re.compile(r"[0-9]+(\.[0-9]+){1,3}([.-]?(a|b|rc|dev|post)[0-9]*)*")


class ManagedInstallError(Exception):
    pass


def managed_venv_dir(home: Path) -> Path:
    return home / ".local/share/herdeck/bridge-venv"


def venv_python(venv: Path) -> Path:
    return venv / "bin" / "python"


def wheel_url(version: str) -> str:
    return f"{RELEASE_BASE.format(version=version)}/{WHEEL_NAME.format(version=version)}"


def sums_url(version: str) -> str:
    return f"{RELEASE_BASE.format(version=version)}/{SUMS_NAME}"


def git_source(version: str) -> str:
    return GIT_SOURCE.format(version=version)


def validate_version(version: str) -> str:
    version = version.strip().removeprefix("v")
    if not _VERSION_RE.fullmatch(version):
        raise ManagedInstallError(f"not a herdeck release version: {version!r}")
    return version


def read_marker(venv: Path) -> dict | None:
    try:
        data = json.loads((venv / MARKER_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _download(url: str, dest: Path) -> None:
    """Fetch ``url`` to ``dest``; raises on any HTTP/network failure."""
    request = urllib.request.Request(url, headers={"User-Agent": "herdeck-service"})
    with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_S) as response:  # noqa: S310
        with dest.open("wb") as handle:
            shutil.copyfileobj(response, handle)


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        stdin=subprocess.DEVNULL,
    )


def _tail(result: subprocess.CompletedProcess, limit: int = 1200) -> str:
    return (((result.stdout or "") + "\n" + (result.stderr or "")).strip())[-limit:]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_sums(text: str) -> dict[str, str]:
    """``sha256sum`` output: ``<hex>  <name>`` (a ``*`` before binary names)."""
    sums = {}
    for line in text.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            sums[parts[1].lstrip("*").strip()] = parts[0].lower()
    return sums


class ManagedInstaller:
    def __init__(
        self,
        *,
        runner: Callable[[list[str], float], subprocess.CompletedProcess] = _run,
        download: Callable[[str, Path], None] = _download,
        which: Callable[[str], str | None] = shutil.which,
        base_python: str | None = None,
        log: Callable[[str], None] = lambda message: print(message, file=sys.stderr),
    ):
        self._runner = runner
        self._download = download
        self._which = which
        self._base_python = base_python
        self._log = log

    def _check(self, argv: list[str], what: str) -> subprocess.CompletedProcess:
        try:
            result = self._runner(argv, INSTALL_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            raise ManagedInstallError(f"{what} timed out") from exc
        except OSError as exc:
            raise ManagedInstallError(f"{what} failed: {exc}") from exc
        if result.returncode != 0:
            raise ManagedInstallError(f"{what} failed:\n{_tail(result)}")
        return result

    def _python_for_venv(self) -> str:
        if self._base_python:
            return self._base_python
        if not getattr(sys, "frozen", False):
            return sys.executable
        # The desktop app's frozen binary is no interpreter: use the system's.
        found = self._which("python3")
        if not found:
            raise ManagedInstallError("no python3 found to create the bridge venv (or install uv)")
        return found

    def _create_venv(self, venv: Path, uv: str | None) -> None:
        if venv_python(venv).exists():
            return
        venv.parent.mkdir(parents=True, exist_ok=True)
        if uv:
            argv = [uv, "venv", "--seed", str(venv)]
            if self._base_python:
                argv += ["--python", self._base_python]
        else:
            argv = [self._python_for_venv(), "-m", "venv", str(venv)]
        self._log(f"creating {venv}")
        self._check(argv, "creating the bridge venv")

    def _fetch_wheel(self, version: str, directory: Path) -> tuple[Path, str, bool] | None:
        """``(wheel, sha256, verified)`` or None when the release has no wheel."""
        wheel = directory / WHEEL_NAME.format(version=version)
        try:
            self._download(wheel_url(version), wheel)
        except Exception as exc:  # 404 (older release) or no network
            self._log(f"release wheel unavailable ({exc}); falling back to git")
            return None
        digest = _sha256(wheel)
        sums_path = directory / SUMS_NAME
        try:
            self._download(sums_url(version), sums_path)
        except Exception:
            self._log("release has no SHA256SUMS; wheel hash not verified")
            return wheel, digest, False
        expected = parse_sums(sums_path.read_text(encoding="utf-8", errors="replace")).get(
            wheel.name
        )
        if expected is None:
            self._log(f"SHA256SUMS does not list {wheel.name}; wheel hash not verified")
            return wheel, digest, False
        if expected != digest:
            raise ManagedInstallError(
                f"{wheel.name} hash mismatch: expected {expected}, downloaded {digest}"
            )
        return wheel, digest, True

    def install(self, venv: Path, version: str) -> dict:
        """Install herdeck ``version`` into ``venv``; returns the marker written."""
        version = validate_version(version)
        uv = self._which("uv")
        self._create_venv(venv, uv)
        python = str(venv_python(venv))
        with tempfile.TemporaryDirectory(prefix="herdeck-managed-") as tmp:
            fetched = self._fetch_wheel(version, Path(tmp))
            if fetched is not None:
                wheel, digest, verified = fetched
                spec, source = str(wheel), wheel_url(version)
            else:
                spec = source = git_source(version)
                digest, verified = None, False
            if uv:
                argv = [uv, "pip", "install", "--python", python, "--upgrade", spec]
            else:
                argv = [python, "-m", "pip", "install", "--upgrade", spec]
            self._log(f"installing herdeck {version} from {source}")
            self._check(argv, f"installing herdeck {version}")
        check = self._check(
            [python, "-c", "import herdeck; print(herdeck.__version__)"],
            "verifying the installed herdeck",
        )
        installed = (check.stdout or "").strip().splitlines()[-1:] or [""]
        if installed[0] != version:
            raise ManagedInstallError(
                f"the bridge venv reports herdeck {installed[0] or '?'}, expected {version}"
            )
        marker = {
            "version": version,
            "source": source,
            "sha256": digest,
            "verified": verified,
            "venv": str(venv),
            "installed_at": int(time.time()),
        }
        path = venv / MARKER_NAME
        fd, tmp_marker = tempfile.mkstemp(dir=venv, prefix=".managed-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(marker, handle, indent=2)
        os.replace(tmp_marker, path)
        return marker
