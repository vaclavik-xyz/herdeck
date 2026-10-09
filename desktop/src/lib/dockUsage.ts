// Client for the runtime's GET /usage (DeckApp.usage_payload): the usage hub's
// providers in wire form, relayed by the Rust `agent_call` proxy (the token is
// injected Rust-side). The dock polls it for the footer usage rings; the
// ring maths, colours and the card's en/cs formatting live here too.

import type { InvokeFn } from "./deckClient";
import { DOCK_MESSAGES } from "./dockMessages";
import { fmt, type Lang } from "./i18n.svelte";

/** One usage window (`5h`, `7d`, ...) of a provider. */
export interface UsageWindow {
  label: string;
  /** 0..100, clamped. */
  usedPercent: number;
  /** ISO timestamp, or null when the provider did not report one. */
  resetsAt: string | null;
  /** Seconds by which the burn rate fills the window before it resets. */
  fullEarlyS: number | null;
}

export interface ProviderUsage {
  provider: string;
  subscription: string;
  plan: string | null;
  windows: UsageWindow[];
}

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

function parseWindow(raw: unknown): UsageWindow | null {
  if (raw == null || typeof raw !== "object") return null;
  const v = raw as Record<string, unknown>;
  const used = num(v.used_percent);
  if (typeof v.label !== "string" || !v.label || used === null) return null;
  const early = num(v.full_early_s);
  return {
    label: v.label,
    usedPercent: Math.min(100, Math.max(0, used)),
    resetsAt: typeof v.resets_at === "string" && v.resets_at ? v.resets_at : null,
    fullEarlyS: early !== null && early > 0 ? early : null,
  };
}

function parseProvider(raw: unknown): ProviderUsage | null {
  if (raw == null || typeof raw !== "object") return null;
  const v = raw as Record<string, unknown>;
  if (typeof v.provider !== "string" || !v.provider) return null;
  return {
    provider: v.provider,
    subscription: typeof v.subscription === "string" ? v.subscription : "unknown",
    plan: typeof v.plan === "string" && v.plan ? v.plan : null,
    windows: Array.isArray(v.windows)
      ? v.windows.map(parseWindow).filter((w): w is UsageWindow => w !== null)
      : [],
  };
}

/** Shape a /usage body; a malformed body or row yields no providers / is dropped. */
export function parseUsage(raw: unknown): ProviderUsage[] {
  if (raw == null || typeof raw !== "object") return [];
  const providers = (raw as Record<string, unknown>).providers;
  if (!Array.isArray(providers)) return [];
  return providers.map(parseProvider).filter((p): p is ProviderUsage => p !== null);
}

/** GET /usage through `agent_call`; any failure is an empty list. */
export async function fetchUsage(invoke: InvokeFn): Promise<ProviderUsage[]> {
  try {
    const raw = (await invoke("agent_call", { method: "GET", path: "/usage" })) as
      | { status?: unknown; body?: unknown }
      | null;
    return raw?.status === 200 ? parseUsage(raw.body) : [];
  } catch {
    return [];
  }
}

// --- footer rings + usage card: pure maths, colours and formatting ---------

/** The dock re-reads /usage this often (and once whenever the window shows). */
export const USAGE_POLL_MS = 60_000;
/** Below this fraction left a ring turns `--st-blocked` (amber)… */
export const USAGE_LOW_LEFT = 0.15;
/** …and below this one `--st-offline` (red). */
export const USAGE_CRITICAL_LEFT = 0.05;
/** The usage card beside the strip (CSS px, unzoomed); the window is grown
 *  for USAGE_CARD_MAX_HEIGHT below the ring, like the agent card's max. */
export const USAGE_CARD_WIDTH = 264;
export const USAGE_CARD_MAX_HEIGHT = 260;

/** One colour per provider. Codex is near-white because green already means
 *  "working" on the strip; any other provider takes the accent. */
export const PROVIDER_COLORS: Readonly<Record<string, string>> = {
  claude: "#D97757",
  codex: "#E6E6E6",
};
const PROVIDER_NAMES: Readonly<Record<string, string>> = { claude: "Claude", codex: "Codex" };

export function providerColor(provider: string): string {
  return PROVIDER_COLORS[provider] ?? "var(--accent)";
}

export function providerName(provider: string): string {
  return PROVIDER_NAMES[provider] ?? provider.charAt(0).toUpperCase() + provider.slice(1);
}

/** A window label from usage._window_label ("30m", "5h", "7d") in minutes;
 *  null for anything else ("?"). */
export function windowMinutes(label: string): number | null {
  const m = /^(\d+)([mhd])$/.exec(label);
  if (!m) return null;
  return Number(m[1]) * { m: 1, h: 60, d: 1440 }[m[2] as "m" | "h" | "d"];
}

/** The fraction of a window still unused (0..1). */
export function leftFraction(usedPercent: number): number {
  return Math.min(1, Math.max(0, 1 - usedPercent / 100));
}

export type UsageTone = "ok" | "low" | "critical";

export function usageTone(left: number): UsageTone {
  if (left < USAGE_CRITICAL_LEFT) return "critical";
  if (left < USAGE_LOW_LEFT) return "low";
  return "ok";
}

function toneColor(tone: UsageTone, base: string): string {
  return tone === "critical" ? "var(--st-offline)" : tone === "low" ? "var(--st-blocked)" : base;
}

/** One ring of a provider's badge: what is left of one window. */
export interface RingArc {
  label: string;
  left: number;
  tone: UsageTone;
  color: string;
}

