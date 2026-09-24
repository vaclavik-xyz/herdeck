import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import AgentCard from "./AgentCard.svelte";
import DeckView from "./DeckView.svelte";
import type { DeckTransport } from "./deckClient";
import type {
  ActionOutcome,
  AgentAction,
  AgentDetail,
  AgentRef,
  AgentTarget,
  AgentTransport,
  DetailResult,
} from "./agentCardClient";
import { setLang } from "./i18n.svelte";

const PROMPT = "Bash command\n  rm -rf build\nDo you want to proceed?\n❯ 1. Yes\n  2. Yes, and don't ask again\n  3. No";

function detail(over: Partial<AgentDetail> = {}): AgentDetail {
  return {
    serverId: "prod",
    paneId: "p0",
    agentType: "claude",
    displayAgent: "",
    label: "herdeck",
    title: "Fix the build",
    repo: "herdeck",
    branch: "main",
    workspace: "ws",
    tab: "tab",
    status: "blocked",
    sinceS: 42,
    backend: "herdr",
    connected: true,
    prompt: PROMPT,
    promptPending: false,
    revision: "rev-1",
    options: [
      { key: "1", label: "Yes", id: "approve", kind: "option", confirm: false },
      { key: "2", label: "Yes, and don't ask again", id: "approve_always", kind: "option", confirm: false },
      { key: "3", label: "No", id: "deny", kind: "option", confirm: true },
    ],
    canStop: true,
    stopConfirm: true,
    canText: true,
    canFocus: true,
    subagents: [],
    ...over,
  };
}

type Call = { action: AgentAction; ref: AgentRef; extra: Record<string, string> };

function fakeTransport(opts: {
  detail?: DetailResult;
  outcome?: ActionOutcome;
} = {}): AgentTransport & { calls: Call[]; targets: AgentTarget[]; refreshes: boolean[] } {
  const calls: Call[] = [];
  const targets: AgentTarget[] = [];
  const refreshes: boolean[] = [];
  return {
    calls,
    targets,
    refreshes,
    detail: async (target, refresh = false) => {
      refreshes.push(refresh);
      targets.push(target);
      return opts.detail ?? { kind: "ok", detail: detail() };
    },
    act: async (action, ref, extra = {}) => {
      calls.push({ action, ref, extra });
      return opts.outcome ?? { ok: true, code: "sent", message: "" };
    },
    termOpen: async () => ({ ok: true, id: "s" }),
    termPoll: () => new Promise<never>(() => {}),
    termClose: async () => {},
  };
}

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
  flushSync();
}

function render(props: Record<string, unknown>) {
  const target = document.createElement("div");
  document.body.appendChild(target);
  const instance = mount(AgentCard, { target, props });
  flushSync();
  return { target, cleanup: () => { unmount(instance); target.remove(); } };
}

function button(target: HTMLElement, text: string): HTMLButtonElement {
  const found = [...target.querySelectorAll<HTMLButtonElement>("button")].find((b) =>
    b.textContent?.includes(text),
  );
  if (!found) throw new Error(`no button "${text}"`);
  return found;
}

