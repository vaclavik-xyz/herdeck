import { describe, expect, it } from "vitest";
import {
  DOCK_SHOW_CHOICES,
  groupKeyOf,
  groupStatus,
  parseDockPrefs,
  stripLayoutDelay,
  stripModel,
  DEFAULT_DOCK_PREFS,
  GROUP_EXPAND_MS,
  GROUP_STAGGER_MS,
  type StripItem,
} from "./dockStrip";
import { agentKey } from "./dockModel";
import { dockAgent } from "./dockFixtures";
import type { DockAgent, DockStatus } from "./dockClient";

const ag = (paneId: string, status: DockStatus, repo = "", over: Partial<DockAgent> = {}) =>
  dockAgent({ paneId, status, repo, workspace: "", title: paneId, sinceS: 10, ...over });

const on = { group: true, show: "active" as const };
const off = { group: false, show: "active" as const };

/** Compact picture of the strip: agent pane ids, groups as "key[p1,p2]". */
function shape(items: StripItem[]): string[] {
  return items.map((it) =>
    it.kind === "agent" ? it.agent.paneId : `${it.label}[${it.members.map((m) => m.paneId).join(",")}]`,
  );
}

describe("groupKeyOf", () => {
  it("is the repo, else the workspace, else empty", () => {
    expect(groupKeyOf(ag("a", "working", "herdeck"))).toBe("herdeck");
    expect(groupKeyOf(ag("a", "working", "", { workspace: "Herdeck WS" }))).toBe("Herdeck WS");
    expect(groupKeyOf(ag("a", "working", "", { workspace: "" }))).toBe("");
  });
});

describe("groupStatus", () => {
  it("is the most urgent member: waiting > done > working > unknown", () => {
    expect(groupStatus([ag("a", "working"), ag("b", "done")])).toBe("done");
    expect(groupStatus([ag("a", "done"), ag("b", "waiting"), ag("c", "working")])).toBe("waiting");
    expect(groupStatus([ag("a", "working"), ag("b", "unknown")])).toBe("working");
  });
});

describe("stripModel: grouping", () => {
  it("folds two or more non-idle agents of one project into one group item", () => {
    const m = stripModel([ag("w1", "working", "herdeck"), ag("w2", "working", "herdeck"), ag("x", "working", "crm")], on);
    expect(shape(m.items)).toEqual(["herdeck[w1,w2]", "x"]);
    const g = m.items[0];
    expect(g.kind).toBe("group");
    if (g.kind === "group") {
      expect(g.status).toBe("working");
      expect(g.members).toHaveLength(2);
      expect(g.lead.paneId).toBe("w1");
    }
  });

  it("needs at least two members: a lone agent stays a plain item", () => {
    const m = stripModel([ag("w1", "working", "herdeck"), ag("w2", "working", "crm")], on);
    expect(shape(m.items)).toEqual(["w1", "w2"]);
  });

  it("falls back to the workspace and never groups an empty key", () => {
    const m = stripModel(
      [
        ag("a", "working", "", { workspace: "WS" }),
        ag("b", "done", "", { workspace: "WS" }),
        ag("c", "working", "", { workspace: "" }),
        ag("d", "working", "", { workspace: "" }),
      ],
      on,
    );
    expect(shape(m.items)).toEqual(["WS[b,a]", "c", "d"]);
  });

  it("never folds a blocked agent: it stands alone at the top", () => {
    const m = stripModel(
      [ag("w", "working", "herdeck"), ag("b", "blocked", "herdeck"), ag("d", "done", "herdeck")],
      on,
    );
    expect(shape(m.items)).toEqual(["b", "herdeck[d,w]"]);
    // A blocked agent plus one other of the same project is no group at all.
    expect(shape(stripModel([ag("b", "blocked", "herdeck"), ag("w", "working", "herdeck")], on).items)).toEqual([
      "b",
      "w",
    ]);
  });

  it("sits a group where its most urgent member would be, ring in that status", () => {
    const m = stripModel(
      [
        ag("x", "done", "crm"),
        ag("w1", "working", "herdeck"),
        ag("wt", "waiting", "herdeck"),
        ag("y", "working", "other"),
      ],
      on,
    );
    expect(shape(m.items)).toEqual(["herdeck[wt,w1]", "x", "y"]);
    expect(m.items[0].kind === "group" && m.items[0].status).toBe("waiting");
  });

  it("does not group idle agents, and keeps them in +N", () => {
    const m = stripModel([ag("i1", "idle", "herdeck"), ag("i2", "idle", "herdeck"), ag("w", "working", "herdeck")], on);
    expect(shape(m.items)).toEqual(["w"]);
    expect(m.folded.map((a) => a.paneId)).toEqual(["i1", "i2"]);
  });

  it("is a group only while every member is offline when marked offline", () => {
    const both = stripModel(
      [ag("a", "working", "h", { offline: true }), ag("b", "working", "h", { offline: true })],
      on,
    ).items[0];
    const one = stripModel([ag("a", "working", "h", { offline: true }), ag("b", "working", "h")], on).items[0];
    expect(both.kind === "group" && both.offline).toBe(true);
    expect(one.kind === "group" && one.offline).toBe(false);
  });

  it("keys a group by its project, distinct from any agent key", () => {
    const m = stripModel([ag("a", "working", "h"), ag("b", "working", "h"), ag("c", "working", "z")], on);
    const keys = m.items.map((i) => i.key);
    expect(new Set(keys).size).toBe(keys.length);
    expect(keys).toContain(agentKey(ag("c", "working", "z")));
  });

  it("turns off with group = false (today's flat strip)", () => {
    const m = stripModel([ag("w1", "working", "herdeck"), ag("w2", "working", "herdeck")], off);
    expect(shape(m.items)).toEqual(["w1", "w2"]);
  });
});

