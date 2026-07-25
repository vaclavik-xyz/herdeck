import { describe, expect, it } from "vitest";
import { fieldPresentation } from "./fieldPresentation";

describe("fieldPresentation", () => {
  it("shows a human label while preserving the config key", () => {
    expect(fieldPresentation("overview_order", "en")).toEqual({
      label: "Server order",
      configKey: "overview_order",
      status: null,
    });
    expect(fieldPresentation("overview_order", "cs")).toEqual({
      label: "Pořadí serverů",
      configKey: "overview_order",
      status: null,
    });
  });

  it("keeps inherited-origin suffixes as separate status", () => {
    expect(fieldPresentation("chat_id (inherited)", "en")).toEqual({
      label: "Chat ID",
      configKey: "chat_id",
      status: "inherited",
    });
  });

  it("leaves already human labels unchanged", () => {
    expect(fieldPresentation("Provider IDs", "en")).toEqual({
      label: "Provider IDs",
      configKey: null,
      status: null,
    });
    expect(fieldPresentation("", "cs")).toEqual({ label: "", configKey: null, status: null });
  });

  it("makes token references explicit", () => {
    expect(fieldPresentation("token_env", "en")).toEqual({
      label: "Token reference",
      configKey: "token_env",
      status: null,
    });
  });
});
