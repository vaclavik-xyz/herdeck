// Framework-free client for the Statistics view: the runtime's `GET /stats`
// (src/herdeck/deckapp/stats.py, which merges every connected bridge's
// history — src/herdeck/history.py) relayed by the Rust `maintenance_call`
// proxy (desktop/src-tauri/src/maintenance.rs allow-lists the exact query).
// Pure parsing + formatting, unit-testable; the en/cs texts live in the view.

import type { InvokeFn } from "./deckClient";

export const STATS_RANGES = [1, 7, 30] as const;
export type StatsRange = (typeof STATS_RANGES)[number];
export const STATS_GROUPS = ["agent", "repo", "agent_type"] as const;
export type StatsGroup = (typeof STATS_GROUPS)[number];

export interface StatsBucket {
  workingMs: number;
  blockedMs: number;
  idleMs: number;
  waitingMs: number;
  doneMs: number;
  blockedCount: number;
  answeredCount: number;
  doneCount: number;
  answerMedianMs: number | null;
  answerP90Ms: number | null;
}

export interface StatsGroupRow extends StatsBucket {
  key: string;
  label: string;
}

export interface StatsDay extends StatsBucket {
  day: string; // YYYY-MM-DD (the runtime's local date)
}

export interface StatsMissing {
  serverId: string;
  reason: string;
}

export interface StatsReport {
  rangeDays: number;
  groupBy: string;
  truncated: boolean;
  servers: string[];
  missing: StatsMissing[];
  total: StatsBucket;
  groups: StatsGroupRow[];
  days: StatsDay[];
}

export type StatsResult =
  | { kind: "ok"; report: StatsReport }
  | { kind: "unavailable"; code: string; missing: StatsMissing[] }
  | { kind: "error"; message: string };

type Rec = Record<string, unknown>;
const rec = (v: unknown): Rec => (v && typeof v === "object" && !Array.isArray(v) ? (v as Rec) : {});
const count = (v: unknown): number => (typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : 0);
const optMs = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : null);
const text = (v: unknown): string => (typeof v === "string" ? v : "");

function bucket(raw: unknown): StatsBucket {
  const v = rec(raw);
  return {
    workingMs: count(v.working_ms),
    blockedMs: count(v.blocked_ms),
    idleMs: count(v.idle_ms),
    waitingMs: count(v.waiting_ms),
    doneMs: count(v.done_ms),
    blockedCount: count(v.blocked_count),
    answeredCount: count(v.answered_count),
    doneCount: count(v.done_count),
    answerMedianMs: optMs(v.answer_median_ms),
    answerP90Ms: optMs(v.answer_p90_ms),
  };
}

function missingList(raw: unknown): StatsMissing[] {
  return (Array.isArray(raw) ? raw : []).map((m) => {
    const v = rec(m);
    return { serverId: text(v.server_id), reason: text(v.reason) };
  }).filter((m) => m.serverId);
}

/** Parse the runtime's /stats answer; null when it is not one. */
export function parseStats(raw: unknown): StatsResult | null {
  const v = rec(raw);
  if (typeof v.ok !== "boolean") return null;
  if (!v.ok) return { kind: "unavailable", code: text(v.code) || "failed", missing: missingList(v.missing) };
  const groups = (Array.isArray(v.groups) ? v.groups : [])
    .map((g) => ({ ...bucket(g), key: text(rec(g).key), label: text(rec(g).label) || text(rec(g).key) }))
    .filter((g) => g.key);
  const days = (Array.isArray(v.days) ? v.days : [])
    .map((d) => ({ ...bucket(d), day: text(rec(d).day) }))
    .filter((d) => /^\d{4}-\d{2}-\d{2}$/.test(d.day));
  return {
    kind: "ok",
    report: {
      rangeDays: count(v.range_days),
      groupBy: text(v.group_by),
      truncated: v.truncated === true,
      servers: (Array.isArray(v.servers) ? v.servers : []).filter((s): s is string => typeof s === "string"),
      missing: missingList(v.missing),
      total: bucket(v.total),
      groups,
      days,
    },
  };
}

export function statsPath(range: StatsRange, group: StatsGroup): string {
  return `/stats?range=${range}&group=${group}`;
}

export async function fetchStats(invoke: InvokeFn, range: StatsRange, group: StatsGroup): Promise<StatsResult> {
  try {
    const raw = rec(await invoke("maintenance_call", { method: "GET", path: statsPath(range, group) }));
    const status = typeof raw.status === "number" ? raw.status : 0;
    if (status !== 200) return { kind: "error", message: `HTTP ${status}` };
    return parseStats(raw.body) ?? { kind: "error", message: "invalid /stats reply" };
  } catch (e) {
    return { kind: "error", message: String(e) };
  }
}

/** Total tracked (non-unknown) time of a bucket. */
export function activeMs(b: StatsBucket): number {
  return b.workingMs + b.blockedMs + b.idleMs + b.waitingMs + b.doneMs;
}

/** Compact duration: "45s", "12m", "3h 05m", "2d 4h". */
export function formatDuration(ms: number | null): string {
  if (ms == null) return "–";
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${String(m % 60).padStart(2, "0")}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

export const CHART_SERIES = ["workingMs", "blockedMs", "waitingMs", "idleMs"] as const;
export type ChartSeries = (typeof CHART_SERIES)[number];

export interface BarSegment {
  series: ChartSeries;
  y: number;
  height: number;
}

export interface DayBar {
  day: string;
  x: number;
  width: number;
  segments: BarSegment[];
  totalMs: number;
}

/** Stacked per-day bars (working / blocked / waiting / idle) scaled into a
 *  `width` × `height` box; the tallest day fills the height. */
export function dayBars(days: StatsDay[], width: number, height: number): DayBar[] {
  if (days.length === 0) return [];
  const max = Math.max(1, ...days.map((d) => CHART_SERIES.reduce((sum, k) => sum + d[k], 0)));
  const slot = width / days.length;
  const barWidth = Math.max(1, slot * 0.7);
  return days.map((d, i) => {
    let y = height;
    const segments: BarSegment[] = [];
    for (const series of CHART_SERIES) {
      const h = (d[series] / max) * height;
      if (h <= 0) continue;
      y -= h;
      segments.push({ series, y, height: h });
    }
    return {
      day: d.day,
      x: i * slot + (slot - barWidth) / 2,
      width: barWidth,
      segments,
      totalMs: CHART_SERIES.reduce((sum, k) => sum + d[k], 0),
    };
  });
}
