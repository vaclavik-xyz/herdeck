"""Opt-in packaged-runtime smoke test with temporary config and in-memory token.

Starts the supplied frozen runtime, checks mixed local Herdr/T3 connections,
then terminates only that child. Does not touch installed Herdeck configuration.
"""
import argparse
import json
import os
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import tomli_w


def main(args):
    token = subprocess.check_output([args.t3_binary, "auth", "session", "issue",
        "--base-dir", args.base_dir, "--token-only", "--ttl", "1h", "--label", "Herdeck packaged pilot"],
        text=True, stderr=subprocess.DEVNULL, timeout=30).strip()
    with tempfile.TemporaryDirectory(prefix="herdeck-t3-package-") as folder:
        config = Path(folder) / "config.toml"
        config.write_text(tomli_w.dumps({"servers": [{"id": "t3-pilot", "url": args.url,
            "backend": "t3", "token_env": "HERDECK_T3_PILOT_TOKEN"}]}))
        local = Path(folder) / "local.toml"
        local.write_text('[local]\nherdr_sessions = ["default"]\n')
        env = {**os.environ, "HERDECK_CONFIG": str(config), "HERDECK_LOCAL_CONFIG": str(local),
            "HERDECK_T3_PILOT_TOKEN": token, "HERDECK_DECKAPP_PORT": "0",
            "HERDECK_RUNTIME_MANAGED": "1"}
        for name in ("HERDECK_MOCK", "HERDECK_PROFILE", "HERDR_SOCKET", "HERDR_SOCKET_PATH"):
            env.pop(name, None)
        proc = subprocess.Popen([args.runtime], env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
        try:
            info = json.loads(proc.stdout.readline())
            health = {}
            for _ in range(40):
                request = urllib.request.Request(info["url"] + "/health?token=" + info["token"])
                with urllib.request.urlopen(request, timeout=2) as response:
                    health = json.load(response)
                if health.get("connections", {}).get("t3-pilot") and health.get("connections", {}).get("local"):
                    print(json.dumps({k: health.get(k) for k in ("source", "connected", "server_ids", "connections")}))
                    return
                time.sleep(.5)
            print(json.dumps({"mixed_connections_verified": False, "connections": health.get("connections")}))
            raise SystemExit(1)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("runtime", "t3-binary", "base-dir", "url"):
        parser.add_argument("--" + name, required=True)
    main(parser.parse_args())
