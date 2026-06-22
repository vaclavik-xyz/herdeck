import streamDeck from "@elgato/streamdeck";
import { BackendProcess, resolveHerdeckCommand } from "./backend-process.js";
import { IpcClient } from "./ipc-client.js";
import { KeyRegistry } from "./registry.js";
import { Adapter } from "./adapter.js";
import { ACTION_UUIDS } from "./actions/core.js";
import { makeSlotAction, makeActionKey } from "./actions/sdk-actions.js";

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

  const backend = new BackendProcess({
    resolveCommand: () => resolveHerdeckCommand({ configuredPath: herdeckPath, envBin: process.env.HERDECK_BIN }),
    devSocket: process.env.HERDECK_ELGATO_DEV_SOCK,
    devToken: process.env.HERDECK_ELGATO_TOKEN,
  });
  backend.onState((s) => adapter.setBackendState(s));

  async function connect(): Promise<void> {
    try {
      await ipc.connectWithRetry(backend.socketPath, { attempts: 120, delayMs: 250 });
      ipc.sendHello(backend.token);
    } catch (err) {
      streamDeck.logger.error(`IPC connect failed: ${err}`);
    }
  }

  // Reconnect after an IPC blip or a backend respawn (the brain re-pushes a full render
  // on a fresh hello, so nothing is lost). The Adapter's onClose handler already flips the
  // keys to the "backend down" placeholder meanwhile. v1 never closes the socket on
  // purpose, so every close is a genuine drop that warrants a reconnect; connect()s are
  // sequential (onClose only fires for a previously-attached stream), so they never stack.
  ipc.onClose(() => void connect());

  backend.start();
  adapter.setBackendState("starting");
  void connect();
});
