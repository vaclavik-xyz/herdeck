// Framework-free client for the mini dock: the runtime's GET /agent/list
// (src/herdeck/deckapp/dock_data.py), relayed by the Rust `agent_call` proxy
// (token injected Rust-side), and project favicons served on the `herdeck`
// image scheme (`/project-icon/<hash>`, proxy.rs). Actions on an agent reuse
// agentCardClient.ts (/agent/detail, /agent/answer, /agent/focus).

import {
  imageSchemeBase,
  LONG_POLL_IGNORED_MS,
  LONG_POLL_MIN_GAP_MS,
  type InvokeFn,
} from "./deckClient";
import { langOf, type Lang } from "./i18n.svelte";
import { parseDockPrefs, type DockPrefs } from "./dockStrip";

export type DockStatus = "blocked" | "waiting" | "done" | "working" | "idle" | "unknown";
const STATUSES: readonly string[] = ["blocked", "waiting", "done", "working", "idle", "unknown"];

/** One agent as the dock shows it (a /agent/list row). */
export interface DockAgent {
  serverId: string;
  paneId: string;
  agentType: string;
  title: string; // task title, else workspace, repo, label (runtime-side)
  repo: string;
  workspace: string;
  branch: string;
  tab: string;
  status: DockStatus;
  sinceS: number; // seconds in this status when the list was sent
  waitingOn: string;
  subagents: number; // running
  offline: boolean; // its server is down (status is the last known one)
  icon: string | null; // project icon hash, null = monogram
  monogram: string;
}

export interface DockList {
  version: number;
  language: Lang;
  connected: boolean;
  agents: DockAgent[];
  /** What a project without a favicon shows: its monogram or the agent's logo. */
  iconFallback: "monogram" | "agent";
}

export const ICON_HASH_RE = /^[0-9a-f]{16}$/;
/** Long-poll hold. Must stay <= agent_card.rs AGENT_WAIT_MAX_MS (20 s): the
 *  proxy clamps there and times out at wait + 6 s. The runtime caps at 25 s. */
export const DOCK_POLL_MS = 20000;
/** Cadence after a failed poll (or a runtime that did not hold the request). */
export const DOCK_RETRY_MS = 2000;
/** Floor between two polls whose version changed (shared with /state). */
export const DOCK_MIN_GAP_MS = LONG_POLL_MIN_GAP_MS;
/** A long-poll answered unchanged faster than this was not held (shared). */
export const DOCK_IGNORED_MS = LONG_POLL_IGNORED_MS;

const str = (v: unknown): string => (typeof v === "string" ? v : "");

/** Shape one /agent/list row, or null when it is not one. */
export function parseDockAgent(raw: unknown): DockAgent | null {
  if (raw == null || typeof raw !== "object") return null;
  const v = raw as Record<string, unknown>;
  const serverId = str(v.server_id);
  const paneId = str(v.pane_id);
  if (!serverId || !paneId) return null;
  if (typeof v.status !== "string" || !STATUSES.includes(v.status)) return null;
  if (typeof v.since_s !== "number" || !Number.isFinite(v.since_s) || v.since_s < 0) return null;
  const icon = typeof v.icon === "string" && ICON_HASH_RE.test(v.icon) ? v.icon : null;
  const monogram = Array.from(str(v.monogram))[0] ?? "?";
  const subagents =
    typeof v.subagents === "number" && Number.isInteger(v.subagents) && v.subagents > 0 ? v.subagents : 0;
  return {
    serverId,
    paneId,
    agentType: str(v.agent_type),
    title: str(v.title),
    repo: str(v.repo),
    workspace: str(v.workspace),
    branch: str(v.branch),
    tab: str(v.tab),
    status: v.status as DockStatus,
    sinceS: Math.floor(v.since_s),
    waitingOn: str(v.waiting_on),
    subagents,
    offline: v.offline === true,
    icon,
    monogram,
  };
}

/** Shape a /agent/list body; malformed rows are dropped, a malformed
 *  envelope is null. */
