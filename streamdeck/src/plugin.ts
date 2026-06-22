import streamDeck from "@elgato/streamdeck";
import { BackendProcess, resolveHerdeckCommand, bundledBackendPath } from "./backend-process.js";
import { IpcClient } from "./ipc-client.js";
import { KeyRegistry } from "./registry.js";
import { Adapter } from "./adapter.js";
import { ACTION_UUIDS } from "./actions/core.js";
import { makeSlotAction, makeActionKey } from "./actions/sdk-actions.js";
import { superviseConnection } from "./connection.js";

type GlobalSettings = { herdeckPath?: string };

const registry = new KeyRegistry();
const ipc = new IpcClient();
const adapter = new Adapter(ipc, registry);

streamDeck.actions.registerAction(makeSlotAction(registry, adapter));
streamDeck.actions.registerAction(makeActionKey(registry, adapter, ACTION_UUIDS.approve, "approve"));
streamDeck.actions.registerAction(makeActionKey(registry, adapter, ACTION_UUIDS.deny, "deny"));
streamDeck.actions.registerAction(makeActionKey(registry, adapter, ACTION_UUIDS.stop, "stop"));
streamDeck.actions.registerAction(makeActionKey(registry, adapter, ACTION_UUIDS.pager, "pager"));

// Initialize AFTER the connection is established. connect() resolves once connected, so
// chaining off it avoids a top-level await (fragile through the ESM bundle) and removes
// any connect()->getGlobalSettings ordering race — getGlobalSettings needs the connection.
void streamDeck.connect().then(async () => {
  // The Property Inspector writes the herdeck binary path to GLOBAL settings (its field
  // uses the `global` attribute). resolveCommand reads a mutable `herdeckPath`, and
  // onDidReceiveGlobalSettings updates it live — so when a user first sets the path the
  // supervisor's next backoff-respawn picks it up automatically (no plugin restart).
  let herdeckPath = (await streamDeck.settings.getGlobalSettings<GlobalSettings>()).herdeckPath;
  streamDeck.settings.onDidReceiveGlobalSettings<GlobalSettings>((ev) => {
    herdeckPath = ev.settings.herdeckPath;
  });

  // The bundled frozen backend (if this is a packaged install) lives next to plugin.js.
  const bundled = bundledBackendPath(import.meta.url);
  const backend = new BackendProcess({
    resolveCommand: () =>
      resolveHerdeckCommand({
        configuredPath: herdeckPath,
        envBin: process.env.HERDECK_BIN,
        bundledPath: bundled,
      }),
    devSocket: process.env.HERDECK_ELGATO_DEV_SOCK,
    devToken: process.env.HERDECK_ELGATO_TOKEN,
  });
  backend.onState((s) => adapter.setBackendState(s));

  const connectOnce = async () => {
    try {
      await ipc.connectWithRetry(backend.socketPath, { attempts: 120, delayMs: 250 });
      ipc.sendHello(backend.token);
    } catch (err) {
      streamDeck.logger.error(`IPC connect failed (will retry on next backend start): ${err}`);
    }
  };

  // Re-attempt the IPC connection on every backend (re)start AND on socket close, with a
  // single attempt in flight. This self-heals the first-run flow: a backend that initially
  // can't spawn (herdeck unresolved) never binds a socket, so the first attempt fails and
  // onClose never fires — but once the user sets the PI path the backend respawns and the
  // "starting" trigger reconnects. Registered BEFORE start() so no "starting" is missed.
  superviseConnection({
    connectOnce,
    onBackendStarting: (cb) => backend.onState((s) => { if (s === "starting") cb(); }),
    onIpcClose: (cb) => ipc.onClose(cb),
  });

  // Best-effort graceful teardown: when the Stream Deck app stops the plugin it sends
  // SIGTERM/SIGINT to the process group, so the spawned backend would be torn down with us
  // anyway — but on the catchable signals we stop it explicitly and send `bye` so the
  // socket closes cleanly rather than relying on EOF. (SIGKILL is uncatchable; that path
  // still relies on the backend handling EOF, which it does.)
  for (const sig of ["SIGTERM", "SIGINT"] as const) {
    process.on(sig, () => {
      backend.stop();
      ipc.close();
      process.exit(0);
    });
  }

  backend.start();
  adapter.setBackendState("starting"); // dev mode never emits "starting"; show it explicitly
});
