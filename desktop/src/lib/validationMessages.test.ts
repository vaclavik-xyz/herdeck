import { describe, expect, it } from "vitest";

import { validationMessage } from "./validationMessages";

describe("validationMessage", () => {
  it("localizes a known code and keeps the raw message as detail", () => {
    expect(validationMessage("servers: 'x'", "unknown_server", "cs")).toBe("Neznámý server: servers: 'x'");
    expect(validationMessage("servers: 'x'", "unknown_server", "en")).toBe("Unknown server: servers: 'x'");
  });

  it("falls back to the raw message without a known code", () => {
    expect(validationMessage("deck.grid: bad", undefined, "cs")).toBe("deck.grid: bad");
    expect(validationMessage("deck.grid: bad", "not_a_code", "cs")).toBe("deck.grid: bad");
  });
});
