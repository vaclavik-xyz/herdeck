import { describe, it, expect, beforeEach } from "vitest";
import {
  parseState,
  summaryLabel,
  emptySummary,
  DeckDiffer,
  commandTransport,
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

describe("DeckDiffer — transactional version gate + per-tile diff", () => {
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

  // Simulate a fully-successful step: commit every planned image and arm the gate.
  const sync = (state: DeckState) => {
    const d = differ.plan(state);
    for (const { index, version } of d.refetch) differ.commitTile(index, version);
    for (const index of d.clear) differ.dropTile(index);
    if (d.panel) differ.commitPanel(d.panel.version);
    differ.markSynced(state.version);
    return d;
  };

  it("plans all initial tiles and the panel on the first snapshot", () => {
    const d = differ.plan(st({ version: 1, tiles: { 0: 1, 1: 1, 2: 1 }, panel: 0 }));
    expect(d.refetch.map((r) => r.index).sort()).toEqual([0, 1, 2]);
    expect(d.panel).toEqual({ version: 0 });
    expect(d.clear).toEqual([]);
  });

  it("plans NOTHING once a version is fully synced (cheap gate)", () => {
    sync(st({ version: 5, tiles: { 0: 1, 1: 1 }, panel: 0 }));
    // even if tile versions would differ, an unchanged overall version short-circuits
    const d = differ.plan(st({ version: 5, tiles: { 0: 9, 1: 9 }, panel: 3 }));
    expect(d).toEqual({ refetch: [], clear: [], panel: null });
  });

  it("plans ONLY the tile whose version advanced after a sync", () => {
    sync(st({ version: 1, tiles: { 0: 1, 1: 1, 2: 1 }, panel: 0 }));
    const d = differ.plan(st({ version: 2, tiles: { 0: 1, 1: 2, 2: 1 }, panel: 0 }));
    expect(d.refetch).toEqual([{ index: 1, version: 2 }]);
    expect(d.clear).toEqual([]);
    expect(d.panel).toBeNull(); // panel version unchanged -> not refetched
  });

  it("plans a clear for a tile that disappears from /state", () => {
    sync(st({ version: 1, tiles: { 0: 1, 1: 1, 2: 1 }, panel: 0 }));
    const d = differ.plan(st({ version: 2, tiles: { 0: 1, 2: 1 }, panel: 0 }));
    expect(d.clear).toEqual([1]);
    expect(d.refetch).toEqual([]);
  });

  it("plans the panel only when its version advances", () => {
    sync(st({ version: 1, panel: 0, tiles: {} }));
    expect(differ.plan(st({ version: 2, panel: 0, tiles: {} })).panel).toBeNull();
    expect(differ.plan(st({ version: 3, panel: 1, tiles: {} })).panel).toEqual({ version: 1 });
  });

  it("ignores the panel when has_panel is false", () => {
    const d = differ.plan(st({ version: 1, hasPanel: false, panel: 7, tiles: {} }));
    expect(d.panel).toBeNull();
  });

  it("keeps re-planning the SAME version until it is marked synced (failure retry)", () => {
    // a plan that is never committed/marked must be re-asked on the next plan
    const a = differ.plan(st({ version: 4, hasPanel: false, tiles: { 0: 1 } }));
    expect(a.refetch).toEqual([{ index: 0, version: 1 }]);
    const b = differ.plan(st({ version: 4, hasPanel: false, tiles: { 0: 1 } }));
    expect(b.refetch).toEqual([{ index: 0, version: 1 }]); // still pending -> retried
  });

  it("reset() forces a full re-plan on the next snapshot (reconnect)", () => {
    sync(st({ version: 9, tiles: { 0: 1 }, panel: 0 }));
    differ.reset();
    const d = differ.plan(st({ version: 1, tiles: { 0: 1 }, panel: 0 }));
    expect(d.refetch).toEqual([{ index: 0, version: 1 }]);
  });
});

describe("commandTransport — talks to the token-free Tauri proxy commands", () => {
  // A fake `invoke` that records calls and dispatches to per-command handlers.
  function fakeInvoke(handlers: Record<string, (args?: Record<string, unknown>) => unknown>) {
    const calls: { cmd: string; args?: Record<string, unknown> }[] = [];
    const invoke = async (cmd: string, args?: Record<string, unknown>) => {
      calls.push({ cmd, args });
      const h = handlers[cmd];
      if (!h) throw new Error(`no handler for ${cmd}`);
      return h(args);
    };
    return { invoke, calls };
  }

  it("fetchState invokes deck_state and returns its JSON", async () => {
    const { invoke, calls } = fakeInvoke({ deck_state: () => ({ version: 4 }) });
    const t = commandTransport(invoke);
    expect(await t.fetchState()).toEqual({ version: 4 });
    expect(calls).toEqual([{ cmd: "deck_state", args: undefined }]);
  });

  it("tileImage invokes deck_tile with the index and passes the data URL through", async () => {
    const { invoke, calls } = fakeInvoke({
      deck_tile: (args) => `data:image/png;base64,T${args?.index}`,
    });
    const t = commandTransport(invoke);
    expect(await t.tileImage(5, 9)).toBe("data:image/png;base64,T5");
    expect(calls).toEqual([{ cmd: "deck_tile", args: { index: 5 } }]);
  });

  it("panelImage invokes deck_panel and passes the data URL through", async () => {
    const { invoke, calls } = fakeInvoke({ deck_panel: () => "data:image/png;base64,P" });
    const t = commandTransport(invoke);
    expect(await t.panelImage(2)).toBe("data:image/png;base64,P");
    expect(calls).toEqual([{ cmd: "deck_panel", args: undefined }]);
  });

  it("tileImage / panelImage return null when the command yields no image (404)", async () => {
    const { invoke } = fakeInvoke({ deck_tile: () => null, deck_panel: () => null });
    const t = commandTransport(invoke);
    expect(await t.tileImage(0, 1)).toBeNull();
    expect(await t.panelImage(1)).toBeNull();
  });

  it("press invokes deck_press with the index and maps the status code", async () => {
    const { invoke, calls } = fakeInvoke({ deck_press: () => 204 });
    const t = commandTransport(invoke);
    expect(await t.press(7)).toEqual({ ok: true, status: 204, forbidden: false });
    expect(calls).toEqual([{ cmd: "deck_press", args: { index: 7 } }]);
  });

  it("press flags a 403 status (bad/stale token)", async () => {
    const { invoke } = fakeInvoke({ deck_press: () => 403 });
    const t = commandTransport(invoke);
    expect(await t.press(0)).toEqual({ ok: false, status: 403, forbidden: true });
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
      tileImage: async (index, version) => `tile-${index}-v${version}`,
      panelImage: async (version) => `panel-v${version}`,
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

  it("retries a tile on the next poll (SAME version) after its image fetch fails", async () => {
    // The version doesn't change between polls; the tile must still be retried
    // because the failed fetch left it unsynced (no permanent staleness).
    let failTile = true;
    let i = 0;
    const states = [
      rawState({ version: 1, tiles: { "0": 1 }, panel: 0 }),
      rawState({ version: 1, tiles: { "0": 1 }, panel: 0 }),
    ];
    const t: DeckTransport = {
      fetchState: async () => states[Math.min(i++, states.length - 1)],
      tileImage: async (index, version) => {
        if (failTile) throw new Error("invoke failed");
        return `tile-${index}-v${version}`;
      },
      panelImage: async (version) => `panel-v${version}`,
      press: async () => ({ ok: true, status: 204, forbidden: false }),
    };
    const differ = new DeckDiffer();
    let view = await stepDeck(t, differ, initialView());
    expect(view.tiles).toEqual({}); // tile 0 failed to load this poll
    failTile = false;
    view = await stepDeck(t, differ, view);
    // same /state.version, but the unsynced tile is retried and now loads
    expect(view.tiles).toEqual({ 0: "tile-0-v1" });
    expect(view.online).toBe(true);
  });
});
