// The mini dock's answer keys (spec §1, keyboard answer flow): which action a
// keydown means while a card is open. Pure — MiniDock owns the DOM side.
import type { DockAgent } from "./dockClient";
import { agentKey, orderAgents } from "./dockModel";

export type DockKeyAction =
  | { kind: "answer"; key: string } // the numbered option ("1"–"9")
  | { kind: "cycle"; dir: 1 | -1 } // Tab / Shift+Tab: next / previous blocked agent
  | { kind: "reply" } // T or /: focus the reply field
  | { kind: "open" } // O: Open in herdr
  | { kind: "close" }; // Esc

export interface DockKeyEvent {
  key: string;
  code?: string;
  shiftKey?: boolean;
  metaKey?: boolean;
  ctrlKey?: boolean;
  altKey?: boolean;
  isComposing?: boolean;
}

export interface DockKeyContext {
  /** What the open card shows (null = no card). */
  card: "agent" | "other" | null;
  /** Focus is in a text field (the reply): every key belongs to it. */
  typing: boolean;
  /** The card was opened by the next-blocked hotkey: Tab cycles the blocked
   *  agents. Otherwise Tab keeps moving focus through the dock's controls. */
  keyboard: boolean;
}

/** The digit a key names. Falls back to the physical key so a layout whose
 *  number row types other characters unshifted (Czech: +ěščřžýáí) works. */
function digitOf(e: DockKeyEvent): string | null {
  if (/^[1-9]$/.test(e.key)) return e.key;
  const m = /^(?:Digit|Numpad)([1-9])$/.exec(e.code ?? "");
  return m ? m[1] : null;
}

export function dockKeyAction(e: DockKeyEvent, ctx: DockKeyContext): DockKeyAction | null {
  if (ctx.typing || e.isComposing || ctx.card === null) return null;
  if (e.key === "Escape") return { kind: "close" };
  if (ctx.card !== "agent") return null;
  // ⌘/Ctrl/Alt chords stay the app's (⌘0 resets the zoom).
  if (e.metaKey || e.ctrlKey || e.altKey) return null;
  if (e.key === "Tab") return ctx.keyboard ? { kind: "cycle", dir: e.shiftKey ? -1 : 1 } : null;
  const digit = digitOf(e);
  if (digit !== null) return { kind: "answer", key: digit };
  const k = e.key.toLowerCase();
  if (k === "t" || e.key === "/") return { kind: "reply" };
  if (k === "o") return { kind: "open" };
  return null;
}

/** Blocked agents, longest blocked first (the strip's own order). */
export function blockedOrder(agents: DockAgent[]): DockAgent[] {
  return orderAgents(agents).filter((a) => a.status === "blocked");
}

/** The blocked agent Tab (dir 1) / Shift+Tab (dir -1) moves to from the open
 *  card's agent; from a card that is not one of them, the first / last. Null
 *  when there is nowhere else to go. */
export function cycleBlocked(agents: DockAgent[], openKey: string | null, dir: 1 | -1): DockAgent | null {
  const blocked = blockedOrder(agents);
  if (blocked.length === 0) return null;
  const at = openKey === null ? -1 : blocked.findIndex((a) => agentKey(a) === openKey);
  if (at < 0) return dir > 0 ? blocked[0] : blocked[blocked.length - 1];
  if (blocked.length === 1) return null;
  return blocked[(at + dir + blocked.length) % blocked.length];
}

/** Focus sits in a text field, where keys type instead of acting. */
export function isTypingTarget(el: EventTarget | Element | null): boolean {
  if (!el || typeof (el as Element).tagName !== "string") return false;
  const node = el as HTMLElement;
  const tag = node.tagName.toLowerCase();
  return tag === "textarea" || tag === "input" || tag === "select" || node.isContentEditable === true;
}