describe("AgentCard", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    setLang("en");
  });
  afterEach(() => vi.useRealTimers());

  it("renders the full prompt, header and parsed options", async () => {
    const transport = fakeTransport();
    const { target, cleanup } = render({ transport, target: { index: 3 }, onClose: () => {} });
    try {
      await settle();
      // First load resolves the tile and asks for a fresh read.
      expect(transport.targets[0]).toEqual({ index: 3 });
      const pre = target.querySelector("pre.prompt");
      expect(pre?.textContent).toBe(PROMPT); // every line, not the deck's three
      expect(target.textContent).toContain("claude");
      expect(target.textContent).toContain("blocked");
      expect(target.textContent).toContain("42s");
      expect(target.textContent).toContain("herdeck · main");
      expect(target.textContent).toContain("ws › tab");
      const opts = [...target.querySelectorAll(".opt")].map((b) => b.textContent);
      expect(opts).toEqual(["1. Yes", "2. Yes, and don't ask again", "3. No"]);
      // Polls follow the agent by identity afterwards.
      await vi.advanceTimersByTimeAsync(1600);
      expect(transport.targets.at(-1)).toEqual({ serverId: "prod", paneId: "p0" });
    } finally { cleanup(); }
  });

  it("submits an option with the prompt revision it showed", async () => {
    const transport = fakeTransport();
    const { target, cleanup } = render({ transport, target: { index: 0 }, onClose: () => {} });
    try {
      await settle();
      button(target, "1. Yes").click();
      await settle();
      expect(transport.calls).toEqual([
        { action: "answer", ref: { serverId: "prod", paneId: "p0" }, extra: { key: "1", revision: "rev-1" } },
      ]);
      expect(target.querySelector("[role=status]")?.textContent).toBe("Sent.");
      // The answered prompt is spent: the card re-reads at once.
      expect(transport.refreshes).toEqual([true, true]);
    } finally { cleanup(); }
  });

  it("arms an option the deck would confirm, and sends on the second click", async () => {
    const transport = fakeTransport();
    const { target, cleanup } = render({ transport, target: { index: 0 }, onClose: () => {} });
    try {
      await settle();
      button(target, "3. No").click();
      await settle();
      expect(transport.calls).toEqual([]);
      const armed = target.querySelector<HTMLButtonElement>(".opt.armed");
      expect(armed?.textContent).toContain("Sure?");
      armed!.click();
      await settle();
      expect(transport.calls.map((c) => c.extra.key)).toEqual(["3"]);
    } finally { cleanup(); }
  });

  it("sends free text on Enter and clears the box", async () => {
    const transport = fakeTransport();
    const { target, cleanup } = render({ transport, target: { index: 0 }, onClose: () => {} });
    try {
      await settle();
      const box = target.querySelector("textarea")!;
      box.value = "use the staging db";
      box.dispatchEvent(new Event("input", { bubbles: true }));
      flushSync();
      // Shift+Enter is a newline, not a send.
      box.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", shiftKey: true, bubbles: true }));
      await settle();
      expect(transport.calls).toEqual([]);
      box.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
      await settle();
      expect(transport.calls).toEqual([
        { action: "text", ref: { serverId: "prod", paneId: "p0" }, extra: { text: "use the staging db" } },
      ]);
      expect(box.value).toBe("");
    } finally { cleanup(); }
  });

  it("surfaces a read-only bridge refusal instead of failing silently", async () => {
    const transport = fakeTransport({
      outcome: { ok: false, code: "readonly", message: "read-only token: 'act' is not allowed" },
    });
    const { target, cleanup } = render({ transport, target: { index: 0 }, onClose: () => {} });
    try {
      await settle();
      button(target, "1. Yes").click();
      await settle();
      const status = target.querySelector(".feedback");
      expect(status?.classList.contains("bad")).toBe(true);
      expect(status?.textContent).toContain("Read-only connection");
      expect(status?.textContent).toContain("'act' is not allowed");
    } finally { cleanup(); }
  });

  it("explains a stale prompt in Czech too", async () => {
    setLang("cs");
    const transport = fakeTransport({ outcome: { ok: false, code: "stale", message: "" } });
    const { target, cleanup } = render({ transport, target: { index: 0 }, onClose: () => {} });
    try {
      await settle();
      button(target, "1. Yes").click();
      await settle();
      expect(target.querySelector(".feedback")?.textContent).toContain("Dotaz se změnil");
      expect(target.querySelector("button.icon")?.getAttribute("title")).toBe("Zavřít kartu agenta");
    } finally { cleanup(); setLang("en"); }
  });

  it("stop needs a second click when the deck would confirm it", async () => {
    const transport = fakeTransport();
    const { target, cleanup } = render({ transport, target: { index: 0 }, onClose: () => {} });
    try {
      await settle();
      button(target, "Stop").click();
      await settle();
      expect(transport.calls).toEqual([]);
      button(target, "Sure?").click();
      await settle();
      expect(transport.calls.map((c) => c.action)).toEqual(["stop"]);
      button(target, "Focus").click();
      await settle();
      expect(transport.calls.map((c) => c.action)).toEqual(["stop", "focus"]);
    } finally { cleanup(); }
  });

  const SUBS: AgentDetail["subagents"] = [
    { id: "c", provider: "claude", type: "Plan", description: "nested plan", model: "", depth: 2, status: "running", durationS: 65 },
    { id: "b", provider: "claude", type: "Explore", description: "find callers", model: "", depth: 1, status: "failed", durationS: 30 },
    { id: "a", provider: "codex", type: "", description: "", model: "", depth: null, status: "done", durationS: 4000 },
    { id: "s", provider: "claude", type: "general", description: "silent", model: "", depth: 1, status: "stale", durationS: 900 },
  ];

  it("lists subagents most recent first with status, depth and durations", async () => {
    const transport = fakeTransport({ detail: { kind: "ok", detail: detail({ subagents: SUBS }) } });
    const { target, cleanup } = render({ transport, target: { index: 0 }, onClose: () => {} });
    try {
      await settle();
      expect(target.textContent).toContain("Subagents");
      const rows = [...target.querySelectorAll<HTMLElement>("li.sub")];
      expect(rows.map((r) => r.querySelector(".sub-type")?.textContent)).toEqual(["Plan", "Explore", "codex", "general"]);
      expect(rows.map((r) => [...r.classList].find((c) => c.startsWith("sub-")))).toEqual([
        "sub-running", "sub-failed", "sub-done", "sub-stale",
      ]);
      expect(rows.map((r) => r.querySelector(".sub-state")?.textContent)).toEqual(["running", "failed", "done", "no signal"]);
      expect(rows[0].style.getPropertyValue("--indent")).toBe("1");
      expect(rows[1].style.getPropertyValue("--indent")).toBe("0");
      expect(rows[0].querySelector(".sub-desc")?.textContent).toBe("nested plan");
      expect(rows[2].querySelector(".sub-desc")).toBeNull();
      const time = (i: number) => rows[i].querySelector(".sub-time");
      expect(time(0)?.textContent).toBe("1m 05s");
      expect(time(0)?.getAttribute("title")).toBe("Running for 1m 05s");
      expect(time(1)?.getAttribute("title")).toBe("Took 30s");
      expect(time(2)?.textContent).toBe("1h 06m");
      // A running duration ticks between polls; finished ones stay put.
      await vi.advanceTimersByTimeAsync(1000);
      flushSync();
      expect(time(0)?.textContent).toBe("1m 06s");
      expect(time(1)?.textContent).toBe("30s");
    } finally { cleanup(); }
  });

  it("hides the subagent section when there are none", async () => {
    const transport = fakeTransport();
    const { target, cleanup } = render({ transport, target: { index: 0 }, onClose: () => {} });
    try {
      await settle();
      expect(target.querySelector("ul.subagents")).toBeNull();
      expect(target.textContent).not.toContain("Subagents");
    } finally { cleanup(); }
  });

  it("labels subagents in Czech", async () => {
    setLang("cs");
    const transport = fakeTransport({ detail: { kind: "ok", detail: detail({ subagents: SUBS }) } });
    const { target, cleanup } = render({ transport, target: { index: 0 }, onClose: () => {} });
    try {
      await settle();
      expect(target.textContent).toContain("Subagenti");
      const states = [...target.querySelectorAll(".sub-state")].map((e) => e.textContent);
      expect(states).toEqual(["běží", "selhal", "hotovo", "bez signálu"]);
      expect(target.querySelector("li.sub-done .sub-time")?.getAttribute("title")).toBe("Trval 1h 06m");
    } finally { cleanup(); setLang("en"); }
  });

  it("says so when the tile has no agent", async () => {
    const transport = fakeTransport({ detail: { kind: "gone" } });
    const { target, cleanup } = render({ transport, target: { index: 9 }, onClose: () => {} });
    try {
      await settle();
      expect(target.textContent).toContain("No agent here anymore");
      expect(target.querySelector(".opt")).toBeNull();
    } finally { cleanup(); }
  });

  it("closes from the icon button (titled) and Escape, and stops polling", async () => {
    const onClose = vi.fn();
    const transport = fakeTransport();
    const { target, cleanup } = render({ transport, target: { index: 0 }, onClose });
    await settle();
    const close = target.querySelector<HTMLButtonElement>("button.icon")!;
    expect(close.getAttribute("title")).toBe("Close agent card");
    close.click();
    target
      .querySelector(".agent-card")!
      .dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(onClose).toHaveBeenCalledTimes(2);
    cleanup();
    const polled = transport.targets.length;
    await vi.advanceTimersByTimeAsync(5000);
    expect(transport.targets.length).toBe(polled);
  });
});