describe("stripModel: dock_show", () => {
  const fleet = [
    ag("b", "blocked", "p1"),
    ag("wt", "waiting", "p2"),
    ag("d", "done", "p3"),
    ag("w", "working", "p4"),
    ag("u", "unknown", "p5"),
    ag("i2", "idle", "p6", { title: "Zeta" }),
    ag("i1", "idle", "p7", { title: "Alpha" }),
  ];

  it("active: every non-idle agent; idle folded into +N by title", () => {
    const m = stripModel(fleet, { group: true, show: "active" });
    expect(shape(m.items)).toEqual(["b", "wt", "d", "w", "u"]);
    expect(m.folded.map((a) => a.title)).toEqual(["Alpha", "Zeta"]);
    expect(m.foldedIdleOnly).toBe(true);
  });

  it("all: idle agents get their own items after the rest; nothing folds", () => {
    const m = stripModel(fleet, { group: true, show: "all" });
    expect(shape(m.items)).toEqual(["b", "wt", "d", "w", "u", "i1", "i2"]); // idle by title
    expect(m.folded).toEqual([]);
  });

  it("needs_you: only blocked and done; everything else folds, most urgent first", () => {
    const m = stripModel(fleet, { group: true, show: "needs_you" });
    expect(shape(m.items)).toEqual(["b", "d"]);
    expect(m.folded.map((a) => a.paneId)).toEqual(["wt", "w", "u", "i1", "i2"]);
    expect(m.foldedIdleOnly).toBe(false);
  });

  it("needs_you still groups done agents of one project and never hides a blocked one", () => {
    const m = stripModel(
      [ag("b1", "blocked", "h"), ag("b2", "blocked", "h"), ag("d1", "done", "h"), ag("d2", "done", "h"), ag("w", "working", "h")],
      { group: true, show: "needs_you" },
    );
    expect(shape(m.items)).toEqual(["b1", "b2", "h[d1,d2]"]);
    expect(m.folded.map((a) => a.paneId)).toEqual(["w"]);
  });

  it("an empty fold says it is idle-only (no chip is drawn for it anyway)", () => {
    const m = stripModel([ag("w", "working")], on);
    expect(m.folded).toEqual([]);
    expect(m.foldedIdleOnly).toBe(true);
  });

  it("every agent appears exactly once across items, members and the fold", () => {
    for (const show of DOCK_SHOW_CHOICES) {
      for (const group of [true, false]) {
        const m = stripModel([...fleet, ag("w2", "working", "p4"), ag("d2", "done", "p3")], { group, show });
        const seen = [
          ...m.items.flatMap((it) => (it.kind === "agent" ? [it.agent] : it.members)),
          ...m.folded,
        ].map(agentKey);
        expect(seen.sort()).toEqual([...fleet, ag("w2", "working", "p4"), ag("d2", "done", "p3")].map(agentKey).sort());
      }
    }
  });
});

describe("parseDockPrefs", () => {
  it("defaults: grouping on, show active, autohide off", () => {
    expect(DEFAULT_DOCK_PREFS).toEqual({ group: true, show: "active", autohide: false });
    expect(parseDockPrefs(undefined)).toEqual(DEFAULT_DOCK_PREFS);
    expect(parseDockPrefs({})).toEqual(DEFAULT_DOCK_PREFS);
    expect(parseDockPrefs("nope")).toEqual(DEFAULT_DOCK_PREFS);
  });

  it("reads the [desktop] keys and ignores wrong types", () => {
    expect(parseDockPrefs({ dock_group: false, dock_show: "needs_you", dock_autohide: true })).toEqual({
      group: false,
      show: "needs_you",
      autohide: true,
    });
    expect(parseDockPrefs({ dock_group: "no", dock_show: "everyone", dock_autohide: 1 })).toEqual(DEFAULT_DOCK_PREFS);
    expect(DOCK_SHOW_CHOICES).toEqual(["active", "all", "needs_you"]);
  });
});

describe("stripLayoutDelay", () => {
  it("grows the window at once and shrinks it only after the collapse animation", () => {
    expect(stripLayoutDelay(300, 400, false)).toBe(0);
    expect(stripLayoutDelay(400, 300, false)).toBeGreaterThanOrEqual(GROUP_EXPAND_MS);
    expect(stripLayoutDelay(400, 400, false)).toBe(0);
  });

  it("never waits with reduced motion", () => {
    expect(stripLayoutDelay(400, 300, true)).toBe(0);
  });

  it("covers the stagger of the members", () => {
    expect(GROUP_STAGGER_MS).toBe(40);
    expect(GROUP_EXPAND_MS).toBe(160);
  });
});
