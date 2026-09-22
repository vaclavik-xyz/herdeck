// Regression tests for the deck-window review fixes: fast-path completeness,
// offline debounce, /state long-poll (contract C2), tile labels (C1) and the
// URI-scheme image path.
import { describe, it, expect } from "vitest";
import {
  DeckDiffer,
  commandTransport,
  httpTransport,
  imageSchemeBase,
  initialView,
  nextPollDelay,
  OfflineDebounce,
  stepDeck,
  type DeckTransport,
  type StatePoll,
} from "./deckClient";

function rawState(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    version: 1,
    slots: 13,
    has_panel: true,
    panel: 0,
    tiles: { "0": 1 },
    summary: { agents: 4, blocked: 1, working: 2, idle: 1, done: 0, waiting: 0 },
    source: "mock",
    connected: true,
    language: "en",
    ...over,
  };
}

function seq(states: unknown[]): DeckTransport {
  let i = 0;
  return {
    async fetchState() {
      const s = states[Math.min(i++, states.length - 1)];
      if (s instanceof Error) throw s;
      return s;
    },
    tileImage: async (index, version) => `tile-${index}-v${version}`,
    panelImage: async (version) => `panel-v${version}`,
    press: async () => ({ ok: true, status: 204, forbidden: false }),
  };
}

// The idle fast path compared everything visible EXCEPT the waiting count and
// the language, so a change in only those never reached the window.
describe("stepDeck fast path sees waiting + language + tile labels", () => {
  it("updates when only the waiting count changes", async () => {
    const t = seq([
      rawState({ version: 1 }),
      rawState({
        version: 2,
        summary: { agents: 4, blocked: 1, working: 2, idle: 1, done: 0, waiting: 3 },
      }),
    ]);
    const d = new DeckDiffer();
    const first = await stepDeck(t, d, initialView());
    const second = await stepDeck(t, d, first);
    expect(second).not.toBe(first);
    expect(second.summary.waiting).toBe(3);
  });

  it("updates when only the language changes", async () => {
    const t = seq([rawState({ version: 1 }), rawState({ version: 2, language: "cs" })]);
    const d = new DeckDiffer();
    const first = await stepDeck(t, d, initialView());
    const second = await stepDeck(t, d, first);
    expect(second.language).toBe("cs");
  });

  it("parses tile_labels and updates when only a label changes", async () => {
    const t = seq([
      rawState({ version: 1, tile_labels: { "0": "herdeck · main · working 3m", x: "junk" } }),
      rawState({ version: 2, tile_labels: { "0": "herdeck · main · working 4m" } }),
    ]);
    const d = new DeckDiffer();
    const first = await stepDeck(t, d, initialView());
    expect(first.labels).toEqual({ 0: "herdeck · main · working 3m" });
    const second = await stepDeck(t, d, first);
    expect(second.labels[0]).toBe("herdeck · main · working 4m");
  });

  it("an older runtime without tile_labels yields no labels", async () => {
    const view = await stepDeck(seq([rawState()]), new DeckDiffer(), initialView());
    expect(view.labels).toEqual({});
  });
});

