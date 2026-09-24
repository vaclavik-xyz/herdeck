"""Runtime maintenance: what the desktop Maintenance section shows and does.

Backs the token-authenticated routes ``GET /maintenance``,
``POST /maintenance/deck/restart`` and ``POST /maintenance/deck/power-cycle``
(deckapp/server.py). Everything here runs on an HTTP handler thread (the
runtime has no event loop to block); subprocesses get an argv list, never a
shell, and a timeout.

- D200 USB presence: a HID enumeration for the D200's VID/PID, cached for
  ``USB_PRESENT_TTL_S``.
- D200 USB location (hub + port, in uhubctl's terms): from Linux sysfs or from
  ``uhubctl``'s own listing, persisted to ``$HERDECK_RUNTIME_DIR/d200-usb.json``
  (default ``~/.cache/herdeck``) so a power-cycle still knows where to cut power
  once the device has vanished from USB. ``[hardware].usb_hub`` +
  ``[hardware].usb_port`` override it.
- Power-cycle: ``uhubctl -l <hub> -p <port> -a cycle -d 2``. When uhubctl lacks
  the permission it reports ``needs_admin`` with the exact command to run.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .. import __version__
from .device_lock import d200_lock_path

log = logging.getLogger(__name__)

# The Ulanzi D200's USB ids — mirrors strmdck's UlanziD200Device.USB_VENDOR_ID /
# USB_PRODUCT_ID (kept literal so this module loads without the deck extra).
D200_VID = 0x2207
D200_PID = 0x0019
USB_STATE_NAME = "d200-usb.json"
USB_PRESENT_TTL_S = 2.0
# How long a discovered hub location stays fresh before GET /maintenance asks
# uhubctl again (its listing takes up to a second on a busy USB tree).
USB_LOCATION_TTL_S = 60.0
UHUBCTL_LIST_TIMEOUT_S = 10.0
UHUBCTL_CYCLE_TIMEOUT_S = 30.0
DECK_RESTART_TIMEOUT_S = 15.0
RUNTIME_LABEL = "dev.herdeck.runtime"
RUNTIME_SYSTEMD_UNIT = "herdeck-runtime.service"
# launchd's PATH is /usr/bin:/bin:/usr/sbin:/sbin — Homebrew's uhubctl is not on it.
UHUBCTL_FALLBACK_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/sbin", "/usr/bin")
_USB_HUB_RE = re.compile(r"[0-9]+(-[0-9]+(\.[0-9]+)*)?")
# uhubctl / libusb wording when the caller lacks permission to switch a port.
_NEEDS_ADMIN_RE = re.compile(
    r"permission|access denied|LIBUSB_ERROR_ACCESS|operation not permitted|"
    r"run (it )?as root|with sudo|root privileges",
    re.IGNORECASE,
)
_ID = f"{D200_VID:04x}:{D200_PID:04x}"


def usb_state_path() -> Path:
    """``$HERDECK_RUNTIME_DIR/d200-usb.json`` — beside d200.lock and runtime.json."""
    return Path(d200_lock_path()).with_name(USB_STATE_NAME)


def load_usb_location(path: Path | None = None) -> dict | None:
    path = path or usb_state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    hub, port = data.get("hub"), data.get("port")
    if not isinstance(hub, str) or not _USB_HUB_RE.fullmatch(hub):
        return None
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 127:
        return None
    return {"hub": hub, "port": port, "seen_at": data.get("seen_at")}


def save_usb_location(hub: str, port: int, path: Path | None = None) -> None:
    """Atomically persist the D200's last-seen hub location."""
    path = path or usb_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"hub": hub, "port": port, "seen_at": int(time.time() * 1000)}
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".d200-usb-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def parse_uhubctl_listing(text: str) -> tuple[str, int] | None:
    """Find the D200 in ``uhubctl``'s status listing::

        Current status for hub 20-1 [2109:2817 VIA Labs, Inc. USB2.0 Hub, ...]
          Port 2: 0103 power enable connect [2207:0019 Ulanzi D200]
    """
    hub = None
    for line in text.splitlines():
        match = re.match(r"\s*Current status for hub (\S+)", line)
        if match:
            hub = match.group(1)
            continue
        match = re.match(r"\s*Port (\d+):.*\[([0-9a-fA-F]{4}:[0-9a-fA-F]{4})", line)
        if match and hub and match.group(2).lower() == _ID and _USB_HUB_RE.fullmatch(hub):
            return hub, int(match.group(1))
    return None


