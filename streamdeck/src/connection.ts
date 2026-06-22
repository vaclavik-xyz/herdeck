/**
 * Keeps a single IPC connection attempt in flight, re-attempting whenever the backend
 * (re)starts and whenever the socket closes. This self-heals the first-run flow: if
 * `herdeck` is not initially resolvable, the first `connectOnce` fails (the socket never
 * appears), but a later backend respawn — after the user sets the Property Inspector path
 * — re-triggers the attempt, so the plugin reconnects without a restart. The in-flight
 * guard prevents the burst of respawns during a failing cold start from stacking many
 * concurrent connect loops.
 */
export interface ConnectionDeps {
  /** Attempt to connect + hello. MUST handle its own errors; it may resolve or reject. */
  connectOnce: () => Promise<void>;
  onBackendStarting: (cb: () => void) => void;
  onIpcClose: (cb: () => void) => void;
}

export function superviseConnection(deps: ConnectionDeps): void {
  let connecting = false;
  const attempt = async () => {
    if (connecting) return;
    connecting = true;
    try {
      await deps.connectOnce();
    } catch {
      // connectOnce is expected to handle its own errors; swallow defensively so a throw
      // can never wedge the `connecting` flag or surface as an unhandled rejection.
    } finally {
      connecting = false;
    }
  };
  deps.onBackendStarting(() => void attempt());
  deps.onIpcClose(() => void attempt());
  void attempt();
}