// One failed poll flipped `online` and flashed the offline overlay (and an
// aria-live "offline" announcement) over a working deck.
describe("OfflineDebounce + stepDeck", () => {
  it("reports offline only after 3 consecutive failures", () => {
    let now = 0;
    const o = new OfflineDebounce(3, 1000, () => now);
    expect(o.fail()).toBe(false);
    now += 10;
    expect(o.fail()).toBe(false);
    now += 10;
    expect(o.fail()).toBe(true);
  });

  it("reports offline once a failure run is ~1s old, even with fewer failures", () => {
    let now = 0;
    const o = new OfflineDebounce(3, 1000, () => now);
    expect(o.fail()).toBe(false);
    now = 1000;
    expect(o.fail()).toBe(true);
  });

  it("a success resets the run", () => {
    let now = 0;
    const o = new OfflineDebounce(3, 1000, () => now);
    o.fail();
    o.fail();
    o.ok();
    expect(o.failing).toBe(false);
    now = 5000;
    expect(o.fail()).toBe(false);
  });

  it("stepDeck keeps the online model through transient failures", async () => {
    const boom = new Error("boom");
    const t = seq([rawState({ version: 1 }), boom, boom, boom]);
    const d = new DeckDiffer();
    const offline = new OfflineDebounce(3, 60_000);
    const online = await stepDeck(t, d, initialView(), { offline });
    expect(online.online).toBe(true);
    const blip = await stepDeck(t, d, online, { offline });
    expect(blip, "a single failure must not re-render at all").toBe(online);
    const blip2 = await stepDeck(t, d, blip, { offline });
    expect(blip2.online).toBe(true);
    const down = await stepDeck(t, d, blip2, { offline });
    expect(down.online).toBe(false);
  });

  it("without a debouncer a failure is reported at once (unchanged behaviour)", async () => {
    const t = seq([rawState({ version: 1 }), new Error("boom")]);
    const d = new DeckDiffer();
    const online = await stepDeck(t, d, initialView());
    expect((await stepDeck(t, d, online)).online).toBe(false);
  });
});

// /state long-poll instead of a 300ms interval (contract C2).
describe("long-poll /state", () => {
  function recording(tiles: Record<string, number>, failImages = false) {
    const polls: (StatePoll | undefined)[] = [];
    const t: DeckTransport = {
      async fetchState(poll) {
        polls.push(poll);
        return rawState({ version: 7, tiles, has_panel: false });
      },
      tileImage: async () => {
        if (failImages) throw new Error("image failed");
        return null;
      },
      panelImage: async () => null,
      press: async () => ({ ok: true, status: 204, forbidden: false }),
    };
    return { t, polls };
  }

  it("stepDeck long-polls with the synced version once it has one", async () => {
    const { t, polls } = recording({});
    const d = new DeckDiffer();
    const v = await stepDeck(t, d, initialView(), { waitMs: 20000 });
    await stepDeck(t, d, v, { waitMs: 20000 });
    expect(polls).toEqual([undefined, { after: 7, waitMs: 20000 }]);
  });

  it("does not long-poll while an image is still unloaded (retry at once)", async () => {
    const { t, polls } = recording({ "0": 1 }, true);
    const d = new DeckDiffer();
    const v = await stepDeck(t, d, initialView(), { waitMs: 20000 });
    await stepDeck(t, d, v, { waitMs: 20000 });
    expect(polls).toEqual([undefined, undefined]);
  });

  it("nextPollDelay re-arms at once after a change or a held timeout", () => {
    const p = { pollMs: 300, failing: false, longPolled: true };
    expect(nextPollDelay({ ...p, before: 1, after: 2, elapsedMs: 5000 })).toBe(0);
    expect(nextPollDelay({ ...p, before: 1, after: 1, elapsedMs: 20000 })).toBe(0);
    // a burst of changes is floored, not spun flat out
    expect(nextPollDelay({ ...p, before: 1, after: 2, elapsedMs: 10 })).toBe(40);
  });

  it("nextPollDelay falls back to interval polling when the runtime ignores the params", () => {
    // unchanged and answered at once -> the runtime did not hold the request
    expect(
      nextPollDelay({ pollMs: 300, failing: false, longPolled: true, before: 4, after: 4, elapsedMs: 3 }),
    ).toBe(300);
  });

  it("nextPollDelay retries at pollMs while failing or unsynced, and starts long-polling after a sync", () => {
    const p = { pollMs: 300, elapsedMs: 3 };
    expect(nextPollDelay({ ...p, failing: true, longPolled: true, before: 1, after: 1 })).toBe(300);
    expect(nextPollDelay({ ...p, failing: false, longPolled: false, before: -1, after: -1 })).toBe(300);
    expect(nextPollDelay({ ...p, failing: false, longPolled: false, before: -1, after: 5 })).toBe(0);
  });

  it("commandTransport passes after/waitMs to deck_state only for a long-poll", async () => {
    const calls: { cmd: string; args?: Record<string, unknown> }[] = [];
    const t = commandTransport(async (cmd, args) => {
      calls.push({ cmd, args });
      return { version: 1 };
    });
    await t.fetchState();
    await t.fetchState({ after: 9, waitMs: 20000 });
    expect(calls).toEqual([
      { cmd: "deck_state", args: undefined },
      { cmd: "deck_state", args: { after: 9, waitMs: 20000 } },
    ]);
  });

  it("httpTransport adds ?after=&wait_ms= and the token, and hands out direct image URLs", async () => {
    const seen: string[] = [];
    const fetchFn = (async (url: string, init?: RequestInit) => {
      seen.push(`${init?.method ?? "GET"} ${url}`);
      return { ok: true, status: 200, json: async () => ({ version: 2 }) } as Response;
    }) as unknown as typeof fetch;
    const t = httpTransport("http://127.0.0.1:9/", "tok", fetchFn);
    expect(await t.fetchState({ after: 2, waitMs: 20000 })).toEqual({ version: 2 });
    await t.fetchState();
    expect(seen).toEqual([
      "GET http://127.0.0.1:9/state?token=tok&after=2&wait_ms=20000",
      "GET http://127.0.0.1:9/state?token=tok",
    ]);
    expect(await t.tileImage(3, 8)).toBe("http://127.0.0.1:9/tile/3?token=tok&v=8");
    expect(await t.panelImage(4)).toBe("http://127.0.0.1:9/panel?token=tok&v=4");
    expect(t.imageFallback).toBeUndefined();
  });

  it("httpTransport treats a non-2xx /state as a failed poll", async () => {
    const fetchFn = (async () => ({ ok: false, status: 403 }) as Response) as unknown as typeof fetch;
    await expect(httpTransport("http://h", "t", fetchFn).fetchState()).rejects.toThrow(/403/);
  });
});