def sysfs_d200_location(root: str = "/sys/bus/usb/devices") -> tuple[str, int] | None:
    """Linux: a USB device directory is named ``<bus>-<port>[.<port>...]``; the
    D200's hub location is everything before its last port number."""
    try:
        names = os.listdir(root)
    except OSError:
        return None
    for name in sorted(names):
        if ":" in name or "-" not in name:
            continue  # interfaces (1-1:1.0) and root hubs (usb1)
        base = os.path.join(root, name)
        try:
            with open(os.path.join(base, "idVendor"), encoding="ascii") as fh:
                vendor = fh.read().strip().lower()
            with open(os.path.join(base, "idProduct"), encoding="ascii") as fh:
                product = fh.read().strip().lower()
        except OSError:
            continue
        if f"{vendor}:{product}" != _ID:
            continue
        match = re.fullmatch(r"([0-9]+-(?:[0-9]+\.)*?)([0-9]+)", name)
        if match is None:
            continue
        prefix, port = match.group(1), int(match.group(2))
        hub = prefix.rstrip(".-")
        if _USB_HUB_RE.fullmatch(hub):
            return hub, port
    return None


def find_uhubctl(configured: str = "", *, which=shutil.which, is_exe=None) -> str | None:
    """``[hardware].uhubctl`` when set (must be executable), else PATH and the
    Homebrew prefixes launchd's minimal PATH leaves out."""
    is_exe = is_exe or (lambda p: os.path.isfile(p) and os.access(p, os.X_OK))
    if configured:
        path = os.path.expanduser(configured)
        return path if is_exe(path) else None
    found = which("uhubctl")
    if found:
        return found
    for directory in UHUBCTL_FALLBACK_DIRS:
        candidate = os.path.join(directory, "uhubctl")
        if is_exe(candidate):
            return candidate
    return None


def _default_enumerate() -> list[dict] | None:
    try:
        import hid
    except Exception:
        return None  # no deck extra / no hidapi: presence unknown
    return list(hid.enumerate(D200_VID, D200_PID))


def _default_runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        stdin=subprocess.DEVNULL,
    )


def _tail(text: str, limit: int = 600) -> str:
    text = (text or "").strip()
    return text[-limit:]


def app_log_path(home: Path, platform: str = sys.platform) -> Path:
    """The desktop app's own log (desktop/src-tauri/src/app_log.rs)."""
    if platform == "darwin":
        return home / "Library/Logs/herdeck/herdeck.log"
    state = os.environ.get("XDG_STATE_HOME") or str(home / ".local/state")
    return Path(state) / "herdeck/herdeck.log"


