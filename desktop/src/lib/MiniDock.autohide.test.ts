// The dock's edge autohide (Task 6) wired into MiniDock: it hides only when
// `dock_autohide` is on AND Rust reports a snapped edge, the window becomes a
// zoom-scaled sliver after the slide, and a card, a group, keyboard focus or
// a blocked agent keeps (or brings) the strip out.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import MiniDock from "./MiniDock.svelte";
import { setLang } from "./i18n.svelte";
import { dockAgent, dockList, fakeAgentTransport, fakeDockTransport } from "./dockFixtures";
import type { DockList } from "./dockClient";
import type { DockLayoutRequest, DockPlacement } from "./dockModel";
import { AUTOHIDE_DELAY_MS, AUTOHIDE_REVEAL_MS, AUTOHIDE_SLIDE_MS } from "./dockAutohide";
import type { DockPrefs } from "./dockStrip";
import { reactiveProps } from "./testProps.svelte";

const AUTOHIDE: DockPrefs = { group: true, show: "active", autohide: true };

function render(first: DockList, extra: Record<string, unknown> = {}, edge: DockPlacement["edge"] = "right") {
  const dock = fakeDockTransport(first);
  const layouts: DockLayoutRequest[] = [];
  const placement = { edge };
  const windowLayout = vi.fn(async (req: DockLayoutRequest): Promise<DockPlacement> => {
    layouts.push(req);
    const hidden = (req.sliverWidth ?? 0) > 0 && placement.edge === "right";
    return { side: "left", stripX: hidden ? req.sliverWidth! - req.stripWidth : 0, stripY: 0, edge: placement.edge };
  });
  const target = document.createElement("div");
  document.body.appendChild(target);
  const props = reactiveProps({
    transport: dock,
    agentTransport: fakeAgentTransport(),
    windowLayout,
    prefs: AUTOHIDE,
    hoverOpenMs: 0,
    ...extra,
  });
  const instance = mount(MiniDock, { target, props }) as { relayout: () => void };
  flushSync();
  return {
    target,
    props,
    layouts,
    placement,
    dock,
    instance,
    cleanup: () => {
      unmount(instance);
      target.remove();
    },
  };
}

const CALM = dockList(
  [
    dockAgent({ paneId: "w", title: "Builds", status: "working", sinceS: 10, repo: "r-w" }),
    dockAgent({ paneId: "d", title: "Finished", status: "done", sinceS: 5, repo: "r-d" }),
  ],
  { version: 1 },
);

const strip = (t: HTMLElement) => t.querySelector<HTMLElement>("nav.strip")!;
const sliver = (t: HTMLElement) => t.querySelector<HTMLElement>(".sliver");
const root = (t: HTMLElement) => t.querySelector<HTMLElement>(".mini-dock")!;
const lastLayout = (layouts: DockLayoutRequest[]) => layouts[layouts.length - 1];
const HIDE_MS = AUTOHIDE_DELAY_MS + AUTOHIDE_SLIDE_MS;

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  vi.useRealTimers();
  setLang("en");
});