// Tiles via the shell's URI scheme instead of base64 over IPC.
describe("commandTransport image scheme", () => {
  it("uses herdeck://localhost on macOS/Linux and http://herdeck.localhost on Windows", () => {
    expect(imageSchemeBase("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit")).toBe(
      "herdeck://localhost",
    );
    expect(imageSchemeBase("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit")).toBe("herdeck://localhost");
    expect(imageSchemeBase("Mozilla/5.0 (Windows NT 10.0; Win64; x64) Edg/120")).toBe(
      "http://herdeck.localhost",
    );
  });

  it("hands out versioned scheme URLs without any IPC", async () => {
    const calls: string[] = [];
    const t = commandTransport(
      async (cmd) => {
        calls.push(cmd);
        return null;
      },
      { schemeBase: "herdeck://localhost" },
    );
    expect(await t.tileImage(3, 17)).toBe("herdeck://localhost/tile/3?v=17");
    expect(await t.panelImage(5)).toBe("herdeck://localhost/panel?v=5");
    expect(calls).toEqual([]);
  });

  it("imageFallback returns base64 and pins the transport to the command path", async () => {
    const t = commandTransport(
      async (cmd, args) =>
        cmd === "deck_tile" ? `data:image/png;base64,T${args?.index}` : "data:image/png;base64,P",
      { schemeBase: "herdeck://localhost" },
    );
    expect(await t.imageFallback!.tile(2)).toBe("data:image/png;base64,T2");
    expect(await t.tileImage(4, 1)).toBe("data:image/png;base64,T4");
    expect(await t.panelImage(1)).toBe("data:image/png;base64,P");
  });

  it("a genuine 404 on the fallback keeps the scheme path", async () => {
    const t = commandTransport(async () => null, { schemeBase: "herdeck://localhost" });
    expect(await t.imageFallback!.tile(2)).toBeNull();
    expect(await t.tileImage(2, 3)).toBe("herdeck://localhost/tile/2?v=3");
  });

  it("offers no fallback when the scheme is disabled", () => {
    const t = commandTransport(async () => null, { imageScheme: false });
    expect(t.imageFallback).toBeUndefined();
  });
});
