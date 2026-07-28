import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import DeckView from "./DeckView.svelte";
import type { DeckTransport } from "./deckClient";

// A transport that answers one tile and accepts presses. The outline is a
// client-side affordance, so the poll result does not matter here.
function fakeTransport(): DeckTransport & { presses: number[] } {
  const presses: number[] = [];
  return {
    presses,
    fetchState: async () => ({
      version: 1,
      slots: 13,
      has_panel: false,
      panel: 0,
      tiles: {},
      summary: {},
      source: "mock",
      connected: true,
      language: "en",
    }),
    tileImage: async () => null,
    panelImage: async () => null,
    press: async (index: number) => {
      presses.push(index);
      return { ok: true, status: 200, forbidden: false };
    },
  };
}

function render(props: Record<string, unknown>) {
  const target = document.createElement("div");
  document.body.appendChild(target);
  const instance = mount(DeckView, { target, props });
  flushSync();
  return { target, cleanup: () => { unmount(instance); target.remove(); } };
}

describe("DeckView press feedback", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  // The outline says "that press landed". It used to be set and never cleared,
  // so the last-pressed cell kept its ring forever — and since the ring is keyed
  // by SLOT index, it stayed put while the agent under it changed, marking a
  // tile the user had never pressed.
  it("outlines the pressed cell and lets the outline go", async () => {
    const transport = fakeTransport();
    const { target, cleanup } = render({ transport, compact: true });
    try {
      const cells = target.querySelectorAll<HTMLButtonElement>(".cell");
      expect(cells.length).toBeGreaterThan(1);

      cells[2].click();
      await vi.advanceTimersByTimeAsync(0);
      flushSync();
      expect(transport.presses).toEqual([2]);
      expect(cells[2].classList.contains("active"), "press left no feedback").toBe(true);

      await vi.advanceTimersByTimeAsync(600);
      flushSync();
      expect(
        target.querySelectorAll(".cell.active").length,
        "the outline outlived the press",
      ).toBe(0);
    } finally { cleanup(); }
  });

  it("moves the outline to the newest press instead of accumulating", async () => {
    const transport = fakeTransport();
    const { target, cleanup } = render({ transport, compact: true });
    try {
      const cells = target.querySelectorAll<HTMLButtonElement>(".cell");
      cells[1].click();
      await vi.advanceTimersByTimeAsync(0);
      await vi.advanceTimersByTimeAsync(200); // still within the first flash
      cells[4].click();
      await vi.advanceTimersByTimeAsync(0);
      flushSync();

      const outlined = [...target.querySelectorAll(".cell")]
        .flatMap((c, i) => (c.classList.contains("active") ? [i] : []));
      expect(outlined).toEqual([4]);
    } finally { cleanup(); }
  });
});
