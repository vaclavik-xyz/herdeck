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

export interface AgentTransport {
  detail(target: AgentTarget, refresh?: boolean): Promise<DetailResult>;
  act(action: AgentAction, ref: AgentRef, extra?: Record<string, string>): Promise<ActionOutcome>;
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

/** Human "since" text: 42s, 5m, 2h (same buckets as the deck tiles). */
export function formatSince(seconds: number | null): string {
  if (seconds == null || seconds < 0) return "";
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  return `${Math.floor(seconds / 3600)}h`;
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
  ): Promise<CallResult> => asCall(await invoke("agent_call", { method, path, body }));
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
  };
}
