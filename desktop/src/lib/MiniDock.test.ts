import { afterEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import MiniDock from "./MiniDock.svelte";
import { setLang } from "./i18n.svelte";
import { DOCK_POLL_MS } from "./dockClient";
import { CARD_WIDTH, STRIP_WIDTH, type DockLayoutRequest } from "./dockModel";
import { dockAgent, dockList, fakeAgentTransport, fakeDockTransport } from "./dockFixtures";
import type { DockList } from "./dockClient";

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
  const instance = mount(MiniDock, {
    target,
    props: { transport: dock, agentTransport: agents, windowLayout, onOpenSettings, ...extra },
  });
  flushSync();
  return {
    target,
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

const FLEET = dockList(
  [
    dockAgent({ paneId: "w", title: "Working one", status: "working", sinceS: 10 }),
    dockAgent({ paneId: "b1", title: "Blocked short", status: "blocked", sinceS: 60 }),
    dockAgent({ paneId: "b2", title: "Blocked long", status: "blocked", sinceS: 300 }),
    dockAgent({ paneId: "d", title: "Done one", status: "done", sinceS: 5 }),
    dockAgent({ paneId: "i1", title: "Idle A", status: "idle", sinceS: 9 }),
    dockAgent({ paneId: "i2", title: "Idle B", status: "idle", sinceS: 9 }),
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
      expect(items(target).map((b) => b.title)).toEqual([
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
      await vi.waitFor(() => expect(items(target)[0]?.title).toBe("X · Potřebuje tě"));
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
