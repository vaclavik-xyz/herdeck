import { describe, it, expect, vi } from "vitest";
import { Adapter, type Surface } from "../src/adapter.js";
import { KeyRegistry } from "../src/registry.js";

function fakeIpc() {
  const cbs: any = {};
  return {
    onReady: (cb: any) => (cbs.ready = cb),
    onRender: (cb: any) => (cbs.render = cb),
    onError: (cb: any) => (cbs.error = cb),
    onClose: (cb: any) => (cbs.close = cb),
    sendSlots: vi.fn(), sendActionKeys: vi.fn(), sendKeyDown: vi.fn(), sendKeyUp: vi.fn(),
    fire: cbs,
  };
}
function fakeSurface(): Surface & { title: string | null } {
  return { title: null, setImage() {}, setTitle(t) { this.title = t; } };
}

describe("Adapter lifecycle placeholders", () => {
  it("shows starting / backend down titles and clears on ready", () => {
    const ipc = fakeIpc();
    const adapter = new Adapter(ipc as any, new KeyRegistry());
    const surf = fakeSurface();
    adapter.registerSurface("s0", surf);

    adapter.setBackendState("starting");
    expect(surf.title).toBe("starting…");
    adapter.setBackendState("down");
    expect(surf.title).toBe("backend down");
    adapter.setBackendState("ready");
    expect(surf.title).toBe("");
  });

  it("a surface registered during 'starting' immediately gets the placeholder", () => {
    const ipc = fakeIpc();
    const adapter = new Adapter(ipc as any, new KeyRegistry());
    adapter.setBackendState("starting");
    const surf = fakeSurface();
    adapter.registerSurface("s1", surf);
    expect(surf.title).toBe("starting…");
  });

  it("an IPC close reverts to backend-down", () => {
    const ipc = fakeIpc();
    const adapter = new Adapter(ipc as any, new KeyRegistry());
    const surf = fakeSurface();
    adapter.registerSurface("s0", surf);
    ipc.fire.ready();           // -> ready, title cleared
    ipc.fire.close();           // -> down
    expect(surf.title).toBe("backend down");
  });
});
