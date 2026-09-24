// "Why is the deck dark?" — turns the runtime's token-gated /health (proxied
// by the Rust `check_health` command, which adds the app's own
// `app_version`) into a list of problems, each with a severity, a stable
// identity (for "dismiss until it changes") and the one action that fixes it.
// Pure and framework-free so every rule is unit-testable; the en/cs sentences
// live in noticeMessages.ts, the rendering in NoticeList.svelte (app window)
// and DeckStatusDot.svelte (deck window).
import { compareVersions } from "./maintenanceClient";

export type Severity = "error" | "warning" | "info";

/** Lower = worse; notices sort by this, the deck dot shows the lowest. */
export const SEVERITY_RANK: Record<Severity, number> = { error: 0, warning: 1, info: 2 };

/** A connection that dropped less than this long ago is not news yet — the
 *  connector's own reconnect usually heals it within a few seconds. */
export const HEALTH_GRACE_MS = 15_000;

/** What a notice's primary button does. */
export type HealthAction =
  | { kind: "update_bridge"; serverId: string }
  | { kind: "restart_deck" }
  | { kind: "restart_runtime" }
  | { kind: "fix_config" };

export type ProblemKind =
  | "config_error"
  | "runtime_mismatch"
  | "bridge_protocol"
  | "bridge_mismatch"
  | "bridge_newer"
  | "bridge_token"
  | "bridge_down"
  | "d200_down"
  | "d200_locked";

export interface HealthProblem {
  /** Identity of the problem ("bridge_down:local") — one row per key. */
  key: string;
  kind: ProblemKind;
  severity: Severity;
  /** Placeholders for the notice sentence ({id}, {runtime}, …). */
  vars: Record<string, string | number>;
  /** When a connection problem started (ms epoch) — the "(3 min)" suffix. */
  sinceMs: number | null;
  /** What "dismiss" is keyed on: a dismissed notice comes back when this
   *  changes (a new outage, other versions, another error text). Never the
   *  ticking duration. */
  content: string;
  /** The raw backend facts (error text, codes) — shown in title=, never as
   *  the sentence itself. */
  detail: string;
  action: HealthAction | null;
}

type Rec = Record<string, unknown>;
const rec = (v: unknown): Rec => (v && typeof v === "object" ? (v as Rec) : {});
const str = (v: unknown): string | null => (typeof v === "string" && v ? v : null);
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

/** Sort by severity (error → warning → info), keeping the payload order
 *  within one severity (config first, then runtime, bridges, D200). */
export function bySeverity<T extends { severity: Severity }>(items: T[]): T[] {
  return items
    .map((item, i) => ({ item, i }))
    .sort((a, b) => SEVERITY_RANK[a.item.severity] - SEVERITY_RANK[b.item.severity] || a.i - b.i)
    .map(({ item }) => item);
}

/** The worst severity of `items`, or null when there are none. */
export function worstSeverity(items: { severity: Severity }[]): Severity | null {
  let worst: Severity | null = null;
  for (const { severity } of items) {
    if (worst === null || SEVERITY_RANK[severity] < SEVERITY_RANK[worst]) worst = severity;
  }
  return worst;
}

/** Every problem the /health payload shows, worst first. Empty when the deck
 *  is fine (or the runtime predates these fields). */
