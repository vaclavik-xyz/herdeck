// The dock card's live output preview: a tiny plain-text screen model fed by
// the runtime's terminal sessions (/agent/term/*, agent_term.py CardTerminals)
// and the session lifecycle around it. Framework-free so the ANSI handling and
// the open/poll/close rules are unit-testable.
//
// The model is deliberately small: it understands what a TUI agent uses to
// paint (cursor positioning, erase, scroll regions, insert/delete) and drops
// everything else (colours, titles, modes). The card shows only the last few
// non-blank lines, so exact cell fidelity is not the goal; a sane tail is.

import { TERM_POLL_MS, type AgentRef, type AgentTransport } from "./agentCardClient";
import { frameBytes } from "./xtermLoader";

/** Size the dock asks the bridge to render the pane at (spec §2). */
export const PREVIEW_COLS = 100;
export const PREVIEW_ROWS = 24;
/** How many non-blank lines the card shows. */
export const PREVIEW_LINES = 8;
/** Per-viewer "preview collapsed" flag. */
export const PREVIEW_STORAGE_KEY = "herdeck.dock.previewCollapsed";
/** A long-poll that came back empty this fast was not held: wait this long. */
export const PREVIEW_BACKOFF_MS = 1000;
const UNHELD_POLL_MS = 200;
/** How long "this server cannot preview" is remembered (skips a dead box). */
const UNSUPPORTED_TTL_MS = 5 * 60_000;
/** A CSI sequence longer than this is garbage: consume it, never act on it. */
const CSI_MAX = 64;

// East Asian wide + emoji ranges (enough for alignment after CJK/emoji text).
const WIDE: Array<[number, number]> = [
  [0x1100, 0x115f], [0x2e80, 0x303e], [0x3041, 0x33ff], [0x3400, 0x4dbf],
  [0x4e00, 0x9fff], [0xa000, 0xa4cf], [0xac00, 0xd7a3], [0xf900, 0xfaff],
  [0xfe30, 0xfe4f], [0xff00, 0xff60], [0xffe0, 0xffe6], [0x1f300, 0x1f64f],
  [0x1f900, 0x1f9ff], [0x20000, 0x3fffd],
];

function charWidth(cp: number): 0 | 1 | 2 {
  if (
    (cp >= 0x0300 && cp <= 0x036f) || // combining diacritics
    (cp >= 0x200b && cp <= 0x200f) || // zero-width space / joiners / marks
    (cp >= 0xfe00 && cp <= 0xfe0f) // variation selectors
  ) {
    return 0;
  }
  for (const [lo, hi] of WIDE) if (cp >= lo && cp <= hi) return 2;
  return 1;
}

type ParseState = "ground" | "esc" | "csi" | "osc" | "oscEsc" | "skip1";

/** A plain-text terminal screen: cells hold one character ("" = the right
 *  half of a wide one). */
export class ScreenModel {
  cols: number;
  rows: number;
  private grid: string[][] = [];
  private x = 0;
  private y = 0;
  private wrapPending = false;
  private top = 0;
  private bottom = 0;
  private saved: [number, number] = [0, 0];
  private state: ParseState = "ground";
  private seq = "";
  private decoder = new TextDecoder("utf-8");

  constructor(cols: number, rows: number) {
    this.cols = Math.max(1, Math.floor(cols));
    this.rows = Math.max(1, Math.floor(rows));
    this.reset();
  }

  /** Clean screen, cursor home, parser and decoder at a fresh boundary. */
  reset(): void {
    this.grid = Array.from({ length: this.rows }, () => this.blankRow());
    this.x = this.y = 0;
    this.wrapPending = false;
    this.top = 0;
    this.bottom = this.rows - 1;
    this.saved = [0, 0];
    this.state = "ground";
    this.seq = "";
    this.decoder = new TextDecoder("utf-8");
  }

