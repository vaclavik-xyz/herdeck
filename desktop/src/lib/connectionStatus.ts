import { effectiveActiveServerIds, serversOf, type ConfigPayload } from "./configClient";
import type { DeckViewModel } from "./deckClient";

export type ConnectionHealth = "connected" | "disconnected" | "unavailable" | "inactive";

export interface LocalConnectionRow {
  kind: "local";
  name: string;
  socketPath: string;
  runtimeId: string | null;
  selected: boolean;
  available: boolean;
  health: ConnectionHealth;
}

export interface RemoteConnectionRow {
  kind: "remote";
  id: string;
  url: string;
  active: boolean;
  health: ConnectionHealth;
}

export interface ConnectionInventory {
  local: LocalConnectionRow[];
  remote: RemoteConnectionRow[];
}

/**
 * Join saved connection choices with the runtime's authoritative connection
 * map. Availability only says that a local socket exists; it does not mean the
 * bridge has connected successfully.
 */
export function connectionInventory(
  payload: ConfigPayload,
  view: DeckViewModel,
): ConnectionInventory {
  const activeRemoteIds = new Set(effectiveActiveServerIds(payload));
  const local = payload.localSessions.map((session): LocalConnectionRow => {
    const runtimeId = view.localConnections[session.name] ?? null;
    const connected = runtimeId !== null && view.connections[runtimeId] === true;
    const health: ConnectionHealth = !session.selected
      ? "inactive"
      : connected
        ? "connected"
        : !session.available
          ? "unavailable"
          : "disconnected";
    return {
      kind: "local",
      name: session.name,
      socketPath: session.socket_path,
      runtimeId,
      selected: session.selected,
      available: session.available,
      health,
    };
  });

  const remote = serversOf(payload).map((server): RemoteConnectionRow => {
    const active = activeRemoteIds.has(server.id);
    return {
      kind: "remote",
      id: server.id,
      url: server.url,
      active,
      health: !active
        ? "inactive"
        : view.connections[server.id] === true
          ? "connected"
          : "disconnected",
    };
  });

  return { local, remote };
}
