// Client for the runtime's GET /usage (DeckApp.usage_payload): the usage hub's
// providers in wire form, relayed by the Rust `agent_call` proxy (the token is
// injected Rust-side). The dock polls it for the footer usage rings.

import type { InvokeFn } from "./deckClient";

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
