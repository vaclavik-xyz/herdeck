// Framework-free client for the desktop agent card: the runtime's /agent/*
// routes (src/herdeck/deckapp/agent_card.py), relayed by the Rust shell's
// `agent_call` command (desktop/src-tauri/src/agent_card.rs), which injects the
// access token so it never lives in JS. Kept DOM- and Svelte-free so the
// parsing and outcome mapping are unit-testable, like deckClient.ts.

import type { InvokeFn } from "./deckClient";

/** One answer choice, exactly as the deck drill would offer it. */
export interface AgentOption {
  key: string; // what the card sends back (option number, fallback/T3 action id)
  label: string;
  id: string | null; // approve | approve_always | deny | … (colour + confirm)
  kind: "option" | "fallback" | "backend";
  confirm: boolean; // the deck arms this first ([safety].require_confirm_for)
}

export type SubagentStatus = "running" | "done" | "failed" | "stale";

/** One subagent of the agent (bridge capability "subagents", read from the
 *  herdeck-subagent-hook spool). Most recent first, as the runtime sends it. */
export interface SubagentRow {
  id: string;
  provider: string;
  type: string;
  description: string;
  model: string;
  depth: number | null; // 1 = spawned by the agent itself; null = unknown
  status: SubagentStatus;
  durationS: number; // running/stale: so far; done/failed: total
}

export interface AgentRef {
  serverId: string;
  paneId: string;
}

/** A card opens on a deck tile (resolved once by the runtime), then follows
 *  that agent by identity so a re-sorted deck cannot swap it underneath. */
export type AgentTarget = { index: number } | AgentRef;

export interface AgentDetail extends AgentRef {
  agentType: string;
  displayAgent: string;
  label: string;
  title: string;
  repo: string;
  branch: string;
  workspace: string;
  tab: string;
  status: string;
  sinceS: number | null;
  backend: string;
  connected: boolean;
  prompt: string | null; // sanitized full prompt; null = none / not read yet
  promptPending: boolean;
  revision: string | null; // the prompt revision an answer must name
  options: AgentOption[];
  canStop: boolean;
  stopConfirm: boolean;
  canText: boolean;
  canFocus: boolean;
  subagents: SubagentRow[];
}

/** What a card action did. `code` is stable (agent_card.outcome on the runtime
 *  plus a few transport-side codes: gone, forbidden, http, unreachable). */
export interface ActionOutcome {
  ok: boolean;
  code: string;
  message: string;
}

export type DetailResult =
  | { kind: "ok"; detail: AgentDetail }
  | { kind: "gone" } // 404: no agent on that tile / it left the fleet / demo mode
  | { kind: "error"; message: string };

export type AgentAction = "answer" | "text" | "stop" | "focus";

/** One live-terminal frame: base64 ANSI straight from herdr (via the bridge). */
export interface TermFrame {
  seq: number;
  full: boolean; // a full repaint: reset the terminal first
  cols: number;
  rows: number;
  data: string;
}

export type TermOpen = { ok: true; id: string } | { ok: false; outcome: ActionOutcome };

/** A long-poll answer. `gone` = the runtime forgot the session (closed,
 *  evicted by a newer preview, or reaped after the window stopped polling). */
export type TermPoll =
  | { kind: "frames"; frames: TermFrame[]; next: number; closed: string | null; gap: boolean }
  | { kind: "gone" }
  | { kind: "error"; message: string };

/** How long a terminal long-poll may be held (runtime clamps to 15 s). */
export const TERM_POLL_MS = 12000;

export interface AgentTransport {
  detail(target: AgentTarget, refresh?: boolean): Promise<DetailResult>;
  act(action: AgentAction, ref: AgentRef, extra?: Record<string, string>): Promise<ActionOutcome>;
  termOpen(ref: AgentRef, cols: number, rows: number): Promise<TermOpen>;
  termPoll(id: string, after: number, waitMs: number): Promise<TermPoll>;
  termClose(id: string): Promise<void>;
}

const str = (v: unknown): string => (typeof v === "string" ? v : "");