def runtime_service_info(home: Path, platform: str = sys.platform) -> dict:
    """Is the runtime installed as a service (herdeck-service install runtime),
    and does its unit run the frozen runtime bundled in a desktop app?"""
    from ..service import APP_RUNTIME_BINARY

    info: dict = {
        "installed": False,
        "label": RUNTIME_LABEL if platform == "darwin" else RUNTIME_SYSTEMD_UNIT,
        "unit_path": None,
        "program": None,
        "from_app": False,
        "log": None,
    }
    program: str | None = None
    if platform == "darwin":
        import plistlib

        unit = home / "Library/LaunchAgents" / f"{RUNTIME_LABEL}.plist"
        try:
            data = plistlib.loads(unit.read_bytes())
        except (OSError, ValueError, plistlib.InvalidFileException):
            data = None
        if isinstance(data, dict):
            info["installed"] = True
            info["unit_path"] = str(unit)
            arguments = data.get("ProgramArguments") or []
            if arguments and isinstance(arguments[0], str):
                program = arguments[0]
            stdout = data.get("StandardOutPath")
            info["log"] = stdout if isinstance(stdout, str) else None
    else:
        unit = home / ".config/systemd/user" / RUNTIME_SYSTEMD_UNIT
        try:
            text = unit.read_text(encoding="utf-8")
        except OSError:
            text = None
        if text is not None:
            info["installed"] = True
            info["unit_path"] = str(unit)
            for line in text.splitlines():
                if line.startswith("ExecStart="):
                    try:
                        words = shlex.split(line[len("ExecStart="):])
                    except ValueError:
                        words = []
                    program = words[0] if words else None
                    break
    info["program"] = program
    info["from_app"] = bool(program) and program.endswith("/" + str(APP_RUNTIME_BINARY))
    return info


def _this_process_is_service(platform: str = sys.platform) -> bool:
    if platform == "darwin":
        # launchd sets XPC_SERVICE_NAME to the job label for its jobs.
        return os.environ.get("XPC_SERVICE_NAME") == RUNTIME_LABEL
    try:
        with open("/proc/self/cgroup", encoding="utf-8") as fh:
            return RUNTIME_SYSTEMD_UNIT in fh.read()
    except OSError:
        return False


