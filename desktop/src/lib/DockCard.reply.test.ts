// The dock card's reply field and live output preview (Task 4).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import DockCard from "./DockCard.svelte";
import { setLang } from "./i18n.svelte";
import { agentDetail, dockAgent, fakeAgentTransport } from "./dockFixtures";
import type { DockAgent } from "./dockClient";
import type { ActionOutcome, AgentTransport, TermOpen, TermPoll } from "./agentCardClient";
import { PREVIEW_STORAGE_KEY } from "./dockPreview";

const b64 = (s: string) => btoa(String.fromCharCode(...new TextEncoder().encode(s)));

/** fakeAgentTransport plus a scriptable terminal and a gate on `act`. */
function transport(opts: {
  canText?: boolean;
  status?: string;
  open?: TermOpen;
  outcomes?: ActionOutcome[];
} = {}) {
  const base = fakeAgentTransport({
    details: [{ kind: "ok", detail: agentDetail({ canText: opts.canText ?? true, status: opts.status ?? "blocked" }) }],
    outcomes: opts.outcomes,
  });
  const polls: Array<(r: TermPoll) => void> = [];
  const term = { open: 0, close: [] as string[] };
  let gate: Promise<void> | null = null;
  const t: AgentTransport = {
    ...base,
    async act(action, ref, extra) {
      if (gate) await gate;
      return base.act(action, ref, extra);
    },
    async termOpen() {
      term.open++;
      return opts.open ?? { ok: true, id: `s${term.open}` };
    },
    termPoll() {
      return new Promise((r) => polls.push(r));
    },
    async termClose(id) {
      term.close.push(id);
    },
  };
  return {
    t,
    base,
    term,
    hold() {
      let release!: () => void;
      gate = new Promise((r) => (release = r));
      return () => {
        gate = null;
        release();
      };
    },
    async frame(text: string) {
      await vi.waitFor(() => expect(polls.length).toBeGreaterThan(0));
      polls.shift()!({
        kind: "frames",
        frames: [{ seq: 1, full: false, cols: 100, rows: 24, data: b64(text) }],
        next: 1,
        closed: null,
        gap: false,
      });
    },
  };
}

function render(agent: DockAgent, t: AgentTransport) {
  const onSent = vi.fn();
  const onReplyFocus = vi.fn();
  const target = document.createElement("div");
  document.body.appendChild(target);
  const instance = mount(DockCard, {
    target,
    props: {
      agent,
      sinceOf: (a: DockAgent) => a.sinceS,
      iconUrl: () => null,
      transport: t,
      onClose: vi.fn(),
      onSent,
      onReplyFocus,
    },
  });
  flushSync();
  let gone = false;
  return {
    target,
    instance,
    onSent,
    onReplyFocus,
    cleanup: () => {
      if (!gone) unmount(instance);
      gone = true;
      target.remove();
    },
  };
}

const field = (t: HTMLElement) => t.querySelector<HTMLTextAreaElement>(".reply textarea");

function type(el: HTMLTextAreaElement, text: string): void {
  el.value = text;
  el.dispatchEvent(new Event("input", { bubbles: true }));
  flushSync();
}

function press(el: HTMLElement, key: string, init: KeyboardEventInit = {}): KeyboardEvent {
  const e = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...init });
  el.dispatchEvent(e);
  flushSync();
  return e;
}

beforeEach(() => localStorage.clear());
afterEach(() => setLang("en"));

