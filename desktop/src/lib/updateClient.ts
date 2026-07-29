export type UpdateInfo = {
  version: string;
  current_version: string;
};

/** The shape a check renders as, whether it ran silently at mount or was
 *  asked for from the tray. "checking" is set by the caller before the
 *  transport call resolves (it never comes out of `runUpdateCheck` itself) so
 *  a manual check has something to show immediately. */
export type UpdateCheckState =
  | { kind: "checking" }
  | { kind: "available"; info: UpdateInfo }
  | { kind: "up-to-date" }
  | { kind: "failed"; reason: string };

type Invoke = (command: string, args?: Record<string, unknown>) => Promise<unknown>;

export function asUpdateInfo(value: unknown): UpdateInfo | null {
  if (value === null) return null;
  if (typeof value !== "object" || Array.isArray(value)) {
    throw new Error("invalid update response");
  }
  const row = value as Record<string, unknown>;
  if (typeof row.version !== "string" || typeof row.current_version !== "string") {
    throw new Error("invalid update response");
  }
  return { version: row.version, current_version: row.current_version };
}

/** Narrow an unvalidated `UPDATE_RESULT_EVENT` payload (crossing the
 *  Rust/webview event bus, or another window's broadcast) into an
 *  `UpdateCheckState`, or `null` for anything unrecognised. `listen<T>`'s
 *  type parameter is a compile-time assertion only — it does not check the
 *  payload actually has that shape — so an unvalidated assignment straight
 *  into rendered state (`state.info.version`) would crash the receiving
 *  window on a malformed or future-shaped message instead of just ignoring
 *  it, the same way `asUpdateInfo`/`asDiscovery` guard every other value
 *  that crosses that boundary. */
export function asUpdateCheckState(value: unknown): UpdateCheckState | null {
  if (value == null || typeof value !== "object") return null;
  const v = value as Record<string, unknown>;
  switch (v.kind) {
    case "checking":
      return { kind: "checking" };
    case "up-to-date":
      return { kind: "up-to-date" };
    case "failed":
      return typeof v.reason === "string" ? { kind: "failed", reason: v.reason } : null;
    case "available":
      try {
        const info = asUpdateInfo(v.info ?? null);
        return info ? { kind: "available", info } : null;
      } catch {
        return null;
      }
    default:
      return null;
  }
}

/** A thrown value's displayable reason, never empty: a message-less `Error`,
 *  a non-Error rejection, or `throw undefined`/`null` all fall back to a
 *  labelled placeholder instead of rendering as a blank or truncated string.
 *  Shared by `runUpdateCheck` and `installUpdate` (App.svelte) so the two
 *  error paths can't drift on how they turn a caught value into text. */
export function reasonOf(error: unknown): string {
  const reason = error instanceof Error ? error.message : String(error);
  return reason.trim() || "unknown error";
}

export function updateTransport(invoke: Invoke) {
  return {
    async check(): Promise<UpdateInfo | null> {
      return asUpdateInfo(await invoke("update_check"));
    },
    async install(): Promise<boolean> {
      const installed = await invoke("update_install");
      if (typeof installed !== "boolean") throw new Error("invalid update install response");
      return installed;
    },
  };
}

/** Run one check and shape the outcome into a state the UI can render
 *  directly. Never throws: a rejected check becomes `{kind:"failed"}` instead
 *  of propagating, so "nothing to install" and "the check itself broke" stay
 *  distinguishable results rather than collapsing into the same silence —
 *  which is what let a real release go unnoticed (see App.svelte). */
export async function runUpdateCheck(
  check: () => Promise<UpdateInfo | null>,
): Promise<UpdateCheckState> {
  try {
    const info = await check();
    return info ? { kind: "available", info } : { kind: "up-to-date" };
  } catch (error) {
    return { kind: "failed", reason: reasonOf(error) };
  }
}