describe("MiniDock autohide", () => {
  it("slides out after the pointer left, then shrinks the window to a sliver", async () => {
    const { target, layouts, cleanup } = render(CALM);
    try {
      await vi.advanceTimersByTimeAsync(0);
      root(target).dispatchEvent(new PointerEvent("pointerenter"));
      root(target).dispatchEvent(new PointerEvent("pointerleave"));
      await vi.advanceTimersByTimeAsync(AUTOHIDE_DELAY_MS);
      expect(strip(target).classList.contains("slid-out")).toBe(true);
      expect(sliver(target)).toBeNull(); // the window is still full while it slides
      expect(lastLayout(layouts).sliverWidth ?? 0).toBe(0);
      await vi.advanceTimersByTimeAsync(AUTOHIDE_SLIDE_MS);
      expect(lastLayout(layouts)).toMatchObject({ sliverWidth: 6, cardWidth: 0 });
      // The sliver carries the most urgent status (done outranks working).
      expect(sliver(target)?.classList.contains("st-done")).toBe(true);
      expect(sliver(target)?.classList.contains("at-left")).toBe(false);
    } finally {
      cleanup();
    }
  });

  it("the sliver is zoom-scaled like everything else sent to Rust", async () => {
    const { layouts, cleanup } = render(CALM, { scale: 1.5 });
    try {
      await vi.advanceTimersByTimeAsync(HIDE_MS);
      expect(lastLayout(layouts)).toMatchObject({ sliverWidth: 9, stripWidth: 84 });
    } finally {
      cleanup();
    }
  });

  it("resting on the sliver grows the window first, then slides the strip in", async () => {
    const { target, layouts, cleanup } = render(CALM);
    try {
      await vi.advanceTimersByTimeAsync(HIDE_MS);
      expect(sliver(target)).not.toBeNull();
      root(target).dispatchEvent(new PointerEvent("pointerenter"));
      await vi.advanceTimersByTimeAsync(AUTOHIDE_REVEAL_MS);
      expect(lastLayout(layouts).sliverWidth).toBe(0);
      expect(sliver(target)).toBeNull();
      await vi.advanceTimersByTimeAsync(20); // one frame at the full size
      expect(strip(target).classList.contains("slid-out")).toBe(false);
    } finally {
      cleanup();
    }
  });

  it("does nothing while the strip is not snapped to an edge", async () => {
    const { target, layouts, cleanup } = render(CALM, {}, null);
    try {
      await vi.advanceTimersByTimeAsync(HIDE_MS * 3);
      expect(strip(target).classList.contains("slid-out")).toBe(false);
      expect(layouts.every((l) => (l.sliverWidth ?? 0) === 0)).toBe(true);
    } finally {
      cleanup();
    }
  });

  it("does nothing with dock_autohide off, and reveals when it is switched off live", async () => {
    const { target, props, cleanup } = render(CALM);
    try {
      await vi.advanceTimersByTimeAsync(HIDE_MS);
      expect(sliver(target)).not.toBeNull();
      (props as Record<string, unknown>).prefs = { ...AUTOHIDE, autohide: false };
      flushSync();
      await vi.advanceTimersByTimeAsync(20);
      expect(sliver(target)).toBeNull();
      expect(strip(target).classList.contains("slid-out")).toBe(false);
      await vi.advanceTimersByTimeAsync(HIDE_MS * 3);
      expect(strip(target).classList.contains("slid-out")).toBe(false);
    } finally {
      cleanup();
    }
  });

  it("never hides while an agent is blocked, and a question brings it back", async () => {
    const { target, dock, cleanup } = render(CALM);
    try {
      await vi.advanceTimersByTimeAsync(HIDE_MS);
      expect(sliver(target)).not.toBeNull();
      dock.push(
        dockList([...CALM.agents, dockAgent({ paneId: "b", title: "Asks", status: "blocked", sinceS: 1, repo: "r-b" })], {
          version: 2,
        }),
      );
      await vi.advanceTimersByTimeAsync(20);
      expect(sliver(target)).toBeNull();
      expect(strip(target).classList.contains("slid-out")).toBe(false);
      await vi.advanceTimersByTimeAsync(HIDE_MS * 3);
      expect(strip(target).classList.contains("slid-out")).toBe(false);
    } finally {
      cleanup();
    }
  });

  it("stays out while a card is open", async () => {
    const { target, cleanup } = render(CALM);
    try {
      await vi.advanceTimersByTimeAsync(0);
      const item = target.querySelector<HTMLButtonElement>("button.item")!;
      item.click(); // pins the card
      await vi.advanceTimersByTimeAsync(HIDE_MS * 3);
      expect(target.querySelector(".card")).not.toBeNull();
      expect(strip(target).classList.contains("slid-out")).toBe(false);
    } finally {
      cleanup();
    }
  });

  it("stays out while the dock has keyboard focus", async () => {
    const { target, cleanup } = render(CALM);
    try {
      window.dispatchEvent(new FocusEvent("focus"));
      await vi.advanceTimersByTimeAsync(HIDE_MS * 3);
      expect(strip(target).classList.contains("slid-out")).toBe(false);
      window.dispatchEvent(new FocusEvent("blur"));
      await vi.advanceTimersByTimeAsync(HIDE_MS);
      expect(strip(target).classList.contains("slid-out")).toBe(true);
    } finally {
      cleanup();
    }
  });

  it("learns a new edge when a drag ended (relayout)", async () => {
    const { target, placement, instance, cleanup } = render(CALM, {}, null);
    try {
      await vi.advanceTimersByTimeAsync(HIDE_MS);
      expect(sliver(target)).toBeNull();
      placement.edge = "left"; // Rust snapped the dropped strip to the left edge
      instance.relayout();
      await vi.advanceTimersByTimeAsync(HIDE_MS);
      expect(sliver(target)?.classList.contains("at-left")).toBe(true);
      expect(strip(target).classList.contains("out-left")).toBe(true);
    } finally {
      cleanup();
    }
  });
});
