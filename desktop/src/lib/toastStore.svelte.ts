// The app window's toast stack: results of actions (update bridge, restart
// deck/runtime, update checks). Success auto-dismisses after
// TOAST_SUCCESS_MS; errors stay until closed; `progress` toasts stay while
// their action runs and are replaced in place (same id) by the outcome.
// Rendered by Toasts.svelte; pushed from anywhere in the window.

export type ToastKind = "success" | "error" | "info" | "progress";

export type StepState = "done" | "active" | "pending" | "failed";

export interface ToastStep {
  key: string;
  label: string;
  state: StepState;
}

export interface Toast {
  id: string;
  kind: ToastKind;
  text: string;
  /** Raw backend facts for title= (never the visible sentence). */
  detail?: string;
  /** A long action's stages (bridge update: download → install → verify → restart). */
  steps?: ToastStep[];
  /** A command the user has to run themselves (copyable, e.g. needs_admin). */
  command?: string;
}

export const TOAST_SUCCESS_MS = 5000;

export const toasts = $state<{ items: Toast[] }>({ items: [] });

const timers = new Map<string, ReturnType<typeof setTimeout>>();
let seq = 0;

/** Show (or replace, by id) a toast; returns its id. */
export function showToast(t: Omit<Toast, "id"> & { id?: string }): string {
  const id = t.id ?? `toast-${++seq}`;
  const toast: Toast = { ...t, id };
  const at = toasts.items.findIndex((x) => x.id === id);
  if (at >= 0) toasts.items[at] = toast;
  else toasts.items.push(toast);
  const old = timers.get(id);
  if (old !== undefined) {
    clearTimeout(old);
    timers.delete(id);
  }
  if (toast.kind === "success") {
    timers.set(id, setTimeout(() => closeToast(id), TOAST_SUCCESS_MS));
  }
  return id;
}

export function closeToast(id: string): void {
  const old = timers.get(id);
  if (old !== undefined) clearTimeout(old);
  timers.delete(id);
  const at = toasts.items.findIndex((x) => x.id === id);
  if (at >= 0) toasts.items.splice(at, 1);
}

export function toastById(id: string): Toast | undefined {
  return toasts.items.find((x) => x.id === id);
}

/** Tests: start from an empty stack. */
export function clearToasts(): void {
  for (const t of timers.values()) clearTimeout(t);
  timers.clear();
  toasts.items.splice(0, toasts.items.length);
}

// --- bridge update stepper ------------------------------------------------------

/** The bridge's progress stages (self_update.py) in order; "restart" is the
 *  final `updated` outcome ("updated to X; restarting"). */
export const BRIDGE_STAGES = ["download", "install", "verify", "restart"] as const;

/** The stepper for a bridge update: every stage before the latest one seen is
 *  done, the latest is active (or failed, when the update failed there);
 *  `updated` marks everything done. */
export function bridgeUpdateSteps(
  stagesSeen: string[],
  code: string,
  labels: Record<(typeof BRIDGE_STAGES)[number], string>,
): ToastStep[] {
  const known = stagesSeen.filter((s): s is (typeof BRIDGE_STAGES)[number] =>
    (BRIDGE_STAGES as readonly string[]).includes(s),
  );
  const last = known.length ? Math.max(...known.map((s) => BRIDGE_STAGES.indexOf(s))) : -1;
  const done = code === "updated";
  const failed = !done && code !== "pending";
  const current = done ? BRIDGE_STAGES.length : Math.max(last, 0);
  return BRIDGE_STAGES.map((key, i) => ({
    key,
    label: labels[key],
    state: done || i < current
      ? "done"
      : i === current
        ? failed ? "failed" : "active"
        : "pending",
  }));
}