export interface UsageRing {
  provider: string;
  name: string;
  /** The provider's own colour (the centre mark); arcs may be toned. */
  color: string;
  /** The shortest window (the session, e.g. 5h). */
  outer: RingArc;
  /** The longest window (e.g. 7d); null with a single window. */
  inner: RingArc | null;
}

/** A provider's windows, shortest first; labels that do not parse keep
 *  their wire order after the ones that do. */
export function windowsByLength(windows: UsageWindow[]): UsageWindow[] {
  const len = (w: UsageWindow) => windowMinutes(w.label) ?? Number.POSITIVE_INFINITY;
  return windows
    .map((w, i) => ({ w, i }))
    .sort((a, b) => len(a.w) - len(b.w) || a.i - b.i)
    .map(({ w }) => w);
}

function arcOf(w: UsageWindow, base: string): RingArc {
  const left = leftFraction(w.usedPercent);
  const tone = usageTone(left);
  return { label: w.label, left, tone, color: toneColor(tone, base) };
}

/** A provider's ring, or null when it reported no window. */
export function usageRing(p: ProviderUsage): UsageRing | null {
  if (p.windows.length === 0) return null;
  const sorted = windowsByLength(p.windows);
  const color = providerColor(p.provider);
  return {
    provider: p.provider,
    name: providerName(p.provider),
    color,
    outer: arcOf(sorted[0], color),
    inner: sorted.length > 1 ? arcOf(sorted[sorted.length - 1], color) : null,
  };
}

/** The footer's rings: providers with data, in `order` (`[usage].providers`)
 *  when given — unlisted ones follow in wire order. The runtime already sends
 *  them in that config order, so the dock passes none. */
export function usageRings(providers: ProviderUsage[], order: readonly string[] = []): UsageRing[] {
  const rank = (p: string) => {
    const i = order.indexOf(p);
    return i < 0 ? order.length : i;
  };
  return providers
    .map((p, i) => ({ ring: usageRing(p), i }))
    .filter((x): x is { ring: UsageRing; i: number } => x.ring !== null)
    .sort((a, b) => rank(a.ring.provider) - rank(b.ring.provider) || a.i - b.i)
    .map(({ ring }) => ring);
}

/** SVG dash for an arc on a `pathLength="100"` circle that starts at 12
 *  o'clock: the stroke covers what is LEFT and ends at 12, so the empty part
 *  grows clockwise from the top as the window is used. Null = draw nothing
 *  (a round cap on a zero-length dash would still paint a dot). */
export function ringArc(left: number): { dash: string; offset: string } | null {
  const l = Math.round(Math.min(1, Math.max(0, left)) * 1000) / 10;
  if (l < 0.5) return null;
  const used = Math.round((100 - l) * 10) / 10;
  return { dash: `${l} 100`, offset: used === 0 ? "0" : `-${used}` };
}

const pct = (usedPercent: number) => Math.round(Math.min(100, Math.max(0, usedPercent)));

/** "< 1 min", "40 min", "1 h 20 min", "3 h". */
export function formatSpan(lang: Lang, seconds: number): string {
  const m = DOCK_MESSAGES[lang];
  const mins = Math.floor(Math.max(0, seconds) / 60);
  if (mins < 1) return m.span_lt_min;
  if (mins < 60) return fmt(m.span_min, { m: mins });
  const h = Math.floor(mins / 60);
  const rest = mins % 60;
  return rest === 0 ? fmt(m.span_h, { h }) : fmt(m.span_hm, { h, m: rest });
}

const MONTHS_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const DAY_MS = 86_400_000;

/** "resets in 1 h 20 min" within a day, else the local date and time
 *  ("resets 12 Oct, 14:05"); null without a (valid) reset time. */
export function formatReset(lang: Lang, resetsAt: string | null, nowMs: number): string | null {
  if (!resetsAt) return null;
  const at = Date.parse(resetsAt);
  if (!Number.isFinite(at)) return null;
  const m = DOCK_MESSAGES[lang];
  const dt = at - nowMs;
  if (dt <= 0) return m.usage_resets_now;
  if (dt <= DAY_MS) return fmt(m.usage_resets_in, { t: formatSpan(lang, dt / 1000) });
  const d = new Date(at);
  const time = `${d.getHours()}:${String(d.getMinutes()).padStart(2, "0")}`;
  const date = fmt(m.usage_date, { d: d.getDate(), m: d.getMonth() + 1, mon: MONTHS_EN[d.getMonth()], time });
  return fmt(m.usage_resets_at, { t: date });
}

/** "62% used · resets in 1 h 20 min" (the reset part only when known). */
export function windowLine(lang: Lang, w: UsageWindow, nowMs: number): string {
  const used = fmt(DOCK_MESSAGES[lang].usage_used, { p: pct(w.usedPercent) });
  const reset = formatReset(lang, w.resetsAt, nowMs);
  return reset ? `${used} · ${reset}` : used;
}

/** "runs out ~40 min before reset", or null when the window is not on track
 *  to fill early. */
export function fullEarlyText(lang: Lang, fullEarlyS: number | null): string | null {
  if (fullEarlyS === null || fullEarlyS <= 0) return null;
  return fmt(DOCK_MESSAGES[lang].usage_full_early, { t: formatSpan(lang, fullEarlyS) });
}

/** The ring button's accessible name: what is left of each window. */
export function ringAriaLabel(lang: Lang, ring: UsageRing): string {
  const m = DOCK_MESSAGES[lang];
  const arcs = ring.inner ? [ring.outer, ring.inner] : [ring.outer];
  const windows = arcs
    .map((a) => fmt(m.usage_left, { p: Math.round(a.left * 100), label: a.label }))
    .join(", ");
  return fmt(m.usage_ring, { name: ring.name, windows });
}
