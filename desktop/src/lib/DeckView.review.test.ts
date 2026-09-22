// DeckView review fixes: tile aria-labels from /state tile_labels (C1), visible
// feedback on a failed press, debounced offline overlay, and the base64
// fallback when a URI-scheme image fails to load.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import DeckView from "./DeckView.svelte";
import type { DeckTransport, PressResult } from "./deckClient";
import { setLang } from "./i18n.svelte";

function state(over: Record<string, unknown> = {}) {
  return {
    version: 1,
    slots: 13,
    has_panel: false,
    panel: 0,
    tiles: {},
    summary: {},
    source: "mock",
    connected: true,
    language: "en",
    ...over,
  };
}

function transport(over: Partial<DeckTransport> = {}): DeckTransport {
  return {
    fetchState: async () => state(),
    tileImage: async () => null,
    panelImage: async () => null,
    press: async () => ({ ok: true, status: 204, forbidden: false }),
    ...over,
  };
}

function render(props: { transport: DeckTransport | null; compact?: boolean }) {
  const target = document.createElement("div");
  document.body.appendChild(target);
  const instance = mount(DeckView, { target, props });
  flushSync();
  let done = false;
  return {
    target,
    cleanup: () => {
      if (done) return;
      done = true;
      unmount(instance);
      target.remove();
    },
  };
}

async function settle(ms = 0): Promise<void> {
  await vi.advanceTimersByTimeAsync(ms);
  flushSync();
}

describe("DeckView review fixes", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    setLang("en");
  });
  afterEach(() => {
    setLang("en");
    vi.useRealTimers();
  });

  it("labels tiles from /state tile_labels, falling back to 'tile N'", async () => {
    const t = transport({
      fetchState: async () => state({ tile_labels: { "0": "herdeck · main · working 3m" } }),
    });
    const { target, cleanup } = render({ transport: t, compact: true });
    try {
      await settle();
      const cells = target.querySelectorAll<HTMLButtonElement>(".cell");
      expect(cells[0].getAttribute("aria-label")).toBe("herdeck · main · working 3m");
      expect(cells[1].getAttribute("aria-label")).toBe("tile 2");
    } finally {
      cleanup();
    }
  });

  it("shows visible, titled feedback when a press fails (network)", async () => {
    const t = transport({
      press: async () => {
        throw new Error("connection refused");
      },
    });
    const { target, cleanup } = render({ transport: t, compact: true });
    try {
      await settle();
      target.querySelectorAll<HTMLButtonElement>(".cell")[3].click();
      await settle();
      const cell = target.querySelectorAll<HTMLButtonElement>(".cell")[3];
      expect(cell.classList.contains("failed")).toBe(true);
      expect(cell.classList.contains("active"), "a failed press must not look landed").toBe(false);
      expect(cell.title).toBe("Press didn't reach the runtime");
      expect(target.querySelector(".press-error[role='status']")?.textContent).toBe(
        "Press didn't reach the runtime",
      );
      await settle(3000);
      expect(target.querySelector(".press-error"), "the message outlived its window").toBeNull();
      expect(target.querySelectorAll(".cell.failed").length).toBe(0);
    } finally {
      cleanup();
    }
  });

  it("explains a 403 / other status, in Czech too", async () => {
    let result: PressResult = { ok: false, status: 403, forbidden: true };
    const t = transport({
      fetchState: async () => state({ language: "cs" }),
      press: async () => result,
    });
    const { target, cleanup } = render({ transport: t, compact: true });
    try {
      await settle();
      target.querySelectorAll<HTMLButtonElement>(".cell")[0].click();
      await settle();
      expect(target.querySelector(".press-error")?.textContent).toBe(
        "Stisk odmítnut: změnil se přístupový token runtime",
      );
      result = { ok: false, status: 400, forbidden: false };
      target.querySelectorAll<HTMLButtonElement>(".cell")[1].click();
      await settle();
      expect(target.querySelector(".press-error")?.textContent).toBe("Runtime stisk odmítl (HTTP 400)");
      expect(target.querySelectorAll(".cell.failed").length, "only the latest cell is marked").toBe(1);
    } finally {
      cleanup();
    }
  });

  it("a single failed poll does not raise the offline overlay", async () => {
    let fail = false;
    const t = transport({
      fetchState: async () => {
        if (fail) throw new Error("blip");
        return state();
      },
    });
    const { target, cleanup } = render({ transport: t, compact: true });
    try {
      await settle();
      expect(target.querySelector(".deck-offline")).toBeNull();
      fail = true;
      await settle(300); // one failed poll
      expect(target.querySelector(".deck-offline"), "one blip flashed the overlay").toBeNull();
      expect(target.querySelector(".src")?.textContent).toBe("mock");
      await settle(1500); // sustained failure
      expect(target.querySelector(".deck-offline")).not.toBeNull();
      expect(target.querySelector(".src")?.textContent).toBe("offline · reconnecting…");
    } finally {
      cleanup();
    }
  });

  it("falls back to the base64 command path when a scheme image fails to load", async () => {
    const t = transport({
      fetchState: async () => state({ tiles: { "0": 5 } }),
      tileImage: async (i, v) => `herdeck://localhost/tile/${i}?v=${v}`,
      imageFallback: {
        tile: async (i) => `data:image/png;base64,T${i}`,
        panel: async () => null,
      },
    });
    const { target, cleanup } = render({ transport: t, compact: true });
    try {
      await settle();
      const img = target.querySelector<HTMLImageElement>(".cell img")!;
      expect(img.getAttribute("src")).toBe("herdeck://localhost/tile/0?v=5");
      img.dispatchEvent(new Event("error"));
      await settle();
      expect(target.querySelector(".cell img")?.getAttribute("src")).toBe("data:image/png;base64,T0");
    } finally {
      cleanup();
    }
  });

  it("long-polls /state once synced (no fixed 300ms interval against a holding runtime)", async () => {
    const polls: unknown[] = [];
    let release: (() => void) | undefined;
    const t = transport({
      fetchState: (poll) => {
        polls.push(poll);
        if (!poll) return Promise.resolve(state({ version: 1 }));
        // a C2 runtime holds the request until the version moves
        return new Promise((resolve) => {
          release = () => resolve(state({ version: 2 }));
        });
      },
    });
    const { cleanup } = render({ transport: t, compact: true });
    try {
      await settle();
      await settle(5000);
      expect(polls).toEqual([undefined, { after: 1, waitMs: 20000 }]);
      release!();
      await settle(100);
      expect(polls[2]).toEqual({ after: 2, waitMs: 20000 });
    } finally {
      cleanup();
    }
  });
});
