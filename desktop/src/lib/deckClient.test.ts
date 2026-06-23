import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  parseState,
  summaryLabel,
  emptySummary,
  DeckDiffer,
  directFetchTransport,
  stepDeck,
  initialView,
  type DeckState,
  type DeckTransport,
} from "./deckClient";

// A minimal /state payload as the sidecar (herdeck.deckapp server) sends it.
function rawState(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    version: 1,
    slots: 13,
    has_panel: true,
    panel: 0,
    tiles: { "0": 1, "1": 1, "2": 1 },
    summary: { agents: 4, blocked: 1, working: 2, idle: 1, done: 0 },
    source: "mock",
    connected: false,
    ...over,
  };
}

describe("parseState", () => {
  it("shapes a well-formed /state payload (snake_case has_panel -> hasPanel)", () => {
    const s = parseState(rawState())!;
    expect(s).not.toBeNull();
    expect(s.version).toBe(1);
    expect(s.slots).toBe(13);
    expect(s.hasPanel).toBe(true);
    expect(s.tiles).toEqual({ 0: 1, 1: 1, 2: 1 });
    expect(s.summary).toEqual({ agents: 4, blocked: 1, working: 2, idle: 1, done: 0 });
    expect(s.source).toBe("mock");
    expect(s.connected).toBe(false);
  });

  it("returns null for junk / not-an-object / missing version", () => {
    expect(parseState(null)).toBeNull();
    expect(parseState(undefined)).toBeNull();
    expect(parseState("nope")).toBeNull();
    expect(parseState({ slots: 13 })).toBeNull(); // no version
  });

  it("defaults summary fields and drops non-integer tile keys", () => {
    const s = parseState({ version: 2, tiles: { "0": 5, x: 9, "-1": 3 }, summary: {} })!;
    expect(s.tiles).toEqual({ 0: 5 });
    expect(s.summary).toEqual(emptySummary());
    expect(s.source).toBe("unknown");
  });
});

describe("summaryLabel", () => {
  it("emphasizes blocked last and pluralizes agents", () => {
    expect(summaryLabel({ agents: 4, blocked: 1, working: 2, idle: 1, done: 0 })).toBe(
      "4 agents · 2 working · 1 idle · ⚠ 1 blocked",
    );
  });

  it("omits zero buckets and uses singular agent", () => {
    expect(summaryLabel({ agents: 1, blocked: 0, working: 0, idle: 1, done: 0 })).toBe(
      "1 agent · 1 idle",
    );
  });

  it("renders only the agent count when everything else is zero", () => {
    expect(summaryLabel(emptySummary())).toBe("0 agents");
  });
});

describe("DeckDiffer — version gate + per-tile diff (the web.py poll port)", () => {
  let differ: DeckDiffer;
  beforeEach(() => {
    differ = new DeckDiffer();
  });

  const st = (over: Partial<DeckState>): DeckState => ({
    version: 1,
    slots: 13,
    hasPanel: true,
    panel: 0,
    tiles: {},
    summary: emptySummary(),
    source: "mock",
    connected: false,
    ...over,
  });

  it("refetches all initial tiles and the panel on the first snapshot", () => {
    const d = differ.reconcile(st({ version: 1, tiles: { 0: 1, 1: 1, 2: 1 }, panel: 0 }));
    expect(d.refetch.map((r) => r.index).sort()).toEqual([0, 1, 2]);
    expect(d.panel).toEqual({ version: 0 });
    expect(d.clear).toEqual([]);
  });

  it("does NOTHING when the overall version is unchanged (cheap gate)", () => {
    differ.reconcile(st({ version: 5, tiles: { 0: 1, 1: 1 } }));
    // even if tile versions would differ, an unchanged overall version short-circuits
    const d = differ.reconcile(st({ version: 5, tiles: { 0: 9, 1: 9 } }));
    expect(d).toEqual({ refetch: [], clear: [], panel: null });
  });

  it("refetches ONLY the tile whose version advanced", () => {
    differ.reconcile(st({ version: 1, tiles: { 0: 1, 1: 1, 2: 1 } }));
    const d = differ.reconcile(st({ version: 2, tiles: { 0: 1, 1: 2, 2: 1 } }));
    expect(d.refetch).toEqual([{ index: 1, version: 2 }]);
    expect(d.clear).toEqual([]);
    expect(d.panel).toBeNull(); // panel version unchanged -> not refetched
  });

  it("clears a tile that disappears from /state", () => {
    differ.reconcile(st({ version: 1, tiles: { 0: 1, 1: 1, 2: 1 } }));
    const d = differ.reconcile(st({ version: 2, tiles: { 0: 1, 2: 1 } }));
    expect(d.clear).toEqual([1]);
    expect(d.refetch).toEqual([]);
  });

  it("refetches the panel only when its version advances", () => {
    differ.reconcile(st({ version: 1, panel: 0, tiles: {} }));
    const same = differ.reconcile(st({ version: 2, panel: 0, tiles: {} }));
    expect(same.panel).toBeNull();
    const moved = differ.reconcile(st({ version: 3, panel: 1, tiles: {} }));
    expect(moved.panel).toEqual({ version: 1 });
  });

  it("ignores the panel when has_panel is false", () => {
    const d = differ.reconcile(st({ version: 1, hasPanel: false, panel: 7, tiles: {} }));
    expect(d.panel).toBeNull();
  });

  it("reset() forces a full refetch on the next snapshot (reconnect)", () => {
    differ.reconcile(st({ version: 9, tiles: { 0: 1 } }));
    differ.reset();
    const d = differ.reconcile(st({ version: 1, tiles: { 0: 1 } }));
    expect(d.refetch).toEqual([{ index: 0, version: 1 }]);
  });
});

