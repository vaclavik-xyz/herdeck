// Bridge-owned shared settings in the config editor (issue #116, spec S5-S7).
//
// A bridge that offers the `settings` capability can own the "shared" part of
// the config (notification rules, answer profiles, safety, macros, launchers,
// usage alerts). The editor edits those fields on a selected TARGET: either a
// bridge (its document from `GET /config` `bridges`, saved with
// `POST /bridge-settings/<id>`) or "This Mac (fallback)" — the local
// config.toml, used by bridges that have not adopted shared settings yet.
//
// Framework-free: pure helpers + an injected put function, like configClient.
//
// The key lists MIRROR src/herdeck/shared_settings.py (SHARED_NOTIFICATION_KEYS,
// SHARED_USAGE_KEYS, SHARED_WHOLE_SECTIONS, extract_shared). The bridge rejects
// any other key, so keep them in sync — bridgeSettings.test.ts pins them.
import type { ConfigPayload } from "./configClient";

export const SHARED_NOTIFICATION_KEYS = ["on", "done_min_work", "done_short_delay", "remind_after", "subagents_done"] as const;
export const SHARED_USAGE_KEYS = ["alert_at", "alert_reset"] as const;
export const SHARED_WHOLE_SECTIONS = ["answer_profiles", "safety", "macros", "start_profiles"] as const;
/** Sections where only some keys are shared (the rest stays local). */
export const SHARED_PARTIAL: Record<string, readonly string[]> = {
  notifications: SHARED_NOTIFICATION_KEYS,
  usage: SHARED_USAGE_KEYS,
};
/** Editor sections that carry shared fields (they get the target picker). */
export const SHARED_SECTIONS: readonly string[] = ["notifications", "usage", ...SHARED_WHOLE_SECTIONS];

/** The picker value for "This Mac (fallback)". Never a server id (ids are non-empty). */
export const FALLBACK_TARGET = "";

type Rec = Record<string, unknown>;

function rec(v: unknown): Rec {
  return v != null && typeof v === "object" && !Array.isArray(v) ? (v as Rec) : {};
}

function clone<T>(v: T): T {
  return v === undefined ? v : (JSON.parse(JSON.stringify(v)) as T);
}

/** One bridge's shared-settings state as `GET /config` `bridges` reports it. */
export interface BridgeShared {
  /** The connected bridge advertises the `settings` capability. */
  offered: boolean;
  connected: boolean;
  /** 0 = never set (the bridge still uses each Mac's local values). */
  revision: number;
  updatedAtMs: number;
  updatedBy: string;
  /** Adopted: the bridge owns the shared settings. */
  set: boolean;
  source: "bridge" | "cache" | "none";
  settings: Rec | null;
}

export function parseBridges(raw: unknown): Record<string, BridgeShared> {
  const out: Record<string, BridgeShared> = {};
  for (const [id, value] of Object.entries(rec(raw))) {
    if (id === "") continue;
    const v = rec(value);
    const source = v.source === "bridge" || v.source === "cache" ? v.source : "none";
    const settings = v.settings != null && typeof v.settings === "object" && !Array.isArray(v.settings)
      ? (v.settings as Rec)
      : null;
    out[id] = {
      offered: v.offered === true,
      connected: v.connected === true,
      revision: typeof v.revision === "number" ? v.revision : 0,
      updatedAtMs: typeof v.updated_at_ms === "number" ? v.updated_at_ms : 0,
      updatedBy: typeof v.updated_by === "string" ? v.updated_by : "",
      set: v.set === true && settings != null,
      source,
      settings,
    };
  }
  return out;
}

export function parseOverlayIgnored(raw: unknown): string[] {
  return Array.isArray(raw) ? raw.filter((v): v is string => typeof v === "string") : [];
}

/** The shared part of a config table (base or a profile overlay): only the
 *  keys present, deep-copied. Mirrors shared_settings.extract_shared. */