function parseOption(raw: unknown): AgentOption | null {
  if (raw == null || typeof raw !== "object") return null;
  const v = raw as Record<string, unknown>;
  const key = str(v.key);
  if (!key) return null;
  const kind = v.kind === "fallback" || v.kind === "backend" ? v.kind : "option";
  return {
    key,
    label: str(v.label),
    id: typeof v.id === "string" && v.id ? v.id : null,
    kind,
    confirm: v.confirm === true,
  };
}

const SUBAGENT_STATUSES: readonly string[] = ["running", "done", "failed", "stale"];

function parseSubagent(raw: unknown): SubagentRow | null {
  if (raw == null || typeof raw !== "object") return null;
  const v = raw as Record<string, unknown>;
  const id = str(v.id);
  if (!id || !SUBAGENT_STATUSES.includes(str(v.status))) return null;
  const depth = typeof v.depth === "number" && Number.isInteger(v.depth) && v.depth >= 0 ? v.depth : null;
  const duration = typeof v.duration_s === "number" && Number.isFinite(v.duration_s) ? v.duration_s : 0;
  return {
    id,
    provider: str(v.provider),
    type: str(v.type),
    description: str(v.description),
    model: str(v.model),
    depth,
    status: str(v.status) as SubagentStatus,
    durationS: Math.max(0, Math.floor(duration)),
  };
}

/** Shape a raw /agent/detail body, or null when it is not one. */
export function parseDetail(raw: unknown): AgentDetail | null {
  if (raw == null || typeof raw !== "object") return null;
  const v = raw as Record<string, unknown>;
  const serverId = str(v.server_id);
  const paneId = str(v.pane_id);
  if (!serverId || !paneId) return null;
  return {
    serverId,
    paneId,
    agentType: str(v.agent_type),
    displayAgent: str(v.display_agent),
    label: str(v.label),
    title: str(v.title),
    repo: str(v.repo),
    branch: str(v.branch),
    workspace: str(v.workspace),
    tab: str(v.tab),
    status: str(v.status) || "unknown",
    sinceS: typeof v.since_s === "number" && Number.isFinite(v.since_s) ? v.since_s : null,
    backend: str(v.backend) || "herdr",
    connected: v.connected === true,
    prompt: typeof v.prompt === "string" ? v.prompt : null,
    promptPending: v.prompt_pending === true,
    revision: typeof v.revision === "string" && v.revision ? v.revision : null,
    options: Array.isArray(v.options)
      ? v.options.map(parseOption).filter((o): o is AgentOption => o !== null)
      : [],
    canStop: v.can_stop === true,
    stopConfirm: v.stop_confirm === true,
    canText: v.can_text === true,
    canFocus: v.can_focus === true,
    subagents: Array.isArray(v.subagents)
      ? v.subagents.map(parseSubagent).filter((r): r is SubagentRow => r !== null)
      : [],
  };
}

/** Map an action's HTTP answer onto an outcome the card can explain. */
export function parseOutcome(status: number, body: unknown): ActionOutcome {
  if (status === 200 && body != null && typeof body === "object") {
    const v = body as Record<string, unknown>;
    if (typeof v.code === "string") {
      return { ok: v.ok === true, code: v.code, message: str(v.message) };
    }
  }
  if (status === 404) return { ok: false, code: "gone", message: "" };
  if (status === 403) return { ok: false, code: "forbidden", message: "" };
  if (status === 400) return { ok: false, code: "invalid", message: "" };
  return { ok: false, code: "http", message: String(status) };
}

function parseFrame(raw: unknown): TermFrame | null {
  if (raw == null || typeof raw !== "object") return null;
  const v = raw as Record<string, unknown>;
  if (typeof v.data !== "string" || typeof v.seq !== "number") return null;
  const n = (x: unknown, d: number) => (typeof x === "number" && Number.isFinite(x) ? x : d);
  return { seq: v.seq, full: v.full === true, cols: n(v.cols, 80), rows: n(v.rows, 24), data: v.data };
}

/** Shape a raw /agent/term/poll body. */
export function parseTermPoll(status: number, body: unknown): TermPoll {
  if (status === 404) return { kind: "gone" };
  if (status !== 200 || body == null || typeof body !== "object") {
    return { kind: "error", message: `HTTP ${status}` };
  }
  const v = body as Record<string, unknown>;
  return {
    kind: "frames",
    frames: Array.isArray(v.frames)
      ? v.frames.map(parseFrame).filter((f): f is TermFrame => f !== null)
      : [],
    next: typeof v.next === "number" ? v.next : 0,
    closed: typeof v.closed === "string" ? v.closed : null,
    gap: v.gap === true,
  };
}

