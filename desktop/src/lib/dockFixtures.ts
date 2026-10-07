// Shared fixtures for the mini dock tests (dockModel/DockItem/DockCard/MiniDock).
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
