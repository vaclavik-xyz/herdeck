"""Opt-in T3 desktop read-state bridge, local to the deck host.

T3 0.0.38 stores scoped visit boundaries in Chromium Local Storage. Read the
current key through LevelDB on a private, stable copy, never open/lock the live DB
or mine obsolete log records. This cannot observe another device's visits.
"""
from __future__ import annotations

import ctypes as C
import ctypes.util
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from .t3_state import timestamp

_UI_KEY = b"_t3code://app\x00\x01t3code:ui-state:v1"
_MAX_COPY = 64 * 1024 * 1024
_FILES = re.compile(r"(?:CURRENT|MANIFEST-[0-9]+|[0-9]+\.(?:log|ldb|sst))\Z")


class DesktopReadError(Exception):
    """Sanitized diagnostic; never includes database keys, values or credentials."""


class LevelDb:
    """Minimal stable C API. Only caller-owned temporary copies may be opened."""

    def __init__(self):
        library = os.environ.get("HERDECK_LEVELDB_LIBRARY")
        if not library:
            library = next((str(p) for p in (
                Path('/opt/homebrew/opt/leveldb/lib/libleveldb.dylib'),
                Path('/usr/local/opt/leveldb/lib/libleveldb.dylib'),
            ) if p.is_file()), None) or ctypes.util.find_library("leveldb")
        if not library:
            raise DesktopReadError("T3 desktop read sync requires LevelDB")
        self.lib = C.CDLL(library)
        ptr, size, string = C.c_void_p, C.c_size_t, C.c_char_p
        signatures = {
            "options_create": (ptr, []), "options_destroy": (None, [ptr]),
            "options_set_paranoid_checks": (None, [ptr, C.c_ubyte]),
            "open": (ptr, [ptr, string, C.POINTER(ptr)]),
            "close": (None, [ptr]),
            "readoptions_create": (ptr, []), "readoptions_destroy": (None, [ptr]),
            "readoptions_set_verify_checksums": (None, [ptr, C.c_ubyte]),
            "get": (ptr, [ptr, ptr, string, size, C.POINTER(size), C.POINTER(ptr)]),
            "free": (None, [ptr]),
        }
        for name, (result, args) in signatures.items():
            try:
                func = getattr(self.lib, 'leveldb_' + name)
            except AttributeError:
                raise DesktopReadError("Incompatible LevelDB library") from None
            func.restype, func.argtypes = result, args
            setattr(self, name, func)

    def check(self, error):
        if error.value:
            self.free(error)
            raise DesktopReadError("T3 desktop storage could not be read")

    def read_copy(self, directory):
        options, read_options = self.options_create(), self.readoptions_create()
        database = value = None
        try:
            self.options_set_paranoid_checks(options, 1)
            self.readoptions_set_verify_checksums(read_options, 1)
            error = C.c_void_p()
            database = self.open(options, os.fsencode(directory), C.byref(error))
            self.check(error)
            if not database:
                raise DesktopReadError("T3 desktop snapshot could not be opened")
            size = C.c_size_t()
            value = self.get(database, read_options, _UI_KEY, len(_UI_KEY), C.byref(size), C.byref(error))
            self.check(error)
            if value is None:
                return None
            if size.value > 8 * 1024 * 1024:
                raise DesktopReadError("T3 desktop UI record exceeds limit")
            return C.string_at(value, size.value)
        finally:
            if value:
                self.free(value)
            if database:
                self.close(database)
            self.readoptions_destroy(read_options)
            self.options_destroy(options)


def inventory(directory):
    if directory.is_symlink():
        raise DesktopReadError("T3 desktop storage must be a local directory")
    files = {}
    for path in directory.iterdir():
        if not _FILES.fullmatch(path.name):
            continue
        if path.is_symlink() or not path.is_file():
            raise DesktopReadError("Unexpected T3 desktop storage entry")
        stat = path.stat()
        files[path.name] = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
    if "CURRENT" not in files or sum(v[1] for v in files.values()) > _MAX_COPY:
        raise DesktopReadError("T3 desktop storage is missing or exceeds limit")
    return files


def visits_from_value(raw):
    if raw is None:
        return {}
    # Chromium's DOMString encoding tag: 0 = UTF-16LE, 1 = Latin-1.
    if not raw or raw[0] not in (0, 1):
        raise DesktopReadError("Unsupported T3 desktop UI encoding")
    value = json.loads(raw[1:].decode('utf-16-le' if raw[0] == 0 else 'latin-1'))
    if not isinstance(value, dict):
        raise DesktopReadError("Unsupported T3 desktop UI record")
    visits = value.get('threadLastVisitedAtById')
    if not isinstance(visits, dict):
        raise DesktopReadError("Unsupported T3 desktop visits")
    if any(not isinstance(k, str) or timestamp(v) is None for k, v in visits.items()):
        raise DesktopReadError("Unsupported T3 desktop visit timestamp")
    return visits


class DesktopSeen:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else Path.home() / 'Library/Application Support/t3code/Local Storage/leveldb'
        self._signature = None
        self._visits = {}
        self._db = None
        self.last_error = None

    def refresh(self):
        try:
            before = inventory(self.directory)
            if before == self._signature:
                return
            # LevelDB may recover/compact its copy. The live directory is only
            # read with ordinary file I/O; its LOCK and session files are excluded.
            with tempfile.TemporaryDirectory(prefix='herdeck-t3-read-') as temp:
                for name in before:
                    shutil.copyfile(self.directory / name, Path(temp) / name)
                if inventory(self.directory) != before:
                    raise DesktopReadError("T3 desktop storage changed during snapshot")
                self._db = self._db or LevelDb()
                raw = self._db.read_copy(temp)
                if raw is None:
                    raise DesktopReadError("T3 desktop UI record is missing")
                visits = visits_from_value(raw)
            self._visits, self._signature = visits, before
            self.last_error = None
        except (OSError, ValueError, DesktopReadError):
            # Never hide Done using an outdated visit after a failed read. Also
            # invalidate the signature so an unchanged recovered DB is retried.
            self._signature, self._visits = None, {}
            self.last_error = "T3 desktop read sync unavailable; keeping completions visible"

    def get(self, environment_id, thread_id):
        if not environment_id:
            return None
        return self._visits.get(f'{environment_id}:{thread_id}')
