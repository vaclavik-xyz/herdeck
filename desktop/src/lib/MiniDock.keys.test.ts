// The dock's keyboard answer flow (Task 5): the next-blocked hotkey opens the
// longest-blocked card, the card takes 1–9 / Tab / T / O / Esc, keys stay out
// of the reply field, and the focus is handed back afterwards.
import { afterEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import MiniDock from "./MiniDock.svelte";
import { setLang } from "./i18n.svelte";
import { agentDetail, dockAgent, dockList, fakeAgentTransport, fakeDockTransport } from "./dockFixtures";
import type { DockList } from "./dockClient";

function render(first: DockList | null, extra: Record<string, unknown> = {}) {
  const dock = fakeDockTransport(first);
  const agents = (extra.agentTransport as ReturnType<typeof fakeAgentTransport>) ?? fakeAgentTransport();
  const releaseFocus = vi.fn();
  const target = document.createElement("div");
  document.body.appendChild(target);
  const instance = mount(MiniDock, {
    target,
    props: {
      transport: dock,
      agentTransport: agents,
      windowLayout: async () => ({ side: "right" as const, stripX: 0, stripY: 0 }),
      releaseFocus,
      herdrSettleMs: 0,
      ...extra,
    },
  }) as { openNextBlocked: () => void };
  flushSync();
  return {
    target,
    dock,
    agents,
    releaseFocus,
    next: () => instance.openNextBlocked(),
    cleanup: () => {
      unmount(instance);
      target.remove();
    },
  };
}

const FLEET = dockList([
  dockAgent({ paneId: "w", title: "Working one", status: "working", sinceS: 10, repo: "r-w" }),
  dockAgent({ paneId: "b1", title: "Blocked short", status: "blocked", sinceS: 60, repo: "r-b1" }),
  dockAgent({ paneId: "b2", title: "Blocked long", status: "blocked", sinceS: 300, repo: "r-b2" }),
]);

const items = (t: HTMLElement) => Array.from(t.querySelectorAll<HTMLButtonElement>("button.item"));
const cardTitle = (t: HTMLElement) => t.querySelector(".card .title")?.textContent;
const press = (key: string, init: KeyboardEventInit = {}, on: EventTarget = window) => {
  const e = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...init });
  on.dispatchEvent(e);
  return e;
};

async function openedByHotkey(first: DockList = FLEET, extra: Record<string, unknown> = {}) {
  const r = render(first, extra);
  await vi.waitFor(() => expect(items(r.target).length).toBeGreaterThan(0));
  r.next();
  await vi.waitFor(() => expect(r.target.querySelector(".card")).not.toBeNull());
  return r;
}

afterEach(() => setLang("en"));

describe("MiniDock next-blocked hotkey", () => {
  it("opens the longest-blocked agent's card with the key legend", async () => {
    const r = await openedByHotkey();
    try {
      expect(cardTitle(r.target)).toBe("Blocked long");
      await vi.waitFor(() => expect(r.target.querySelector(".keys")?.textContent).toBe("1–9 answer · Tab next · T reply · Esc"));
    } finally {
      r.cleanup();
    }
  });

  it("again from a hotkey card moves to the next blocked agent", async () => {
    const r = await openedByHotkey();
    try {
      r.next();
      await vi.waitFor(() => expect(cardTitle(r.target)).toBe("Blocked short"));
    } finally {
      r.cleanup();
    }
  });

  it("waits for the first list when it arrives before one", async () => {
    const r = render(FLEET); // its first answer is still in flight
    try {
      expect(items(r.target)).toHaveLength(0);
      r.next();
      await vi.waitFor(() => expect(cardTitle(r.target)).toBe("Blocked long"));
    } finally {
      r.cleanup();
    }
  });

  it("says nobody is waiting, then hides and hands the focus back", async () => {
    const calm = dockList([dockAgent({ paneId: "w", status: "working", repo: "r-w" })]);
    const r = render(calm, { toastMs: 250 });
    try {
      await vi.waitFor(() => expect(items(r.target)).toHaveLength(1));
      r.next();
      await vi.waitFor(() => expect(r.target.querySelector(".toast")?.textContent).toBe("Nobody is waiting"));
      expect(r.target.querySelector(".card")).toBeNull();
      await vi.waitFor(() => expect(r.target.querySelector(".toast")).toBeNull());
      expect(r.releaseFocus).toHaveBeenCalledTimes(1);
    } finally {
      r.cleanup();
    }
  });

  it("the toast is localized", async () => {
    setLang("cs");
    const calm = dockList([dockAgent({ paneId: "w", status: "working", repo: "r-w" })], { language: "cs" });
    const r = render(calm);
    try {
      await vi.waitFor(() => expect(items(r.target)).toHaveLength(1));
      r.next();
      await vi.waitFor(() => expect(r.target.querySelector(".toast")?.textContent).toBe("Nikdo nečeká"));
    } finally {
      r.cleanup();
    }
  });
});