  /** Keep what fits (top-left anchored); the cursor stays on screen. */
  resize(cols: number, rows: number): void {
    const c = Math.max(1, Math.floor(cols));
    const r = Math.max(1, Math.floor(rows));
    const grid: string[][] = [];
    for (let i = 0; i < r; i++) {
      const old = this.grid[i];
      const row = this.blankRow(c);
      if (old) for (let j = 0; j < Math.min(c, old.length); j++) row[j] = old[j];
      grid.push(row);
    }
    this.grid = grid;
    this.cols = c;
    this.rows = r;
    this.top = 0;
    this.bottom = r - 1;
    this.x = Math.min(this.x, c - 1);
    this.y = Math.min(this.y, r - 1);
    this.wrapPending = false;
  }

  write(bytes: Uint8Array): void {
    this.feed(this.decoder.decode(bytes, { stream: true }));
  }

  /** Every row as text, trailing blanks trimmed. */
  lines(): string[] {
    return this.grid.map((row) => row.join("").trimEnd());
  }

  // --- parser --------------------------------------------------------------
  feed(text: string): void {
    for (const ch of text) {
      const cp = ch.codePointAt(0) ?? 0;
      switch (this.state) {
        case "ground":
          this.ground(ch, cp);
          break;
        case "esc":
          this.escape(ch);
          break;
        case "csi":
          if (cp >= 0x40 && cp <= 0x7e) {
            const seq = this.seq;
            this.state = "ground";
            this.seq = "";
            if (seq.length <= CSI_MAX) this.csi(seq, ch);
          } else if (cp === 0x1b) {
            this.state = "esc"; // an aborted sequence
            this.seq = "";
          } else if (this.seq.length <= CSI_MAX) {
            this.seq += ch;
          }
          break;
        case "osc": // OSC / DCS / SOS / PM / APC: skipped up to BEL or ST
          if (cp === 0x07) this.state = "ground";
          else if (cp === 0x1b) this.state = "oscEsc";
          break;
        case "oscEsc":
          this.state = "ground"; // ESC \ (ST); anything else ends it too
          break;
        case "skip1":
          this.state = "ground";
          break;
      }
    }
  }

  private ground(ch: string, cp: number): void {
    if (cp === 0x1b) {
      this.state = "esc";
      return;
    }
    if (cp < 0x20 || cp === 0x7f || (cp >= 0x80 && cp < 0xa0)) {
      if (cp === 0x0d) {
        this.x = 0;
        this.wrapPending = false;
      } else if (cp === 0x0a || cp === 0x0b || cp === 0x0c) {
        this.lineFeed();
      } else if (cp === 0x08) {
        this.x = Math.max(0, this.x - 1);
        this.wrapPending = false;
      } else if (cp === 0x09) {
        this.x = Math.min(this.cols - 1, (Math.floor(this.x / 8) + 1) * 8);
      }
      return;
    }
    this.print(ch, charWidth(cp));
  }

  private escape(ch: string): void {
    this.state = "ground";
    switch (ch) {
      case "[":
        this.state = "csi";
        this.seq = "";
        break;
      case "]":
      case "P":
      case "X":
      case "^":
      case "_":
        this.state = "osc";
        break;
      case "(":
      case ")":
      case "*":
      case "+":
      case "-":
      case ".":
      case "/":
      case "#":
      case "%":
      case " ":
        this.state = "skip1"; // charset / line-attribute designators
        break;
      case "7":
        this.saved = [this.x, this.y];
        break;
      case "8":
        [this.x, this.y] = this.saved;
        this.wrapPending = false;
        break;
      case "D":
        this.lineFeed();
        break;
      case "E":
        this.x = 0;
        this.lineFeed();
        break;
      case "M":
        this.wrapPending = false;
        if (this.y === this.top) this.scrollDown(1);
        else this.y = Math.max(0, this.y - 1);
        break;
      case "c":
        this.reset();
        break;
      default:
        break; // ESC = / ESC > / unknown: nothing to draw
    }
  }

