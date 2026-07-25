import { describe, expect, it, vi } from "vitest";

import { asUpdateInfo, updateTransport } from "./updateClient";

describe("update client", () => {
  it("accepts a shaped update and null when current", () => {
    expect(asUpdateInfo(null)).toBeNull();
    expect(asUpdateInfo({ version: "0.2.0", current_version: "0.1.0" })).toEqual({
      version: "0.2.0",
      current_version: "0.1.0",
    });
  });

  it("rejects malformed native responses", () => {
    expect(() => asUpdateInfo({ version: 2 })).toThrow("invalid update response");
  });

  it("uses separate check and install commands", async () => {
    const invoke = vi
      .fn()
      .mockResolvedValueOnce({ version: "0.2.0", current_version: "0.1.0" })
      .mockResolvedValueOnce(true);
    const transport = updateTransport(invoke);

    await expect(transport.check()).resolves.toEqual({
      version: "0.2.0",
      current_version: "0.1.0",
    });
    await expect(transport.install()).resolves.toBe(true);
    expect(invoke.mock.calls).toEqual([["update_check"], ["update_install"]]);
  });
});