/** Human "since" text: 42s, 5m, 2h (same buckets as the deck tiles). */
export function formatSince(seconds: number | null): string {
  if (seconds == null || seconds < 0) return "";
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  return `${Math.floor(seconds / 3600)}h`;
}

/** A subagent's duration: 42s, 5m 03s, 2h 05m (finer than formatSince —
 *  the list compares siblings that are often minutes apart). */
export function formatDuration(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  const pad = (n: number) => String(n).padStart(2, "0");
  if (s < 3600) return `${Math.floor(s / 60)}m ${pad(s % 60)}s`;
  return `${Math.floor(s / 3600)}h ${pad(Math.floor((s % 3600) / 60))}m`;
}

/** Indent level for a subagent row: depth 1 (direct child) is flush; nested
 *  ones step in, capped so a deep chain cannot push the text off the card. */
export function subagentIndent(depth: number | null): number {
  return depth == null ? 0 : Math.min(3, Math.max(0, depth - 1));
}

function detailPath(target: AgentTarget, refresh: boolean): string {
  const p = new URLSearchParams();
  if ("index" in target) p.set("index", String(target.index));
  else {
    p.set("server_id", target.serverId);
    p.set("pane_id", target.paneId);
  }
  if (refresh) p.set("refresh", "1");
  return `/agent/detail?${p.toString()}`;
}

interface CallResult {
  status: number;
  body: unknown;
}

function asCall(raw: unknown): CallResult {
  const v = (raw ?? {}) as Record<string, unknown>;
  return { status: typeof v.status === "number" ? v.status : 0, body: v.body ?? null };
}

/** The production transport: every call goes through the Rust `agent_call`
 *  proxy (token injected Rust-side). */
export function agentCallTransport(invoke: InvokeFn): AgentTransport {
  const call = async (
    method: "GET" | "POST",
    path: string,
    body?: Record<string, unknown>,
    waitMs?: number,
  ): Promise<CallResult> =>
    asCall(
      await invoke(
        "agent_call",
        waitMs === undefined ? { method, path, body } : { method, path, body, waitMs },
      ),
    );
  return {
    async detail(target, refresh = false) {
      let r: CallResult;
      try {
        r = await call("GET", detailPath(target, refresh));
      } catch (e) {
        return { kind: "error", message: String(e) };
      }
      if (r.status === 404) return { kind: "gone" };
      const detail = r.status === 200 ? parseDetail(r.body) : null;
      return detail ? { kind: "ok", detail } : { kind: "error", message: `HTTP ${r.status}` };
    },
    async act(action, ref, extra = {}) {
      try {
        const r = await call("POST", `/agent/${action}`, {
          server_id: ref.serverId,
          pane_id: ref.paneId,
          ...extra,
        });
        return parseOutcome(r.status, r.body);
      } catch (e) {
        return { ok: false, code: "unreachable", message: String(e) };
      }
    },
    async termOpen(ref, cols, rows) {
      let r: CallResult;
      try {
        r = await call("POST", "/agent/term/open", {
          server_id: ref.serverId,
          pane_id: ref.paneId,
          cols,
          rows,
        });
      } catch (e) {
        return { ok: false, outcome: { ok: false, code: "unreachable", message: String(e) } };
      }
      const v = (r.body ?? {}) as Record<string, unknown>;
      if (r.status === 200 && v.ok === true && typeof v.id === "string") return { ok: true, id: v.id };
      return { ok: false, outcome: parseOutcome(r.status, r.body) };
    },
    async termPoll(id, after, waitMs) {
      const p = new URLSearchParams({ id, after: String(after), wait_ms: String(waitMs) });
      try {
        const r = await call("GET", `/agent/term/poll?${p.toString()}`, undefined, waitMs);
        return parseTermPoll(r.status, r.body);
      } catch (e) {
        return { kind: "error", message: String(e) };
      }
    },
    async termClose(id) {
      try {
        await call("POST", "/agent/term/close", { id });
      } catch {
        // best effort: the runtime reaps an unpolled session on its own
      }
    },
  };
}
