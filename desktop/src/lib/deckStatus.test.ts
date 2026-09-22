import { describe, expect, it } from "vitest";

import { initialView } from "./deckClient";
import { adoptDeckStatus, deckPreviewMounted } from "./deckStatus";

describe("adoptDeckStatus", () => {
  it("keeps the previous object when only tile images changed", () => {
    const prev = { ...initialView(), online: true };
    const next = { ...prev, tiles: { 0: "data:image/png;base64,AAA" }, panel: "x" };
    expect(adoptDeckStatus(prev, next)).toBe(prev);
  });

  it("adopts the new object when a status field changed", () => {
    const prev = { ...initialView(), online: true };
    const next = { ...prev, connections: { macbench: true } };
    expect(adoptDeckStatus(prev, next)).toBe(next);
    const summary = { ...prev, summary: { ...prev.summary, working: 2 } };
    expect(adoptDeckStatus(prev, summary)).toBe(summary);
  });
});

describe("deckPreviewMounted", () => {
  it("is true only where ConfigApp renders a live DeckView", () => {
    expect(deckPreviewMounted("overview", false)).toBe(true);
    expect(deckPreviewMounted("deck", true)).toBe(true);
    expect(deckPreviewMounted("deck", false)).toBe(false);
    expect(deckPreviewMounted("view", true)).toBe(false);
  });
});