  private csi(seq: string, final: string): void {
    const priv = /^[?<=>]/.test(seq) ? seq[0] : "";
    const body = priv ? seq.slice(1) : seq;
    const inter = body.match(/[\x20-\x2f]+$/)?.[0] ?? "";
    const params = (inter ? body.slice(0, -inter.length) : body)
      .split(";")
      .map((p) => parseInt(p.split(":")[0], 10));
    const n = (i: number, def = 1): number => {
      const v = params[i];
      return Number.isFinite(v) && v > 0 ? v : def;
    };
    if (priv === "?") {
      if ((final === "h" || final === "l") && params.some((p) => p === 1049 || p === 1047 || p === 47)) {
        this.eraseAll();
        this.x = this.y = 0;
        this.wrapPending = false;
      }
      return;
    }
    if (priv || inter) return;
    this.wrapPending = false;
    const clampX = (v: number) => Math.max(0, Math.min(this.cols - 1, v));
    const clampY = (v: number) => Math.max(0, Math.min(this.rows - 1, v));
    switch (final) {
      case "A":
        this.y = clampY(this.y - n(0));
        break;
      case "B":
      case "e":
        this.y = clampY(this.y + n(0));
        break;
      case "C":
      case "a":
        this.x = clampX(this.x + n(0));
        break;
      case "D":
        this.x = clampX(this.x - n(0));
        break;
      case "E":
        this.y = clampY(this.y + n(0));
        this.x = 0;
        break;
      case "F":
        this.y = clampY(this.y - n(0));
        this.x = 0;
        break;
      case "G":
      case "`":
        this.x = clampX(n(0) - 1);
        break;
      case "H":
      case "f":
        this.y = clampY(n(0) - 1);
        this.x = clampX(n(1) - 1);
        break;
      case "d":
        this.y = clampY(n(0) - 1);
        break;
      case "J": {
        const mode = n(0, 0);
        if (mode === 0) {
          this.eraseInRow(this.y, this.x, this.cols);
          for (let r = this.y + 1; r < this.rows; r++) this.grid[r] = this.blankRow();
        } else if (mode === 1) {
          for (let r = 0; r < this.y; r++) this.grid[r] = this.blankRow();
          this.eraseInRow(this.y, 0, this.x + 1);
        } else {
          this.eraseAll();
        }
        break;
      }
      case "K": {
        const mode = n(0, 0);
        if (mode === 0) this.eraseInRow(this.y, this.x, this.cols);
        else if (mode === 1) this.eraseInRow(this.y, 0, this.x + 1);
        else this.eraseInRow(this.y, 0, this.cols);
        break;
      }
      case "X":
        this.eraseInRow(this.y, this.x, Math.min(this.cols, this.x + n(0)));
        break;
      case "P": {
        const row = this.grid[this.y];
        row.splice(this.x, Math.min(n(0), this.cols - this.x));
        while (row.length < this.cols) row.push(" ");
        break;
      }
      case "@": {
        const row = this.grid[this.y];
        const k = Math.min(n(0), this.cols - this.x);
        row.splice(this.x, 0, ...Array<string>(k).fill(" "));
        row.length = this.cols;
        break;
      }
      case "L":
        if (this.y >= this.top && this.y <= this.bottom) this.shiftDown(this.y, this.bottom, n(0));
        this.x = 0;
        break;
      case "M":
        if (this.y >= this.top && this.y <= this.bottom) this.shiftUp(this.y, this.bottom, n(0));
        this.x = 0;
        break;
      case "S":
        this.scrollUp(n(0));
        break;
      case "T":
        this.scrollDown(n(0));
        break;
      case "r": {
        const t = n(0) - 1;
        const b = Math.min(this.rows, n(1, this.rows)) - 1;
        if (t < b) {
          this.top = t;
          this.bottom = b;
        } else {
          this.top = 0;
          this.bottom = this.rows - 1;
        }
        this.x = this.y = 0;
        break;
      }
      case "s":
        this.saved = [this.x, this.y];
        break;
      case "u":
        [this.x, this.y] = this.saved;
        break;
      default:
        break; // SGR (m), modes, reports…: nothing visible
    }
  }

