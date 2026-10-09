// The mini dock's autohide (`[desktop].dock_autohide`, design §5) as a pure
// state machine: no DOM, no Svelte, only timers (unit-tested with fake ones).
//
// - Enabled only while the pref is on AND the strip is snapped to a
//   work-area edge (Rust `dock_layout` reports the edge).
// - "shown" → the pointer left the dock for AUTOHIDE_DELAY_MS → "hiding"
//   (the strip slides out, AUTOHIDE_SLIDE_MS) → "hidden" (MiniDock then has
//   Rust shrink the window to the sliver).
// - "hidden" → the pointer rests on the sliver for AUTOHIDE_REVEAL_MS (a
//   pass-by does not count) → "shown" (MiniDock grows the window, then the
//   strip slides in).
// - `held` (a card or group open, keyboard focus, ANY agent blocked) keeps it
//   shown and reveals it at once: a question must never hide.

import type { DockAgent, DockStatus } from "./dockClient";
import { STATUS_RANK } from "./dockModel";

export const AUTOHIDE_DELAY_MS = 1200;
export const AUTOHIDE_SLIDE_MS = 180;
export const AUTOHIDE_REVEAL_MS = 100;
/** The hidden dock's visible edge, CSS px (times the dock's zoom). */
export const SLIVER_WIDTH = 6;

export type AutohidePhase = "shown" | "hiding" | "hidden";

export interface AutohideInputs {
  /** `dock_autohide` is on and the strip is snapped to an edge. */
  enabled: boolean;
  /** Something keeps the strip out (see the header). */
  held: boolean;
}

export interface Autohide {
  readonly phase: AutohidePhase;
  update(inputs: Partial<AutohideInputs>): void;
  /** The pointer entered (true) or left (false) the dock window. */
  pointer(inside: boolean): void;
  destroy(): void;
}

export function createAutohide(options: {
  onPhase: (phase: AutohidePhase) => void;
  delayMs?: number;
  /** A number, or read at each hide (reduced motion can change live). */
  slideMs?: number | (() => number);
  revealMs?: number;
}): Autohide {
  const delayMs = options.delayMs ?? AUTOHIDE_DELAY_MS;
  const slideOption = options.slideMs ?? AUTOHIDE_SLIDE_MS;
  const slideMsNow = () => (typeof slideOption === "function" ? slideOption() : slideOption);
  const revealMs = options.revealMs ?? AUTOHIDE_REVEAL_MS;
  let phase: AutohidePhase = "shown";
  let enabled = false;
  let held = false;
  let inside = false;
  let timer: ReturnType<typeof setTimeout> | undefined;

  function clear(): void {
    clearTimeout(timer);
    timer = undefined;
  }

  function set(next: AutohidePhase): void {
    if (next === phase) return;
    phase = next;
    options.onPhase(next);
  }

  function slideOut(): void {
    set("hiding");
    const slideMs = slideMsNow();
    if (slideMs <= 0) {
      set("hidden");
      return;
    }
    timer = setTimeout(() => {
      timer = undefined;
      set("hidden");
    }, slideMs);
  }

  /** Re-derive what should happen from the current inputs. */
  function evaluate(): void {
    clear();
    if (!enabled || held) {
      set("shown");
      return;
    }
    if (phase === "shown") {
      if (!inside) timer = setTimeout(slideOut, delayMs);
      return;
    }
    if (phase === "hiding") {
      if (inside) set("shown"); // came back while it slid out (window still full)
      else slideOut();
      return;
    }
    // Hidden: only a pointer resting on the sliver brings it back.
    if (inside) {
      timer = setTimeout(() => {
        timer = undefined;
        set("shown");
      }, revealMs);
    }
  }

  return {
    get phase() {
      return phase;
    },
    update(inputs) {
      const nextEnabled = inputs.enabled ?? enabled;
      const nextHeld = inputs.held ?? held;
      if (nextEnabled === enabled && nextHeld === held) return;
      enabled = nextEnabled;
      held = nextHeld;
      evaluate();
    },
    pointer(next) {
      if (next === inside) return;
      inside = next;
      evaluate();
    },
    destroy() {
      clear();
      enabled = false;
    },
  };
}

/** The sliver's colour: the most urgent status among the agents, or null
 *  (neutral) with nobody there. */
export function sliverStatus(agents: DockAgent[]): DockStatus | null {
  let best: DockStatus | null = null;
  for (const a of agents) {
    if (best === null || STATUS_RANK[a.status] < STATUS_RANK[best]) best = a.status;
  }
  return best;
}