describe("DeckView opens the agent card", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  function deck(): DeckTransport & { presses: number[] } {
    const presses: number[] = [];
    return {
      presses,
      fetchState: async () => ({
        version: 1, slots: 13, has_panel: false, panel: 0, tiles: {}, summary: {},
        source: "live", connected: true, language: "en",
      }),
      tileImage: async () => null,
      panelImage: async () => null,
      press: async (i: number) => {
        presses.push(i);
        return { ok: true, status: 204, forbidden: false };
      },
    };
  }

  function mountDeck(props: Record<string, unknown>) {
    const target = document.createElement("div");
    document.body.appendChild(target);
    const instance = mount(DeckView, { target, props });
    flushSync();
    return { target, cleanup: () => { unmount(instance); target.remove(); } };
  }

  it("on Option-click (without pressing the tile) and a plain click still presses", async () => {
    const transport = deck();
    const agentTransport = fakeTransport();
    const { target, cleanup } = mountDeck({ transport, agentTransport, compact: true });
    try {
      const cells = target.querySelectorAll<HTMLButtonElement>(".cell");
      cells[2].dispatchEvent(new MouseEvent("click", { altKey: true, bubbles: true }));
      await settle();
      expect(transport.presses).toEqual([]);
      expect(target.querySelector(".agent-card")).not.toBeNull();
      expect(agentTransport.targets[0]).toEqual({ index: 2 });
      cells[4].click();
      await settle();
      expect(transport.presses).toEqual([4]);
    } finally { cleanup(); }
  });

  it("on a long press, swallowing the trailing click", async () => {
    const transport = deck();
    const agentTransport = fakeTransport();
    const { target, cleanup } = mountDeck({ transport, agentTransport, compact: true });
    try {
      const cell = target.querySelectorAll<HTMLButtonElement>(".cell")[1];
      cell.dispatchEvent(new PointerEvent("pointerdown", { button: 0, bubbles: true }));
      await vi.advanceTimersByTimeAsync(600);
      cell.dispatchEvent(new PointerEvent("pointerup", { bubbles: true }));
      cell.click();
      await settle();
      expect(transport.presses).toEqual([]);
      expect(target.querySelector(".agent-card")).not.toBeNull();
    } finally { cleanup(); }
  });

  it("never in the config preview (jump mode)", async () => {
    const transport = deck();
    const agentTransport = fakeTransport();
    const { target, cleanup } = mountDeck({ transport, agentTransport, onJump: () => {} });
    try {
      target.querySelectorAll<HTMLButtonElement>(".cell")[2]
        .dispatchEvent(new MouseEvent("click", { altKey: true, bubbles: true }));
      await settle();
      expect(target.querySelector(".agent-card")).toBeNull();
    } finally { cleanup(); }
  });
});