  // --- screen ops ----------------------------------------------------------
  private blankRow(cols = this.cols): string[] {
    return Array<string>(cols).fill(" ");
  }

  private print(ch: string, width: 0 | 1 | 2): void {
    if (width === 0) {
      // Combine with the cell just written (or the one under the cursor).
      const cx = this.wrapPending ? this.x : Math.max(0, this.x - 1);
      const row = this.grid[this.y];
      if (row[cx] !== undefined && row[cx] !== "") row[cx] += ch;
      return;
    }
    if (this.wrapPending || (width === 2 && this.x === this.cols - 1 && this.cols > 1)) {
      this.x = 0;
      this.lineFeed();
    }
    const row = this.grid[this.y];
    row[this.x] = ch;
    if (width === 2 && this.x + 1 < this.cols) row[this.x + 1] = "";
    this.x += width;
    if (this.x >= this.cols) {
      this.x = this.cols - 1;
      this.wrapPending = true;
    }
  }

  private lineFeed(): void {
    this.wrapPending = false;
    if (this.y === this.bottom) this.scrollUp(1);
    else if (this.y < this.rows - 1) this.y++;
  }

  private scrollUp(k: number): void {
    this.shiftUp(this.top, this.bottom, k);
  }

  private scrollDown(k: number): void {
    this.shiftDown(this.top, this.bottom, k);
  }

  /** Rows from..to move up by k; blank rows enter at `to`. */
  private shiftUp(from: number, to: number, k: number): void {
    const count = Math.min(k, to - from + 1);
    this.grid.splice(from, count);
    this.grid.splice(to - count + 1, 0, ...Array.from({ length: count }, () => this.blankRow()));
  }

  /** Rows from..to move down by k; blank rows enter at `from`. */
  private shiftDown(from: number, to: number, k: number): void {
    const count = Math.min(k, to - from + 1);
    this.grid.splice(to - count + 1, count);
    this.grid.splice(from, 0, ...Array.from({ length: count }, () => this.blankRow()));
  }

  private eraseInRow(r: number, from: number, to: number): void {
    const row = this.grid[r];
    for (let c = Math.max(0, from); c < Math.min(this.cols, to); c++) row[c] = " ";
  }

  private eraseAll(): void {
    this.grid = Array.from({ length: this.rows }, () => this.blankRow());
  }
}

/** The tail the card shows: the last `n` non-blank rows, right-trimmed. */
export function previewLines(rows: string[], n = PREVIEW_LINES): string[] {
  return rows
    .map((r) => r.trimEnd())
    .filter((r) => r.trim() !== "")
    .slice(-n);
}

// --- the per-viewer collapsed flag ------------------------------------------

type KV = Pick<Storage, "getItem" | "setItem">;

/** The browser's localStorage, or null where it is missing or blocked. */
export function viewerStorage(): KV | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

