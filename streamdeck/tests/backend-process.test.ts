import { describe, it, expect, vi } from "vitest";
import { EventEmitter } from "node:events";
import { BackendProcess, resolveHerdeckCommand } from "../src/backend-process.js";

function fakeChild() {
  const child: any = new EventEmitter();
  child.kill = vi.fn();
  return child;
}

describe("resolveHerdeckCommand", () => {
  it("prefers the PI-configured path, then HERDECK_BIN, then PATH", () => {
    expect(resolveHerdeckCommand({ configuredPath: "/opt/h", envBin: "/usr/bin/herdeck" })).toEqual({ command: "/opt/h", args: [] });
    expect(resolveHerdeckCommand({ envBin: "/usr/bin/herdeck" })).toEqual({ command: "/usr/bin/herdeck", args: [] });
    expect(resolveHerdeckCommand({})).toEqual({ command: "herdeck", args: [] });
  });
});

describe("BackendProcess", () => {
  it("spawns herdeck with the env contract and a generated socket+token", () => {
    const child = fakeChild();
    const spawn = vi.fn(() => child);
    const bp = new BackendProcess({
      resolveCommand: () => ({ command: "herdeck", args: [] }),
      spawn: spawn as any,
      randomToken: () => "tok123",
      tmpDir: "/tmp",
    });
    bp.start();
    expect(bp.token).toBe("tok123");
    expect(bp.socketPath).toMatch(/^\/tmp\/herdeck-elgato-.*\.sock$/);
    const [cmd, args, options] = spawn.mock.calls[0];
    expect(cmd).toBe("herdeck");
    expect(args).toEqual([]);
    expect(options.env.HERDECK_ELGATO_SOCK).toBe(bp.socketPath);
    expect(options.env.HERDECK_ELGATO_TOKEN).toBe("tok123");
    expect(options.env.HERDECK_DECK).toBe("elgato-plugin");
  });

  it("respawns with bounded exponential backoff after the child exits", () => {
    const children = [fakeChild(), fakeChild(), fakeChild()];
    let i = 0;
    const spawn = vi.fn(() => children[i++]);
    const delays: number[] = [];
    const setTimer = (cb: () => void, ms: number) => { delays.push(ms); cb(); return 0; };
    const states: string[] = [];

    const bp = new BackendProcess({
      resolveCommand: () => ({ command: "herdeck", args: [] }),
      spawn: spawn as any,
      randomToken: () => "t",
      tmpDir: "/tmp",
      setTimer,
      baseBackoffMs: 500,
      maxBackoffMs: 1000,
    });
    bp.onState((s) => states.push(s));
    bp.start();                       // spawn #1 (starting)
    children[0].emit("exit", 1);      // down -> backoff 500 -> spawn #2 (starting)
    children[1].emit("exit", 1);      // down -> backoff 1000 -> spawn #3 (starting)

    expect(spawn).toHaveBeenCalledTimes(3);
    expect(delays).toEqual([500, 1000]);          // doubling, capped at maxBackoffMs
    expect(states).toEqual(["starting", "down", "starting", "down", "starting"]);
    bp.stop();
    expect(children[2].kill).toHaveBeenCalled();
  });

  it("respawns when spawn fails with an 'error' event (ENOENT) and no 'exit' fires", () => {
    const children = [fakeChild(), fakeChild()];
    let i = 0;
    const spawn = vi.fn(() => children[i++]);
    const setTimer = (cb: () => void) => { cb(); return 0; };
    const states: string[] = [];
    const bp = new BackendProcess({
      resolveCommand: () => ({ command: "herdeck", args: [] }),
      spawn: spawn as any, randomToken: () => "t", tmpDir: "/tmp", setTimer,
    });
    bp.onState((s) => states.push(s));
    bp.start();                                              // spawn #1 (starting)
    children[0].emit("error", new Error("spawn herdeck ENOENT")); // error, NOT exit
    expect(spawn).toHaveBeenCalledTimes(2);                 // respawned despite no 'exit'
    expect(states).toEqual(["starting", "down", "starting"]);
    bp.stop();
  });

  it("a single failure schedules exactly one respawn even if error+close both fire", () => {
    const children = [fakeChild(), fakeChild()];
    let i = 0;
    const spawn = vi.fn(() => children[i++]);
    const setTimer = (cb: () => void) => { cb(); return 0; };
    const bp = new BackendProcess({
      resolveCommand: () => ({ command: "herdeck", args: [] }),
      spawn: spawn as any, randomToken: () => "t", tmpDir: "/tmp", setTimer,
    });
    bp.start();
    children[0].emit("error", new Error("ENOENT"));
    children[0].emit("close", 1); // a second terminal signal for the SAME child
    expect(spawn).toHaveBeenCalledTimes(2); // not 3 — the settled guard de-dupes
    bp.stop();
  });

  it("dev mode connects to a known socket with the shared token instead of spawning", () => {
    const spawn = vi.fn();
    const bp = new BackendProcess({
      resolveCommand: () => ({ command: "herdeck", args: [] }),
      devSocket: "/tmp/dev.sock",
      devToken: "shared-token",
      spawn: spawn as any,
    });
    bp.start();
    expect(spawn).not.toHaveBeenCalled();
    expect(bp.spawned).toBe(false);
    expect(bp.socketPath).toBe("/tmp/dev.sock");
    expect(bp.token).toBe("shared-token"); // matches the external backend so hello authenticates
  });

  it("dev mode without a token fails fast (would otherwise silently mis-authenticate)", () => {
    expect(() => new BackendProcess({
      resolveCommand: () => ({ command: "herdeck", args: [] }),
      devSocket: "/tmp/dev.sock",
      spawn: vi.fn() as any,
    })).toThrow(/token/i);
  });
});
