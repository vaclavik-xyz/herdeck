import { spawn as nodeSpawn } from "node:child_process";
import crypto from "node:crypto";
import os from "node:os";
import path from "node:path";

type SpawnFn = (command: string, args: string[], options: any) => { kill: (sig?: string) => void; on: (ev: string, cb: (...a: any[]) => void) => void };

export function resolveHerdeckCommand(opts: { configuredPath?: string; envBin?: string }): { command: string; args: string[] } {
  // PI-configured path wins (the Stream Deck app's PATH usually lacks the user's venv),
  // then HERDECK_BIN, then `herdeck` on PATH. Frozen-binary path is a packaging follow-up.
  const command = opts.configuredPath || opts.envBin || "herdeck";
  return { command, args: [] };
}

export interface BackendOptions {
  resolveCommand: () => { command: string; args: string[] };
  devSocket?: string;
  devToken?: string;
  spawn?: SpawnFn;
  randomToken?: () => string;
  tmpDir?: string;
  setTimer?: (cb: () => void, ms: number) => unknown;
  baseBackoffMs?: number;
  maxBackoffMs?: number;
}

export class BackendProcess {
  private readonly opts: Required<Omit<BackendOptions, "devSocket" | "devToken">> & { devSocket?: string };
  private child: { kill: (sig?: string) => void; on: (ev: string, cb: (...a: any[]) => void) => void } | null = null;
  private stopped = false;
  private started = false;
  private backoff: number;
  private stateCbs: Array<(s: "starting" | "down") => void> = [];
  readonly socketPath: string;
  readonly token: string;

  constructor(options: BackendOptions) {
    if (options.devSocket && !options.devToken) {
      throw new Error(
        "dev mode (HERDECK_ELGATO_DEV_SOCK) requires HERDECK_ELGATO_TOKEN — the shell must share the external backend's token to authenticate",
      );
    }
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
    // Dev mode: reuse the token the external backend was launched with (guaranteed
    // present by the guard above). Spawn mode: a fresh one-shot secret we hand the child.
    this.token = options.devSocket ? (options.devToken as string) : this.opts.randomToken();
    this.socketPath = this.opts.devSocket
      ?? path.join(this.opts.tmpDir, `herdeck-elgato-${process.pid}-${this.token.slice(0, 8)}.sock`);
  }

  get spawned(): boolean { return !this.opts.devSocket; }

  onState(cb: (s: "starting" | "down") => void) { this.stateCbs.push(cb); }
  private emitState(s: "starting" | "down") { this.stateCbs.forEach((cb) => cb(s)); }

  start(): void {
    if (this.opts.devSocket) return; // dev mode: an external backend already owns the socket
    if (this.started) return; // idempotent: the supervise loop is already running — a second
    // start() must not spawn a duplicate backend that would rebind the same socket and orphan.
    this.started = true;
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
    // First terminal signal wins. Critically, a spawn that fails (ENOENT — herdeck not
    // on PATH, the first-run state before the user sets the PI path) emits "error" and
    // NOT "exit", so listening only for "exit" would leave the supervisor permanently
    // dead and never pick up a later PI path. Treat error/exit/close uniformly, guarded
    // so one failure schedules exactly one respawn.
    let settled = false;
    const onTerminal = () => {
      if (settled) return;
      settled = true;
      if (this.child === child) this.child = null; // never clobber a newer child's reference
      if (this.stopped) return;
      this.emitState("down");
      const delay = this.backoff;
      this.backoff = Math.min(this.backoff * 2, this.opts.maxBackoffMs);
      this.opts.setTimer(() => this.spawnOnce(), delay);
    };
    child.on("error", onTerminal); // spawn failure (e.g. ENOENT) — emitted instead of "exit"
    child.on("exit", onTerminal);
    child.on("close", onTerminal); // safety net
  }

  stop(): void {
    this.stopped = true;
    this.child?.kill();
    this.child = null;
  }
}
