import { afterEach, describe, expect, it, vi } from "vitest";
import {
  PREVIEW_COLS,
  PREVIEW_LINES,
  PREVIEW_ROWS,
  PREVIEW_STORAGE_KEY,
  ScreenModel,
  previewLines,
  readPreviewCollapsed,
  startPreview,
  writePreviewCollapsed,
} from "./dockPreview";
import type { AgentTransport, TermFrame, TermOpen, TermPoll } from "./agentCardClient";

const b64 = (s: string) => btoa(String.fromCharCode(...new TextEncoder().encode(s)));

function screen(text: string, cols = 20, rows = 6): ScreenModel {
  const s = new ScreenModel(cols, rows);
  s.write(new TextEncoder().encode(text));
  return s;
}

describe("ScreenModel", () => {
  it("prints text, CR/LF and strips SGR colours", () => {
    const s = screen("\x1b[1;32mhello\x1b[0m world\r\nnext line");
    expect(s.lines().slice(0, 2)).toEqual(["hello world", "next line"]);
  });

  it("a bare LF keeps the column, a CR returns to column 0", () => {
    expect(screen("ab\ncd").lines().slice(0, 2)).toEqual(["ab", "  cd"]);
    expect(screen("abcd\rxy").lines()[0]).toBe("xycd");
  });

  it("positions the cursor (CUP, CHA, relative moves) and erases", () => {
    const s = screen("\x1b[2J\x1b[3;5Hmid\x1b[1;1Htop\x1b[2;1Hxxxx\x1b[2;3H\x1b[K\x1b[5G!");
    expect(s.lines().slice(0, 3)).toEqual(["top", "xx  !", "    mid"]);
    const r = screen("abc\x1b[2Dz\x1b[Bq\x1b[Aw");
    expect(r.lines().slice(0, 2)).toEqual(["azcw", "  q"]);
  });

  it("scrolls when output runs past the last row", () => {
    const s = screen("1\r\n2\r\n3\r\n4", 10, 3);
    expect(s.lines()).toEqual(["2", "3", "4"]);
  });

  it("wraps a line that reaches the right margin", () => {
    expect(screen("abcdefgh", 5, 3).lines()).toEqual(["abcde", "fgh", ""]);
  });

  it("ignores OSC titles, private modes and charset designators", () => {
    const s = screen("\x1b]0;my title\x07\x1b[?25l\x1b(Bok\x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\");
    expect(s.lines()[0]).toBe("oklink");
  });

  it("an alternate-screen switch starts from a clean screen", () => {
    expect(screen("old\x1b[?1049h\x1b[Hnew").lines()[0]).toBe("new");
  });

  it("decodes UTF-8 split across writes", () => {
    const s = new ScreenModel(20, 3);
    const bytes = new TextEncoder().encode("✓ done");
    s.write(bytes.slice(0, 2));
    s.write(bytes.slice(2));
    expect(s.lines()[0]).toBe("✓ done");
  });

  it("handles sequences split across writes", () => {
    const s = new ScreenModel(20, 3);
    s.write(new TextEncoder().encode("a\x1b[3"));
    s.write(new TextEncoder().encode("1mb"));
    expect(s.lines()[0]).toBe("ab");
  });

  it("insert/delete lines and characters inside a scroll region", () => {
    const s = screen("1\r\n2\r\n3\r\n4\x1b[2;3r\x1b[2;1H\x1b[M", 10, 4);
    expect(s.lines()).toEqual(["1", "3", "", "4"]);
    const c = screen("abcdef\x1b[1;2H\x1b[2P\x1b[1;1H\x1b[1@", 10, 2);
    expect(c.lines()[0]).toBe(" adef");
  });

  it("reset clears the screen; resize keeps what fits", () => {
    const s = screen("hello\r\nworld", 10, 3);
    s.resize(3, 2);
    expect(s.lines()).toEqual(["hel", "wor"]);
    s.reset();
    expect(s.lines()).toEqual(["", ""]);
  });
});

describe("previewLines", () => {
  it("keeps the last 8 non-blank lines, right-trimmed", () => {
    const rows = ["a", "", "b  ", "c", "d", "", "e", "f", "g", "h", "i", "   ", ""];
    expect(previewLines(rows)).toEqual(["b", "c", "d", "e", "f", "g", "h", "i"]);
    expect(previewLines(["", " "])).toEqual([]);
    expect(PREVIEW_LINES).toBe(8);
  });
});

describe("collapsed preference", () => {
  it("round-trips through storage under a herdeck.dock.* key", () => {
    const store = new Map<string, string>();
    const storage = {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
    };
    expect(PREVIEW_STORAGE_KEY.startsWith("herdeck.dock.")).toBe(true);
    expect(readPreviewCollapsed(storage)).toBe(false);
    writePreviewCollapsed(storage, true);
    expect(readPreviewCollapsed(storage)).toBe(true);
    writePreviewCollapsed(storage, false);
    expect(readPreviewCollapsed(storage)).toBe(false);
  });

  it("a throwing or missing storage reads as expanded and never throws", () => {
    const bad = {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("denied");
      },
    };
    expect(readPreviewCollapsed(bad)).toBe(false);
    expect(() => writePreviewCollapsed(bad, true)).not.toThrow();
    expect(readPreviewCollapsed(null)).toBe(false);
  });
});

