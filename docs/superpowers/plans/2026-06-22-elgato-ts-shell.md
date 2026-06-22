# Elgato TS Shell — Implementation Plan (Plan 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the thin TypeScript `@elgato/streamdeck` shell + `.streamDeckPlugin` packaging that drives the already-merged Python "brain" (`src/herdeck/elgato/`) — it spawns/supervises the backend, speaks the backend's exact IPC contract, forwards presses, and renders the PNGs the brain hands back. No logic of record on the TS side.

**Architecture:** A new `streamdeck/` Node project. Pure-TS, SDK-free core modules — `protocol` (wire framing), `ipc-client` (Unix-socket client), `backend-process` (spawn + supervise + bounded backoff), `registry` (local authoritative set of placed keys), `adapter` (render→setImage, snapshot push, key forwarding, lifecycle placeholders) — are each fully unit-testable without hardware or the SDK. Only `src/actions/*` + `src/plugin.ts` import `@elgato/streamdeck`; they are thin glue mapping SDK events onto the core. The brain is the single source of truth; the shell renders images it is handed and forwards presses.

**Tech Stack:** TypeScript (ESM), Node 20, `@elgato/streamdeck` SDK, `vitest` (tests), `rollup` (bundle → `bin/plugin.js`), stdlib `net`/`child_process`/`crypto`. The backend is the merged `herdeck.elgato` Python package; this plan adds **no** Python.

## Global Constraints

These are the binding facts of the **merged** backend (`src/herdeck/elgato/ipc.py`, `protocol.py`). The TS wire format must match them byte-for-byte; copy values verbatim.

- **Transport:** newline-delimited **compact** JSON over a Unix domain socket. Python uses `json.dumps(separators=(",",":"))` + `"\n"`; JS `JSON.stringify` already emits no spaces, so `JSON.stringify(obj) + "\n"` matches.
- **`PROTOCOL_VERSION = 1`.**
- **TS → brain messages** (exact `type` strings + field names):
  - `{"type":"hello","protocol_version":1,"token":<str>,"device":<str?>,"size":<obj?>}` — backend checks **only** `protocol_version` (snake_case) and `token`; `device`/`size` are accepted and ignored.
  - `{"type":"slots","slots":[{"instanceId":<str>,"coord":{"col":<int>,"row":<int>}}]}` — the **full** current list (backend `set_slots` replaces).
  - `{"type":"action_keys","action_keys":[{"instanceId":<str>,"type":<"approve"|"deny"|"stop"|"pager">,"coord":{"col":<int>,"row":<int>}}]}` — the **full** current list.
  - `{"type":"keyDown","instanceId":<str>}` — backend no-ops it (forward-compat).
  - `{"type":"keyUp","instanceId":<str>}` — triggers the action.
  - `{"type":"bye"}` — **connection-level graceful close**; the backend ignores any `instanceId` and closes the socket. Send it **only** on shell teardown, **never** per-key.
- **brain → TS messages:**
  - `{"type":"ready"}` — sent after a valid `hello`.
  - `{"type":"render","keys":{<instanceId>:{"image":<base64-png-no-prefix>,"title":<str|null>}}}` — only changed instances (only-on-change diff). `image` is **raw** base64 (no `data:` prefix); wrap as `data:image/png;base64,<image>` before `setImage`.
  - `{"type":"error","reason":<str>}` — sent on bad `hello`, then the connection closes.
- **Field-name mapping:** `instanceId` (camelCase), `coord.col`/`coord.row`, `protocol_version`/`action_keys` (snake_case). The SDK supplies `coordinates.column`/`coordinates.row` → emit `coord.col = column`, `coord.row = row`.
- **Key removal** is communicated by re-sending the full `slots`/`action_keys` snapshot **without** the key — there is no incremental remove and `bye` is not per-key.
- **Process/discovery contract:** the **shell** picks an unused socket path and generates a one-shot token, passes them to the spawned backend via env `HERDECK_ELGATO_SOCK` / `HERDECK_ELGATO_TOKEN` and `HERDECK_DECK=elgato-plugin`; the **backend** creates and binds that socket. The shell must **not** pre-create the socket file; it retries connecting until the backend binds it (cold start).
- **Single client:** a new authenticated `hello` supersedes the previous connection; on IPC reconnect (backend alive) the brain re-pushes a full render.
- **Plugin identity:** plugin UUID `xyz.vaclavik.herdeck`; action UUIDs `xyz.vaclavik.herdeck.{slot,approve,deny,stop,pager}`; Stream Deck action **Category** `"herdr"`; Keypad-only (`Controllers: ["Keypad"]`).
- **SDK pinning:** add `@elgato/streamdeck` at a pinned major (`^1.0.0`) in `streamdeck/package.json`; the implementer verifies the action/event signatures in Task 7 against the installed version and adjusts the thin glue only (the SDK-free core never changes).
- **No hardware / no E2E:** all tests run without a deck — pure-TS modules use fakes (in-memory sockets, injected `spawn`/timers); SDK glue mocks the SDK event objects.

All paths below are relative to the repo root `/Users/admin/projects/herdeck`. All commands run from `streamdeck/` unless noted.

---

### Task 1: Scaffold `streamdeck/` + protocol module (wire framing)

**Files:**
- Create: `streamdeck/package.json`
- Create: `streamdeck/tsconfig.json`
- Create: `streamdeck/vitest.config.ts`
- Create: `streamdeck/.gitignore`
- Create: `streamdeck/src/protocol.ts`
- Test: `streamdeck/tests/protocol.test.ts`

**Interfaces:**
- Produces:
  - `PROTOCOL_VERSION = 1`
  - Types: `Coord = { col: number; row: number }`; `SlotEntry = { instanceId: string; coord: Coord }`; `ActionKind = "approve" | "deny" | "stop" | "pager"`; `ActionKeyEntry = { instanceId: string; type: ActionKind; coord: Coord }`; `RenderKeys = Record<string, { image: string; title: string | null }>`.
  - `encodeLine(obj: unknown): string` — compact JSON + `"\n"`.
  - `decodeLine(line: string): any` — `JSON.parse` (throws on bad JSON).
  - `splitFrames(buffer: string): { frames: string[]; rest: string }` — split a stream buffer on `"\n"`, returning complete frames and the trailing partial.
  - Builders returning plain objects: `helloMsg(token: string, device?: string, size?: object)`, `slotsMsg(slots: SlotEntry[])`, `actionKeysMsg(keys: ActionKeyEntry[])`, `keyDownMsg(instanceId: string)`, `keyUpMsg(instanceId: string)`, `byeMsg()`.

- [ ] **Step 1: Write the failing test**

Create `streamdeck/package.json`:

```json
{
  "name": "herdeck-streamdeck",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "scripts": {
    "test": "vitest run",
    "build": "rollup -c"
  },
  "dependencies": {
    "@elgato/streamdeck": "^1.0.0"
  },
  "devDependencies": {
    "@rollup/plugin-commonjs": "^28.0.0",
    "@rollup/plugin-node-resolve": "^15.3.0",
    "@rollup/plugin-typescript": "^12.1.0",
    "rollup": "^4.0.0",
    "tslib": "^2.7.0",
    "typescript": "^5.6.0",
    "vitest": "^2.1.0"
  }
}
```

Create `streamdeck/tsconfig.json`:

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "experimentalDecorators": true,
    "useDefineForClassFields": false,
    "outDir": "bin",
    "rootDir": "src"
  },
  "include": ["src"]
}
```

Create `streamdeck/vitest.config.ts`:

```typescript
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: { include: ["tests/**/*.test.ts"], environment: "node" },
});
```

Create `streamdeck/.gitignore`:

```
node_modules/
bin/
*.streamDeckPlugin
```

Create `streamdeck/tests/protocol.test.ts`:

```typescript
import { describe, it, expect } from "vitest";
import {
  PROTOCOL_VERSION,
  encodeLine,
  decodeLine,
  splitFrames,
  helloMsg,
  slotsMsg,
  actionKeysMsg,
  keyDownMsg,
  keyUpMsg,
  byeMsg,
} from "../src/protocol.js";

