"""Opt-in real T3 restart probe; owns and terminates only its child servers."""
import argparse
import asyncio
import json
import socket
import subprocess
import tempfile

from herdeck.config import ServerConfig
from herdeck.t3 import T3Connector


async def main(binary):
    with tempfile.TemporaryDirectory(prefix="herdeck-t3-reconnect-") as directory:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        cmd = [binary, "--base-dir", directory, "--host", "127.0.0.1", "--port", str(port),
               "--no-browser", "--log-level", "error"]
        def start():
            return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc = start()
        task = None
        try:
            await asyncio.sleep(2)
            token = subprocess.check_output([binary, "auth", "session", "issue", "--base-dir", directory,
                "--token-only", "--ttl", "5m"], text=True, stderr=subprocess.DEVNULL).strip()
            online = []
            c = T3Connector(ServerConfig("pilot", f"http://127.0.0.1:{port}", token, "t3"),
                on_snapshot=lambda *a: None, on_event=lambda *a: None,
                on_connection=lambda sid, up: online.append(up))
            task = asyncio.create_task(c.run())
            async def until(predicate):
                for _ in range(30):
                    if predicate():
                        return
                    await asyncio.sleep(.5)
                raise AssertionError("T3 reconnect deadline exceeded")
            await until(lambda: online and online[-1])
            before = set(c.states)
            epoch = c._epoch
            proc.terminate()
            await asyncio.to_thread(proc.wait, timeout=10)
            await until(lambda: online and not online[-1])
            proc = start()
            await until(lambda: online and online[-1])
            assert set(c.states) == before and c._epoch != epoch
            print(json.dumps({"reconnect_verified": True, "same_threads": True,
                              "stale_controls_invalidated": True}))
        finally:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            proc.terminate()
            await asyncio.to_thread(proc.wait, timeout=10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True)
    asyncio.run(main(parser.parse_args().binary))