// --- session lifecycle -------------------------------------------------------

function frame(data: string, over: Partial<TermFrame> = {}): TermFrame {
  return { seq: 1, full: false, cols: 40, rows: 10, data: b64(data), ...over };
}

function fakeTerm(opened: TermOpen = { ok: true, id: "s1" }) {
  const polls: Array<(r: TermPoll) => void> = [];
  const calls = {
    open: [] as Array<{ cols: number; rows: number }>,
    poll: [] as Array<{ id: string; after: number }>,
    close: [] as string[],
  };
  let resolveOpen: ((r: TermOpen) => void) | null = null;
  const transport: Pick<AgentTransport, "termOpen" | "termPoll" | "termClose"> = {
    termOpen(_ref, cols, rows) {
      calls.open.push({ cols, rows });
      return new Promise((r) => {
        resolveOpen = r;
      });
    },
    termPoll(id, after) {
      calls.poll.push({ id, after });
      return new Promise((r) => polls.push(r));
    },
    async termClose(id) {
      calls.close.push(id);
    },
  };
  return {
    transport,
    calls,
    open: async (r: TermOpen = opened) => {
      resolveOpen?.(r);
      await new Promise((res) => setTimeout(res, 0));
    },
    answer: async (r: TermPoll) => {
      const next = polls.shift();
      if (!next) throw new Error("no poll pending");
      next(r);
      await new Promise((res) => setTimeout(res, 0));
    },
  };
}

const ref = { serverId: "prod", paneId: "p0" };
const tick = () => new Promise((res) => setTimeout(res, 0));

