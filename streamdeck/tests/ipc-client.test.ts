import { describe, it, expect } from "vitest";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { EventEmitter } from "node:events";
import { IpcClient } from "../src/ipc-client.js";
import { encodeLine } from "../src/protocol.js";

function fakeStream() {
  const s: any = new EventEmitter();
  s.written = [] as string[];
  s.write = (d: string) => { s.written.push(d); return true; };
  s.end = () => {};
  return s;
}

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

  it("a stale connection closing after a reconnect does not disconnect the live client", async () => {
    const streams = [fakeStream(), fakeStream()];
    let i = 0;
    const client = new IpcClient({ connect: () => streams[i++] });
    let closes = 0;
    client.onClose(() => closes++);

    const p1 = client.connectWithRetry("/x", { attempts: 1 });
    streams[0].emit("connect");
    await p1; // connection A attached

    const p2 = client.connectWithRetry("/x", { attempts: 1 });
    streams[1].emit("connect");
    await p2; // connection B attached BEFORE A closed

    streams[0].emit("close"); // the stale A finally closes
    client.sendKeyUp("s0");

    expect(closes).toBe(0); // no false disconnect — B is still live
    expect(streams[1].written.at(-1)).toBe('{"type":"keyUp","instanceId":"s0"}\n'); // sent on B
    expect(streams[0].written).toEqual([]); // nothing went to the stale A
  });
});
