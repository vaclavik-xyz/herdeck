// The app window's latest health problems, shared between NoticeList (which
// polls /health and renders the rows) and ConfigApp (the problem-count badge
// on the Maintenance entry of the settings navigation).
import { worstSeverity, type HealthProblem, type Severity } from "./healthStatus";

export const healthState = $state<{ problems: HealthProblem[] }>({ problems: [] });

export function setHealthProblems(problems: HealthProblem[]): void {
  healthState.problems = problems;
}

/** The Maintenance badge: how many problems (info facts are not problems;
 *  dismissed ones still are — Maintenance is where they get fixed) and the
 *  colour of the worst. null = no badge. */
export function maintenanceBadge(problems: HealthProblem[]): { count: number; severity: Severity } | null {
  const real = problems.filter((p) => p.severity !== "info");
  const severity = worstSeverity(real);
  return severity === null ? null : { count: real.length, severity };
}