describe("protocol wire format matches the Python backend", () => {
  it("protocol version is 1", () => {
    expect(PROTOCOL_VERSION).toBe(1);
  });

  it("encodeLine is compact JSON + newline (matches json.dumps separators)", () => {
    expect(encodeLine({ type: "keyUp", instanceId: "s0" })).toBe('{"type":"keyUp","instanceId":"s0"}\n');
  });

  it("hello carries snake_case protocol_version + token", () => {
    expect(helloMsg("secret", "MK.2", { columns: 5, rows: 3 })).toEqual({
      type: "hello",
      protocol_version: 1,
      token: "secret",
      device: "MK.2",
      size: { columns: 5, rows: 3 },
    });
  });

  it("slots/action_keys use instanceId + coord{col,row} + snake_case action_keys", () => {
    expect(slotsMsg([{ instanceId: "s0", coord: { col: 1, row: 0 } }])).toEqual({
      type: "slots",
      slots: [{ instanceId: "s0", coord: { col: 1, row: 0 } }],
    });
    expect(actionKeysMsg([{ instanceId: "a", type: "approve", coord: { col: 0, row: 2 } }])).toEqual({
      type: "action_keys",
      action_keys: [{ instanceId: "a", type: "approve", coord: { col: 0, row: 2 } }],
    });
  });

  it("keyDown/keyUp/bye shapes", () => {
    expect(keyDownMsg("s0")).toEqual({ type: "keyDown", instanceId: "s0" });
    expect(keyUpMsg("s0")).toEqual({ type: "keyUp", instanceId: "s0" });
    expect(byeMsg()).toEqual({ type: "bye" });
  });

  it("splitFrames yields complete frames and keeps the trailing partial", () => {
    const { frames, rest } = splitFrames('{"type":"ready"}\n{"type":"render","ke');
    expect(frames).toEqual(['{"type":"ready"}']);
    expect(rest).toBe('{"type":"render","ke');
    expect(decodeLine(frames[0])).toEqual({ type: "ready" });
  });
});
```

- [ ] **Step 2: Install deps and run the test to verify it fails**

Run:
```bash
cd streamdeck && npm install && npx vitest run tests/protocol.test.ts
```
Expected: FAIL — `Cannot find module '../src/protocol.js'`.

- [ ] **Step 3: Write minimal implementation**

Create `streamdeck/src/protocol.ts`:

```typescript
export const PROTOCOL_VERSION = 1;

export type Coord = { col: number; row: number };
export type SlotEntry = { instanceId: string; coord: Coord };
export type ActionKind = "approve" | "deny" | "stop" | "pager";
export type ActionKeyEntry = { instanceId: string; type: ActionKind; coord: Coord };
export type RenderKeys = Record<string, { image: string; title: string | null }>;

export function encodeLine(obj: unknown): string {
  return JSON.stringify(obj) + "\n";
}

export function decodeLine(line: string): any {
  return JSON.parse(line);
}

export function splitFrames(buffer: string): { frames: string[]; rest: string } {
  const parts = buffer.split("\n");
  const rest = parts.pop() ?? "";
  return { frames: parts.filter((p) => p.length > 0), rest };
}

export function helloMsg(token: string, device?: string, size?: object) {
  return { type: "hello", protocol_version: PROTOCOL_VERSION, token, device, size };
}

export function slotsMsg(slots: SlotEntry[]) {
  return { type: "slots", slots };
}

export function actionKeysMsg(keys: ActionKeyEntry[]) {
  return { type: "action_keys", action_keys: keys };
}

export function keyDownMsg(instanceId: string) {
  return { type: "keyDown", instanceId };
}

export function keyUpMsg(instanceId: string) {
  return { type: "keyUp", instanceId };
}

