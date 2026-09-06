"""Local completion acknowledgments; T3 has no shared read-marker API."""
import hashlib
import json
import os
import tempfile
from pathlib import Path


class SeenStore:
    def __init__(self, directory: Path, server: str):
        self.path = directory / (hashlib.sha256(server.encode()).hexdigest() + ".json")

    def _read(self):
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, thread):
        return self._read().get(thread)

    def mark(self, thread, completed):
        data = self._read()
        data[thread] = completed
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(dir=self.path.parent, prefix=".seen-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(path, self.path)
        finally:
            Path(path).unlink(missing_ok=True)
