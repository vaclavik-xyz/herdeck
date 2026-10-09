// Pure presentation logic of the mini dock: what the strip shows, in which
// order, how an answer option is coloured, the monogram fallback, and the
// shapes exchanged with Rust's `dock_layout`. No DOM, no Svelte.

import type { AgentOption } from "./agentCardClient";
import type { DockAgent, DockList, DockStatus } from "./dockClient";
import { MONOGRAM_PALETTE } from "./statusColors";

/** Collapsed strip width (deck_prefs.rs DOCK_STRIP_WIDTH). */
export const STRIP_WIDTH = 56;
export const CARD_WIDTH = 300;
/** Gap between strip and card (dock_layout.rs DOCK_CARD_GAP). */
export const CARD_GAP = 8;
/** Tallest agent card (DockCard .card max-height): prompt + options + the
 *  output preview + the reply field. Rust dock_layout keeps it on screen. */
export const CARD_MAX_HEIGHT = 520;
export const HOVER_OPEN_MS = 120;
export const HOVER_CLOSE_MS = 250;
/** Re-poll while the runtime is still reading a blocked prompt. */
export const PROMPT_REPOLL_MS = 800;

/** Urgency order of the statuses (lower = more urgent). */
export const STATUS_RANK: Record<DockStatus, number> = {
  blocked: 0,
  waiting: 1,
  done: 2,
  working: 3,
  unknown: 4,
  idle: 5,
};

const cmp = (x: string, y: string): number => (x < y ? -1 : x > y ? 1 : 0);

export function agentKey(a: { serverId: string; paneId: string }): string {
  return `${a.serverId}\u0000${a.paneId}`;
}

/** blocked (longest waiting first) · waiting · done · working · unknown ·
 *  idle; inside a group longest-in-status first, then identity. */
export function orderAgents(agents: DockAgent[]): DockAgent[] {
  return [...agents].sort(
    (p, q) => STATUS_RANK[p.status] - STATUS_RANK[q.status] || q.sinceS - p.sinceS || cmp(agentKey(p), agentKey(q)),
  );
}

export interface DockLayout {
  visible: DockAgent[]; // the strip
  idle: DockAgent[]; // folded into "+N", by title
}

export function dockLayout(agents: DockAgent[]): DockLayout {
  const ordered = orderAgents(agents);
  return {
    visible: ordered.filter((a) => a.status !== "idle"),
    idle: ordered
      .filter((a) => a.status === "idle")
      .sort((p, q) => cmp(p.title, q.title) || cmp(agentKey(p), agentKey(q))),
  };
}

export type OptionTone = "approve" | "approve_always" | "deny" | "other";

/** The deck's colour for an answer option, from the runtime's action id only
 *  (the runtime resolves it with the configured profile, so dock and deck
 *  agree): approve green, approve-always amber, deny red, anything else blue.
 *  `agentType` is accepted for call-site stability and deliberately unused. */
export function optionTone(option: Pick<AgentOption, "key" | "label" | "id">, _agentType?: string): OptionTone {
  const id = option.id;
  return id === "approve" || id === "approve_always" || id === "deny" ? id : "other";
}

/** 32-bit FNV-1a over UTF-16 code units: a cheap, stable seed hash. */
export function fnv1a(text: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h >>> 0;
}

export function monogramColor(seed: string): string {
  return MONOGRAM_PALETTE[fnv1a(seed) % MONOGRAM_PALETTE.length];
}

/** Ink for text on a monogram fill, by relative luminance. */
export function monogramInk(color: string): "dark" | "light" {
  const m = color.match(/^rgb\((\d+),\s*(\d+),\s*(\d+)\)$/);
  if (!m) return "light";
  const [r, g, b] = [m[1], m[2], m[3]].map(Number);
  return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255 > 0.55 ? "dark" : "light";
}

/** Seconds in status now, from the list's `since_s` and when it arrived. */
export function liveSince(sinceS: number, receivedAtMs: number, nowMs: number): number {
  return sinceS + Math.max(0, Math.floor((nowMs - receivedAtMs) / 1000));
}

export type NeutralReason = "loading" | "disconnected" | "empty";

/** Why the strip shows its single neutral item, or null when it has agents. */
export function neutralReason(list: DockList | null): NeutralReason | null {
  if (list === null) return "loading";
  if (list.agents.length > 0) return null;
  return list.connected ? "empty" : "disconnected";
}

export interface DockPlacement {
  side: "left" | "right"; // where the card opens
  stripX: number; // strip offset inside the window, CSS px
  stripY: number;
  /** The work-area edge the strip is snapped to (null: not snapped). */
  edge?: "left" | "right" | null;
}

export interface DockLayoutRequest {
  stripWidth: number;
  stripHeight: number;
  cardWidth: number; // 0 = collapse to the strip
  cardHeight: number;
  /** > 0 (and no card): autohide, shrink to a sliver this wide at the edge. */
  sliverWidth?: number;
}

export type WindowLayoutFn = (req: DockLayoutRequest) => Promise<DockPlacement>;

/** Rust `dock_layout`'s `{side, stripX, stripY, edge}`; anything else =
 *  collapsed and not snapped. */
export function parsePlacement(raw: unknown): DockPlacement {
  const v = (raw ?? {}) as Record<string, unknown>;
  const num = (x: unknown) => typeof x === "number" && Number.isFinite(x);
  if ((v.side === "left" || v.side === "right") && num(v.stripX) && num(v.stripY)) {
    const edge = v.edge === "left" || v.edge === "right" ? v.edge : null;
    return { side: v.side, stripX: v.stripX as number, stripY: v.stripY as number, edge };
  }
  return { side: "right", stripX: 0, stripY: 0, edge: null };
}