class Maintenance:
    """Maintenance facts and actions for one DeckApp.

    ``enumerate_usb``, ``runner``, ``which`` and ``sysfs_root`` are injectable
    so tests never touch real USB or run real tools."""

    def __init__(
        self,
        app,
        *,
        enumerate_usb: Callable[[], list[dict] | None] | None = None,
        runner: Callable[[list[str], float], subprocess.CompletedProcess] | None = None,
        which=shutil.which,
        is_exe=None,
        sysfs_root: str = "/sys/bus/usb/devices",
        home: Path | None = None,
        platform: str = sys.platform,
        clock: Callable[[], float] = time.monotonic,
        state_path: Path | None = None,
    ):
        self._app = app
        self._enumerate = enumerate_usb or _default_enumerate
        self._runner = runner or _default_runner
        self._which = which
        self._is_exe = is_exe
        self._sysfs_root = sysfs_root
        self._home = home or Path.home()
        self._platform = platform
        self._clock = clock
        self._state_path = state_path
        self._lock = threading.Lock()  # guards the caches below
        self._present: tuple[float, bool | None] | None = None
        self._location_checked_at: float | None = None
        self._restart_lock = threading.Lock()
        self._cycle_lock = threading.Lock()

    # --- facts ---------------------------------------------------------------

    def _sink(self):
        for sink in list(getattr(self._app, "_sinks", [])):
            if callable(getattr(sink, "restart", None)):
                return sink
        return None

    def _hardware(self):
        return self._app.config.hardware

    def usb_present(self) -> bool | None:
        now = self._clock()
        with self._lock:
            cached = self._present
            if cached is not None and now - cached[0] < USB_PRESENT_TTL_S:
                return cached[1]
        try:
            devices = self._enumerate()
        except Exception:
            log.debug("HID enumeration failed", exc_info=True)
            devices = None
        present = None if devices is None else any(
            d.get("vendor_id") == D200_VID and d.get("product_id") == D200_PID for d in devices
        )
        with self._lock:
            self._present = (now, present)
        return present

    def _uhubctl(self) -> str | None:
        return find_uhubctl(self._hardware().uhubctl, which=self._which, is_exe=self._is_exe)

    def _discover_location(self, *, force: bool = False) -> None:
        """Refresh the persisted location while the D200 is on USB."""
        now = self._clock()
        with self._lock:
            checked = self._location_checked_at
            if not force and checked is not None and now - checked < USB_LOCATION_TTL_S:
                return
            self._location_checked_at = now
        found = sysfs_d200_location(self._sysfs_root) if self._platform != "darwin" else None
        if found is None:
            uhubctl = self._uhubctl()
            if uhubctl is not None:
                try:
                    listing = self._runner([uhubctl], UHUBCTL_LIST_TIMEOUT_S)
                    found = parse_uhubctl_listing(
                        (listing.stdout or "") + "\n" + (listing.stderr or "")
                    )
                except (OSError, subprocess.SubprocessError):
                    log.info("uhubctl listing failed", exc_info=True)
        if found is None:
            return
        try:  # rewritten each time (at most once per TTL) to keep seen_at fresh
            save_usb_location(*found, path=self._state_path)
        except OSError:
            log.warning("could not persist the D200 USB location", exc_info=True)

    def location(self) -> dict | None:
        """``{hub, port, source}``: the config pin wins over the last-seen one."""
        hw = self._hardware()
        if hw.usb_hub and hw.usb_port:
            return {"hub": hw.usb_hub, "port": hw.usb_port, "source": "config"}
        seen = load_usb_location(self._state_path)
        if seen is not None:
            return {"hub": seen["hub"], "port": seen["port"], "source": "last_seen"}
        return None

    def power_cycle_status(self, location: dict | None) -> dict:
        uhubctl = self._uhubctl()
        reason = None
        if uhubctl is None:
            reason = (
                "uhubctl_not_executable" if self._hardware().uhubctl else "uhubctl_missing"
            )
        elif location is None:
            reason = "location_unknown"
        return {
            "available": reason is None,
            "reason": reason,
            "uhubctl": uhubctl,
            "hub": location["hub"] if location else None,
            "port": location["port"] if location else None,
            "source": location["source"] if location else None,
        }

    def d200_status(self) -> dict:
        sink = self._sink()
        health = {}
        if sink is not None and callable(getattr(sink, "health", None)):
            health = dict(sink.health())
        present = self.usb_present()
        if present:
            self._discover_location()
        location = self.location()
        if sink is None:
            state = "unsupervised"
        elif health.get("connected"):
            state = "connected"
        elif health.get("lock_owner") is not None:
            state = "locked"
        elif present is False:
            state = "not_on_usb"
        else:
            state = "disconnected"
        return {
            **health,
            "supervised": sink is not None,
            "state": state,
            "usb_present": present,
            "usb_location": f"{location['hub']}:{location['port']}" if location else None,
            "power_cycle": self.power_cycle_status(location),
        }

    def status(self) -> dict:
        app = self._app
        service = runtime_service_info(self._home, self._platform)
        spawned_by_app = os.environ.get("HERDECK_RUNTIME_MANAGED") == "1"
        servers = {}
        server_health = getattr(app._source, "server_health", None)
        hooks_summary = getattr(app._source, "hooks_summary", None)
        usage_agent_summary = getattr(app._source, "usage_agent_summary", None)
        if callable(server_health):
            for sid, facts in server_health().items():
                # self_update: the bridge understands {"type": "update"}.
                # managed: its health probe says it runs from a managed install
                # and so may update itself; None = unknown (not asked yet, no
                # answer, an older bridge, or a non-herdeck backend).
                managed = facts.get("managed")
                servers[sid] = {
                    **facts,
                    "self_update": facts.get("self_update") is True,
                    "managed": managed if isinstance(managed, bool) else None,
                    # Subagent hooks on that bridge's machine (hooks_relay.py):
                    # {claude: {installed, ...}, codex: {...}}; None = unknown
                    # (not asked yet, an older bridge, a read-only token, T3).
                    "hooks": hooks_summary(sid) if callable(hooks_summary) else None,
                    # The usage agent on that bridge's machine (usage_agent_relay.py):
                    # {installed, running, fresh, bridge_usage, file_age_s,
                    # providers, error}; None = unknown (not asked yet, an older
                    # bridge, a read-only token, T3).
                    "usage_agent": (
                        usage_agent_summary(sid) if callable(usage_agent_summary) else None
                    ),
                }
        return {
            "version": __version__,
            "pid": os.getpid(),
            "uptime_s": int(time.monotonic() - getattr(app, "_started_at", time.monotonic())),
            "process": {
                # (herdeck.frozen.is_frozen, without its Pillow import)
                "frozen": bool(getattr(sys, "frozen", False)),
                "executable": sys.executable,
                "spawned_by_app": spawned_by_app,
                "is_service": _this_process_is_service(self._platform),
            },
            "service": {
                key: service[key]
                for key in ("installed", "label", "unit_path", "program", "from_app")
            },
            "logs": {
                "runtime": service["log"],
                "app": str(app_log_path(self._home, self._platform)),
            },
            "d200": self.d200_status(),
            "servers": servers,
            # Why the existing config does not load (None = it loads): the
            # runtime then shows an error state, never demo agents.
            "config_error": getattr(app, "config_error", None),
        }

    # --- actions -------------------------------------------------------------

    def restart_deck(self) -> dict:
        sink = self._sink()
        if sink is None:
            return {"ok": False, "outcome": "unsupported"}
        if not self._restart_lock.acquire(blocking=False):
            return {"ok": False, "outcome": "busy"}
        try:
            result = dict(sink.restart(timeout=DECK_RESTART_TIMEOUT_S))
        finally:
            self._restart_lock.release()
        outcome = result.get("outcome")
        if outcome == "failed":
            with self._lock:
                self._present = None  # re-probe: the answer decides the outcome
            present = self.usb_present()
            result["usb_present"] = present
            if present is not True:
                result["outcome"] = "not_present"
        elif outcome == "reopened":
            self._repaint()
        result["ok"] = result["outcome"] == "reopened"
        log.warning("maintenance: deck restart -> %s", result["outcome"])
        return result

    def _repaint(self) -> None:
        """A fresh full frame to every sink (the reopened D200 already got the
        retained one; this one carries anything that changed meanwhile)."""
        app = self._app
        refresh = getattr(app, "_refresh_locked", None)
        lock = getattr(app, "_lock", None)
        if refresh is None or lock is None:
            return
        try:
            with lock:
                refresh(working=None, full=True)
        except Exception:
            log.warning("repaint after deck restart failed", exc_info=True)

    def power_cycle(self) -> dict:
        if not self._cycle_lock.acquire(blocking=False):
            return {"ok": False, "outcome": "busy"}
        try:
            return self._power_cycle_locked()
        finally:
            self._cycle_lock.release()

    def _power_cycle_locked(self) -> dict:
        if self.usb_present():
            self._discover_location(force=True)
        status = self.power_cycle_status(self.location())
        if not status["available"]:
            return {"ok": False, "outcome": "unavailable", "reason": status["reason"]}
        argv = [
            status["uhubctl"], "-l", status["hub"], "-p", str(status["port"]),
            "-a", "cycle", "-d", "2",
        ]
        command = shlex.join(argv)
        try:
            result = self._runner(argv, UHUBCTL_CYCLE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            log.warning("maintenance: %s timed out", command)
            return {"ok": False, "outcome": "timeout", "command": command}
        except OSError as exc:
            return {"ok": False, "outcome": "failed", "command": command, "error": str(exc)}
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        if result.returncode != 0:
            if _NEEDS_ADMIN_RE.search(output):
                log.warning("maintenance: %s needs admin rights", command)
                return {
                    "ok": False,
                    "outcome": "needs_admin",
                    "command": "sudo " + command,
                    "error": _tail(output),
                }
            log.warning("maintenance: %s failed (%s)", command, result.returncode)
            return {
                "ok": False,
                "outcome": "failed",
                "command": command,
                "error": _tail(output),
            }
        log.warning("maintenance: %s ok", command)
        with self._lock:
            self._present = None
        sink = self._sink()
        if sink is not None and callable(getattr(sink, "kick", None)):
            sink.kick()  # reopen as soon as the device re-enumerates
        return {
            "ok": True,
            "outcome": "cycled",
            "command": command,
            "hub": status["hub"],
            "port": status["port"],
        }
