export type UpdateInfo = {
  version: string;
  current_version: string;
};

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
