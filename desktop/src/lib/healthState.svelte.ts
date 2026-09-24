// The app window's latest health problems, shared between NoticeList (which
// polls /health and renders the rows) and ConfigApp (the problem-count badge
// on the Maintenance entry of the settings navigation).
import { worstSeverity, type HealthProblem, type Severity } from "./healthStatus";

// `usageBridgesEmpty`: /health `usage.bridges_empty` — servers whose bridge
// offers usage limits but sends no numbers (the Maintenance section points
// their "Usage limits helper" row out).
export const healthState = $state<{ problems: HealthProblem[]; usageBridgesEmpty: string[] }>({ problems: [], usageBridgesEmpty: [] });

export function setHealthProblems(problems: HealthProblem[]): void {
  healthState.problems = problems;
}

export function setUsageBridgesEmpty(ids: string[]): void {
  const cur = healthState.usageBridgesEmpty;
  if (cur.length === ids.length && cur.every((id, i) => id === ids[i])) return;
  healthState.usageBridgesEmpty = ids;
}

/** /health `usage.bridges_empty` (empty when absent: an older runtime). */
export function usageBridgesEmpty(raw: unknown): string[] {
  const usage = raw && typeof raw === "object" ? (raw as Record<string, unknown>).usage : null;
  const list = usage && typeof usage === "object" ? (usage as Record<string, unknown>).bridges_empty : null;
  return Array.isArray(list) ? list.filter((id): id is string => typeof id === "string") : [];
}

/** The Maintenance badge: how many problems (info facts are not problems;
 *  dismissed ones still are — Maintenance is where they get fixed) and the
 *  colour of the worst. null = no badge. */
export function maintenanceBadge(problems: HealthProblem[]): { count: number; severity: Severity } | null {
  const real = problems.filter((p) => p.severity !== "info");
  const severity = worstSeverity(real);
  return severity === null ? null : { count: real.length, severity };
}