export function readPreviewCollapsed(storage: KV | null): boolean {
  try {
    return storage?.getItem(PREVIEW_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

export function writePreviewCollapsed(storage: KV | null, collapsed: boolean): void {
  try {
    storage?.setItem(PREVIEW_STORAGE_KEY, collapsed ? "1" : "0");
  } catch {
    // private window / blocked site data: the flag just won't stick
  }
}

// --- servers that cannot preview ---------------------------------------------

const unsupported = new Map<string, number>();

/** The bridge on `serverId` answered "unsupported" recently: a collapsed card
 *  does not offer a preview that would only vanish on expand. */
export function previewUnsupported(serverId: string, now = Date.now()): boolean {
  const until = unsupported.get(serverId);
  if (until === undefined) return false;
  if (now < until) return true;
  unsupported.delete(serverId);
  return false;
}

export function markPreviewUnsupported(serverId: string, now = Date.now()): void {
  unsupported.set(serverId, now + UNSUPPORTED_TTL_MS);
}

// --- one live session per dock -----------------------------------------------

export interface PreviewCallbacks {
  /** The session is open (the first frame may still be on its way). */
  onOpen?(): void;
  /** The visible tail changed (never called with the same text twice). */
  onLines(lines: string[]): void;
  /** The preview is over: an open outcome code (unsupported, invalid,
   *  disconnected, …), the runtime's close reason, "gone" or "error". */
  onEnd(reason: string): void;
}

export interface PreviewHandle {
  /** Close the remote session (idempotent) and drop any poll in flight. */
  stop(): void;
}

type TermTransport = Pick<AgentTransport, "termOpen" | "termPoll" | "termClose">;

let active: PreviewHandle | null = null;

/** Open a preview of `ref` and keep long-polling it. A dock shows one card at
 *  a time, and the bridge caps observers per connection: starting a preview
 *  stops the previous one. */
export function startPreview(
  transport: TermTransport,
  ref: AgentRef,
  cb: PreviewCallbacks,
  opts: { waitMs?: number; backoffMs?: number } = {},
): PreviewHandle {
  const waitMs = opts.waitMs ?? TERM_POLL_MS;
  const backoffMs = opts.backoffMs ?? PREVIEW_BACKOFF_MS;
  active?.stop();
  let stopped = false;
  let session: string | null = null;
  let backoff: ReturnType<typeof setTimeout> | undefined;
  let wake: (() => void) | null = null;

  const finish = (): void => {
    stopped = true;
    clearTimeout(backoff);
    wake?.();
    if (active === handle) active = null;
  };
  const handle: PreviewHandle = {
    stop() {
      if (stopped) return;
      finish();
      const id = session;
      session = null;
      if (id) void transport.termClose(id);
    },
  };
  active = handle;

  const pause = (ms: number) =>
    new Promise<void>((resolve) => {
      wake = resolve;
      backoff = setTimeout(resolve, ms);
    });

  async function run(): Promise<void> {
    const opened = await transport.termOpen(ref, PREVIEW_COLS, PREVIEW_ROWS);
    if (stopped) {
      // Stopped while opening: never leave that one running.
      if (opened.ok) void transport.termClose(opened.id);
      return;
    }
    if (!opened.ok) {
      finish();
      cb.onEnd(opened.outcome.code);
      return;
    }
    session = opened.id;
    cb.onOpen?.();
    const model = new ScreenModel(PREVIEW_COLS, PREVIEW_ROWS);
    let after = 0;
    let shown = "";
    while (!stopped) {
      const started = Date.now();
      const polled = await transport.termPoll(opened.id, after, waitMs);
      if (stopped) return;
      if (polled.kind === "gone") {
        session = null; // the runtime already forgot it
        finish();
        cb.onEnd("gone");
        return;
      }
      if (polled.kind === "error") {
        handle.stop();
        cb.onEnd("error");
        return;
      }
      if (polled.gap) model.reset();
      for (const f of polled.frames) {
        if (f.cols !== model.cols || f.rows !== model.rows) model.resize(f.cols, f.rows);
        if (f.full) model.reset();
        model.write(frameBytes(f.data));
      }
      after = polled.next;
      const lines = previewLines(model.lines());
      const key = lines.join("\n");
      if (key !== shown) {
        shown = key;
        cb.onLines(lines);
      }
      if (polled.closed) {
        session = null; // delivered with its close: the runtime dropped it
        finish();
        cb.onEnd(polled.closed);
        return;
      }
      if (polled.frames.length === 0 && Date.now() - started < UNHELD_POLL_MS && backoffMs > 0) {
        await pause(backoffMs); // a runtime that did not hold the poll: no hot loop
      }
    }
  }
  void run();
  return handle;
}
