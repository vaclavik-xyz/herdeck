// Pure model of the mini dock strip: which items it shows, in which order,
// and what folds into "+N" — given `[desktop].dock_show` and `dock_group`.
// No DOM, no Svelte (MiniDock/DockGroup render it).
//
// Rules (design: docs/superpowers/specs/2026-10-09-dock-control-center-design.md §3, §6):
// - Order is dockModel.orderAgents (blocked · waiting · done · working · unknown · idle).
// - A blocked agent NEVER folds into a group or the "+N" fold: a question
//   must never be hidden.
// - Grouping: non-idle, non-blocked agents sharing a project key (repo, else
//   workspace; empty never groups) fold into one item once there are >= 2.
//   The group sits where its most urgent member would sit.

import type { DockAgent, DockStatus } from "./dockClient";
import { agentKey, orderAgents, STATUS_RANK } from "./dockModel";

export type DockShow = "active" | "all" | "needs_you";
export const DOCK_SHOW_CHOICES: DockShow[] = ["active", "all", "needs_you"];

export interface DockPrefs {
  /** `[desktop].dock_group`: fold a project's agents into one item. */
  group: boolean;
  /** `[desktop].dock_show`: who gets an item of their own. */
  show: DockShow;
  /** `[desktop].dock_autohide` (behaviour lives with edge snap). */
  autohide: boolean;
}

export const DEFAULT_DOCK_PREFS: DockPrefs = { group: true, show: "active", autohide: false };

/** The dock's prefs from a raw `[desktop]` table (the redacted config's
 *  `base.desktop`); a missing or wrong-typed key keeps its default. */
export function parseDockPrefs(desktop: unknown): DockPrefs {
  const v = desktop !== null && typeof desktop === "object" ? (desktop as Record<string, unknown>) : {};
  return {
    group: typeof v.dock_group === "boolean" ? v.dock_group : DEFAULT_DOCK_PREFS.group,
    show: DOCK_SHOW_CHOICES.includes(v.dock_show as DockShow) ? (v.dock_show as DockShow) : DEFAULT_DOCK_PREFS.show,
    autohide: typeof v.dock_autohide === "boolean" ? v.dock_autohide : DEFAULT_DOCK_PREFS.autohide,
  };
}

/** Motion of an expanding group: each member slides in this much after the
 *  previous one, over this long. */
export const GROUP_STAGGER_MS = 40;
export const GROUP_EXPAND_MS = 160;
/** An expanded group folds back this long after the pointer left it. */
export const GROUP_COLLAPSE_MS = 400;

export type StripItem =
  | { kind: "agent"; key: string; agent: DockAgent }
  | {
      kind: "group";
      key: string;
      /** The project key (repo, else workspace): the group's name. */
      label: string;
      /** The most urgent member's status (the ring). */
      status: DockStatus;
      /** Every member's server is down. */
      offline: boolean;
      /** The most urgent member: whose project mark the group shows. */
      lead: DockAgent;
      /** In strip order. */
      members: DockAgent[];
    };

/** What a group member's item does (DockGroup → MiniDock): the same as a
 *  standalone item on the strip. */
export interface MemberHandlers {
  enter: (agent: DockAgent, top: number) => void;
  leave: () => void;
  focus: (agent: DockAgent, top: number) => void;
  blur: () => void;
  click: (agent: DockAgent, top: number) => void;
}

export interface StripModel {
  items: StripItem[];
  /** Folded into "+N": idle agents by title, or (needs_you) everything that
   *  is neither blocked nor done, most urgent first. */
  folded: DockAgent[];
  /** The fold holds only idle agents (its chip/card say "idle"). */
  foldedIdleOnly: boolean;
}

/** What groups an agent: its repo, else its workspace; "" never groups. */
export function groupKeyOf(a: DockAgent): string {
  return a.repo || a.workspace || "";
}

/** The most urgent status among the members (waiting > done > working > unknown). */
export function groupStatus(members: DockAgent[]): DockStatus {
  let best: DockStatus = members[0]?.status ?? "unknown";
  for (const m of members) if (STATUS_RANK[m.status] < STATUS_RANK[best]) best = m.status;
  return best;
}

const cmp = (x: string, y: string): number => (x < y ? -1 : x > y ? 1 : 0);
const byTitle = (p: DockAgent, q: DockAgent): number => cmp(p.title, q.title) || cmp(agentKey(p), agentKey(q));

function ownsItem(a: DockAgent, show: DockShow): boolean {
  if (show === "all") return true;
  if (show === "needs_you") return a.status === "blocked" || a.status === "done";
  return a.status !== "idle";
}

export function stripModel(agents: DockAgent[], prefs: Pick<DockPrefs, "group" | "show">): StripModel {
  const ordered = orderAgents(agents);
  const own = ordered.filter((a) => ownsItem(a, prefs.show));
  const folded = ordered
    .filter((a) => !ownsItem(a, prefs.show))
    .sort((p, q) => STATUS_RANK[p.status] - STATUS_RANK[q.status] || byTitle(p, q));
  // Idle agents with their own items ("all") come after the rest, by title.
  const active = own.filter((a) => a.status !== "idle");
  const idle = own.filter((a) => a.status === "idle").sort(byTitle);

  const groupable = (a: DockAgent) => prefs.group && a.status !== "blocked" && groupKeyOf(a) !== "";
  const byProject = new Map<string, DockAgent[]>();
  for (const a of active) {
    if (!groupable(a)) continue;
    const k = groupKeyOf(a);
    const members = byProject.get(k);
    if (members) members.push(a);
    else byProject.set(k, [a]);
  }

  const items: StripItem[] = [];
  const placed = new Set<string>();
  for (const a of active) {
    const members = groupable(a) ? byProject.get(groupKeyOf(a))! : [a];
    if (members.length < 2) {
      items.push({ kind: "agent", key: agentKey(a), agent: a });
      continue;
    }
    const label = groupKeyOf(a);
    if (placed.has(label)) continue; // the group already sits at its lead
    placed.add(label);
    items.push({
      kind: "group",
      key: `group\u0000${label}`,
      label,
      status: groupStatus(members),
      offline: members.every((m) => m.offline),
      lead: members[0],
      members,
    });
  }
  for (const a of idle) items.push({ kind: "agent", key: agentKey(a), agent: a });

  return { items, folded, foldedIdleOnly: folded.every((a) => a.status === "idle") };
}

/** How long to wait before refitting the window to a new strip height: grow
 *  at once (the content must not be clipped while it slides out), shrink only
 *  after a collapsing group has finished animating. */
export function stripLayoutDelay(prevHeight: number, nextHeight: number, reducedMotion: boolean): number {
  if (reducedMotion || nextHeight >= prevHeight) return 0;
  return GROUP_EXPAND_MS + GROUP_STAGGER_MS;
}
