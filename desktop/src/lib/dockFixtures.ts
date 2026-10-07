// Shared fixtures for the mini dock tests (dockModel/DockItem/DockCard/MiniDock).
import type {
  ActionOutcome,
  AgentAction,
  AgentDetail,
  AgentRef,
  AgentTarget,
  AgentTransport,
  DetailResult,
} from "./agentCardClient";
import type { DockAgent, DockList } from "./dockClient";

export function dockAgent(over: Partial<DockAgent> = {}): DockAgent {
  return {
    serverId: "prod",
    paneId: "p0",
    agentType: "claude",
    title: "Bank API",
    repo: "macdoktor-crm",
    workspace: "macDoktor CRM",
    branch: "feat/bank",
    tab: "",
    status: "blocked",
    sinceS: 260,
    waitingOn: "",
    subagents: 0,
    offline: false,
    icon: null,
    monogram: "M",
    ...over,
  };
}

export function dockList(agents: DockAgent[], over: Partial<DockList> = {}): DockList {
  return { version: 1, language: "en", connected: true, agents, ...over };
}

export const DOCK_PROMPT =
  "Bash command\n  rm -rf build\nDo you want to proceed?\n❯ 1. Yes\n  2. Yes, and don't ask again\n  3. No";

export function agentDetail(over: Partial<AgentDetail> = {}): AgentDetail {
  return {
    serverId: "prod",
    paneId: "p0",
    agentType: "claude",
    displayAgent: "",
    label: "p0",
    title: "Bank API",
    repo: "macdoktor-crm",
    branch: "feat/bank",
    workspace: "macDoktor CRM",
    tab: "",
    status: "blocked",
    sinceS: 260,
    backend: "herdr",
    connected: true,
    prompt: DOCK_PROMPT,
    promptPending: false,
    revision: "rev-1",
    options: [
      { key: "1", label: "Yes", id: "approve", kind: "option", confirm: false },
      { key: "2", label: "Yes, and don't ask again", id: "approve_always", kind: "option", confirm: false },
      { key: "3", label: "No", id: "deny", kind: "option", confirm: false },
    ],
    canStop: true,
    stopConfirm: true,
    canText: true,
    canFocus: true,
    subagents: [],
    ...over,
  };
}

export function fakeAgentTransport(
  opts: { details?: DetailResult[]; outcomes?: ActionOutcome[] } = {},
): AgentTransport & {
  detailCalls: Array<{ target: AgentTarget; refresh: boolean }>;
  actCalls: Array<{ action: AgentAction; ref: AgentRef; extra: Record<string, string> }>;
} {
  const details = [...(opts.details ?? [{ kind: "ok", detail: agentDetail() } as DetailResult])];
  const outcomes = [...(opts.outcomes ?? [{ ok: true, code: "sent", message: "" }])];
  const next = <T>(queue: T[]): T => (queue.length > 1 ? queue.shift()! : queue[0]);
  const detailCalls: Array<{ target: AgentTarget; refresh: boolean }> = [];
  const actCalls: Array<{ action: AgentAction; ref: AgentRef; extra: Record<string, string> }> = [];
  return {
    detailCalls,
    actCalls,
    async detail(target, refresh = false) {
      detailCalls.push({ target, refresh });
      return next(details);
    },
    async act(action, ref, extra = {}) {
      actCalls.push({ action, ref, extra });
      return next(outcomes);
    },
    async termOpen() {
      return { ok: false, outcome: { ok: false, code: "unsupported", message: "" } };
    },
    async termPoll() {
      return { kind: "gone" };
    },
    async termClose() {},
  };
}
