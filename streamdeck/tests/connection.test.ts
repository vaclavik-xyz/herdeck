import { describe, it, expect, vi } from "vitest";
import { superviseConnection } from "../src/connection.js";

const flush = () => new Promise((r) => setTimeout(r, 0));

describe("superviseConnection", () => {
  it("attempts on start and re-attempts on backend-starting / ipc-close, one in flight", async () => {
    let resolveConnect: () => void = () => {};
    const connectOnce = vi.fn(() => new Promise<void>((res) => { resolveConnect = res; }));
    const triggers: any = {};
    superviseConnection({
      connectOnce: connectOnce as any,
      onBackendStarting: (cb) => (triggers.starting = cb),
      onIpcClose: (cb) => (triggers.close = cb),
    });

    expect(connectOnce).toHaveBeenCalledTimes(1); // initial attempt

    // while an attempt is in flight, further triggers do NOT stack a second one
    triggers.starting();
    triggers.close();
    expect(connectOnce).toHaveBeenCalledTimes(1);

    // once the in-flight attempt settles, a later trigger attempts again
    resolveConnect();
    await flush();
    triggers.starting();
    expect(connectOnce).toHaveBeenCalledTimes(2);
  });

  it("a failed attempt does not wedge the in-flight guard — a later trigger retries", async () => {
    let rejectConnect: (e: any) => void = () => {};
    const connectOnce = vi.fn(() => new Promise<void>((_res, rej) => { rejectConnect = rej; }));
    const triggers: any = {};
    superviseConnection({
      connectOnce: connectOnce as any,
      onBackendStarting: (cb) => (triggers.starting = cb),
      onIpcClose: (cb) => (triggers.close = cb),
    });
    expect(connectOnce).toHaveBeenCalledTimes(1);

    rejectConnect(new Error("connect failed")); // first attempt fails (e.g. socket never appeared)
    await flush();
    triggers.starting(); // a later backend respawn (user fixed the path) must retry
    expect(connectOnce).toHaveBeenCalledTimes(2);
  });
});