export function byeMsg() {
  return { type: "bye" };
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd streamdeck && npx vitest run tests/protocol.test.ts`
Expected: PASS (all assertions).

- [ ] **Step 5: Commit**

```bash
git add streamdeck/package.json streamdeck/tsconfig.json streamdeck/vitest.config.ts \
  streamdeck/.gitignore streamdeck/src/protocol.ts streamdeck/tests/protocol.test.ts
git commit -m "feat(ts-shell): scaffold streamdeck project and IPC wire protocol"
sha=$(git rev-parse HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

If Roborev reports findings, fix them before starting the next task.

---

### Task 2: IPC client (socket connect + framing + events)

**Files:**
- Create: `streamdeck/src/ipc-client.ts`
- Test: `streamdeck/tests/ipc-client.test.ts`

**Interfaces:**
- Consumes: `protocol` (`encodeLine`, `splitFrames`, `decodeLine`, builders, `RenderKeys`, `SlotEntry`, `ActionKeyEntry`).
- Produces: `class IpcClient` constructed with `new IpcClient({ connect?: (path: string) => Duplex })` (default uses `net.connect`). A `Duplex` is any stream with `write(data)`, `on("data"|"close"|"error")`, `end()`. Methods:
  - `connectWithRetry(path: string, opts?: { attempts?: number; delayMs?: number; setTimer?: (cb, ms) => unknown }): Promise<void>` — retries `connect(path)` until the stream opens or attempts exhaust (cold start; the backend binds the socket lazily).
  - `sendHello(token: string, device?: string, size?: object): void`, `sendSlots(slots: SlotEntry[]): void`, `sendActionKeys(keys: ActionKeyEntry[]): void`, `sendKeyDown(id: string): void`, `sendKeyUp(id: string): void`.
  - `close(): void` — sends `bye` then ends the stream.
  - Event registration: `onReady(cb: () => void)`, `onRender(cb: (keys: RenderKeys) => void)`, `onError(cb: (reason: string) => void)`, `onClose(cb: () => void)`.

- [ ] **Step 1: Write the failing test**

Create `streamdeck/tests/ipc-client.test.ts`:

```typescript
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd streamdeck && npx vitest run tests/ipc-client.test.ts`
Expected: FAIL — `Cannot find module '../src/ipc-client.js'`.

- [ ] **Step 3: Write minimal implementation**

Create `streamdeck/src/ipc-client.ts`:

```typescript
import net from "node:net";
import type { Duplex } from "node:stream";
import {
  encodeLine,
  decodeLine,
  splitFrames,
  helloMsg,
  slotsMsg,
  actionKeysMsg,
  keyDownMsg,
  keyUpMsg,
  byeMsg,
  type RenderKeys,
  type SlotEntry,
  type ActionKeyEntry,
} from "./protocol.js";

type ConnectFn = (path: string) => Duplex;

const defaultConnect: ConnectFn = (path) => net.connect({ path });

export class IpcClient {
  private readonly connectFn: ConnectFn;
  private stream: Duplex | null = null;
  private buffer = "";
  private readyCbs: Array<() => void> = [];
  private renderCbs: Array<(keys: RenderKeys) => void> = [];
  private errorCbs: Array<(reason: string) => void> = [];
  private closeCbs: Array<() => void> = [];

  constructor(opts: { connect?: ConnectFn } = {}) {
    this.connectFn = opts.connect ?? defaultConnect;
  }

  onReady(cb: () => void) { this.readyCbs.push(cb); }
  onRender(cb: (keys: RenderKeys) => void) { this.renderCbs.push(cb); }
  onError(cb: (reason: string) => void) { this.errorCbs.push(cb); }
  onClose(cb: () => void) { this.closeCbs.push(cb); }

  async connectWithRetry(
    path: string,
    opts: { attempts?: number; delayMs?: number; setTimer?: (cb: () => void, ms: number) => unknown } = {},
  ): Promise<void> {
    const attempts = opts.attempts ?? 30;
    const delayMs = opts.delayMs ?? 100;
    const setTimer = opts.setTimer ?? ((cb, ms) => setTimeout(cb, ms));
    for (let i = 0; i < attempts; i++) {
      try {
        await this.tryConnect(path);
        return;
      } catch {
        if (i === attempts - 1) throw new Error(`IPC connect failed after ${attempts} attempts: ${path}`);
        await new Promise<void>((res) => setTimer(res, delayMs));
      }
    }
  }

  private tryConnect(path: string): Promise<void> {
    return new Promise<void>((resolve, reject) => {
      const stream = this.connectFn(path);
      const onErr = (err: Error) => reject(err);
      stream.once("error", onErr);
      stream.once("connect", () => {
        stream.removeListener("error", onErr);
        this.attach(stream);
        resolve();
      });
    });
  }

  private attach(stream: Duplex) {
    this.stream = stream;
    this.buffer = ""; // fresh buffer per connection so a reconnect never inherits a partial frame
    stream.on("data", (chunk: Buffer) => this.onData(chunk));
    stream.on("error", () => {});
    stream.on("close", () => {
      this.stream = null;
      this.closeCbs.forEach((cb) => cb());
    });
  }

  private onData(chunk: Buffer) {
    this.buffer += chunk.toString();
    const { frames, rest } = splitFrames(this.buffer);
    this.buffer = rest;
    for (const frame of frames) {
      let msg: any;
      try { msg = decodeLine(frame); } catch { continue; }
      if (msg.type === "ready") this.readyCbs.forEach((cb) => cb());
      else if (msg.type === "render") this.renderCbs.forEach((cb) => cb(msg.keys as RenderKeys));
      else if (msg.type === "error") this.errorCbs.forEach((cb) => cb(String(msg.reason ?? "")));
    }
  }

  private write(obj: unknown) {
    this.stream?.write(encodeLine(obj));
  }

  sendHello(token: string, device?: string, size?: object) { this.write(helloMsg(token, device, size)); }
  sendSlots(slots: SlotEntry[]) { this.write(slotsMsg(slots)); }
  sendActionKeys(keys: ActionKeyEntry[]) { this.write(actionKeysMsg(keys)); }
  sendKeyDown(id: string) { this.write(keyDownMsg(id)); }
  sendKeyUp(id: string) { this.write(keyUpMsg(id)); }

  close() {
    if (this.stream) {
      this.write(byeMsg());
      this.stream.end();
    }
  }
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd streamdeck && npx vitest run tests/ipc-client.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add streamdeck/src/ipc-client.ts streamdeck/tests/ipc-client.test.ts
git commit -m "feat(ts-shell): IPC client with framing, retry-connect and events"
sha=$(git rev-parse HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

---

### Task 3: Backend process supervisor (spawn + bounded backoff + discovery)

**Files:**
- Create: `streamdeck/src/backend-process.ts`
- Test: `streamdeck/tests/backend-process.test.ts`

**Interfaces:**
- Produces: `class BackendProcess` constructed with `new BackendProcess(opts)` where `opts`:
  - `resolveCommand(): { command: string; args: string[] }` — how to launch herdeck (explicit PI path → `herdeck` on PATH → known venv). Injected so tests are deterministic.
  - `devSocket?: string` — if set, **connect** to this socket instead of spawning (dev mode); `socketPath` returns it and `start()` is a no-op.
  - `spawn?` (default `child_process.spawn`), `randomToken?` (default 16 random bytes hex), `tmpDir?` (default `os.tmpdir()`), `setTimer?` (default `setTimeout`), `maxBackoffMs?` (default `30000`), `baseBackoffMs?` (default `500`).
  - State: `get socketPath(): string`, `get token(): string`, `get spawned(): boolean` (false in dev mode).
  - `start(): void` — spawns the backend (unless dev mode) with env `HERDECK_ELGATO_SOCK`, `HERDECK_ELGATO_TOKEN`, `HERDECK_DECK=elgato-plugin`; on child exit, respawns with bounded exponential backoff.
  - `stop(): void` — stop supervising and kill the child.
  - `onState(cb: (state: "starting" | "down") => void)` — `starting` on each (re)spawn, `down` while waiting to respawn after an exit.

- [ ] **Step 1: Write the failing test**

Create `streamdeck/tests/backend-process.test.ts`:

```typescript
import { describe, it, expect, vi } from "vitest";
import { EventEmitter } from "node:events";
import { BackendProcess } from "../src/backend-process.js";

function fakeChild() {
  const child: any = new EventEmitter();
  child.kill = vi.fn();
  return child;
}

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

  it("dev mode connects to a known socket instead of spawning", () => {
    const spawn = vi.fn();
    const bp = new BackendProcess({
      resolveCommand: () => ({ command: "herdeck", args: [] }),
      devSocket: "/tmp/dev.sock",
      spawn: spawn as any,
    });
    bp.start();
    expect(spawn).not.toHaveBeenCalled();
    expect(bp.spawned).toBe(false);
    expect(bp.socketPath).toBe("/tmp/dev.sock");
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd streamdeck && npx vitest run tests/backend-process.test.ts`
Expected: FAIL — `Cannot find module '../src/backend-process.js'`.

- [ ] **Step 3: Write minimal implementation**

Create `streamdeck/src/backend-process.ts`:

```typescript
import { spawn as nodeSpawn } from "node:child_process";
import crypto from "node:crypto";
import os from "node:os";
import path from "node:path";

type SpawnFn = (command: string, args: string[], options: any) => { kill: (sig?: string) => void; on: (ev: string, cb: (...a: any[]) => void) => void };

export interface BackendOptions {
  resolveCommand: () => { command: string; args: string[] };
  devSocket?: string;
  spawn?: SpawnFn;
  randomToken?: () => string;
  tmpDir?: string;
  setTimer?: (cb: () => void, ms: number) => unknown;
  baseBackoffMs?: number;
  maxBackoffMs?: number;
}

export class BackendProcess {
  private readonly opts: Required<Omit<BackendOptions, "devSocket">> & { devSocket?: string };
  private child: { kill: (sig?: string) => void; on: (ev: string, cb: (...a: any[]) => void) => void } | null = null;
  private stopped = false;
  private backoff: number;
  private stateCbs: Array<(s: "starting" | "down") => void> = [];
  readonly socketPath: string;
  readonly token: string;

  constructor(options: BackendOptions) {
    this.opts = {
      resolveCommand: options.resolveCommand,
      devSocket: options.devSocket,
      spawn: options.spawn ?? (nodeSpawn as unknown as SpawnFn),
      randomToken: options.randomToken ?? (() => crypto.randomBytes(16).toString("hex")),
      tmpDir: options.tmpDir ?? os.tmpdir(),
      setTimer: options.setTimer ?? ((cb, ms) => setTimeout(cb, ms)),
      baseBackoffMs: options.baseBackoffMs ?? 500,
      maxBackoffMs: options.maxBackoffMs ?? 30000,
    };
    this.backoff = this.opts.baseBackoffMs;
    this.token = this.opts.randomToken();
    this.socketPath = this.opts.devSocket
      ?? path.join(this.opts.tmpDir, `herdeck-elgato-${process.pid}-${this.token.slice(0, 8)}.sock`);
  }

  get spawned(): boolean { return !this.opts.devSocket; }

  onState(cb: (s: "starting" | "down") => void) { this.stateCbs.push(cb); }
  private emitState(s: "starting" | "down") { this.stateCbs.forEach((cb) => cb(s)); }

  start(): void {
    if (this.opts.devSocket) return; // dev mode: an external backend already owns the socket
    this.spawnOnce();
  }

  private spawnOnce(): void {
    if (this.stopped) return;
    const { command, args } = this.opts.resolveCommand();
    this.emitState("starting");
    const child = this.opts.spawn(command, args, {
      env: {
        ...process.env,
        HERDECK_ELGATO_SOCK: this.socketPath,
        HERDECK_ELGATO_TOKEN: this.token,
        HERDECK_DECK: "elgato-plugin",
      },
      stdio: "inherit",
    });
    this.child = child;
    child.on("exit", () => {
      this.child = null;
      if (this.stopped) return;
      this.emitState("down");
      const delay = this.backoff;
      this.backoff = Math.min(this.backoff * 2, this.opts.maxBackoffMs);
      this.opts.setTimer(() => this.spawnOnce(), delay);
    });
  }

  stop(): void {
    this.stopped = true;
    this.child?.kill();
    this.child = null;
  }
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd streamdeck && npx vitest run tests/backend-process.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add streamdeck/src/backend-process.ts streamdeck/tests/backend-process.test.ts
git commit -m "feat(ts-shell): supervise the Python backend with bounded backoff"
sha=$(git rev-parse HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

---

### Task 4: Key registry (local authoritative set → full snapshots)

**Files:**
- Create: `streamdeck/src/registry.ts`
- Test: `streamdeck/tests/registry.test.ts`

**Interfaces:**
- Consumes: `protocol` types (`Coord`, `SlotEntry`, `ActionKeyEntry`, `ActionKind`).
- Produces: `class KeyRegistry`:
  - `addSlot(instanceId: string, coord: Coord): void`, `removeSlot(instanceId: string): void`
  - `addActionKey(instanceId: string, type: ActionKind, coord: Coord): void`, `removeActionKey(instanceId: string): void`
  - `slotsSnapshot(): SlotEntry[]` — the full current slot list (insertion order)
  - `actionKeysSnapshot(): ActionKeyEntry[]` — the full current action-key list
  - `onChange(cb: () => void): void` — fired after every add/remove so the adapter re-sends the **full** snapshot (the backend replaces, and `bye` is connection-level, never per-key).

- [ ] **Step 1: Write the failing test**

Create `streamdeck/tests/registry.test.ts`:

```typescript
import { describe, it, expect, vi } from "vitest";
import { KeyRegistry } from "../src/registry.js";

describe("KeyRegistry", () => {
  it("tracks slots + action keys and yields full snapshots", () => {
    const r = new KeyRegistry();
    r.addSlot("s0", { col: 0, row: 0 });
    r.addSlot("s1", { col: 1, row: 0 });
    r.addActionKey("a", "approve", { col: 0, row: 2 });
    expect(r.slotsSnapshot()).toEqual([
      { instanceId: "s0", coord: { col: 0, row: 0 } },
      { instanceId: "s1", coord: { col: 1, row: 0 } },
    ]);
    expect(r.actionKeysSnapshot()).toEqual([{ instanceId: "a", type: "approve", coord: { col: 0, row: 2 } }]);
  });

  it("removal drops the key from the next snapshot (no per-key bye)", () => {
    const r = new KeyRegistry();
    r.addSlot("s0", { col: 0, row: 0 });
    r.addSlot("s1", { col: 1, row: 0 });
    r.removeSlot("s0");
    expect(r.slotsSnapshot()).toEqual([{ instanceId: "s1", coord: { col: 1, row: 0 } }]);
  });

  it("fires onChange after each mutation", () => {
    const r = new KeyRegistry();
    const cb = vi.fn();
    r.onChange(cb);
    r.addSlot("s0", { col: 0, row: 0 });
    r.addActionKey("a", "stop", { col: 2, row: 2 });
    r.removeSlot("s0");
    expect(cb).toHaveBeenCalledTimes(3);
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd streamdeck && npx vitest run tests/registry.test.ts`
Expected: FAIL — `Cannot find module '../src/registry.js'`.

- [ ] **Step 3: Write minimal implementation**

Create `streamdeck/src/registry.ts`:

```typescript
import type { Coord, SlotEntry, ActionKeyEntry, ActionKind } from "./protocol.js";

export class KeyRegistry {
  private slots = new Map<string, Coord>();
  private actions = new Map<string, { type: ActionKind; coord: Coord }>();
  private changeCbs: Array<() => void> = [];

  onChange(cb: () => void) { this.changeCbs.push(cb); }
  private changed() { this.changeCbs.forEach((cb) => cb()); }

  addSlot(instanceId: string, coord: Coord) { this.slots.set(instanceId, coord); this.changed(); }
  removeSlot(instanceId: string) { this.slots.delete(instanceId); this.changed(); }
  addActionKey(instanceId: string, type: ActionKind, coord: Coord) { this.actions.set(instanceId, { type, coord }); this.changed(); }
  removeActionKey(instanceId: string) { this.actions.delete(instanceId); this.changed(); }

  slotsSnapshot(): SlotEntry[] {
    return [...this.slots.entries()].map(([instanceId, coord]) => ({ instanceId, coord }));
  }

  actionKeysSnapshot(): ActionKeyEntry[] {
    return [...this.actions.entries()].map(([instanceId, { type, coord }]) => ({ instanceId, type, coord }));
  }
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd streamdeck && npx vitest run tests/registry.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add streamdeck/src/registry.ts streamdeck/tests/registry.test.ts
git commit -m "feat(ts-shell): local key registry producing full snapshots"
sha=$(git rev-parse HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

---

### Task 5: Adapter — render→setImage, snapshot push, key forwarding

**Files:**
- Create: `streamdeck/src/adapter.ts`
- Test: `streamdeck/tests/adapter.test.ts`

**Interfaces:**
- Consumes: `IpcClient` (Task 2: `onReady`/`onRender`/`sendSlots`/`sendActionKeys`/`sendKeyDown`/`sendKeyUp`), `KeyRegistry` (Task 4: `onChange`/`slotsSnapshot`/`actionKeysSnapshot`).
- Produces:
  - `interface Surface { setImage(image: string): void; setTitle(title: string): void }` — the per-key visual handle (an SDK action implements it in Task 7).
  - `class Adapter` constructed with `new Adapter(ipc: IpcClient, registry: KeyRegistry)`.
    - `registerSurface(instanceId: string, surface: Surface): void` / `unregisterSurface(instanceId: string): void`
    - `handleKeyDown(instanceId: string): void` → `ipc.sendKeyDown(id)`
    - `handleKeyUp(instanceId: string): void` → `ipc.sendKeyUp(id)`
    - on `ipc.onReady`: mark ready and push the current snapshots; on `registry.onChange`: push snapshots **iff** ready (else they go out on the next ready)
    - on `ipc.onRender(keys)`: for each `instanceId`, look up its surface and `setImage("data:image/png;base64," + image)` and cache the last authoritative image (consumed in Task 6)
  - `lastImageFor(instanceId: string): string | undefined` — exposed for Task 6.

- [ ] **Step 1: Write the failing test**

Create `streamdeck/tests/adapter.test.ts`:

```typescript
import { describe, it, expect, vi } from "vitest";
import { Adapter, type Surface } from "../src/adapter.js";
import { KeyRegistry } from "../src/registry.js";

function fakeIpc() {
  const cbs: any = {};
  return {
    onReady: (cb: any) => (cbs.ready = cb),
    onRender: (cb: any) => (cbs.render = cb),
    onError: vi.fn(),
    onClose: vi.fn(),
    sendSlots: vi.fn(),
    sendActionKeys: vi.fn(),
    sendKeyDown: vi.fn(),
    sendKeyUp: vi.fn(),
    fire: cbs,
  };
}

function fakeSurface(): Surface & { img: string | null; title: string | null } {
  return { img: null, title: null, setImage(i) { this.img = i; }, setTitle(t) { this.title = t; } };
}

describe("Adapter", () => {
  it("pushes the full snapshot to the brain on ready", () => {
    const ipc = fakeIpc();
    const reg = new KeyRegistry();
    reg.addSlot("s0", { col: 0, row: 0 });
    reg.addActionKey("a", "approve", { col: 0, row: 2 });
    new Adapter(ipc as any, reg);
    ipc.fire.ready();
    expect(ipc.sendSlots).toHaveBeenCalledWith([{ instanceId: "s0", coord: { col: 0, row: 0 } }]);
    expect(ipc.sendActionKeys).toHaveBeenCalledWith([{ instanceId: "a", type: "approve", coord: { col: 0, row: 2 } }]);
  });

  it("re-pushes snapshots when the registry changes after ready", () => {
    const ipc = fakeIpc();
    const reg = new KeyRegistry();
    new Adapter(ipc as any, reg);
    ipc.fire.ready();
    ipc.sendSlots.mockClear();
    reg.addSlot("s9", { col: 4, row: 0 });
    expect(ipc.sendSlots).toHaveBeenCalledWith([{ instanceId: "s9", coord: { col: 4, row: 0 } }]);
  });

  it("does not push snapshots before ready", () => {
    const ipc = fakeIpc();
    const reg = new KeyRegistry();
    new Adapter(ipc as any, reg);
    reg.addSlot("s0", { col: 0, row: 0 }); // before ready
    expect(ipc.sendSlots).not.toHaveBeenCalled();
  });

  it("renders base64 images as data URLs onto the matching surface and caches them", () => {
    const ipc = fakeIpc();
    const reg = new KeyRegistry();
    const adapter = new Adapter(ipc as any, reg);
    const surf = fakeSurface();
    adapter.registerSurface("s0", surf);
    ipc.fire.render({ s0: { image: "QUJD", title: null }, unknown: { image: "ZZ", title: null } });
    expect(surf.img).toBe("data:image/png;base64,QUJD");
    expect(adapter.lastImageFor("s0")).toBe("data:image/png;base64,QUJD");
  });

  it("forwards key presses to the brain", () => {
    const ipc = fakeIpc();
    const adapter = new Adapter(ipc as any, new KeyRegistry());
    adapter.handleKeyDown("s0");
    adapter.handleKeyUp("s0");
    expect(ipc.sendKeyDown).toHaveBeenCalledWith("s0");
    expect(ipc.sendKeyUp).toHaveBeenCalledWith("s0");
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd streamdeck && npx vitest run tests/adapter.test.ts`
Expected: FAIL — `Cannot find module '../src/adapter.js'`.

- [ ] **Step 3: Write minimal implementation**

Create `streamdeck/src/adapter.ts`:

```typescript
import type { IpcClient } from "./ipc-client.js";
import type { KeyRegistry } from "./registry.js";
import type { RenderKeys } from "./protocol.js";

export interface Surface {
  setImage(image: string): void;
  setTitle(title: string): void;
}

export class Adapter {
  private ready = false;
  private surfaces = new Map<string, Surface>();
  private lastImage = new Map<string, string>();

  constructor(private readonly ipc: IpcClient, private readonly registry: KeyRegistry) {
    this.ipc.onReady(() => {
      this.ready = true;
      this.pushSnapshots();
    });
    this.ipc.onRender((keys) => this.applyRender(keys));
    this.registry.onChange(() => {
      if (this.ready) this.pushSnapshots();
    });
  }

  registerSurface(instanceId: string, surface: Surface) { this.surfaces.set(instanceId, surface); }
  unregisterSurface(instanceId: string) { this.surfaces.delete(instanceId); this.lastImage.delete(instanceId); }

  handleKeyDown(instanceId: string) { this.ipc.sendKeyDown(instanceId); }
  handleKeyUp(instanceId: string) { this.ipc.sendKeyUp(instanceId); }

  lastImageFor(instanceId: string): string | undefined { return this.lastImage.get(instanceId); }

  private pushSnapshots() {
    this.ipc.sendSlots(this.registry.slotsSnapshot());
    this.ipc.sendActionKeys(this.registry.actionKeysSnapshot());
  }

  private applyRender(keys: RenderKeys) {
    for (const [instanceId, { image }] of Object.entries(keys)) {
      const dataUrl = `data:image/png;base64,${image}`;
      this.lastImage.set(instanceId, dataUrl);
      this.surfaces.get(instanceId)?.setImage(dataUrl);
    }
  }
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd streamdeck && npx vitest run tests/adapter.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add streamdeck/src/adapter.ts streamdeck/tests/adapter.test.ts
git commit -m "feat(ts-shell): adapter wiring renders, snapshots and presses"
sha=$(git rev-parse HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

---

### Task 6: Lifecycle placeholder states (starting / backend-down)

**Files:**
- Modify: `streamdeck/src/adapter.ts`
- Test: `streamdeck/tests/adapter-lifecycle.test.ts`

**Interfaces:**
- Consumes: `BackendProcess.onState` ("starting" | "down") (Task 3), `Adapter` surfaces (Task 5).
- Produces (added to `Adapter`):
  - `setBackendState(state: "starting" | "down" | "ready"): void` — before the brain is up the shell must render its own placeholder, because no `render` will arrive. `starting` → every registered surface `setTitle("starting…")`; `down` → `setTitle("backend down")`; `ready` → clear the title (`setTitle("")`) so authoritative renders own the key. A surface registered **while** in a non-ready state is immediately given the current placeholder title.
  - The constructor wires `ipc.onReady` to also call `setBackendState("ready")`, and `ipc.onClose` to call `setBackendState("down")`.

- [ ] **Step 1: Write the failing test**

Create `streamdeck/tests/adapter-lifecycle.test.ts`:

```typescript
import { describe, it, expect, vi } from "vitest";
import { Adapter, type Surface } from "../src/adapter.js";
import { KeyRegistry } from "../src/registry.js";

function fakeIpc() {
  const cbs: any = {};
  return {
    onReady: (cb: any) => (cbs.ready = cb),
    onRender: (cb: any) => (cbs.render = cb),
    onError: (cb: any) => (cbs.error = cb),
    onClose: (cb: any) => (cbs.close = cb),
    sendSlots: vi.fn(), sendActionKeys: vi.fn(), sendKeyDown: vi.fn(), sendKeyUp: vi.fn(),
    fire: cbs,
  };
}
function fakeSurface(): Surface & { title: string | null } {
  return { title: null, setImage() {}, setTitle(t) { this.title = t; } };
}

describe("Adapter lifecycle placeholders", () => {
  it("shows starting / backend down titles and clears on ready", () => {
    const ipc = fakeIpc();
    const adapter = new Adapter(ipc as any, new KeyRegistry());
    const surf = fakeSurface();
    adapter.registerSurface("s0", surf);

    adapter.setBackendState("starting");
    expect(surf.title).toBe("starting…");
    adapter.setBackendState("down");
    expect(surf.title).toBe("backend down");
    adapter.setBackendState("ready");
    expect(surf.title).toBe("");
  });

  it("a surface registered during 'starting' immediately gets the placeholder", () => {
    const ipc = fakeIpc();
    const adapter = new Adapter(ipc as any, new KeyRegistry());
    adapter.setBackendState("starting");
    const surf = fakeSurface();
    adapter.registerSurface("s1", surf);
    expect(surf.title).toBe("starting…");
  });

  it("an IPC close reverts to backend-down", () => {
    const ipc = fakeIpc();
    const adapter = new Adapter(ipc as any, new KeyRegistry());
    const surf = fakeSurface();
    adapter.registerSurface("s0", surf);
    ipc.fire.ready();           // -> ready, title cleared
    ipc.fire.close();           // -> down
    expect(surf.title).toBe("backend down");
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd streamdeck && npx vitest run tests/adapter-lifecycle.test.ts`
Expected: FAIL — `adapter.setBackendState is not a function`.

- [ ] **Step 3: Write minimal implementation**

In `streamdeck/src/adapter.ts`, add a `backendState` field and the placeholder logic. Replace the class body's top + constructor + `registerSurface` with:

```typescript
export class Adapter {
  private ready = false;
  private backendState: "starting" | "down" | "ready" = "starting";
  private surfaces = new Map<string, Surface>();
  private lastImage = new Map<string, string>();

  constructor(private readonly ipc: IpcClient, private readonly registry: KeyRegistry) {
    this.ipc.onReady(() => {
      this.ready = true;
      this.setBackendState("ready");
      this.pushSnapshots();
    });
    this.ipc.onClose(() => {
      this.ready = false;
      this.setBackendState("down");
    });
    this.ipc.onRender((keys) => this.applyRender(keys));
    this.registry.onChange(() => {
      if (this.ready) this.pushSnapshots();
    });
  }

  registerSurface(instanceId: string, surface: Surface) {
    this.surfaces.set(instanceId, surface);
    const title = this.placeholderTitle();
    if (title !== null) surface.setTitle(title);
  }

  setBackendState(state: "starting" | "down" | "ready") {
    this.backendState = state;
    const title = state === "ready" ? "" : this.placeholderTitle();
    if (title !== null) this.surfaces.forEach((s) => s.setTitle(title));
  }

  private placeholderTitle(): string | null {
    if (this.backendState === "starting") return "starting…";
    if (this.backendState === "down") return "backend down";
    return null; // ready: authoritative renders own the key
  }
```

Leave the rest of the class (`unregisterSurface`, `handleKeyDown/Up`, `lastImageFor`, `pushSnapshots`, `applyRender`) unchanged. (The closing `}` of the class stays.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd streamdeck && npx vitest run tests/adapter.test.ts tests/adapter-lifecycle.test.ts`
Expected: PASS (both files — the Task 5 tests still pass).

- [ ] **Step 5: Commit**

```bash
git add streamdeck/src/adapter.ts streamdeck/tests/adapter-lifecycle.test.ts
git commit -m "feat(ts-shell): render starting/backend-down placeholder states"
sha=$(git rev-parse HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

---

### Task 7: SDK action classes + plugin entry

**Files:**
- Create: `streamdeck/src/actions/core.ts` (SDK-free delegation core + UUIDs — unit-tested)
- Create: `streamdeck/src/actions/sdk-actions.ts` (thin `@elgato/streamdeck` glue — type-checked, not unit-tested)
- Create: `streamdeck/src/plugin.ts`
- Test: `streamdeck/tests/actions.test.ts`

**Interfaces:**
- Consumes: `Adapter` (`registerSurface`/`unregisterSurface`/`handleKeyDown`/`handleKeyUp`, `Surface`), `KeyRegistry` (`addSlot`/`removeSlot`/`addActionKey`/`removeActionKey`), `@elgato/streamdeck` (`action`, `SingletonAction`, event args).
- Produces:
  - `src/actions/core.ts` — **no SDK import**, so the test loads it without the SDK (keeps this module in the SDK-free core). Exports the pure delegation the SDK glue calls:
    - `onSlotAppear(registry, adapter, id, coord, surface)`, `onSlotDisappear(registry, adapter, id)`
    - `onActionAppear(registry, adapter, id, type, coord, surface)`, `onActionDisappear(registry, adapter, id)`
    - `coordToWire(coordinates: { column: number; row: number }): Coord` → `{ col: column, row: row }`
    - `ACTION_UUIDS = { slot, approve, deny, stop, pager }` (the `xyz.vaclavik.herdeck.*` UUIDs)
  - `src/actions/sdk-actions.ts` — imports the SDK + `core`; exports `makeSlotAction(registry, adapter)` and `makeActionKey(registry, adapter, uuid, type)` returning registered `SingletonAction` instances that delegate to `core`.
  - `plugin.ts` builds `BackendProcess` → `IpcClient` → `Adapter`, registers the SDK actions, connects.

- [ ] **Step 1: Write the failing test**

Create `streamdeck/tests/actions.test.ts`:

```typescript
import { describe, it, expect, vi } from "vitest";
import { KeyRegistry } from "../src/registry.js";
import {
  coordToWire,
  onSlotAppear,
  onSlotDisappear,
  onActionAppear,
  onActionDisappear,
} from "../src/actions/core.js";

function fakeAdapter() {
  return { registerSurface: vi.fn(), unregisterSurface: vi.fn(), handleKeyDown: vi.fn(), handleKeyUp: vi.fn() };
}

describe("action <-> core delegation (SDK-free)", () => {
  it("maps SDK coordinates.column -> wire coord.col", () => {
    expect(coordToWire({ column: 3, row: 2 })).toEqual({ col: 3, row: 2 });
  });

  it("slot appear registers a slot + surface; disappear removes both", () => {
    const reg = new KeyRegistry();
    const adapter = fakeAdapter();
    const surface = { setImage() {}, setTitle() {} };
    onSlotAppear(reg, adapter as any, "s0", { col: 0, row: 0 }, surface);
    expect(reg.slotsSnapshot()).toEqual([{ instanceId: "s0", coord: { col: 0, row: 0 } }]);
    expect(adapter.registerSurface).toHaveBeenCalledWith("s0", surface);
    onSlotDisappear(reg, adapter as any, "s0");
    expect(reg.slotsSnapshot()).toEqual([]);
    expect(adapter.unregisterSurface).toHaveBeenCalledWith("s0");
  });

  it("action-key appear registers typed action + surface; disappear removes", () => {
    const reg = new KeyRegistry();
    const adapter = fakeAdapter();
    const surface = { setImage() {}, setTitle() {} };
    onActionAppear(reg, adapter as any, "a", "approve", { col: 0, row: 2 }, surface);
    expect(reg.actionKeysSnapshot()).toEqual([{ instanceId: "a", type: "approve", coord: { col: 0, row: 2 } }]);
    expect(adapter.registerSurface).toHaveBeenCalledWith("a", surface);
    onActionDisappear(reg, adapter as any, "a");
    expect(reg.actionKeysSnapshot()).toEqual([]);
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd streamdeck && npx vitest run tests/actions.test.ts`
Expected: FAIL — `Cannot find module '../src/actions/core.js'`.

- [ ] **Step 3: Write minimal implementation**

Create `streamdeck/src/actions/core.ts` (SDK-free — the delegation core + UUIDs):

```typescript
import type { Coord, ActionKind } from "../protocol.js";
import type { Adapter, Surface } from "../adapter.js";
import type { KeyRegistry } from "../registry.js";

export const ACTION_UUIDS = {
  slot: "xyz.vaclavik.herdeck.slot",
  approve: "xyz.vaclavik.herdeck.approve",
  deny: "xyz.vaclavik.herdeck.deny",
  stop: "xyz.vaclavik.herdeck.stop",
  pager: "xyz.vaclavik.herdeck.pager",
} as const;

export function coordToWire(coordinates: { column: number; row: number }): Coord {
  return { col: coordinates.column, row: coordinates.row };
}

export function onSlotAppear(reg: KeyRegistry, adapter: Adapter, id: string, coord: Coord, surface: Surface) {
  adapter.registerSurface(id, surface);
  reg.addSlot(id, coord);
}
export function onSlotDisappear(reg: KeyRegistry, adapter: Adapter, id: string) {
  reg.removeSlot(id);
  adapter.unregisterSurface(id);
}
export function onActionAppear(reg: KeyRegistry, adapter: Adapter, id: string, type: ActionKind, coord: Coord, surface: Surface) {
  adapter.registerSurface(id, surface);
  reg.addActionKey(id, type, coord);
}
export function onActionDisappear(reg: KeyRegistry, adapter: Adapter, id: string) {
  reg.removeActionKey(id);
  adapter.unregisterSurface(id);
}
```

Create `streamdeck/src/actions/sdk-actions.ts` (thin `@elgato/streamdeck` glue; imports `core`):

```typescript
import { action, SingletonAction } from "@elgato/streamdeck";
import type { WillAppearEvent, WillDisappearEvent, KeyDownEvent, KeyUpEvent } from "@elgato/streamdeck";
import type { ActionKind } from "../protocol.js";
import type { Adapter, Surface } from "../adapter.js";
import type { KeyRegistry } from "../registry.js";
import { coordToWire, onSlotAppear, onSlotDisappear, onActionAppear, onActionDisappear } from "./core.js";

function surfaceOf(ev: WillAppearEvent): Surface {
  return {
    setImage: (image: string) => void ev.action.setImage(image),
    setTitle: (title: string) => void ev.action.setTitle(title),
  };
}

export function makeSlotAction(reg: KeyRegistry, adapter: Adapter) {
  @action({ UUID: "xyz.vaclavik.herdeck.slot" })
  class AgentSlotAction extends SingletonAction {
    override onWillAppear(ev: WillAppearEvent) {
      onSlotAppear(reg, adapter, ev.action.id, coordToWire(ev.action.coordinates!), surfaceOf(ev));
    }
    override onWillDisappear(ev: WillDisappearEvent) { onSlotDisappear(reg, adapter, ev.action.id); }
    override onKeyDown(ev: KeyDownEvent) { adapter.handleKeyDown(ev.action.id); }
    override onKeyUp(ev: KeyUpEvent) { adapter.handleKeyUp(ev.action.id); }
  }
  return new AgentSlotAction();
}

export function makeActionKey(reg: KeyRegistry, adapter: Adapter, uuid: string, type: ActionKind) {
  @action({ UUID: uuid })
  class HerdrActionKey extends SingletonAction {
    override onWillAppear(ev: WillAppearEvent) {
      onActionAppear(reg, adapter, ev.action.id, type, coordToWire(ev.action.coordinates!), surfaceOf(ev));
    }
    override onWillDisappear(ev: WillDisappearEvent) { onActionDisappear(reg, adapter, ev.action.id); }
    override onKeyDown(ev: KeyDownEvent) { adapter.handleKeyDown(ev.action.id); }
    override onKeyUp(ev: KeyUpEvent) { adapter.handleKeyUp(ev.action.id); }
  }
  return new HerdrActionKey();
}
```

Create `streamdeck/src/plugin.ts`:

```typescript
import streamDeck from "@elgato/streamdeck";
import { BackendProcess } from "./backend-process.js";
import { IpcClient } from "./ipc-client.js";
import { KeyRegistry } from "./registry.js";
import { Adapter } from "./adapter.js";
import { ACTION_UUIDS } from "./actions/core.js";
import { makeSlotAction, makeActionKey } from "./actions/sdk-actions.js";

function resolveCommand(): { command: string; args: string[] } {
  // PI-configured path > `herdeck` on PATH. (Frozen-binary path is a packaging follow-up.)
  const configured = process.env.HERDECK_BIN;
  return configured ? { command: configured, args: [] } : { command: "herdeck", args: [] };
}

const registry = new KeyRegistry();
const backend = new BackendProcess({ resolveCommand, devSocket: process.env.HERDECK_ELGATO_DEV_SOCK });
const ipc = new IpcClient();
const adapter = new Adapter(ipc, registry);

backend.onState((s) => adapter.setBackendState(s));

streamDeck.actions.registerAction(makeSlotAction(registry, adapter));
streamDeck.actions.registerAction(makeActionKey(registry, adapter, ACTION_UUIDS.approve, "approve"));
streamDeck.actions.registerAction(makeActionKey(registry, adapter, ACTION_UUIDS.deny, "deny"));
streamDeck.actions.registerAction(makeActionKey(registry, adapter, ACTION_UUIDS.stop, "stop"));
streamDeck.actions.registerAction(makeActionKey(registry, adapter, ACTION_UUIDS.pager, "pager"));

let shuttingDown = false;

async function connect(): Promise<void> {
  try {
    await ipc.connectWithRetry(backend.socketPath, { attempts: 120, delayMs: 250 });
    ipc.sendHello(backend.token);
  } catch (err) {
    streamDeck.logger.error(`IPC connect failed: ${err}`);
  }
}

// Reconnect after an IPC blip or a backend respawn (the brain re-pushes a full
// render on a fresh hello, so nothing is lost). The Adapter's own onClose handler
// already flips the keys to the "backend down" placeholder meanwhile.
ipc.onClose(() => {
  if (!shuttingDown) void connect();
});

streamDeck.connect();
backend.start();
adapter.setBackendState("starting");
void connect();
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd streamdeck && npx vitest run tests/actions.test.ts`
Expected: PASS (the SDK-free delegation core). The SDK glue (`makeSlotAction`/`makeActionKey`/`plugin.ts`) is verified to compile in Step 5.

- [ ] **Step 5: Type-check, then commit**

Run: `cd streamdeck && npx tsc --noEmit`
Expected: PASS (no type errors; confirms the SDK signatures used in the glue match the installed `@elgato/streamdeck`). If the installed SDK differs (e.g. `coordinates` shape or event class names), adjust **only** `src/actions/sdk-actions.ts` and `src/plugin.ts` — never the SDK-free `core.ts`.

```bash
git add streamdeck/src/actions/core.ts streamdeck/src/actions/sdk-actions.ts \
  streamdeck/src/plugin.ts streamdeck/tests/actions.test.ts
git commit -m "feat(ts-shell): SDK action classes and plugin entry point"
sha=$(git rev-parse HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

---

### Task 8: Manifest, `.sdPlugin` bundle, build, Property Inspector

**Files:**
- Create: `streamdeck/xyz.vaclavik.herdeck.sdPlugin/manifest.json`
- Create: `streamdeck/xyz.vaclavik.herdeck.sdPlugin/ui/herdeck.html`
- Create: `streamdeck/xyz.vaclavik.herdeck.sdPlugin/imgs/.gitkeep`
- Create: `streamdeck/rollup.config.mjs`
- Test: `streamdeck/tests/manifest.test.ts`
- Modify: `README.md`

**Interfaces:**
- Consumes: `ACTION_UUIDS` (Task 7) — the manifest action UUIDs must match exactly.
- Produces: a buildable `.sdPlugin` bundle whose `bin/plugin.js` is the rollup output of `src/plugin.ts`; a manifest declaring the 5 actions under Category `herdr`, Keypad-only, Nodejs runtime; a Property Inspector exposing the herdeck binary path.

- [ ] **Step 1: Write the failing test**

Create `streamdeck/tests/manifest.test.ts`:

```typescript
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { ACTION_UUIDS } from "../src/actions/core.js";

const manifest = JSON.parse(
  readFileSync(fileURLToPath(new URL("../xyz.vaclavik.herdeck.sdPlugin/manifest.json", import.meta.url)), "utf8"),
);

describe("manifest.json", () => {
  it("declares the herdeck plugin with a Node code path", () => {
    expect(manifest.UUID).toBe("xyz.vaclavik.herdeck");
    expect(manifest.CodePath).toBe("bin/plugin.js");
    expect(manifest.Nodejs?.Version).toBeTruthy();
    expect(manifest.SDKVersion).toBe(2);
  });

  it("declares all five herdr actions, Keypad-only, under the herdr category", () => {
    const uuids = manifest.Actions.map((a: any) => a.UUID).sort();
    expect(uuids).toEqual(Object.values(ACTION_UUIDS).slice().sort());
    for (const a of manifest.Actions) {
      expect(a.Controllers).toEqual(["Keypad"]);
      expect(Array.isArray(a.States) && a.States.length >= 1).toBe(true);
    }
    expect(manifest.Category).toBe("herdr");
  });

  it("Approve/Deny/Stop/Pager declare the Property Inspector for the herdeck path", () => {
    expect(manifest.PropertyInspectorPath).toBe("ui/herdeck.html");
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd streamdeck && npx vitest run tests/manifest.test.ts`
Expected: FAIL — `ENOENT ... manifest.json`.

- [ ] **Step 3: Write minimal implementation**

Create `streamdeck/xyz.vaclavik.herdeck.sdPlugin/manifest.json`:

```json
{
  "$schema": "https://schemas.elgato.com/streamdeck/plugins/manifest.json",
  "Name": "herdeck",
  "Version": "0.1.0.0",
  "Author": "Filip Vaclavik",
  "Description": "Control AI coding agents under herdr from your Stream Deck.",
  "Category": "herdr",
  "Icon": "imgs/plugin/category",
  "UUID": "xyz.vaclavik.herdeck",
  "SDKVersion": 2,
  "PropertyInspectorPath": "ui/herdeck.html",
  "CodePath": "bin/plugin.js",
  "Nodejs": { "Version": "20", "Debug": "enabled" },
  "Software": { "MinimumVersion": "6.5" },
  "OS": [
    { "Platform": "mac", "MinimumVersion": "12" },
    { "Platform": "windows", "MinimumVersion": "10" }
  ],
  "Actions": [
    {
      "Name": "Agent Slot",
      "UUID": "xyz.vaclavik.herdeck.slot",
      "Icon": "imgs/actions/slot",
      "Tooltip": "Shows a live herdr agent; press to select + focus it.",
      "Controllers": ["Keypad"],
      "States": [{ "Image": "imgs/actions/slot", "TitleAlignment": "bottom" }]
    },
    {
      "Name": "Approve",
      "UUID": "xyz.vaclavik.herdeck.approve",
      "Icon": "imgs/actions/approve",
      "Tooltip": "Approve the selected blocked agent (binary prompts only).",
      "Controllers": ["Keypad"],
      "States": [{ "Image": "imgs/actions/approve" }]
    },
    {
      "Name": "Deny",
      "UUID": "xyz.vaclavik.herdeck.deny",
      "Icon": "imgs/actions/deny",
      "Tooltip": "Deny the selected blocked agent (binary prompts only).",
      "Controllers": ["Keypad"],
      "States": [{ "Image": "imgs/actions/deny" }]
    },
    {
      "Name": "Stop",
      "UUID": "xyz.vaclavik.herdeck.stop",
      "Icon": "imgs/actions/stop",
      "Tooltip": "Force-stop the selected agent (arm, then confirm).",
      "Controllers": ["Keypad"],
      "States": [{ "Image": "imgs/actions/stop" }]
    },
    {
      "Name": "Pager",
      "UUID": "xyz.vaclavik.herdeck.pager",
      "Icon": "imgs/actions/pager",
      "Tooltip": "Cycle selection through agents that need attention.",
      "Controllers": ["Keypad"],
      "States": [{ "Image": "imgs/actions/pager" }]
    }
  ]
}
```

Create `streamdeck/xyz.vaclavik.herdeck.sdPlugin/ui/herdeck.html`:

```html
<!DOCTYPE html>
<html>
  <head><meta charset="utf-8" /></head>
  <body>
    <sdpi-item label="herdeck binary">
      <sdpi-textfield setting="herdeckPath" placeholder="herdeck (or absolute path)"></sdpi-textfield>
    </sdpi-item>
    <script src="https://sdpi-components.dev/releases/v4/sdpi-components.js"></script>
  </body>
</html>
```

Create `streamdeck/xyz.vaclavik.herdeck.sdPlugin/imgs/.gitkeep` (empty file; real icon art is a design follow-up — the bundle references `imgs/...` paths that ship as PNGs before Marketplace submission, but the logic and build do not depend on the art).

Create `streamdeck/rollup.config.mjs`:

```javascript
import typescript from "@rollup/plugin-typescript";
import nodeResolve from "@rollup/plugin-node-resolve";
import commonjs from "@rollup/plugin-commonjs";

export default {
  input: "src/plugin.ts",
  output: {
    file: "xyz.vaclavik.herdeck.sdPlugin/bin/plugin.js",
    format: "esm",
    sourcemap: true,
  },
  external: ["node:net", "node:child_process", "node:crypto", "node:os", "node:path", "node:stream", "node:events"],
  plugins: [
    typescript({ tsconfig: "./tsconfig.json" }),
    nodeResolve({ browser: false, preferBuiltins: true }),
    commonjs(),
  ],
};
```

- [ ] **Step 4: Run the manifest test, then build, to verify both pass**

Run: `cd streamdeck && npx vitest run tests/manifest.test.ts`
Expected: PASS.

Run: `cd streamdeck && npm run build`
Expected: rollup writes `xyz.vaclavik.herdeck.sdPlugin/bin/plugin.js` with no errors.

Run: `cd streamdeck && test -f xyz.vaclavik.herdeck.sdPlugin/bin/plugin.js && echo BUILT`
Expected: prints `BUILT`.

- [ ] **Step 5: Document the shell in `README.md`**

Add a short paragraph to the existing **"Stream Deck (Elgato) plugin backend"** section in `README.md` (added in Plan 1) — a new subsection noting the TS shell now exists:

```markdown
### Plugin shell (TypeScript)

The native plugin's TypeScript shell lives in `streamdeck/` and is built with the
`@elgato/streamdeck` SDK. It spawns and supervises the Python backend (passing the
socket path + one-shot token via `HERDECK_ELGATO_SOCK`/`HERDECK_ELGATO_TOKEN` and
`HERDECK_DECK=elgato-plugin`), forwards key presses, and renders the PNGs the
backend hands back — no logic of its own. Build it with `cd streamdeck && npm install
&& npm run build`; the bundle is `streamdeck/xyz.vaclavik.herdeck.sdPlugin/`.
Packaging it into a double-clickable `.streamDeckPlugin` and shipping a frozen
backend are packaging follow-ups.
```

- [ ] **Step 6: Commit**

```bash
git add streamdeck/xyz.vaclavik.herdeck.sdPlugin/manifest.json \
  streamdeck/xyz.vaclavik.herdeck.sdPlugin/ui/herdeck.html \
  streamdeck/xyz.vaclavik.herdeck.sdPlugin/imgs/.gitkeep \
  streamdeck/rollup.config.mjs streamdeck/tests/manifest.test.ts README.md
git commit -m "feat(ts-shell): manifest, sdPlugin bundle, rollup build and PI"
sha=$(git rev-parse HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

- [ ] **Step 7: Full verification**

Run: `cd streamdeck && npx vitest run`
Expected: all test files pass (protocol, ipc-client, backend-process, registry, adapter, adapter-lifecycle, actions, manifest).

Run: `cd streamdeck && npx tsc --noEmit && npm run build`
Expected: type-check clean, bundle built.

---

## Self-Review Checklist

- **Spec coverage** (design `2026-06-22-elgato-plugin-design.md`):
  - Process lifecycle — TS spawns+supervises backend, env discovery, cold-start retry, bounded backoff, starting/down states: Tasks 3, 6, 7.
  - IPC contract (hello/slots/action_keys/keyDown/keyUp/bye → ready/render/error), newline JSON, base64 image: Tasks 1, 2, 5.
  - Action types Agent Slot/Approve/Deny/Stop/Pager in `herdr` category: Tasks 7, 8.
  - Coordinate→wire mapping, full-snapshot key model (no per-key bye): Tasks 1 (constraint), 4, 7.
  - Optimistic highlight on keyDown / authoritative render from brain: Tasks 5, 7 (keyDown forwarded; SDK native press feedback is the highlight; brain image is authoritative; no TS-invented frame that could stick).
  - Render → setImage (base64 → data URL), only-on-change handled by brain: Task 5.
  - IPC reconnect (backend alive → brain re-pushes full render; backend killed → reset + `starting…`): Tasks 2 (reusable client + buffer reset + reconnect test), 6 (close → `backend down`), 7 (`plugin.ts` reconnect-on-close).
  - Property Inspector for herdeck path; dev-mode socket: Tasks 3, 8.
  - Packaging into `.streamDeckPlugin` + frozen backend: explicitly **out of scope** (follow-up) — README + manifest note.
  - Testing without hardware: every core module unit-tested with fakes; SDK glue type-checked + delegation core unit-tested; no E2E: all tasks.
- **Out of scope (correctly absent):** multi-option prompt rendering, send-text/launch/profile-switch, `.streamDeckPlugin` packaging, frozen PyInstaller backend, touchscreen/dials, real icon art.
- **Type/contract consistency:**
  - `instanceId`/`coord.{col,row}`/`protocol_version`/`action_keys` field names — Task 1 builders, asserted byte-exact; consumed unchanged in Tasks 2, 5, 7.
  - `ActionKind = "approve"|"deny"|"stop"|"pager"` — Task 1, used in 4, 7, 8.
  - `Surface { setImage; setTitle }` — Task 5, implemented by SDK glue in Task 7.
  - `ACTION_UUIDS` (`xyz.vaclavik.herdeck.*`) — Task 7, asserted against manifest in Task 8.
  - `BackendProcess` env keys `HERDECK_ELGATO_SOCK`/`HERDECK_ELGATO_TOKEN`/`HERDECK_DECK` — Task 3, match the merged backend's `discover_ipc`.
```
