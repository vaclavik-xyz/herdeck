import type { IpcClient } from "./ipc-client.js";
import type { KeyRegistry } from "./registry.js";
import type { RenderKeys } from "./protocol.js";

export interface Surface {
  setImage(image: string): void;
  setTitle(title: string): void;
}

export class Adapter {
  private ready = false;
  private backendState: "starting" | "down" | "ready" = "starting";
  private surfaces = new Map<string, Surface>();
  private lastImage = new Map<string, string>();

  constructor(private readonly ipc: IpcClient, private readonly registry: KeyRegistry) {
    this.ipc.onReady(() => {
      this.ready = true;
      this.setBackendState("ready");
      this.pushSnapshots();
    });
    this.ipc.onClose(() => {
      this.ready = false;
      this.setBackendState("down");
    });
    this.ipc.onRender((keys) => this.applyRender(keys));
    this.registry.onChange(() => {
      if (this.ready) this.pushSnapshots();
    });
  }

  registerSurface(instanceId: string, surface: Surface) {
    this.surfaces.set(instanceId, surface);
    const title = this.placeholderTitle();
    if (title !== null) surface.setTitle(title);
  }

  setBackendState(state: "starting" | "down" | "ready") {
    this.backendState = state;
    const title = state === "ready" ? "" : this.placeholderTitle();
    if (title !== null) this.surfaces.forEach((s) => s.setTitle(title));
  }

  private placeholderTitle(): string | null {
    if (this.backendState === "starting") return "starting…";
    if (this.backendState === "down") return "backend down";
    return null; // ready: authoritative renders own the key
  }

  unregisterSurface(instanceId: string) { this.surfaces.delete(instanceId); this.lastImage.delete(instanceId); }

  handleKeyDown(instanceId: string) { this.ipc.sendKeyDown(instanceId); }
  handleKeyUp(instanceId: string) { this.ipc.sendKeyUp(instanceId); }

  lastImageFor(instanceId: string): string | undefined { return this.lastImage.get(instanceId); }

  private pushSnapshots() {
    this.ipc.sendSlots(this.registry.slotsSnapshot());
    this.ipc.sendActionKeys(this.registry.actionKeysSnapshot());
  }

  private applyRender(keys: RenderKeys) {
    for (const [instanceId, { image }] of Object.entries(keys)) {
      const dataUrl = `data:image/png;base64,${image}`;
      this.lastImage.set(instanceId, dataUrl);
      this.surfaces.get(instanceId)?.setImage(dataUrl);
    }
  }
}
