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
    // Guard every handler against a superseded connection: once a newer connection has
    // attached, a stale stream's late data/close must not corrupt the buffer or wrongly
    // mark the live client disconnected (which would silently drop subsequent sends).
    stream.on("data", (chunk: Buffer) => {
      if (this.stream === stream) this.onData(chunk);
    });
    stream.on("error", () => {});
    stream.on("close", () => {
      if (this.stream !== stream) return;
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
