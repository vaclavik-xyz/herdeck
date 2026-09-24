import { describe, expect, it } from "vitest";
import { dayBars, fetchStats, formatDuration, parseStats, statsPath, type StatsDay } from "./statsClient";

const H = 3_600_000;

const raw = {
  ok: true,
  code: "ok",
  servers: ["m4"],
  missing: [{ server_id: "old", reason: "unsupported" }],
  range_days: 7,
  group_by: "repo",
  truncated: false,
  total: { working_ms: 3 * H, blocked_ms: H, answered_count: 2, blocked_count: 3, done_count: 1, answer_median_ms: 60_000, answer_p90_ms: null },
  groups: [{ key: "herdeck", label: "herdeck", working_ms: 3 * H }, { label: "no key" }],
  days: [{ day: "2026-09-24", working_ms: H }, { day: "bogus" }],
};

describe("statsClient", () => {
  it("parses a /stats answer defensively", () => {
    const r = parseStats(raw);
    expect(r?.kind).toBe("ok");
    if (r?.kind !== "ok") return;
    expect(r.report.total.workingMs).toBe(3 * H);
    expect(r.report.total.answerMedianMs).toBe(60_000);
    expect(r.report.total.answerP90Ms).toBeNull();
    expect(r.report.total.idleMs).toBe(0);
    expect(r.report.groups.map((g) => g.key)).toEqual(["herdeck"]);
    expect(r.report.days.map((d) => d.day)).toEqual(["2026-09-24"]);
    expect(r.report.missing).toEqual([{ serverId: "old", reason: "unsupported" }]);
    expect(parseStats({ ok: false, code: "unsupported", missing: [] })).toEqual({ kind: "unavailable", code: "unsupported", missing: [] });
    expect(parseStats(null)).toBeNull();
  });

  it("fetches through the maintenance proxy", async () => {
    const calls: unknown[] = [];
    const invoke = async (cmd: string, args?: Record<string, unknown>) => {
      calls.push([cmd, args]);
      return { status: 200, body: raw };
    };
    const r = await fetchStats(invoke, 30, "agent_type");
    expect(r.kind).toBe("ok");
    expect(calls).toEqual([["maintenance_call", { method: "GET", path: "/stats?range=30&group=agent_type" }]]);
    expect(statsPath(1, "repo")).toBe("/stats?range=1&group=repo");
    expect(await fetchStats(async () => ({ status: 404, body: null }), 7, "agent")).toEqual({ kind: "error", message: "HTTP 404" });
    expect((await fetchStats(async () => { throw new Error("down"); }, 7, "agent")).kind).toBe("error");
  });

  it("formats durations compactly", () => {
    expect(formatDuration(null)).toBe("–");
    expect(formatDuration(45_000)).toBe("45s");
    expect(formatDuration(12 * 60_000)).toBe("12m");
    expect(formatDuration(3 * H + 5 * 60_000)).toBe("3h 05m");
    expect(formatDuration(52 * H)).toBe("2d 4h");
  });

  it("scales stacked day bars to the tallest day", () => {
    const day = (d: string, working: number, blocked: number): StatsDay => ({
      day: d, workingMs: working, blockedMs: blocked, idleMs: 0, waitingMs: 0, doneMs: 0,
      blockedCount: 0, answeredCount: 0, doneCount: 0, answerMedianMs: null, answerP90Ms: null,
    });
    const bars = dayBars([day("a", 2 * H, 2 * H), day("b", H, 0)], 200, 100);
    expect(bars).toHaveLength(2);
    expect(bars[0].segments.map((s) => [s.series, s.y, s.height])).toEqual([["workingMs", 50, 50], ["blockedMs", 0, 50]]);
    expect(bars[1].segments).toEqual([{ series: "workingMs", y: 75, height: 25 }]);
    expect(bars[1].x).toBeGreaterThan(bars[0].x);
    expect(dayBars([], 100, 100)).toEqual([]);
  });
});