export function extractShared(table: Rec): Rec {
  const out: Rec = {};
  for (const [section, keys] of Object.entries(SHARED_PARTIAL)) {
    const t = rec(table[section]);
    const picked: Rec = {};
    for (const key of keys) if (key in t) picked[key] = clone(t[key]);
    if (Object.keys(picked).length > 0) out[section] = picked;
  }
  for (const section of SHARED_WHOLE_SECTIONS) {
    if (section in table) out[section] = clone(table[section]);
  }
  return out;
}

/** A copy of `table` whose every shared location holds `source`'s value (or
 *  is absent when `source` lacks it). Non-shared keys and key order stay. */
export function overwriteShared(table: Rec, source: Rec): Rec {
  const out = clone(table) ?? {};
  for (const [section, keys] of Object.entries(SHARED_PARTIAL)) {
    const src = rec(source[section]);
    const had = section in out;
    const t: Rec = rec(out[section]);
    for (const key of keys) {
      if (key in src) t[key] = clone(src[key]);
      else delete t[key];
    }
    if (had || Object.keys(t).length > 0) out[section] = t;
    if (Object.keys(t).length === 0 && !(section in source)) delete out[section];
  }
  for (const section of SHARED_WHOLE_SECTIONS) {
    if (section in source) out[section] = clone(source[section]);
    else delete out[section];
  }
  return out;
}

/** The payload a shared section edits for a bridge target: base shared keys
 *  from the bridge document, and NO profile overlays of shared keys (an
 *  adopted bridge ignores them, spec S7). */
export function composeShared(payload: ConfigPayload, settings: Rec): ConfigPayload {
  const profiles: Record<string, Rec> = {};
  for (const [name, overlay] of Object.entries(payload.profiles)) profiles[name] = overwriteShared(overlay, {});
  return { ...payload, base: overwriteShared(payload.base, settings), profiles };
}

/** Undo `composeShared` after a section edit: the local payload (shared keys
 *  back to `original`'s) and the edited shared document. */
export function splitShared(original: ConfigPayload, edited: ConfigPayload): { payload: ConfigPayload; shared: Rec } {
  const profiles: Record<string, Rec> = {};
  for (const [name, overlay] of Object.entries(edited.profiles)) {
    profiles[name] = overwriteShared(overlay, extractShared(original.profiles[name] ?? {}));
  }
  return {
    payload: { ...edited, base: overwriteShared(edited.base, extractShared(original.base)), profiles },
    shared: extractShared(edited.base),
  };
}

/** Key-order independent JSON, for "did this change" checks. */
export function stableJson(v: unknown): string {
  if (Array.isArray(v)) return `[${v.map(stableJson).join(",")}]`;
  if (v != null && typeof v === "object") {
    const o = v as Rec;
    return `{${Object.keys(o).sort().map((k) => `${JSON.stringify(k)}:${stableJson(o[k])}`).join(",")}}`;
  }
  return JSON.stringify(v) ?? "null";
}

// --- targets -------------------------------------------------------------------

/** Bridges the picker lists: connected ones that offer settings, plus adopted
 *  ones even while offline (shown read-only with their last known settings). */
export function targetIds(bridges: Record<string, BridgeShared>): string[] {
  return Object.entries(bridges)
    .filter(([, b]) => (b.offered && b.connected) || b.set)
    .map(([id]) => id);
}

/** Connected bridges that adopted shared settings. */
export function adoptedIds(bridges: Record<string, BridgeShared>): string[] {
  return Object.entries(bridges).filter(([, b]) => b.set && b.connected).map(([id]) => id);
}

/** First adopted connected bridge, else "This Mac (fallback)". */
export function defaultTarget(bridges: Record<string, BridgeShared>): string {
  return adoptedIds(bridges)[0] ?? FALLBACK_TARGET;
}

/** Keep a still-listed target across reloads; otherwise fall back to the default. */
export function resolveTarget(current: string | null, bridges: Record<string, BridgeShared>): string {
  if (current != null && (current === FALLBACK_TARGET || targetIds(bridges).includes(current))) return current;
  return defaultTarget(bridges);
}

