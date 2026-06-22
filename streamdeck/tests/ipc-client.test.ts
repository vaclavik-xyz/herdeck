import { describe, it, expect } from "vitest";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { IpcClient } from "../src/ipc-client.js";
import { encodeLine } from "../src/protocol.js";

function tmpSock(): string {
  return path.join(os.tmpdir(), `herdeck-ipc-test-${process.pid}-${Math.random().toString(16).slice(2)}.sock`);
}

describe("IpcClient against a fake backend socket", () => {
  it("connects, sends hello, and parses ready + render (incl. split frames)", async () => {
    const sockPath = tmpSock();
    const got: any = { ready: false, render: null as any, received: [] as string[] };

    const server = net.createServer((conn) => {
      conn.on("data", (d) => got.received.push(d.toString()));
      // emit ready, then a render split across two writes to exercise buffering
      conn.write(encodeLine({ type: "ready" }));
      const r = encodeLine({ type: "render", keys: { s0: { image: "QUJD", title: null } } });
      conn.write(r.slice(0, 10));
      setTimeout(() => conn.write(r.slice(10)), 5);
    });
    await new Promise<void>((res) => server.listen(sockPath, res));

    const client = new IpcClient();
    client.onReady(() => (got.ready = true));
    client.onRender((keys) => (got.render = keys));
    await client.connectWithRetry(sockPath, { attempts: 10, delayMs: 5 });
    client.sendHello("secret", "MK.2");

    await new Promise((res) => setTimeout(res, 40));
    expect(got.ready).toBe(true);
    expect(got.render).toEqual({ s0: { image: "QUJD", title: null } });
    expect(got.received[0]).toBe('{"type":"hello","protocol_version":1,"token":"secret","device":"MK.2"}\n');

    client.close();
    await new Promise((res) => setTimeout(res, 10));
    expect(got.received.at(-1)).toBe('{"type":"bye"}\n');
    server.close();
  });

  it("connectWithRetry waits for a socket that appears late", async () => {
    const sockPath = tmpSock();
    const client = new IpcClient();
    const connecting = client.connectWithRetry(sockPath, { attempts: 20, delayMs: 5 });

    const server = net.createServer(() => {});
    setTimeout(() => server.listen(sockPath), 30); // bind AFTER the client starts retrying

    await expect(connecting).resolves.toBeUndefined();
    client.close();
    server.close();
  });

  it("reconnects cleanly on the same client after a close (fresh buffer)", async () => {
    const sockPath = tmpSock();
    let conns = 0;
    const server = net.createServer((conn) => {
      conns++;
      conn.write(encodeLine({ type: "ready" }));
    });
    await new Promise<void>((res) => server.listen(sockPath, res));

    const client = new IpcClient();
    let readies = 0;
    client.onReady(() => readies++);

    await client.connectWithRetry(sockPath, { attempts: 10, delayMs: 5 });
    await new Promise((res) => setTimeout(res, 10));
    client.close(); // backend respawn / IPC blip
    await new Promise((res) => setTimeout(res, 10));
    await client.connectWithRetry(sockPath, { attempts: 10, delayMs: 5 }); // reconnect same client
    await new Promise((res) => setTimeout(res, 10));

    expect(conns).toBe(2);
    expect(readies).toBe(2); // each connection parsed ready independently — buffer was reset
    client.close();
    server.close();
  });
});