export function parseDockList(raw: unknown): DockList | null {
  if (raw == null || typeof raw !== "object") return null;
  const v = raw as Record<string, unknown>;
  if (typeof v.version !== "number" || !Number.isInteger(v.version) || v.version < 0) return null;
  if (!Array.isArray(v.agents)) return null;
  return {
    version: v.version,
    language: langOf(v.language),
    connected: v.connected === true,
    iconFallback: v.icon_fallback === "agent" ? "agent" : "monogram",
    agents: v.agents.map(parseDockAgent).filter((a): a is DockAgent => a !== null),
  };
}

/** `herdeck://localhost/project-icon/<hash>` (Windows: `http://herdeck.localhost/…`). */
export function projectIconUrl(base: string, hash: string): string | null {
  return ICON_HASH_RE.test(hash) ? `${base}/project-icon/${hash}` : null;
}

const AGENT_TYPE_RE = /^[a-z0-9_-]{1,32}$/;

/** `herdeck://localhost/agent-mark/<type>`: the agent's own logo (runtime
 *  `dock_data.agent_mark_png`), the fallback for a project without a favicon. */
export function agentMarkUrl(base: string, agentType: string): string | null {
  return AGENT_TYPE_RE.test(agentType) ? `${base}/agent-mark/${agentType}` : null;
}

/** The favicon when the agent has one, else its agent mark when the fallback
 *  is "agent", else null (the monogram). */
export function markUrl(
  base: string,
  agent: DockAgent,
  fallback: "monogram" | "agent",
): string | null {
  if (agent.icon) return projectIconUrl(base, agent.icon);
  return fallback === "agent" ? agentMarkUrl(base, agent.agentType) : null;
}

/** Delay before the next /agent/list poll (same rules as deckClient's
 *  nextPollDelay, with the dock's own constants). */
export function dockNextDelay(p: {
  failed: boolean;
  longPolled: boolean;
  changed: boolean;
  elapsedMs: number;
}): number {
  if (p.failed) return DOCK_RETRY_MS;
  if (!p.longPolled) return 0;
  if (!p.changed && p.elapsedMs < DOCK_IGNORED_MS) return DOCK_RETRY_MS;
  return p.changed ? Math.max(0, DOCK_MIN_GAP_MS - p.elapsedMs) : 0;
}

export interface DockTransport {
  /** `after` null = a plain poll; else long-poll until the version moves. */
  list(after: number | null, waitMs: number): Promise<DockList | null>;
  iconUrl(hash: string): string | null;
  /** The image for an agent's dock item, per the list's icon fallback. */
  markUrl(agent: DockAgent, fallback: "monogram" | "agent"): string | null;
  /** A usage provider's logo (`claude`, `codex`): the same agent mark. */
  providerMarkUrl(provider: string): string | null;
}

export function dockTransport(invoke: InvokeFn, schemeBase?: string): DockTransport {
  const base =
    schemeBase ?? imageSchemeBase(typeof navigator === "undefined" ? "" : navigator.userAgent);
  return {
    async list(after, waitMs) {
      const args =
        after === null
          ? { method: "GET", path: "/agent/list" }
          : { method: "GET", path: `/agent/list?after=${after}&wait_ms=${waitMs}`, waitMs };
      try {
        const raw = (await invoke("agent_call", args)) as { status?: unknown; body?: unknown } | null;
        return raw?.status === 200 ? parseDockList(raw.body) : null;
      } catch {
        return null;
      }
    },
    iconUrl: (hash) => projectIconUrl(base, hash),
    markUrl: (agent, fallback) => markUrl(base, agent, fallback),
    providerMarkUrl: (provider) => agentMarkUrl(base, provider),
  };
}

/** The dock's `[desktop]` prefs from the runtime's redacted config (Rust
 *  `config_read` → GET /config), or null when it cannot be read (keep what
 *  the dock has). Re-read on `dock-prefs-changed` after the editor's Apply. */
export async function fetchDockPrefs(invoke: InvokeFn): Promise<DockPrefs | null> {
  try {
    const raw = (await invoke("config_read")) as { base?: unknown } | null;
    if (raw == null || typeof raw !== "object" || raw.base == null || typeof raw.base !== "object") return null;
    return parseDockPrefs((raw.base as Record<string, unknown>).desktop);
  } catch {
    return null;
  }
}
