"""Short-lived credentials for opt-in probes; never printed or persisted."""
import json
import subprocess
from contextlib import contextmanager


@contextmanager
def session_token(binary, base_dir):
    issued = json.loads(subprocess.check_output([binary, "auth", "session", "issue",
        "--base-dir", base_dir, "--json", "--ttl", "1h", "--label", "Herdeck disposable pilot"],
        text=True, stderr=subprocess.DEVNULL, timeout=30))
    try:
        yield issued["token"]
    finally:
        result = subprocess.run([binary, "auth", "session", "revoke", issued["sessionId"],
            "--base-dir", base_dir], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=30, check=False)
        if result.returncode:
            raise RuntimeError("Could not revoke the disposable T3 probe session")
