import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import AgentTerminal from "./AgentTerminal.svelte";
import AgentCard from "./AgentCard.svelte";
import type { AgentTransport, TermOpen, TermPoll } from "./agentCardClient";
import type { TerminalHandle } from "./xtermLoader";
import { setLang } from "./i18n.svelte";

const REF = { serverId: "prod", paneId: "p0" };
const b64 = (s: string) => btoa(s);

class FakeTerm implements TerminalHandle {
  cols = 90;
  rows = 20;
  written: string[] = [];
  resets = 0;
  disposed = false;
  write(bytes: Uint8Array) {
    this.written.push(String.fromCharCode(...bytes));
  }
  reset() {
    this.resets++;
    this.written = [];
  }
  resize(cols: number, rows: number) {
    this.cols = cols;
    this.rows = rows;
  }
  fit() {}
  dispose() {
    this.disposed = true;
  }
}

class FakeDoc {
  hidden = false;
  private listeners: (() => void)[] = [];
  addEventListener(_: string, fn: () => void) {
    this.listeners.push(fn);
  }
  removeEventListener(_: string, fn: () => void) {
    this.listeners = this.listeners.filter((l) => l !== fn);
  }
  set(hidden: boolean) {
    this.hidden = hidden;
    for (const l of this.listeners) l();
  }
}

/** A transport whose polls are answered from a queue; an empty queue parks
 *  the poll until `push` (like the runtime's long-poll). */
function termTransport(open: TermOpen = { ok: true, id: "s1" }) {
  const opens: { cols: number; rows: number }[] = [];
  const closes: string[] = [];
  const polls: { id: string; after: number }[] = [];
  const queue: TermPoll[] = [];
  let waiting: ((p: TermPoll) => void) | null = null;
  const transport: AgentTransport = {
    detail: async () => ({ kind: "gone" }),
    act: async () => ({ ok: true, code: "sent", message: "" }),
    termOpen: async (_ref, cols, rows) => {
      opens.push({ cols, rows });
      return open;
    },
    termPoll: (id, after) => {
      polls.push({ id, after });
      const next = queue.shift();
      if (next) return Promise.resolve(next);
      return new Promise((resolve) => (waiting = resolve));
    },
    termClose: async (id) => {
      closes.push(id);
    },
  };
  return {
    transport,
    opens,
    closes,
    polls,
    push(p: TermPoll) {
      if (waiting) {
        const w = waiting;
        waiting = null;
        w(p);
      } else queue.push(p);
    },
  };
}

async function settle(): Promise<void> {
  for (let i = 0; i < 5; i++) await vi.advanceTimersByTimeAsync(0);
  flushSync();
}

function render(props: Record<string, unknown>) {
  const target = document.createElement("div");
  document.body.appendChild(target);
  const instance = mount(AgentTerminal, { target, props });
  flushSync();
  return { target, cleanup: () => { unmount(instance); target.remove(); } };
}

describe("AgentTerminal", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    setLang("en");
  });
  afterEach(() => vi.useRealTimers());

  it("opens at the fitted size, writes frames (a full frame resets) and follows the cursor", async () => {
    const t = termTransport();
    const term = new FakeTerm();
    const { target, cleanup } = render({
      transport: t.transport, agent: REF, createTerminal: async () => term, doc: new FakeDoc(),
    });
    try {
      await settle();
      expect(t.opens).toEqual([{ cols: 90, rows: 20 }]);
      t.push({
        kind: "frames",
        frames: [
          { seq: 1, full: false, cols: 90, rows: 20, data: b64("old") },
          { seq: 2, full: true, cols: 100, rows: 30, data: b64("\x1b[1mhello") },
        ],
        next: 2,
        closed: null,
        gap: false,
      });
      await settle();
      expect(term.written).toEqual(["\x1b[1mhello"]);
      expect([term.cols, term.rows]).toEqual([100, 30]);
      expect(t.polls.at(-1)).toEqual({ id: "s1", after: 2 });
      expect(target.textContent).toContain("Live");
    } finally { cleanup(); }
  });

  it("stops observing when it unmounts (card closed / toggled off)", async () => {
    const t = termTransport();
    const term = new FakeTerm();
    const { cleanup } = render({
      transport: t.transport, agent: REF, createTerminal: async () => term, doc: new FakeDoc(),
    });
    await settle();
    cleanup();
    expect(t.closes).toEqual(["s1"]);
    expect(term.disposed).toBe(true);
  });

  it("stops while the window is hidden and resumes when shown", async () => {
    const t = termTransport();
    const doc = new FakeDoc();
    const { target, cleanup } = render({
      transport: t.transport, agent: REF, createTerminal: async () => new FakeTerm(), doc,
    });
    try {
      await settle();
      doc.set(true);
      await settle();
      expect(t.closes).toEqual(["s1"]);
      expect(target.textContent).toContain("Paused while the window is hidden");
      doc.set(false);
      await settle();
      expect(t.opens.length).toBe(2);
    } finally { cleanup(); }
  });

  it("explains why the bridge ended the preview, in Czech too", async () => {
    setLang("cs");
    const t = termTransport();
    const { target, cleanup } = render({
      transport: t.transport, agent: REF, createTerminal: async () => new FakeTerm(), doc: new FakeDoc(),
    });
    try {
      await settle();
      t.push({ kind: "frames", frames: [], next: 0, closed: "too many live previews", gap: false });
      await settle();
      expect(target.textContent).toContain("příliš mnoho živých náhledů");
      // the runtime already dropped it: nothing to stop
      expect(t.closes).toEqual([]);
      expect(target.querySelector("button")?.textContent).toBe("Spustit znovu");
    } finally { cleanup(); setLang("en"); }
  });

  it("reports an open refused by the runtime", async () => {
    const t = termTransport({ ok: false, outcome: { ok: false, code: "disconnected", message: "" } });
    const { target, cleanup } = render({
      transport: t.transport, agent: REF, createTerminal: async () => new FakeTerm(), doc: new FakeDoc(),
    });
    try {
      await settle();
      expect(target.textContent).toContain("the server is disconnected");
      expect(t.polls).toEqual([]);
    } finally { cleanup(); }
  });
});

describe("AgentCard live terminal toggle", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("starts on toggle and stops observing when the card closes", async () => {
    const t = termTransport();
    t.transport.detail = async () => ({
      kind: "ok",
      detail: {
        serverId: "prod", paneId: "p0", agentType: "codex", displayAgent: "", label: "x",
        title: "", repo: "", branch: "", workspace: "", tab: "", status: "working", sinceS: 5,
        backend: "herdr", connected: true, prompt: null, promptPending: false, revision: null,
        options: [], canStop: true, stopConfirm: true, canText: true, canFocus: true, subagents: [],
      },
    });
    const target = document.createElement("div");
    document.body.appendChild(target);
    const instance = mount(AgentCard, {
      target,
      props: {
        transport: t.transport, target: { index: 0 }, onClose: () => {},
        createTerminal: async () => new FakeTerm(),
      },
    });
    await settle();
    const toggle = [...target.querySelectorAll("button")].find((b) => b.textContent === "Live terminal")!;
    expect(toggle.getAttribute("title")).toContain("read-only live view");
    toggle.click();
    await settle();
    expect(t.opens.length).toBe(1);
    unmount(instance);
    target.remove();
    expect(t.closes).toEqual(["s1"]);
  });
});