describe("MiniDock card keys", () => {
  it("a digit answers that option with the prompt's revision, then releases the focus", async () => {
    const r = await openedByHotkey();
    try {
      await vi.waitFor(() => expect(r.target.querySelectorAll(".opt")).toHaveLength(3));
      expect(press("2").defaultPrevented).toBe(true);
      await vi.waitFor(() => expect(r.agents.actCalls).toHaveLength(1));
      expect(r.agents.actCalls[0]).toMatchObject({
        action: "answer",
        ref: { serverId: "prod", paneId: "b2" },
        extra: { key: "2", revision: "rev-1" },
      });
      await vi.waitFor(() => expect(r.releaseFocus).toHaveBeenCalledTimes(1));
    } finally {
      r.cleanup();
    }
  });

  it("the physical digit works on a Czech layout", async () => {
    const r = await openedByHotkey();
    try {
      await vi.waitFor(() => expect(r.target.querySelectorAll(".opt")).toHaveLength(3));
      press("ě", { code: "Digit2" });
      await vi.waitFor(() => expect(r.agents.actCalls[0]?.extra.key).toBe("2"));
    } finally {
      r.cleanup();
    }
  });

  it("a digit with no such option does nothing", async () => {
    const r = await openedByHotkey();
    try {
      await vi.waitFor(() => expect(r.target.querySelectorAll(".opt")).toHaveLength(3));
      expect(press("7").defaultPrevented).toBe(false);
      await new Promise((res) => setTimeout(res, 10));
      expect(r.agents.actCalls).toHaveLength(0);
    } finally {
      r.cleanup();
    }
  });

  it("Tab / Shift+Tab move between the blocked agents", async () => {
    const r = await openedByHotkey();
    try {
      expect(press("Tab").defaultPrevented).toBe(true);
      await vi.waitFor(() => expect(cardTitle(r.target)).toBe("Blocked short"));
      press("Tab", { shiftKey: true });
      await vi.waitFor(() => expect(cardTitle(r.target)).toBe("Blocked long"));
    } finally {
      r.cleanup();
    }
  });

  it("Tab keeps moving focus in a card opened by the mouse", async () => {
    const r = render(FLEET);
    try {
      await vi.waitFor(() => expect(items(r.target).length).toBe(3));
      items(r.target)[0].click(); // the strip's first item: Blocked long
      await vi.waitFor(() => expect(r.target.querySelector(".card")).not.toBeNull());
      expect(r.target.querySelector(".keys")).toBeNull();
      expect(press("Tab").defaultPrevented).toBe(false);
    } finally {
      r.cleanup();
    }
  });

  it("Esc closes the card and releases the focus", async () => {
    const r = await openedByHotkey();
    try {
      press("Escape");
      await vi.waitFor(() => expect(r.target.querySelector(".card")).toBeNull());
      expect(r.releaseFocus).toHaveBeenCalledTimes(1);
    } finally {
      r.cleanup();
    }
  });

  it("O opens in herdr and releases the focus only after herdr had its turn", async () => {
    const r = await openedByHotkey(FLEET, { herdrSettleMs: 300 });
    try {
      await vi.waitFor(() => expect(r.target.querySelector("button.focus")).not.toBeNull());
      press("o");
      await vi.waitFor(() => expect(r.target.querySelector(".card")).toBeNull(), { interval: 5 });
      expect(r.agents.actCalls[0]?.action).toBe("focus");
      expect(r.releaseFocus).not.toHaveBeenCalled();
      await vi.waitFor(() => expect(r.releaseFocus).toHaveBeenCalledTimes(1));
    } finally {
      r.cleanup();
    }
  });

  it("T focuses the reply field; there digits type, Esc only blurs, Enter sends", async () => {
    const agents = fakeAgentTransport({ details: [{ kind: "ok", detail: agentDetail({ paneId: "b2" }) }] });
    const r = await openedByHotkey(FLEET, { agentTransport: agents });
    try {
      await vi.waitFor(() => expect(r.target.querySelector("textarea")).not.toBeNull());
      const field = r.target.querySelector("textarea")!;
      expect(press("t").defaultPrevented).toBe(true);
      expect(document.activeElement).toBe(field);

      // A digit inside the field is text, not an answer.
      expect(press("1", {}, field).defaultPrevented).toBe(false);
      await new Promise((res) => setTimeout(res, 10));
      expect(agents.actCalls).toHaveLength(0);

      // Esc leaves the field but keeps the card.
      press("Escape", {}, field);
      flushSync();
      expect(document.activeElement).not.toBe(field);
      expect(r.target.querySelector(".card")).not.toBeNull();
      expect(r.releaseFocus).not.toHaveBeenCalled();

      // Enter sends the typed reply.
      field.focus();
      field.value = "use the staging db";
      field.dispatchEvent(new Event("input", { bubbles: true }));
      press("Enter", {}, field);
      await vi.waitFor(() => expect(agents.actCalls[0]).toMatchObject({ action: "text", extra: { text: "use the staging db" } }));
    } finally {
      r.cleanup();
    }
  });
});