describe("startPreview", () => {
  let stopAll: Array<() => void> = [];
  afterEach(() => {
    for (const s of stopAll) s();
    stopAll = [];
  });

  it("opens 100×24, feeds frames and reports only changed lines", async () => {
    const t = fakeTerm();
    const onLines = vi.fn();
    const onEnd = vi.fn();
    const p = startPreview(t.transport, ref, { onLines, onEnd }, { backoffMs: 0 });
    stopAll.push(p.stop);
    expect(t.calls.open).toEqual([{ cols: PREVIEW_COLS, rows: PREVIEW_ROWS }]);
    expect([PREVIEW_COLS, PREVIEW_ROWS]).toEqual([100, 24]);
    await t.open();
    expect(t.calls.poll[0]).toEqual({ id: "s1", after: 0 });
    await t.answer({ kind: "frames", frames: [frame("hello\r\n")], next: 1, closed: null, gap: false });
    expect(onLines).toHaveBeenLastCalledWith(["hello"]);
    expect(t.calls.poll[1]).toEqual({ id: "s1", after: 1 });
    // A frame that changes nothing visible (cursor move, colour) is not re-reported.
    await t.answer({ kind: "frames", frames: [frame("\x1b[0m\x1b[H")], next: 2, closed: null, gap: false });
    // An empty long-poll answer neither.
    await t.answer({ kind: "frames", frames: [], next: 2, closed: null, gap: false });
    expect(onLines).toHaveBeenCalledTimes(1);
    await t.answer({ kind: "frames", frames: [frame("\x1b[2;1Hworld")], next: 3, closed: null, gap: false });
    expect(onLines).toHaveBeenLastCalledWith(["hello", "world"]);
    expect(onEnd).not.toHaveBeenCalled();
  });

  it("a full frame repaints from a clean screen", async () => {
    const t = fakeTerm();
    const onLines = vi.fn();
    const p = startPreview(t.transport, ref, { onLines, onEnd: vi.fn() });
    stopAll.push(p.stop);
    await t.open();
    await t.answer({ kind: "frames", frames: [frame("old stuff")], next: 1, closed: null, gap: false });
    await t.answer({ kind: "frames", frames: [frame("new", { full: true })], next: 2, closed: null, gap: false });
    expect(onLines).toHaveBeenLastCalledWith(["new"]);
  });

  it("unsupported / invalid open ends at once with the outcome code", async () => {
    for (const code of ["unsupported", "invalid", "disconnected"]) {
      const t = fakeTerm();
      const onEnd = vi.fn();
      startPreview(t.transport, ref, { onLines: vi.fn(), onEnd });
      await t.open({ ok: false, outcome: { ok: false, code, message: "" } });
      expect(onEnd).toHaveBeenCalledWith(code);
      expect(t.calls.poll).toHaveLength(0);
      expect(t.calls.close).toHaveLength(0);
    }
  });

  it("stop closes the open session once and stops polling", async () => {
    const t = fakeTerm();
    const p = startPreview(t.transport, ref, { onLines: vi.fn(), onEnd: vi.fn() });
    await t.open();
    p.stop();
    p.stop();
    expect(t.calls.close).toEqual(["s1"]);
    await t.answer({ kind: "frames", frames: [frame("late")], next: 1, closed: null, gap: false });
    expect(t.calls.poll).toHaveLength(1);
  });

  it("stopped while opening: the session that opens late is closed at once", async () => {
    const t = fakeTerm();
    const onLines = vi.fn();
    const p = startPreview(t.transport, ref, { onLines, onEnd: vi.fn() });
    p.stop();
    await t.open();
    expect(t.calls.close).toEqual(["s1"]);
    expect(t.calls.poll).toHaveLength(0);
    expect(onLines).not.toHaveBeenCalled();
  });

  it("only one session per dock: a new preview stops the previous one", async () => {
    const a = fakeTerm({ ok: true, id: "a" });
    const pa = startPreview(a.transport, ref, { onLines: vi.fn(), onEnd: vi.fn() });
    stopAll.push(pa.stop);
    await a.open();
    const b = fakeTerm({ ok: true, id: "b" });
    const pb = startPreview(b.transport, { serverId: "prod", paneId: "p1" }, { onLines: vi.fn(), onEnd: vi.fn() });
    stopAll.push(pb.stop);
    expect(a.calls.close).toEqual(["a"]);
    await b.open();
    expect(b.calls.close).toEqual([]);
  });

  it("a runtime close or a forgotten session ends the preview without a close call", async () => {
    const t = fakeTerm();
    const onEnd = vi.fn();
    startPreview(t.transport, ref, { onLines: vi.fn(), onEnd });
    await t.open();
    await t.answer({ kind: "frames", frames: [frame("bye")], next: 1, closed: "stream ended", gap: false });
    expect(onEnd).toHaveBeenCalledWith("stream ended");
    expect(t.calls.close).toEqual([]);

    const g = fakeTerm();
    const onGone = vi.fn();
    startPreview(g.transport, ref, { onLines: vi.fn(), onEnd: onGone });
    await g.open();
    await g.answer({ kind: "gone" });
    expect(onGone).toHaveBeenCalledWith("gone");
    expect(g.calls.close).toEqual([]);
  });

  it("a poll error closes the session and ends", async () => {
    const t = fakeTerm();
    const onEnd = vi.fn();
    startPreview(t.transport, ref, { onLines: vi.fn(), onEnd });
    await t.open();
    await t.answer({ kind: "error", message: "HTTP 500" });
    await tick();
    expect(onEnd).toHaveBeenCalledWith("error");
    expect(t.calls.close).toEqual(["s1"]);
  });
});

describe("startPreview backoff", () => {
  it("waits after an unheld empty poll, and a stop during the wait ends it", async () => {
    const t = fakeTerm();
    const p = startPreview(t.transport, ref, { onLines: vi.fn(), onEnd: vi.fn() }, { backoffMs: 40 });
    await t.open();
    await t.answer({ kind: "frames", frames: [], next: 0, closed: null, gap: false });
    expect(t.calls.poll).toHaveLength(1);
    await new Promise((res) => setTimeout(res, 60));
    expect(t.calls.poll).toHaveLength(2);
    await t.answer({ kind: "frames", frames: [], next: 0, closed: null, gap: false });
    p.stop();
    await new Promise((res) => setTimeout(res, 60));
    expect(t.calls.poll).toHaveLength(2);
    expect(t.calls.close).toEqual(["s1"]);
  });
});