describe("DockCard reply field", () => {
  it("appears only when the detail allows free text, with its placeholder", async () => {
    const yes = render(dockAgent(), transport().t);
    const no = render(dockAgent(), transport({ canText: false }).t);
    try {
      await vi.waitFor(() => expect(field(yes.target)).not.toBeNull());
      expect(field(yes.target)!.placeholder).toBe("Reply to agent…");
      expect(field(yes.target)!.maxLength).toBe(4000);
      await vi.waitFor(() => expect(no.target.querySelector(".opt")).not.toBeNull());
      expect(field(no.target)).toBeNull();
    } finally {
      yes.cleanup();
      no.cleanup();
    }
  });

  it("Enter sends the text, shows Sending… then Sent ✓ and clears the field", async () => {
    const tr = transport();
    const { target, onSent, cleanup } = render(dockAgent(), tr.t);
    try {
      await vi.waitFor(() => expect(field(target)).not.toBeNull());
      const el = field(target)!;
      type(el, "use the staging DB\nthen rerun");
      const release = tr.hold();
      const e = press(el, "Enter");
      expect(e.defaultPrevented).toBe(true);
      await vi.waitFor(() => expect(target.textContent).toContain("Sending…"));
      expect(el.readOnly).toBe(true);
      release();
      await vi.waitFor(() => expect(target.querySelector(".hint.ok")?.textContent).toBe("Sent ✓"));
      expect(tr.base.actCalls).toEqual([
        { action: "text", ref: { serverId: "prod", paneId: "p0" }, extra: { text: "use the staging DB\nthen rerun" } },
      ]);
      expect(el.value).toBe("");
      expect(el.readOnly).toBe(false);
      expect(onSent).toHaveBeenCalledTimes(1); // a blocked agent was answered
    } finally {
      cleanup();
    }
  });

  it("Shift+Enter, IME composition and a blank field never send", async () => {
    const tr = transport();
    const { target, cleanup } = render(dockAgent(), tr.t);
    try {
      await vi.waitFor(() => expect(field(target)).not.toBeNull());
      const el = field(target)!;
      press(el, "Enter"); // empty
      type(el, "   ");
      press(el, "Enter"); // blank
      type(el, "line one");
      expect(press(el, "Enter", { shiftKey: true }).defaultPrevented).toBe(false);
      press(el, "Enter", { isComposing: true });
      await Promise.resolve();
      expect(tr.base.actCalls).toHaveLength(0);
      expect(target.querySelector<HTMLButtonElement>("button.send")!.disabled).toBe(false);
    } finally {
      cleanup();
    }
  });

  it("an error keeps the text and explains it; typing clears the error", async () => {
    const tr = transport({ outcomes: [{ ok: false, code: "disconnected", message: "" }] });
    const { target, onSent, cleanup } = render(dockAgent(), tr.t);
    try {
      await vi.waitFor(() => expect(field(target)).not.toBeNull());
      const el = field(target)!;
      type(el, "hello");
      target.querySelector<HTMLButtonElement>("button.send")!.click();
      await vi.waitFor(() =>
        expect(target.querySelector(".hint.bad")?.textContent).toBe("The agent's server is offline."),
      );
      expect(el.value).toBe("hello");
      expect(onSent).not.toHaveBeenCalled();
      type(el, "hello!");
      expect(target.querySelector(".hint.bad")).toBeNull();
    } finally {
      cleanup();
    }
  });

  it("a reply to a working agent does not count as an answer", async () => {
    const tr = transport({ status: "working" });
    const { target, onSent, cleanup } = render(dockAgent({ status: "working" }), tr.t);
    try {
      await vi.waitFor(() => expect(field(target)).not.toBeNull());
      type(field(target)!, "also add tests");
      press(field(target)!, "Enter");
      await vi.waitFor(() => expect(target.querySelector(".hint.ok")).not.toBeNull());
      expect(onSent).not.toHaveBeenCalled();
    } finally {
      cleanup();
    }
  });

  it("focusReply puts the caret in the field and focus is reported", async () => {
    const { target, instance, onReplyFocus, cleanup } = render(dockAgent(), transport().t);
    try {
      await vi.waitFor(() => expect(field(target)).not.toBeNull());
      const card = instance as unknown as { focusReply(): boolean };
      expect(card.focusReply()).toBe(true);
      expect(document.activeElement).toBe(field(target));
      expect(onReplyFocus).toHaveBeenLastCalledWith(true);
      field(target)!.blur();
      expect(onReplyFocus).toHaveBeenLastCalledWith(false);
    } finally {
      cleanup();
    }
  });

  it("speaks Czech", async () => {
    setLang("cs");
    const { target, cleanup } = render(dockAgent(), transport().t);
    try {
      await vi.waitFor(() => expect(field(target)).not.toBeNull());
      expect(field(target)!.placeholder).toBe("Odpovědět agentovi…");
      expect(target.querySelector("button.send")!.getAttribute("title")).toBe("Odeslat (Enter)");
    } finally {
      cleanup();
    }
  });
});

