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

  // A second press must CANCEL the first press's timer, not merely outrank it:
  // otherwise the earlier deadline fires mid-flash and blanks the newer outline.
  it("restarts the deadline on the next press instead of inheriting the old one", async () => {
    const transport = fakeTransport();
    const { target, cleanup } = render({ transport, compact: true });
    try {
      const cells = target.querySelectorAll<HTMLButtonElement>(".cell");
      cells[1].click();
      await vi.advanceTimersByTimeAsync(0);
      await vi.advanceTimersByTimeAsync(200); // t≈200, inside the first flash
      cells[4].click();
      await vi.advanceTimersByTimeAsync(0);
      flushSync();

      const outlined = () => [...target.querySelectorAll(".cell")]
        .flatMap((c, i) => (c.classList.contains("active") ? [i] : []));
      expect(outlined()).toEqual([4]);

      // t≈500: past the FIRST press's original deadline (450). If its timer was
      // never cancelled, it fires here and wrongly clears cell 4.
      await vi.advanceTimersByTimeAsync(300);
      flushSync();
      expect(outlined(), "a stale timer cleared the newer outline").toEqual([4]);

      await vi.advanceTimersByTimeAsync(250); // t≈750, past cell 4's own deadline
      flushSync();
      expect(outlined()).toEqual([]);
    } finally { cleanup(); }
  });

  // Pressing the same tile twice is a normal deck interaction; with the ring
  // already lit, an unchanged DOM makes the second press look dropped.
  it("retriggers the flash when the same cell is pressed again", async () => {
    const transport = fakeTransport();
    const { target, cleanup } = render({ transport, compact: true });
    try {
      const cell = target.querySelectorAll<HTMLButtonElement>(".cell")[3];
      cell.click();
      await vi.advanceTimersByTimeAsync(0);
      flushSync();
      const first = cell.classList.contains("alt");

      await vi.advanceTimersByTimeAsync(150); // still lit
      cell.click();
      await vi.advanceTimersByTimeAsync(0);
      flushSync();

      expect(cell.classList.contains("active")).toBe(true);
      expect(
        cell.classList.contains("alt"),
        "the second press left the DOM unchanged, so the animation never restarted",
      ).toBe(!first);
    } finally { cleanup(); }
  });

  // A press can still be in flight when the window mode switches or the app
  // quits. Without the guard the resolving press installs a 450ms timer that
  // teardown has already run past, and writes state on a dead component.
  it("installs no timer when the press resolves after teardown", async () => {
    let land: (() => void) | undefined;
    const transport: DeckTransport = {
      fetchState: async () => ({ version: 1, slots: 13, tiles: {}, source: "mock", connected: true }),
      tileImage: async () => null,
      panelImage: async () => null,
      press: () => new Promise((resolve) => {
        land = () => resolve({ ok: true, status: 200, forbidden: false });
      }),
    };
    const { target, cleanup } = render({ transport, compact: true });
    target.querySelectorAll<HTMLButtonElement>(".cell")[0].click();
    await vi.advanceTimersByTimeAsync(0);
    expect(land, "the press never reached the transport").toBeDefined();

    cleanup();
    const afterTeardown = vi.getTimerCount();
    land!();
    await vi.advanceTimersByTimeAsync(0);

    expect(
      vi.getTimerCount(),
      "a press that landed after teardown installed an uncancellable timer",
    ).toBe(afterTeardown);
  });
});
