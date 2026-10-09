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

// --- ring maths, colours and formatting (the footer rings + usage card) ----

import {
  formatReset,
  formatSpan,
  fullEarlyText,
  leftFraction,
  providerColor,
  providerName,
  ringArc,
  ringAriaLabel,
  usageRing,
  usageRings,
  usageTone,
  windowLine,
  windowMinutes,
  type ProviderUsage,
  type UsageWindow,
} from "./dockUsage";

const win = (label: string, usedPercent: number, extra: Partial<UsageWindow> = {}): UsageWindow => ({
  label,
  usedPercent,
  resetsAt: null,
  fullEarlyS: null,
  ...extra,
});
const prov = (provider: string, windows: UsageWindow[], plan: string | null = null): ProviderUsage => ({
  provider,
  subscription: "paid",
  plan,
  windows,
});

describe("window maths", () => {
  it("reads a window label as minutes", () => {
    expect(windowMinutes("5h")).toBe(300);
    expect(windowMinutes("7d")).toBe(10080);
    expect(windowMinutes("30m")).toBe(30);
    expect(windowMinutes("?")).toBeNull();
    expect(windowMinutes("")).toBeNull();
  });

  it("left is the unused fraction, clamped", () => {
    expect(leftFraction(0)).toBe(1);
    expect(leftFraction(62)).toBeCloseTo(0.38);
    expect(leftFraction(100)).toBe(0);
    expect(leftFraction(140)).toBe(0);
    expect(leftFraction(-5)).toBe(1);
  });

  it("tones a ring by what is left: < 15 % low, < 5 % critical", () => {
    expect(usageTone(1)).toBe("ok");
    expect(usageTone(0.15)).toBe("ok");
    expect(usageTone(0.149)).toBe("low");
    expect(usageTone(0.05)).toBe("low");
    expect(usageTone(0.049)).toBe("critical");
    expect(usageTone(0)).toBe("critical");
  });

  it("colours: Claude orange, Codex near-white, others the accent", () => {
    expect(providerColor("claude")).toBe("#D97757");
    expect(providerColor("codex")).toBe("#E6E6E6");
    expect(providerColor("gemini")).toBe("var(--accent)");
    expect(providerName("claude")).toBe("Claude");
    expect(providerName("codex")).toBe("Codex");
    expect(providerName("gemini")).toBe("Gemini");
  });

  it("an arc draws what is left, ending at 12 o'clock (drains clockwise)", () => {
    expect(ringArc(1)).toEqual({ dash: "100 100", offset: "0" });
    expect(ringArc(0.38)).toEqual({ dash: "38 100", offset: "-62" });
    // Nothing left: no stroke at all (a round cap on a zero dash is a dot).
    expect(ringArc(0)).toBeNull();
    expect(ringArc(0.004)).toBeNull();
  });
});

describe("usageRing", () => {
  it("outer = shortest window, inner = longest, whatever the wire order", () => {
    const r = usageRing(prov("claude", [win("7d", 20), win("5h", 90)]))!;
    expect(r.outer).toMatchObject({ label: "5h", tone: "low", color: "var(--st-blocked)" });
    expect(r.outer.left).toBeCloseTo(0.1);
    expect(r.inner).toMatchObject({ label: "7d", tone: "ok", color: "#D97757" });
    expect(r.inner!.left).toBeCloseTo(0.8);
    expect(r.color).toBe("#D97757");
    expect(r.name).toBe("Claude");
  });

  it("critical windows turn red", () => {
    const r = usageRing(prov("codex", [win("5h", 97)]))!;
    expect(r.outer.color).toBe("var(--st-offline)");
  });

  it("one window = no inner ring; unparseable labels keep wire order", () => {
    expect(usageRing(prov("codex", [win("5h", 10)]))!.inner).toBeNull();
    const r = usageRing(prov("x", [win("?", 10), win("??", 50)]))!;
    expect(r.outer.label).toBe("?");
    expect(r.inner!.label).toBe("??");
  });

  it("a provider without windows has no ring", () => {
    expect(usageRing(prov("claude", []))).toBeNull();
  });

  it("usageRings keeps providers with data, in the configured order if given", () => {
    const list = [prov("codex", [win("5h", 1)]), prov("empty", []), prov("claude", [win("5h", 1)])];
    expect(usageRings(list).map((r) => r.provider)).toEqual(["codex", "claude"]);
    expect(usageRings(list, ["claude", "codex"]).map((r) => r.provider)).toEqual(["claude", "codex"]);
    expect(usageRings(list, ["claude"]).map((r) => r.provider)).toEqual(["claude", "codex"]);
    expect(usageRings([])).toEqual([]);
  });
});

describe("usage formatting", () => {
  const NOW = new Date(2026, 9, 9, 12, 0, 0).getTime();
  const at = (ms: number) => new Date(NOW + ms).toISOString();

  it("formats a span in en and cs", () => {
    expect(formatSpan("en", 20)).toBe("< 1 min");
    expect(formatSpan("en", 40 * 60)).toBe("40 min");
    expect(formatSpan("en", 80 * 60)).toBe("1 h 20 min");
    expect(formatSpan("en", 3 * 3600 + 20)).toBe("3 h");
    expect(formatSpan("cs", 20)).toBe("< 1 min");
    expect(formatSpan("cs", 80 * 60)).toBe("1 h 20 min");
  });

  it("a reset within 24 h is relative, a later one a date and time", () => {
    expect(formatReset("en", at(80 * 60 * 1000), NOW)).toBe("resets in 1 h 20 min");
    expect(formatReset("cs", at(80 * 60 * 1000), NOW)).toBe("obnoví se za 1 h 20 min");
    const later = new Date(2026, 9, 12, 14, 5).toISOString();
    expect(formatReset("en", later, NOW)).toBe("resets 12 Oct, 14:05");
    expect(formatReset("cs", later, NOW)).toBe("obnoví se 12. 10., 14:05");
    expect(formatReset("en", at(-1000), NOW)).toBe("resets now");
    expect(formatReset("cs", at(-1000), NOW)).toBe("obnovuje se");
    expect(formatReset("en", null, NOW)).toBeNull();
    expect(formatReset("en", "garbage", NOW)).toBeNull();
  });

  it("a window line: used percent, then the reset", () => {
    const w = win("5h", 61.6, { resetsAt: at(80 * 60 * 1000) });
    expect(windowLine("en", w, NOW)).toBe("62% used · resets in 1 h 20 min");
    expect(windowLine("cs", w, NOW)).toBe("využito 62 % · obnoví se za 1 h 20 min");
    expect(windowLine("en", win("7d", 3), NOW)).toBe("3% used");
  });

  it("the full-early hint", () => {
    expect(fullEarlyText("en", 2400)).toBe("runs out ~40 min before reset");
    expect(fullEarlyText("cs", 2400)).toBe("dojde zhruba 40 min před obnovením");
    expect(fullEarlyText("en", null)).toBeNull();
  });

  it("a ring's accessible name lists what is left per window", () => {
    const r = usageRing(prov("claude", [win("5h", 62), win("7d", 20)]))!;
    expect(ringAriaLabel("en", r)).toBe("Claude usage: 38% left (5h), 80% left (7d)");
    expect(ringAriaLabel("cs", r)).toBe("Využití Claude: zbývá 38 % (5h), zbývá 80 % (7d)");
  });
});
