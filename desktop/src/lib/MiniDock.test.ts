import { afterEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import MiniDock from "./MiniDock.svelte";
import { setLang } from "./i18n.svelte";
import { DOCK_POLL_MS } from "./dockClient";
import { CARD_WIDTH, STRIP_WIDTH, type DockLayoutRequest } from "./dockModel";
import { dockAgent, dockList, fakeAgentTransport, fakeDockTransport } from "./dockFixtures";
import type { DockList } from "./dockClient";
import { reactiveProps } from "./testProps.svelte";

function render(first: DockList | null, extra: Record<string, unknown> = {}) {
  const dock = fakeDockTransport(first);
  const agents = fakeAgentTransport();
  const layouts: DockLayoutRequest[] = [];
  const windowLayout = vi.fn(async (req: DockLayoutRequest) => {
    layouts.push(req);
    return { side: "right" as const, stripX: 0, stripY: 0 };
  });
  const onOpenSettings = vi.fn();
  const target = document.createElement("div");
  document.body.appendChild(target);
  // Reactive, so a test can change a prop (e.g. prefs) after mount.
  const props = reactiveProps({ transport: dock, agentTransport: agents, windowLayout, onOpenSettings, ...extra });
  const instance = mount(MiniDock, { target, props });
  flushSync();
  return {
    target,
    props,
    dock,
    agents,
    layouts,
    onOpenSettings,
    cleanup: () => {
      unmount(instance);
      target.remove();
    },
  };
}

const items = (t: HTMLElement) => Array.from(t.querySelectorAll<HTMLButtonElement>("button.item"));
const hover = (el: Element) => el.dispatchEvent(new MouseEvent("mouseenter"));

// One project per agent: the flat strip (project groups have their own tests).
const FLEET = dockList(
  [
    dockAgent({ paneId: "w", title: "Working one", status: "working", sinceS: 10, repo: "r-w" }),
    dockAgent({ paneId: "b1", title: "Blocked short", status: "blocked", sinceS: 60, repo: "r-b1" }),
    dockAgent({ paneId: "b2", title: "Blocked long", status: "blocked", sinceS: 300, repo: "r-b2" }),
    dockAgent({ paneId: "d", title: "Done one", status: "done", sinceS: 5, repo: "r-d" }),
    dockAgent({ paneId: "i1", title: "Idle A", status: "idle", sinceS: 9, repo: "r-i1" }),
    dockAgent({ paneId: "i2", title: "Idle B", status: "idle", sinceS: 9, repo: "r-i2" }),
  ],
  { version: 7 },
);

afterEach(() => setLang("en"));

describe("MiniDock icon fallback", () => {
  const bare = [dockAgent({ paneId: "a", status: "working", icon: null, agentType: "codex" })];
  it("shows the agent mark for an agent without a favicon when the fallback is agent", async () => {
    const { target, cleanup } = render(dockList(bare, { iconFallback: "agent" }));
    try {
      await vi.waitFor(() => expect(items(target)).toHaveLength(1));
      expect(target.querySelector("button.item img")?.getAttribute("src")).toBe(
        "herdeck://localhost/agent-mark/codex",
      );
    } finally {
      cleanup();
    }
  });
  it("keeps the monogram when the fallback is monogram", async () => {
    const { target, cleanup } = render(dockList(bare));
    try {
      await vi.waitFor(() => expect(items(target)).toHaveLength(1));
      expect(target.querySelector("button.item img")).toBeNull();
    } finally {
      cleanup();
    }
  });
});

describe("MiniDock strip", () => {
  it("shows non-idle agents in order and folds idle ones into +N", async () => {
    const { target, cleanup } = render(FLEET);
    try {
      await vi.waitFor(() => expect(items(target)).toHaveLength(4));
      expect(items(target).map((b) => b.getAttribute("aria-label"))).toEqual([
        "Blocked long · Needs you",
        "Blocked short · Needs you",
        "Done one · Done",
        "Working one · Working",
      ]);
      const chip = target.querySelector<HTMLButtonElement>("button.idle-chip")!;
      expect(chip.textContent?.trim()).toBe("+2");
      expect(chip.title).toBe("2 idle agents");
      expect(target.querySelector(".grip")?.getAttribute("title")).toBe("Drag to move the dock");
    } finally {
      cleanup();
    }
  });

  it("polls plainly first, then long-polls after the version it has", async () => {
    const { dock, cleanup } = render(FLEET);
    try {
      await vi.waitFor(() => expect(dock.calls).toHaveLength(2));
      expect(dock.calls[0].after).toBeNull();
      expect(dock.calls[1]).toEqual({ after: 7, waitMs: DOCK_POLL_MS });
    } finally {
      cleanup();
    }
  });

  it("asks for a window scaled by its zoom and lays the strip out in unzoomed px", async () => {
    const { target, layouts, cleanup } = render(FLEET, { scale: 1.2 });
    try {
      await vi.waitFor(() => expect(layouts.length).toBeGreaterThan(0));
      expect(layouts[0].stripWidth).toBeCloseTo(STRIP_WIDTH * 1.2);
      expect(layouts[0].cardWidth).toBe(0);
      const root = target.querySelector<HTMLElement>(".mini-dock")!;
      expect(root.style.zoom).toBe("1.2");
    } finally {
      cleanup();
    }
  });

  it("starts collapsed to the strip", async () => {
    const { layouts, cleanup } = render(FLEET);
    try {
      await vi.waitFor(() => expect(layouts.length).toBeGreaterThan(0));
      expect(layouts[0]).toMatchObject({ stripWidth: STRIP_WIDTH, cardWidth: 0, cardHeight: 0 });
    } finally {
      cleanup();
    }
  });

  it("follows the runtime's language", async () => {
    const { target, cleanup } = render(dockList([dockAgent({ title: "X" })], { language: "cs" }));
    try {
      await vi.waitFor(() => expect(items(target)[0]?.getAttribute("aria-label")).toBe("X · Potřebuje tě"));
    } finally {
      cleanup();
    }
  });

  it("shows one neutral item for an empty fleet and opens Settings on click", async () => {
    const { target, onOpenSettings, cleanup } = render(dockList([]));
    try {
      const neutral = await vi.waitFor(() => {
        const b = target.querySelector<HTMLButtonElement>("button.neutral");
        expect(b?.title).toBe("No active agents");
        return b!;
      });
      expect(items(target)).toHaveLength(0);
      neutral.click();
      expect(onOpenSettings).toHaveBeenCalledTimes(1);
    } finally {
      cleanup();
    }
  });

  it("is never empty while the runtime is unreachable", async () => {
    const { target, cleanup } = render(null);
    try {
      await vi.waitFor(() =>
        expect(target.querySelector("button.neutral")?.getAttribute("title")).toBe("Connecting to the runtime…"),
      );
      // "Connecting" shows before the first poll resolves; stale follows it.
      await vi.waitFor(() => expect(target.querySelector(".mini-dock")?.classList.contains("stale")).toBe(true));
    } finally {
      cleanup();
    }
  });

  it("keeps the last agents and dims the strip when a poll fails", async () => {
    const { target, dock, cleanup } = render(FLEET);
    try {
      await vi.waitFor(() => expect(dock.calls).toHaveLength(2));
      dock.push(null);
      await vi.waitFor(() => expect(target.querySelector(".mini-dock")?.classList.contains("stale")).toBe(true));
      expect(items(target)).toHaveLength(4);
      expect(target.querySelector(".offline-dot")?.getAttribute("title")).toBe(
        "Lost the runtime — showing the last known agents",
      );
    } finally {
      cleanup();
    }
  });

  it("turns animation off for reduced motion", async () => {
    const { target, cleanup } = render(FLEET, { reducedMotion: true });
    try {
      expect(target.querySelector(".mini-dock")?.classList.contains("reduced")).toBe(true);
    } finally {
      cleanup();
    }
  });
});

describe("MiniDock card", () => {
  it("opens a card on hover after the intent delay and grows the window", async () => {
    const { target, layouts, cleanup } = render(FLEET);
    try {
      await vi.waitFor(() => expect(items(target)).toHaveLength(4));
      hover(items(target)[3]);
      expect(target.querySelector(".card-slot")).toBeNull(); // not before the delay
      await vi.waitFor(() => expect(target.querySelector(".card-slot .title")?.textContent).toBe("Working one"));
      expect(layouts.at(-1)).toMatchObject({ cardWidth: CARD_WIDTH });
    } finally {
      cleanup();
    }
  });

  it("closes on Esc and shrinks the window back", async () => {
    const { target, layouts, cleanup } = render(FLEET);
    try {
      await vi.waitFor(() => expect(items(target)).toHaveLength(4));
      items(target)[0].click(); // pin
      await vi.waitFor(() => expect(target.querySelector(".card-slot")).not.toBeNull());
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
      await vi.waitFor(() => expect(target.querySelector(".card-slot")).toBeNull());
      expect(layouts.at(-1)).toMatchObject({ cardWidth: 0, cardHeight: 0 });
    } finally {
      cleanup();
    }
  });

  it("closes an unpinned card after the pointer leaves, keeps a pinned one", async () => {
    const { target, cleanup } = render(FLEET, { hoverCloseMs: 30 });
    try {
      await vi.waitFor(() => expect(items(target)).toHaveLength(4));
      const first = items(target)[0];
      hover(first);
      await vi.waitFor(() => expect(target.querySelector(".card-slot")).not.toBeNull());
      first.dispatchEvent(new MouseEvent("mouseleave"));
      await vi.waitFor(() => expect(target.querySelector(".card-slot")).toBeNull());

      first.click();
      await vi.waitFor(() => expect(target.querySelector(".card-slot")).not.toBeNull());
      first.dispatchEvent(new MouseEvent("mouseleave"));
      await new Promise((r) => setTimeout(r, 80));
      expect(target.querySelector(".card-slot")).not.toBeNull();
    } finally {
      cleanup();
    }
  });

  it("answers a blocked agent, then closes once it leaves blocked", async () => {
    const { target, dock, agents, cleanup } = render(FLEET);
    try {
      await vi.waitFor(() => expect(items(target)).toHaveLength(4));
      items(target)[0].click();
      const yes = await vi.waitFor(() => {
        const b = target.querySelector<HTMLButtonElement>("button.opt.tone-approve");
        expect(b).not.toBeNull();
        return b!;
      });
      yes.click();
      await vi.waitFor(() => expect(target.textContent).toContain("Sent ✓"));
      expect(agents.actCalls[0]).toEqual({
        action: "answer",
        ref: { serverId: "prod", paneId: "b2" },
        extra: { key: "1", revision: "rev-1" },
      });
      await vi.waitFor(() => expect(dock.calls).toHaveLength(2));
      dock.push({
        ...FLEET,
        version: 8,
        agents: FLEET.agents.map((a) => (a.paneId === "b2" ? { ...a, status: "working", sinceS: 0 } : a)),
      });
      await vi.waitFor(() => expect(target.querySelector(".card-slot")).toBeNull());
    } finally {
      cleanup();
    }
  });

  it("closes the card of an agent that left the fleet", async () => {
    const { target, dock, cleanup } = render(FLEET);
    try {
      await vi.waitFor(() => expect(items(target)).toHaveLength(4));
      items(target)[3].click();
      await vi.waitFor(() => expect(target.querySelector(".card-slot")).not.toBeNull());
      await vi.waitFor(() => expect(dock.calls).toHaveLength(2));
      dock.push({ ...FLEET, version: 9, agents: FLEET.agents.filter((a) => a.paneId !== "w") });
      await vi.waitFor(() => expect(target.querySelector(".card-slot")).toBeNull());
    } finally {
      cleanup();
    }
  });

  it("closes the card after the pointer leaves even if the hovered item folded away", async () => {
    const { target, dock, cleanup } = render(FLEET, { hoverCloseMs: 30 });
    try {
      await vi.waitFor(() => expect(items(target)).toHaveLength(4));
      hover(items(target)[3]);
      await vi.waitFor(() => expect(target.querySelector(".card-slot")).not.toBeNull());
      await vi.waitFor(() => expect(dock.calls).toHaveLength(2));
      dock.push({
        ...FLEET,
        version: 8,
        agents: FLEET.agents.map((a) => (a.paneId === "w" ? { ...a, status: "idle" } : a)),
      });
      await vi.waitFor(() => expect(items(target)).toHaveLength(3));
      target.querySelector(".card-slot")!.dispatchEvent(new MouseEvent("mouseenter"));
      target.querySelector(".card-slot")!.dispatchEvent(new MouseEvent("mouseleave"));
      await vi.waitFor(() => expect(target.querySelector(".card-slot")).toBeNull());
    } finally {
      cleanup();
    }
  });

  it("opens the idle list from the +N chip", async () => {
    const { target, cleanup } = render(FLEET);
    try {
      await vi.waitFor(() => expect(target.querySelector("button.idle-chip")).not.toBeNull());
      target.querySelector<HTMLButtonElement>("button.idle-chip")!.click();
      await vi.waitFor(() => expect(target.querySelector(".card-slot .title")?.textContent).toBe("Idle agents (2)"));
      expect(Array.from(target.querySelectorAll(".row-title")).map((e) => e.textContent)).toEqual(["Idle A", "Idle B"]);
    } finally {
      cleanup();
    }
  });
});

// --- project groups and dock_show ([desktop].dock_group / dock_show) --------

const PROJECT = dockList(
  [
    dockAgent({ paneId: "b", title: "Asks", status: "blocked", sinceS: 90, repo: "herdeck" }),
    dockAgent({ paneId: "w", title: "Builds", status: "working", sinceS: 30, repo: "herdeck" }),
    dockAgent({ paneId: "d", title: "Finished", status: "done", sinceS: 20, repo: "herdeck" }),
    dockAgent({ paneId: "x", title: "Elsewhere", status: "working", sinceS: 10, repo: "crm" }),
    dockAgent({ paneId: "i", title: "Resting", status: "idle", sinceS: 5, repo: "crm" }),
  ],
  { version: 3 },
);

const heads = (t: HTMLElement) => Array.from(t.querySelectorAll<HTMLButtonElement>(".group > button.item"));
const box = (t: HTMLElement) => t.querySelector<HTMLElement>("[data-group-members]")!;
const isOpen = (t: HTMLElement) => box(t).dataset.open === "true";
const members = (t: HTMLElement) => Array.from(box(t).querySelectorAll<HTMLButtonElement>("button.item"));
const group = (t: HTMLElement) => t.querySelector<HTMLElement>(".group")!;

describe("MiniDock project groups", () => {
  it("folds a project's non-blocked agents into one item with a count; blocked stays alone on top", async () => {
    const { target, cleanup } = render(PROJECT);
    try {
      await vi.waitFor(() => expect(heads(target)).toHaveLength(1));
      const strip = Array.from(target.querySelectorAll<HTMLButtonElement>("nav > button.item, .group > button.item"));
      expect(strip.map((b) => b.getAttribute("aria-label"))).toEqual([
        "Asks · Needs you",
        "herdeck · 2 agents · Done",
        "Elsewhere · Working",
      ]);
      const head = heads(target)[0];
      expect(head.querySelector(".count")?.textContent).toBe("2");
      expect(head.classList.contains("st-done")).toBe(true);
      expect(head.getAttribute("aria-expanded")).toBe("false");
      expect(head.hasAttribute("title")).toBe(false);
      // Folded members are out of reach until the group opens.
      expect(box(target).inert).toBe(true);
      expect(members(target).map((b) => b.getAttribute("aria-label"))).toEqual([
        "Finished · Done",
        "Builds · Working",
      ]);
    } finally {
      cleanup();
    }
  });

  it("keeps the flat strip with dock_group = false", async () => {
    const { target, cleanup } = render(PROJECT, { prefs: { group: false, show: "active", autohide: false } });
    try {
      await vi.waitFor(() => expect(items(target)).toHaveLength(4));
      expect(heads(target)).toHaveLength(0);
    } finally {
      cleanup();
    }
  });

  it("applies new prefs live", async () => {
    const { target, props, cleanup } = render(PROJECT);
    try {
      await vi.waitFor(() => expect(heads(target)).toHaveLength(1));
      (props as Record<string, unknown>).prefs = { group: false, show: "active", autohide: false };
      flushSync();
      expect(heads(target)).toHaveLength(0);
      expect(items(target)).toHaveLength(4);
    } finally {
      cleanup();
    }
  });

  it("expands on hover after the intent delay and folds back after the pointer left", async () => {
    vi.useFakeTimers();
    const { target, cleanup } = render(PROJECT, { hoverOpenMs: 120, groupCollapseMs: 400 });
    try {
      await vi.advanceTimersByTimeAsync(0);
      expect(heads(target)).toHaveLength(1);
      hover(group(target));
      await vi.advanceTimersByTimeAsync(100);
      expect(isOpen(target)).toBe(false); // still inside the intent delay
      await vi.advanceTimersByTimeAsync(40);
      expect(isOpen(target)).toBe(true);
      expect(heads(target)[0].getAttribute("aria-expanded")).toBe("true");
      expect(box(target).inert).toBe(false);
      expect(group(target).classList.contains("expanded")).toBe(true);
      // Members are staggered 40 ms apart.
      expect(Array.from(box(target).querySelectorAll<HTMLElement>(".member")).map((e) => e.style.getPropertyValue("--i"))).toEqual(["0", "1"]);

      group(target).dispatchEvent(new MouseEvent("mouseleave"));
      await vi.advanceTimersByTimeAsync(380);
      expect(isOpen(target)).toBe(true); // not before 400 ms
      // Coming back in time keeps it open.
      hover(group(target));
      await vi.advanceTimersByTimeAsync(500);
      expect(isOpen(target)).toBe(true);
      group(target).dispatchEvent(new MouseEvent("mouseleave"));
      await vi.advanceTimersByTimeAsync(400);
      expect(isOpen(target)).toBe(false);
    } finally {
      cleanup();
      vi.useRealTimers();
    }
  });

  it("expands at once on click, folds on Esc", async () => {
    const { target, cleanup } = render(PROJECT);
    try {
      await vi.waitFor(() => expect(heads(target)).toHaveLength(1));
      heads(target)[0].click();
      flushSync();
      expect(isOpen(target)).toBe(true);
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
      flushSync();
      expect(isOpen(target)).toBe(false);
    } finally {
      cleanup();
    }
  });

  it("a member opens its own card, and the group stays open while that card is", async () => {
    const { target, cleanup } = render(PROJECT, { groupCollapseMs: 30, hoverCloseMs: 30 });
    try {
      await vi.waitFor(() => expect(heads(target)).toHaveLength(1));
      heads(target)[0].click();
      flushSync();
      const builds = members(target)[1];
      builds.click(); // pin its card
      await vi.waitFor(() => expect(target.querySelector(".card-slot .title")?.textContent).toBe("Builds"));
      group(target).dispatchEvent(new MouseEvent("mouseleave"));
      await new Promise((r) => setTimeout(r, 80));
      expect(isOpen(target)).toBe(true); // the card holds it open
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
      await vi.waitFor(() => expect(target.querySelector(".card-slot")).toBeNull());
      await vi.waitFor(() => expect(isOpen(target)).toBe(false));
    } finally {
      cleanup();
    }
  });

  it("opens the group when the agent of an open card folds into it", async () => {
    const solo = dockList([PROJECT.agents[1], PROJECT.agents[3]], { version: 1 });
    const { target, dock, cleanup } = render(solo);
    try {
      await vi.waitFor(() => expect(items(target)).toHaveLength(2));
      items(target)[0].click(); // Builds
      await vi.waitFor(() => expect(target.querySelector(".card-slot .title")?.textContent).toBe("Builds"));
      await vi.waitFor(() => expect(dock.calls).toHaveLength(2));
      dock.push(dockList([PROJECT.agents[1], PROJECT.agents[2], PROJECT.agents[3]], { version: 2 }));
      await vi.waitFor(() => expect(heads(target)).toHaveLength(1));
      expect(isOpen(target)).toBe(true);
      expect(target.querySelector(".card-slot .title")?.textContent).toBe("Builds");
    } finally {
      cleanup();
    }
  });
});

describe("MiniDock window fit with groups", () => {
  // jsdom lays nothing out: give the strip and the group box fixed heights.
  const STRIP_H = 300;
  const MEMBERS_H = 104;
  let restore: () => void = () => {};
  function fakeGeometry(): void {
    const off = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight")!;
    const scroll = Object.getOwnPropertyDescriptor(Element.prototype, "scrollHeight")!;
    Object.defineProperty(HTMLElement.prototype, "offsetHeight", {
      configurable: true,
      get(this: HTMLElement) {
        if (this.classList.contains("strip")) return STRIP_H; // box not grown yet
        return 0;
      },
    });
    Object.defineProperty(Element.prototype, "scrollHeight", {
      configurable: true,
      get(this: Element) {
        return this.classList.contains("members-clip") ? MEMBERS_H : 0;
      },
    });
    restore = () => {
      Object.defineProperty(HTMLElement.prototype, "offsetHeight", off);
      Object.defineProperty(Element.prototype, "scrollHeight", scroll);
    };
  }
  afterEach(() => restore());

  it("grows the window for the open group at once (zoomed), shrinks it after the fold animation", async () => {
    fakeGeometry();
    const { target, layouts, cleanup } = render(PROJECT, { scale: 1.5 });
    try {
      await vi.waitFor(() => expect(heads(target)).toHaveLength(1));
      await vi.waitFor(() => expect(layouts.at(-1)?.stripHeight).toBeCloseTo(STRIP_H * 1.5));
      heads(target)[0].click();
      flushSync();
      await vi.waitFor(() => expect(layouts.at(-1)?.stripHeight).toBeCloseTo((STRIP_H + MEMBERS_H) * 1.5));
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
      flushSync();
      await new Promise((r) => setTimeout(r, 60));
      expect(layouts.at(-1)?.stripHeight).toBeCloseTo((STRIP_H + MEMBERS_H) * 1.5); // still animating
      await vi.waitFor(() => expect(layouts.at(-1)?.stripHeight).toBeCloseTo(STRIP_H * 1.5));
    } finally {
      cleanup();
    }
  });

  it("shrinks at once with reduced motion", async () => {
    fakeGeometry();
    const { target, layouts, cleanup } = render(PROJECT, { reducedMotion: true });
    try {
      await vi.waitFor(() => expect(heads(target)).toHaveLength(1));
      heads(target)[0].click();
      flushSync();
      await vi.waitFor(() => expect(layouts.at(-1)?.stripHeight).toBe(STRIP_H + MEMBERS_H));
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
      flushSync();
      await vi.waitFor(() => expect(layouts.at(-1)?.stripHeight).toBe(STRIP_H));
    } finally {
      cleanup();
    }
  });
});

describe("MiniDock dock_show", () => {
  it("all: idle agents get their own items, no +N", async () => {
    const { target, cleanup } = render(FLEET, { prefs: { group: true, show: "all", autohide: false } });
    try {
      await vi.waitFor(() => expect(items(target)).toHaveLength(6));
      expect(target.querySelector("button.idle-chip")).toBeNull();
    } finally {
      cleanup();
    }
  });

  it("needs_you: only blocked and done; the rest folds into +N as other agents", async () => {
    const { target, cleanup } = render(FLEET, { prefs: { group: true, show: "needs_you", autohide: false } });
    try {
      await vi.waitFor(() => expect(items(target)).toHaveLength(3));
      expect(items(target).map((b) => b.getAttribute("aria-label"))).toEqual([
        "Blocked long · Needs you",
        "Blocked short · Needs you",
        "Done one · Done",
      ]);
      const chip = target.querySelector<HTMLButtonElement>("button.idle-chip")!;
      expect(chip.textContent?.trim()).toBe("+3");
      expect(chip.title).toBe("3 more agents");
      chip.click();
      await vi.waitFor(() => expect(target.querySelector(".card-slot .title")?.textContent).toBe("Other agents (3)"));
      expect(Array.from(target.querySelectorAll(".row-title")).map((e) => e.textContent)).toEqual([
        "Working one",
        "Idle A",
        "Idle B",
      ]);
    } finally {
      cleanup();
    }
  });
});
