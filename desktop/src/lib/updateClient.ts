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
    return { kind: "failed", reason: error instanceof Error ? error.message : String(error) };
  }
}
