import { describe, expect, it, vi } from "vitest";
import { fetchUsage, parseUsage } from "./dockUsage";

const BODY = {
  providers: [
    {
      provider: "claude",
      subscription: "max",
      plan: "Max",
      windows: [
        { label: "5h", used_percent: 38, resets_at: "2026-10-09T12:00:00Z", full_early_s: 2400 },
        { label: "7d", used_percent: 140, resets_at: null, full_early_s: null },
        { label: "bad", used_percent: "x" },
      ],
    },
    { provider: "codex", windows: "nope" },
    { nope: true },
  ],
};

describe("parseUsage", () => {
  it("shapes providers, clamps percent and drops malformed rows", () => {
    const out = parseUsage(BODY);
    expect(out).toHaveLength(2);
    expect(out[0].windows).toEqual([
      { label: "5h", usedPercent: 38, resetsAt: "2026-10-09T12:00:00Z", fullEarlyS: 2400 },
      { label: "7d", usedPercent: 100, resetsAt: null, fullEarlyS: null },
    ]);
    expect(out[1]).toEqual({ provider: "codex", subscription: "unknown", plan: null, windows: [] });
  });

  it("malformed bodies are an empty list", () => {
    for (const raw of [null, 5, "x", {}, { providers: 1 }]) expect(parseUsage(raw)).toEqual([]);
  });
});

describe("fetchUsage", () => {
  it("calls GET /usage through agent_call", async () => {
    const invoke = vi.fn().mockResolvedValue({ status: 200, body: BODY });
    expect(await fetchUsage(invoke)).toHaveLength(2);
    expect(invoke).toHaveBeenCalledWith("agent_call", { method: "GET", path: "/usage" });
  });

  it("is empty on a non-200 status or a failed call", async () => {
    expect(await fetchUsage(vi.fn().mockResolvedValue({ status: 403, body: BODY }))).toEqual([]);
    expect(await fetchUsage(vi.fn().mockRejectedValue(new Error("down")))).toEqual([]);
  });
});
