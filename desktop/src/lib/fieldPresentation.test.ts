import { describe, expect, it } from "vitest";
import { fieldPresentation } from "./fieldPresentation";

describe("fieldPresentation", () => {
  it("shows a human label while preserving the config key", () => {
    expect(fieldPresentation("overview_order", "en")).toEqual({
      label: "Server order",
      configKey: "overview_order",
    });
    expect(fieldPresentation("overview_order", "cs")).toEqual({
      label: "Pořadí serverů",
      configKey: "overview_order",
    });
  });

  it("recognizes inherited-origin suffixes without exposing them as labels", () => {
    expect(fieldPresentation("chat_id (base)", "en")).toEqual({
      label: "Chat ID",
      configKey: "chat_id",
    });
  });

  it("leaves already human labels unchanged", () => {
    expect(fieldPresentation("Provider IDs", "en")).toEqual({
      label: "Provider IDs",
      configKey: null,
    });
    expect(fieldPresentation("", "cs")).toEqual({ label: "", configKey: null });
  });
});
