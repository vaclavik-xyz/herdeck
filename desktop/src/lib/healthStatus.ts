// "Why is the deck dark?" — turns the runtime's token-gated /health (proxied
// by the Rust `check_health` command, which adds the app's own
// `app_version`) into one short, non-blocking status line. Pure and
// framework-free so every rule is unit-testable; HealthNotice.svelte polls
// and renders it with its en/cs catalog.
import { fmt } from "./i18n.svelte";
import { compareVersions } from "./maintenanceClient";

/** The catalog keys the line is assembled from (HealthNotice owns en+cs). */
export interface HealthMessages {
  runtime_mismatch: string; // {runtime} {app}
  bridge_mismatch: string; // {id} {bridge} {runtime}
  bridge_protocol: string; // {id}
  bridge_token: string; // {id} {since}
  bridge_down: string; // {id} {since}
  d200_down: string; // {since}
  d200_locked: string; // {pid}
  seconds: string; // {n}
  minutes: string; // {n}
  hours: string; // {n}
}

/** A connection that dropped less than this long ago is not news yet — the
 *  connector's own reconnect usually heals it within a few seconds. */
export const HEALTH_GRACE_MS = 15_000;

type Rec = Record<string, unknown>;
const rec = (v: unknown): Rec => (v && typeof v === "object" ? (v as Rec) : {});
const str = (v: unknown): string | null => (typeof v === "string" && v ? v : null);
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

function ago(sinceMs: number | null, now: number, m: HealthMessages): string {
  if (sinceMs === null) return "";
  const s = Math.max(0, Math.round((now - sinceMs) / 1000));
  if (s < 60) return fmt(m.seconds, { n: s });
  if (s < 3600) return fmt(m.minutes, { n: Math.round(s / 60) });
  return fmt(m.hours, { n: Math.round(s / 3600) });
}

/** What a HealthNotice button next to a problem does. */
export type HealthAction =
  | { kind: "update_bridge"; serverId: string }
  | { kind: "restart_deck" }
  | { kind: "open_maintenance" };

export interface HealthItem {
  text: string;
  action: HealthAction;
}

/** Every problem the /health payload shows, most actionable first, each with
 *  the action that addresses it. Empty when the deck is fine (or the runtime
 *  predates these fields). */
export function healthItems(raw: unknown, m: HealthMessages, now: number = Date.now()): HealthItem[] {
  const h = rec(raw);
  const out: HealthItem[] = [];
  const maintenance: HealthAction = { kind: "open_maintenance" };
  const runtime = str(h.version);
  const app = str(h.app_version);
  if (runtime && app && runtime !== app) {
    out.push({ text: fmt(m.runtime_mismatch, { runtime, app }), action: maintenance });
  }
  for (const [id, value] of Object.entries(rec(h.servers))) {
    const s = rec(value);
    if (s.protocol_supported === false) {
      out.push({ text: fmt(m.bridge_protocol, { id }), action: maintenance });
    }
    const bridge = str(s.bridge_version);
    if (runtime && bridge && bridge !== runtime) {
      // Only a managed, self-updating bridge that is BEHIND the runtime can be
      // updated from here; anything else needs the Maintenance section's
      // explanation (managed install command, update the app, …).
      const updatable = s.self_update === true && s.managed === true && compareVersions(bridge, runtime) === -1;
      out.push({
        text: fmt(m.bridge_mismatch, { id, bridge, runtime }),
        action: updatable ? { kind: "update_bridge", serverId: id } : maintenance,
      });
    }
    const since = num(s.since);
    if (s.connected === false && (since === null || now - since >= HEALTH_GRACE_MS)) {
      const token = (str(s.last_error) ?? "").startsWith("token rejected");
      // A server that never connected (configured but not running) is not an
      // outage; a rejected token still is — that is a misconfiguration.
      if (s.ever_connected === false && !token) continue;
      out.push({
        text: fmt(token ? m.bridge_token : m.bridge_down, { id, since: ago(since, now, m) }).trim(),
        action: maintenance,
      });
    }
  }
  const d200 = rec(h.d200);
  const owner = num(d200.lock_owner);
  if (owner !== null) {
    out.push({ text: fmt(m.d200_locked, { pid: owner }), action: { kind: "restart_deck" } });
  } else if (d200.connected === false && num(d200.last_frame_at) !== null) {
    // Only a D200 this runtime has driven before counts as "disconnected":
    // a machine without one keeps failing to open it, and that is normal.
    const since = num(d200.since);
    if (since === null || now - since >= HEALTH_GRACE_MS) {
      out.push({ text: fmt(m.d200_down, { since: ago(since, now, m) }).trim(), action: { kind: "restart_deck" } });
    }
  }
  return out;
}

/** The problem texts alone (see `healthItems`). */
export function healthProblems(raw: unknown, m: HealthMessages, now: number = Date.now()): string[] {
  return healthItems(raw, m, now).map((item) => item.text);
}

/** The distinct actions of `items`, in order (one button each). */
export function healthActions(items: HealthItem[]): HealthAction[] {
  const seen = new Set<string>();
  const out: HealthAction[] = [];
  for (const { action } of items) {
    const key = action.kind === "update_bridge" ? `${action.kind}:${action.serverId}` : action.kind;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(action);
  }
  return out;
}

/** The single line HealthNotice shows ("" = nothing wrong). */
export function healthLine(raw: unknown, m: HealthMessages, now: number = Date.now()): string {
  return healthProblems(raw, m, now).join(" · ");
}