describe("directFetchTransport — token auth, ported from web.py auth()/press()", () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("GET /state carries the token as a query param and returns parsed JSON", async () => {
    fetchMock.mockResolvedValue({ ok: true, status: 200, json: async () => ({ version: 3 }) });
    const t = directFetchTransport("http://127.0.0.1:51234/", "tok 1");
    const body = await t.fetchState();
    expect(body).toEqual({ version: 3 });
    expect(fetchMock).toHaveBeenCalledWith("http://127.0.0.1:51234/state?token=tok%201");
  });

  it("fetchState throws on a non-ok status (offline tick)", async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 403, json: async () => ({}) });
    const t = directFetchTransport("http://127.0.0.1:51234", "tok");
    await expect(t.fetchState()).rejects.toThrow();
  });

  it("tileSrc / panelSrc append the cache-buster and the token", () => {
    const t = directFetchTransport("http://127.0.0.1:9", "t&k");
    expect(t.tileSrc(2, 7)).toBe("http://127.0.0.1:9/tile/2?v=7&token=t%26k");
    expect(t.panelSrc(4)).toBe("http://127.0.0.1:9/panel?v=4&token=t%26k");
  });

  it("press POSTs to /press/{i} with the X-Herdeck-Token header", async () => {
    fetchMock.mockResolvedValue({ ok: true, status: 204 });
    const t = directFetchTransport("http://127.0.0.1:9", "secret");
    const r = await t.press(5);
    expect(r).toEqual({ ok: true, status: 204, forbidden: false });
    expect(fetchMock).toHaveBeenCalledWith("http://127.0.0.1:9/press/5", {
      method: "POST",
      headers: { "X-Herdeck-Token": "secret" },
    });
  });

  it("press flags a 403 so the shell can re-pull discovery", async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 403 });
    const t = directFetchTransport("http://127.0.0.1:9", "secret");
    const r = await t.press(0);
    expect(r).toEqual({ ok: false, status: 403, forbidden: true });
  });
});

describe("stepDeck — folds a poll into the render model", () => {
  // A fake transport that records calls and yields deterministic srcs.
  function fakeTransport(states: unknown[]): DeckTransport & { pressed: number[] } {
    let i = 0;
    return {
      pressed: [],
      async fetchState() {
        const s = states[Math.min(i, states.length - 1)];
        i += 1;
        if (s instanceof Error) throw s;
        return s;
      },
      tileSrc: (index, version) => `tile-${index}-v${version}`,
      panelSrc: (version) => `panel-v${version}`,
      async press(index) {
        this.pressed.push(index);
        return { ok: true, status: 204, forbidden: false };
      },
    };
  }

  it("refetches only changed tiles across successive polls", async () => {
    const t = fakeTransport([
      rawState({ version: 1, tiles: { "0": 1, "1": 1 }, panel: 0 }),
      rawState({ version: 2, tiles: { "0": 1, "1": 2 }, panel: 0 }),
    ]);
    const differ = new DeckDiffer();
    let view = initialView();

    view = await stepDeck(t, differ, view);
    expect(view.online).toBe(true);
    expect(view.tiles).toEqual({ 0: "tile-0-v1", 1: "tile-1-v1" });
    expect(view.panel).toBe("panel-v0");

    view = await stepDeck(t, differ, view);
    // only tile 1 changed; tile 0's src is untouched (same string), panel untouched
    expect(view.tiles).toEqual({ 0: "tile-0-v1", 1: "tile-1-v2" });
    expect(view.panel).toBe("panel-v0");
  });

  it("exposes the summary, source and connected flag for the footer/indicator", async () => {
    const t = fakeTransport([rawState({ source: "live", connected: true })]);
    const view = await stepDeck(t, new DeckDiffer(), initialView());
    expect(view.summary).toEqual({ agents: 4, blocked: 1, working: 2, idle: 1, done: 0 });
    expect(view.source).toBe("live");
    expect(view.connected).toBe(true);
    expect(summaryLabel(view.summary)).toContain("⚠ 1 blocked");
  });

  it("goes offline (keeping last tiles) when the fetch fails", async () => {
    const t = fakeTransport([
      rawState({ version: 1, tiles: { "0": 1 } }),
      new Error("network down"),
    ]);
    const differ = new DeckDiffer();
    let view = await stepDeck(t, differ, initialView());
    expect(view.online).toBe(true);
    view = await stepDeck(t, differ, view);
    expect(view.online).toBe(false);
    expect(view.tiles).toEqual({ 0: "tile-0-v1" }); // last-known grid preserved, no flash
  });

  it("goes offline when /state is unparseable", async () => {
    const t = fakeTransport([{ garbage: true }]);
    const view = await stepDeck(t, new DeckDiffer(), initialView());
    expect(view.online).toBe(false);
  });
});