export function healthProblems(raw: unknown, now: number = Date.now()): HealthProblem[] {
  const h = rec(raw);
  const out: HealthProblem[] = [];
  const push = (p: Omit<HealthProblem, "sinceMs" | "detail"> & { sinceMs?: number | null; detail?: string }) =>
    out.push({ sinceMs: null, detail: "", ...p });

  // An existing config that does not load: the runtime shows an error state
  // (never the demo fleet), so this is the first thing to fix.
  const configError = str(h.config_error);
  if (configError) {
    push({ key: "config_error", kind: "config_error", severity: "error", vars: {}, content: configError, detail: configError, action: { kind: "fix_config" } });
  }
  const runtime = str(h.version);
  const app = str(h.app_version);
  if (runtime && app && runtime !== app) {
    push({
      key: "runtime_mismatch", kind: "runtime_mismatch", severity: "warning", vars: { runtime, app },
      content: `${runtime}|${app}`, detail: `runtime ${runtime} ≠ app ${app}`, action: { kind: "restart_runtime" },
    });
  }
  for (const [id, value] of Object.entries(rec(h.servers))) {
    const s = rec(value);
    if (s.protocol_supported === false) {
      push({
        key: `bridge_protocol:${id}`, kind: "bridge_protocol", severity: "error", vars: { id },
        content: String(num(s.protocol) ?? ""), detail: `protocol ${String(s.protocol ?? "?")}`, action: null,
      });
    }
    const bridge = str(s.bridge_version);
    if (runtime && bridge && bridge !== runtime) {
      const order = compareVersions(bridge, runtime);
      // Only a managed, self-updating bridge that is BEHIND the runtime can be
      // updated from here; anything else needs the Maintenance section's
      // explanation (managed install command, update the app, …).
      const updatable = s.self_update === true && s.managed === true && order === -1;
      const newer = order === 1;
      push({
        key: `bridge_version:${id}`, kind: newer ? "bridge_newer" : "bridge_mismatch",
        severity: newer ? "info" : "warning", vars: { id, bridge, runtime },
        content: `${bridge}|${runtime}`, detail: `bridge ${bridge} ≠ runtime ${runtime}`,
        action: updatable ? { kind: "update_bridge", serverId: id } : null,
      });
    }
    const since = num(s.since);
    if (s.connected === false && (since === null || now - since >= HEALTH_GRACE_MS)) {
      const lastError = str(s.last_error) ?? "";
      const token = lastError.startsWith("token rejected");
      // A server that never connected (configured but not running, e.g. an
      // unused T3) is not an outage; a rejected token still is — that is a
      // misconfiguration.
      if (s.ever_connected === false && !token) continue;
      push({
        key: `bridge_link:${id}`, kind: token ? "bridge_token" : "bridge_down",
        severity: token ? "error" : "warning", vars: { id }, sinceMs: since,
        content: `${token ? "token" : "down"}|${since ?? ""}`, detail: lastError, action: null,
      });
    }
  }
  const d200 = rec(h.d200);
  const owner = num(d200.lock_owner);
  if (owner !== null) {
    push({
      key: "d200", kind: "d200_locked", severity: "warning", vars: { pid: owner },
      content: `locked|${owner}`, detail: `lock_owner ${owner}`, action: { kind: "restart_deck" },
    });
  } else if (d200.connected === false && num(d200.last_frame_at) !== null) {
    // Only a D200 this runtime has driven before counts as "disconnected":
    // a machine without one keeps failing to open it, and that is normal.
    const since = num(d200.since);
    if (since === null || now - since >= HEALTH_GRACE_MS) {
      push({
        key: "d200", kind: "d200_down", severity: "warning", vars: {}, sinceMs: since,
        content: `down|${since ?? ""}`, detail: str(d200.last_error) ?? "", action: { kind: "restart_deck" },
      });
    }
  }
  return bySeverity(out);
}

/** Where "Fix config…" goes: the settings section a config error most likely
 *  lives in, by the words the runtime's loader uses; Maintenance (which shows
 *  the full error and the recovery hint) otherwise. */
export function configErrorSection(error: string): string {
  const e = error.toLowerCase();
  if (/\b(server|servers|token|bridge|url)\b/.test(e)) return "servers";
  if (/\b(grid|deck|d200|layout)\b/.test(e)) return "deck";
  if (/\b(view|language|tile_\w+)\b/.test(e)) return "view";
  if (/\bprofile/.test(e)) return "profiles";
  return "maintenance";
}
