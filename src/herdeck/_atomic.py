"""Atomic 0600 file write shared by the bridge-owned stores."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_private(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` via temp file + ``os.replace``, mode 0600.

    On any failure the temp file is removed and the existing file is untouched.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=path.name + ".", delete=False)
    try:
        with tmp:
            tmp.write(data)
        os.chmod(tmp.name, 0o600)
        os.replace(tmp.name, path)
    except BaseException:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise
