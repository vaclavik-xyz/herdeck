import { describe, expect, it } from "vitest";
import {
  agentKey,
  dockLayout,
  fnv1a,
  liveSince,
  monogramColor,
  monogramInk,
  neutralReason,
  optionTone,
  orderAgents,
  parsePlacement,
} from "./dockModel";
import { DOCK_MESSAGES, outcomeText, statusLabel } from "./dockMessages";
import { MONOGRAM_PALETTE } from "./statusColors";
import { dockAgent, dockList } from "./dockFixtures";
import type { DockStatus } from "./dockClient";

const a = (paneId: string, status: DockStatus, sinceS: number, title = paneId) =>
  dockAgent({ paneId, status, sinceS, title });

describe("orderAgents / dockLayout", () => {
  it("orders blocked (longest first) · waiting · done · working · unknown", () => {
    const ordered = orderAgents([
      a("w", "working", 10),
      a("b1", "blocked", 60),
      a("d", "done", 5),
      a("u", "unknown", 1),
      a("b2", "blocked", 300),
      a("wt", "waiting", 30),
    ]);
    expect(ordered.map((x) => x.paneId)).toEqual(["b2", "b1", "wt", "d", "w", "u"]);
  });

  it("breaks ties by since, then by identity, so the order is stable", () => {
    const ordered = orderAgents([a("z", "working", 10), a("y", "working", 10), a("x", "working", 20)]);
    expect(ordered.map((x) => x.paneId)).toEqual(["x", "y", "z"]);
  });

  it("folds idle agents out of the strip, listed by title", () => {
    const layout = dockLayout([a("i2", "idle", 1, "Zeta"), a("b", "blocked", 9), a("i1", "idle", 1, "Alpha")]);
    expect(layout.visible.map((x) => x.paneId)).toEqual(["b"]);
    expect(layout.idle.map((x) => x.title)).toEqual(["Alpha", "Zeta"]);
  });

  it("keys an agent by server and pane", () => {
    expect(agentKey({ serverId: "a", paneId: "b" })).not.toBe(agentKey({ serverId: "ab", paneId: "" }));
  });
});

describe("optionTone (runtime action id only)", () => {
  it("maps the runtime's action id to a tone", () => {
    expect(optionTone({ key: "3", label: "Maybe", id: "deny" }, "claude")).toBe("deny");
    expect(optionTone({ key: "9", label: "x", id: "approve" }, "codex")).toBe("approve");
    expect(optionTone({ key: "9", label: "x", id: "approve_always" }, "codex")).toBe("approve_always");
  });

  it("treats a null or unknown id as other, ignoring key and label", () => {
    expect(optionTone({ key: "1", label: "Yes", id: null }, "claude")).toBe("other");
    expect(optionTone({ key: "2", label: "Yes, allow edits", id: null }, "claude")).toBe("other");
    expect(optionTone({ key: "5", label: "Don't ask again", id: null }, "codex")).toBe("other");
    expect(optionTone({ key: "3", label: "No", id: null }, "claude")).toBe("other");
    expect(optionTone({ key: "4", label: "Nothing else", id: "whatever" }, "codex")).toBe("other");
  });
});

describe("monogram", () => {
  it("hashes with 32-bit FNV-1a", () => {
    expect(fnv1a("")).toBe(0x811c9dc5);
    expect(fnv1a("a")).toBe(0xe40c292c);
  });

  it("colours a seed stably from the deck's palette", () => {
    expect(MONOGRAM_PALETTE).toHaveLength(8);
    expect(MONOGRAM_PALETTE).toContain(monogramColor("herdeck"));
    expect(monogramColor("herdeck")).toBe(monogramColor("herdeck"));
    const colours = new Set(["a", "b", "c", "d", "e", "f", "g", "h"].map(monogramColor));
    expect(colours.size).toBeGreaterThan(1);
  });

  it("inks a light fill dark and a dark fill light", () => {
    expect(monogramInk("rgb(244,160,0)")).toBe("dark");
    expect(monogramInk("rgb(92,107,192)")).toBe("light");
    expect(monogramInk("not a colour")).toBe("light");
  });
});

describe("liveSince / neutralReason / parsePlacement", () => {
  it("ticks since forward from when the list arrived", () => {
    expect(liveSince(260, 1000, 3500)).toBe(262);
    expect(liveSince(10, 5000, 4000)).toBe(10);
  });

  it("names why the strip has nothing to show", () => {
    expect(neutralReason(null)).toBe("loading");
    expect(neutralReason(dockList([]))).toBe("empty");
    expect(neutralReason(dockList([], { connected: false }))).toBe("disconnected");
    expect(neutralReason(dockList([dockAgent()], { connected: false }))).toBeNull();
  });

  it("parses dock_layout's reply and defaults anything else", () => {
    expect(parsePlacement({ side: "left", stripX: 308, stripY: 4 })).toEqual({ side: "left", stripX: 308, stripY: 4 });
    expect(parsePlacement({ side: "up", stripX: 1, stripY: 2 })).toEqual({ side: "right", stripX: 0, stripY: 0 });
    expect(parsePlacement(null)).toEqual({ side: "right", stripX: 0, stripY: 0 });
  });
});

describe("dock messages", () => {
  it("carry the same keys in en and cs", () => {
    expect(Object.keys(DOCK_MESSAGES.cs).sort()).toEqual(Object.keys(DOCK_MESSAGES.en).sort());
  });

  it("label statuses and outcomes in both languages", () => {
    expect(statusLabel("en", "blocked")).toBe("Needs you");
    expect(statusLabel("cs", "blocked")).toBe("Potřebuje tě");
    expect(statusLabel("en", "working", true)).toBe("Server offline");
    expect(outcomeText("en", { ok: false, code: "stale", message: "" })).toBe(
      "The prompt changed — read the new one before answering.",
    );
    expect(outcomeText("cs", { ok: false, code: "weird", message: "boom" })).toBe("Nepovedlo se: boom");
  });
});