/** - fallback: This Mac (config.toml) is edited.
 *  - unset: a bridge that has not adopted shared settings — it uses this Mac's
 *    values, so the fields edit config.toml and adoption is offered.
 *  - adopted: the bridge's own document is edited.
 *  - offline: an adopted bridge that is not connected — read-only. */
export type TargetMode = "fallback" | "unset" | "adopted" | "offline";

export function targetMode(bridges: Record<string, BridgeShared>, target: string): TargetMode {
  const b = target === FALLBACK_TARGET ? undefined : bridges[target];
  if (b == null) return "fallback";
  if (!b.set) return "unset";
  return b.connected ? "adopted" : "offline";
}

// --- saving --------------------------------------------------------------------

/** How the editor reaches `POST /bridge-settings/<id>`: resolves to the
 *  runtime's `{status, body}` (see configClient `commandTransport`). */
export type BridgePutFn = (serverId: string, body: { base_revision: number; settings: Rec }) => Promise<unknown>;

/** An unsaved edit of one bridge's document, based on the revision it was read at. */
export interface BridgeDraft {
  baseRevision: number;
  settings: Rec;
}

export type PutErrorCode =
  | "stale_revision" | "invalid" | "too_large" | "offline" | "timeout" | "bridge_error" | "unknown_server" | "unreachable" | "http";

export interface PutOutcome {
  serverId: string;
  ok: boolean;
  /** HTTP status (0 = the runtime was unreachable). */
  status: number;
  revision: number | null;
  error: PutErrorCode | null;
  messages: string[];
}

function errorCode(status: number, error: unknown): PutErrorCode {
  if (status === 409) return "stale_revision";
  if (status === 422) return error === "too_large" ? "too_large" : "invalid";
  if (status === 503) return "offline";
  if (status === 504) return "timeout";
  if (status === 502) return "bridge_error";
  if (status === 404) return "unknown_server";
  return "http";
}

/** Send one put; never throws. */
export async function putBridgeSettings(put: BridgePutFn, serverId: string, baseRevision: number, settings: Rec): Promise<PutOutcome> {
  try {
    const r = rec(await put(serverId, { base_revision: baseRevision, settings }));
    const status = typeof r.status === "number" ? r.status : 0;
    const body = rec(r.body);
    const revision = typeof body.revision === "number" ? body.revision : null;
    const messages = Array.isArray(body.messages) ? body.messages.filter((m): m is string => typeof m === "string") : [];
    const ok = status === 200 && body.ok === true;
    return { serverId, ok, status, revision, error: ok ? null : errorCode(status, body.error), messages };
  } catch (e) {
    return { serverId, ok: false, status: 0, revision: null, error: "unreachable", messages: [String(e)] };
  }
}

/** Save every draft (one put per bridge, each with its own base revision).
 *  With `applyAllFrom`, that bridge's draft also goes to every other adopted
 *  connected bridge, based on that bridge's current revision. */
export async function saveDrafts(
  put: BridgePutFn,
  bridges: Record<string, BridgeShared>,
  drafts: Record<string, BridgeDraft>,
  applyAllFrom: string | null,
): Promise<PutOutcome[]> {
  const plan = new Map<string, BridgeDraft>();
  for (const [id, draft] of Object.entries(drafts)) plan.set(id, draft);
  const source = applyAllFrom != null ? drafts[applyAllFrom] : undefined;
  if (source != null) {
    for (const id of adoptedIds(bridges)) {
      const own = drafts[id]?.baseRevision ?? bridges[id].revision;
      plan.set(id, { baseRevision: own, settings: source.settings });
    }
  }
  const out: PutOutcome[] = [];
  for (const [id, draft] of plan) out.push(await putBridgeSettings(put, id, draft.baseRevision, draft.settings));
  return out;
}
