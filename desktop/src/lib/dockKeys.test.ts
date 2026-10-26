import { describe, expect, it } from "vitest";
import { blockedOrder, cycleBlocked, dockKeyAction, isTypingTarget, type DockKeyContext } from "./dockKeys";
import { agentKey } from "./dockModel";
import { dockAgent } from "./dockFixtures";

const card: DockKeyContext = { card: "agent", typing: false, keyboard: false };
const kb: DockKeyContext = { ...card, keyboard: true };

describe("dockKeyAction", () => {
  it("maps digits 1-9 to that option", () => {
    expect(dockKeyAction({ key: "1" }, card)).toEqual({ kind: "answer", key: "1" });
    expect(dockKeyAction({ key: "9" }, card)).toEqual({ kind: "answer", key: "9" });
    expect(dockKeyAction({ key: "0" }, card)).toBeNull();
  });
  it("reads the physical digit on a layout whose number row types letters (Czech)", () => {
    expect(dockKeyAction({ key: "ě", code: "Digit2" }, card)).toEqual({ kind: "answer", key: "2" });
    expect(dockKeyAction({ key: "3", code: "Numpad3" }, card)).toEqual({ kind: "answer", key: "3" });
  });
  it("leaves Cmd/Ctrl/Alt chords alone (Cmd+0 zoom, Cmd+1…)", () => {
    expect(dockKeyAction({ key: "1", metaKey: true }, card)).toBeNull();
    expect(dockKeyAction({ key: "1", ctrlKey: true }, card)).toBeNull();
    expect(dockKeyAction({ key: "t", altKey: true }, card)).toBeNull();
  });
  it("T or / focuses the reply, O opens in herdr, Esc closes", () => {
    expect(dockKeyAction({ key: "t" }, card)).toEqual({ kind: "reply" });
    expect(dockKeyAction({ key: "T", shiftKey: true }, card)).toEqual({ kind: "reply" });
    expect(dockKeyAction({ key: "/" }, card)).toEqual({ kind: "reply" });
    expect(dockKeyAction({ key: "o" }, card)).toEqual({ kind: "open" });
    expect(dockKeyAction({ key: "Escape" }, card)).toEqual({ kind: "close" });
  });
  it("Tab cycles the blocked agents only for a card the hotkey opened", () => {
    expect(dockKeyAction({ key: "Tab" }, kb)).toEqual({ kind: "cycle", dir: 1 });
    expect(dockKeyAction({ key: "Tab", shiftKey: true }, kb)).toEqual({ kind: "cycle", dir: -1 });
    expect(dockKeyAction({ key: "Tab" }, card)).toBeNull();
  });
  it("ignores every key while typing in the reply field", () => {
    for (const key of ["1", "t", "/", "o", "Tab", "Escape"]) {
      expect(dockKeyAction({ key }, { ...kb, typing: true })).toBeNull();
    }
    expect(dockKeyAction({ key: "1", isComposing: true }, card)).toBeNull();
  });
  it("does nothing without a card; only Esc acts on a non-agent card", () => {
    expect(dockKeyAction({ key: "1" }, { ...card, card: null })).toBeNull();
    expect(dockKeyAction({ key: "Escape" }, { ...card, card: null })).toBeNull();
    expect(dockKeyAction({ key: "1" }, { ...card, card: "other" })).toBeNull();
    expect(dockKeyAction({ key: "Escape" }, { ...card, card: "other" })).toEqual({ kind: "close" });
  });
});

describe("blocked cycling", () => {
  const fleet = [
    dockAgent({ paneId: "w", status: "working", sinceS: 999 }),
    dockAgent({ paneId: "b1", status: "blocked", sinceS: 60 }),
    dockAgent({ paneId: "b2", status: "blocked", sinceS: 300 }),
    dockAgent({ paneId: "b3", status: "blocked", sinceS: 10 }),
  ];
  const key = (paneId: string) => agentKey(dockAgent({ paneId }));
  it("orders the blocked agents longest blocked first", () => {
    expect(blockedOrder(fleet).map((a) => a.paneId)).toEqual(["b2", "b1", "b3"]);
  });
  it("moves forward and back with wrap-around", () => {
    expect(cycleBlocked(fleet, key("b2"), 1)?.paneId).toBe("b1");
    expect(cycleBlocked(fleet, key("b3"), 1)?.paneId).toBe("b2");
    expect(cycleBlocked(fleet, key("b2"), -1)?.paneId).toBe("b3");
  });
  it("starts at the first / last from a card that is not blocked", () => {
    expect(cycleBlocked(fleet, key("w"), 1)?.paneId).toBe("b2");
    expect(cycleBlocked(fleet, null, -1)?.paneId).toBe("b3");
  });
  it("has nowhere to go with one or no blocked agent", () => {
    expect(cycleBlocked([fleet[2]], key("b2"), 1)).toBeNull();
    expect(cycleBlocked([fleet[0]], key("w"), 1)).toBeNull();
  });
});

describe("isTypingTarget", () => {
  it("knows text fields from buttons", () => {
    expect(isTypingTarget(document.createElement("textarea"))).toBe(true);
    expect(isTypingTarget(document.createElement("input"))).toBe(true);
    expect(isTypingTarget(document.createElement("button"))).toBe(false);
    expect(isTypingTarget(null)).toBe(false);
    expect(isTypingTarget(window)).toBe(false);
  });
});