describe("DockCard output preview", () => {
  it("opens a session, shows the last lines and closes it on unmount", async () => {
    const tr = transport();
    const { target, cleanup } = render(dockAgent(), tr.t);
    try {
      expect(tr.term.open).toBe(1);
      const lines = Array.from({ length: 12 }, (_, i) => `line ${i + 1}`).join("\r\n");
      await tr.frame(`\x1b[32m${lines}\x1b[0m`);
      await vi.waitFor(() => expect(target.querySelectorAll(".screen .ln")).toHaveLength(8));
      const shown = Array.from(target.querySelectorAll(".screen .ln")).map((n) => n.textContent);
      expect(shown).toEqual(["line 5", "line 6", "line 7", "line 8", "line 9", "line 10", "line 11", "line 12"]);
      expect(target.querySelector(".preview .state")?.textContent?.trim()).toBe("live");
    } finally {
      cleanup();
    }
    expect(tr.term.close).toEqual(["s1"]);
  });

  it("an unsupported bridge leaves no box, not even a collapsed one later", async () => {
    const agent = dockAgent({ serverId: "old-bridge" });
    const tr = transport({ open: { ok: false, outcome: { ok: false, code: "unsupported", message: "" } } });
    const { target, cleanup } = render(agent, tr.t);
    try {
      await vi.waitFor(() => expect(target.querySelector(".opt")).not.toBeNull());
      await Promise.resolve();
      flushSync();
      expect(target.querySelector(".preview")).toBeNull();
    } finally {
      cleanup();
    }
    expect(tr.term.close).toEqual([]);

    localStorage.setItem(PREVIEW_STORAGE_KEY, "1");
    const again = render(agent, transport().t);
    try {
      await vi.waitFor(() => expect(again.target.querySelector(".opt")).not.toBeNull());
      expect(again.target.querySelector(".preview")).toBeNull();
    } finally {
      again.cleanup();
    }
  });

  it("collapsing closes the session and is remembered; collapsed opens none", async () => {
    const tr = transport();
    const first = render(dockAgent(), tr.t);
    try {
      await tr.frame("hello");
      await vi.waitFor(() => expect(first.target.querySelector(".screen")).not.toBeNull());
      const bar = first.target.querySelector<HTMLButtonElement>(".preview .bar")!;
      expect(bar.getAttribute("aria-expanded")).toBe("true");
      bar.click();
      flushSync();
      expect(tr.term.close).toEqual(["s1"]);
      expect(first.target.querySelector(".screen")).toBeNull();
      expect(localStorage.getItem(PREVIEW_STORAGE_KEY)).toBe("1");
    } finally {
      first.cleanup();
    }

    const tr2 = transport();
    const second = render(dockAgent(), tr2.t);
    try {
      await vi.waitFor(() => expect(second.target.querySelector(".preview .bar")).not.toBeNull());
      expect(tr2.term.open).toBe(0);
      expect(second.target.querySelector(".preview .bar")!.getAttribute("aria-expanded")).toBe("false");
      second.target.querySelector<HTMLButtonElement>(".preview .bar")!.click();
      flushSync();
      expect(tr2.term.open).toBe(1);
      expect(localStorage.getItem(PREVIEW_STORAGE_KEY)).toBe("0");
    } finally {
      second.cleanup();
    }
  });
});
