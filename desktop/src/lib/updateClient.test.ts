import { describe, expect, it, vi } from "vitest";

import { asUpdateInfo, reasonOf, runUpdateCheck, updateTransport } from "./updateClient";

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

// runUpdateCheck shapes a check's outcome for the UI — the manual "check for
// updates" tray item and the silent mount check both go through it (see
// App.svelte), so all three transport outcomes have to map to a distinct,
// renderable state. This is the seam that makes the fourth outcome (a failed
// check) testable at all, since it is exactly what the mount-only path used
// to swallow — the bug a manual check exists to fix.
describe("runUpdateCheck", () => {
  it("reports an available update", async () => {
    const info = { version: "0.2.0", current_version: "0.1.0" };
    await expect(runUpdateCheck(async () => info)).resolves.toEqual({
      kind: "available",
      info,
    });
  });

  it("reports up to date when the check finds nothing", async () => {
    await expect(runUpdateCheck(async () => null)).resolves.toEqual({ kind: "up-to-date" });
  });

  it("reports failure with the error's message instead of throwing", async () => {
    await expect(
      runUpdateCheck(async () => {
        throw new Error("offline");
      }),
    ).resolves.toEqual({ kind: "failed", reason: "offline" });
  });

  it("stringifies a non-Error rejection rather than losing the reason", async () => {
    await expect(
      runUpdateCheck(async () => {
        throw "network down";
      }),
    ).resolves.toEqual({ kind: "failed", reason: "network down" });
  });

  it("falls back to a labelled reason for a message-less Error", async () => {
    await expect(
      runUpdateCheck(async () => {
        throw new Error();
      }),
    ).resolves.toEqual({ kind: "failed", reason: "unknown error" });
  });
});

// reasonOf is the shared seam runUpdateCheck and App.svelte's installUpdate
// both call, so an empty or blank thrown message can't render as a dangling
// "Update check failed: " with nothing after the colon in one path but not
// the other.
describe("reasonOf", () => {
  it("uses an Error's message", () => {
    expect(reasonOf(new Error("offline"))).toBe("offline");
  });

  it("stringifies a non-Error value", () => {
    expect(reasonOf("network down")).toBe("network down");
  });

  it("falls back to a label for a message-less Error", () => {
    expect(reasonOf(new Error())).toBe("unknown error");
    expect(reasonOf(new Error("   "))).toBe("unknown error");
  });

  it("stringifies undefined/null rejections rather than blanking them out", () => {
    // Not empty (String(undefined) === "undefined"), so these are NOT the
    // "unknown error" fallback case above — they at least name what was
    // thrown, which the fallback only kicks in for a truly blank message.
    expect(reasonOf(undefined)).toBe("undefined");
    expect(reasonOf(null)).toBe("null");
  });
});
