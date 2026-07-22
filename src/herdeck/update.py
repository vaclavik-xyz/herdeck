from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from urllib.parse import urlparse

from . import __version__

DEFAULT_RELEASE_ENDPOINT = "https://api.github.com/repos/vaclavik-xyz/herdeck/releases/latest"
_VERSION_RE = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)


class UpdateCheckError(RuntimeError):
    pass


@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value: str) -> Version:
        match = _VERSION_RE.fullmatch(value.strip())
        if not match:
            raise UpdateCheckError(f"invalid release version: {value!r}")
        prerelease = tuple((match.group("prerelease") or "").split("."))
        if prerelease == ("",):
            prerelease = ()
        return cls(
            int(match.group("major")),
            int(match.group("minor")),
            int(match.group("patch")),
            prerelease,
        )

    def __lt__(self, other: Version) -> bool:
        own_core = (self.major, self.minor, self.patch)
        other_core = (other.major, other.minor, other.patch)
        if own_core != other_core:
            return own_core < other_core
        if not self.prerelease:
            return False
        if not other.prerelease:
            return True
        for own, theirs in zip(self.prerelease, other.prerelease, strict=False):
            if own == theirs:
                continue
            own_numeric = own.isdigit()
            theirs_numeric = theirs.isdigit()
            if own_numeric and theirs_numeric:
                return int(own) < int(theirs)
            if own_numeric != theirs_numeric:
                return own_numeric
            return own < theirs
        return len(self.prerelease) < len(other.prerelease)


@dataclass(frozen=True)
class UpdateStatus:
    current_version: str
    latest_version: str
    update_available: bool
    release_url: str
    published_at: str | None = None


def _validate_endpoint(endpoint: str) -> None:
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.netloc:
        raise UpdateCheckError("update endpoint must use HTTPS")


def _fetch_json(endpoint: str, *, timeout: float = 5.0) -> dict:
    _validate_endpoint(endpoint)
    request = urllib.request.Request(
        endpoint,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"herdeck/{__version__}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (OSError, urllib.error.HTTPError, urllib.error.URLError, ValueError) as exc:
        raise UpdateCheckError(f"could not check for updates: {exc}") from exc
    if not isinstance(payload, dict):
        raise UpdateCheckError("update endpoint returned an invalid response")
    return payload


def check_for_update(
    *,
    current_version: str = __version__,
    endpoint: str = DEFAULT_RELEASE_ENDPOINT,
    fetch_json: Callable[[str], dict] | None = None,
) -> UpdateStatus:
    fetch_json = fetch_json or _fetch_json
    payload = fetch_json(endpoint)
    tag = payload.get("tag_name")
    release_url = payload.get("html_url")
    if not isinstance(tag, str) or not isinstance(release_url, str):
        raise UpdateCheckError("release response is missing tag_name or html_url")
    current = Version.parse(current_version)
    latest = Version.parse(tag)
    return UpdateStatus(
        current_version=current_version,
        latest_version=tag.removeprefix("v"),
        update_available=current < latest,
        release_url=release_url,
        published_at=payload.get("published_at")
        if isinstance(payload.get("published_at"), str)
        else None,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="herdeck update")
    parser.add_argument(
        "--check",
        action="store_true",
        help="check the signed release channel without installing anything",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.check:
        _parser().error("automatic installation is not available yet; use --check")
    endpoint = os.environ.get("HERDECK_UPDATE_URL", DEFAULT_RELEASE_ENDPOINT)
    try:
        status = check_for_update(endpoint=endpoint)
    except UpdateCheckError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        else:
            print(f"herdeck update check failed: {exc}")
        return 2
    if args.json:
        print(json.dumps({"ok": True, **asdict(status)}, sort_keys=True))
    elif status.update_available:
        print(
            f"herdeck {status.latest_version} is available "
            f"(current {status.current_version})\n{status.release_url}"
        )
    else:
        print(f"herdeck {status.current_version} is up to date")
    return 0
