import { describe, it, expect, vi } from "vitest";
import { Adapter, type Surface } from "../src/adapter.js";
import { KeyRegistry } from "../src/registry.js";

function fakeIpc() {
  const cbs: any = {};
  return {
    onReady: (cb: any) => (cbs.ready = cb),
    onRender: (cb: any) => (cbs.render = cb),
    onError: vi.fn(),
    onClose: vi.fn(),
    sendSlots: vi.fn(),
    sendActionKeys: vi.fn(),
    sendKeyDown: vi.fn(),
    sendKeyUp: vi.fn(),
    fire: cbs,
  };
}

function fakeSurface(): Surface & { img: string | null; title: string | null } {
  return { img: null, title: null, setImage(i) { this.img = i; }, setTitle(t) { this.title = t; } };
}

describe("Adapter", () => {
  it("pushes the full snapshot to the brain on ready", () => {
    const ipc = fakeIpc();
    const reg = new KeyRegistry();
    reg.addSlot("s0", { col: 0, row: 0 });
    reg.addActionKey("a", "approve", { col: 0, row: 2 });
    new Adapter(ipc as any, reg);
    ipc.fire.ready();
    expect(ipc.sendSlots).toHaveBeenCalledWith([{ instanceId: "s0", coord: { col: 0, row: 0 } }]);
    expect(ipc.sendActionKeys).toHaveBeenCalledWith([{ instanceId: "a", type: "approve", coord: { col: 0, row: 2 } }]);
  });

  it("re-pushes snapshots when the registry changes after ready", () => {
    const ipc = fakeIpc();
    const reg = new KeyRegistry();
    new Adapter(ipc as any, reg);
    ipc.fire.ready();
    ipc.sendSlots.mockClear();
    reg.addSlot("s9", { col: 4, row: 0 });
    expect(ipc.sendSlots).toHaveBeenCalledWith([{ instanceId: "s9", coord: { col: 4, row: 0 } }]);
  });

  it("does not push snapshots before ready", () => {
    const ipc = fakeIpc();
    const reg = new KeyRegistry();
    new Adapter(ipc as any, reg);
    reg.addSlot("s0", { col: 0, row: 0 }); // before ready
    expect(ipc.sendSlots).not.toHaveBeenCalled();
  });

  it("renders base64 images as data URLs onto the matching surface and caches them", () => {
    const ipc = fakeIpc();
    const reg = new KeyRegistry();
    const adapter = new Adapter(ipc as any, reg);
    const surf = fakeSurface();
    adapter.registerSurface("s0", surf);
    ipc.fire.render({ s0: { image: "QUJD", title: null }, unknown: { image: "ZZ", title: null } });
    expect(surf.img).toBe("data:image/png;base64,QUJD");
    expect(adapter.lastImageFor("s0")).toBe("data:image/png;base64,QUJD");
  });

  it("forwards key presses to the brain", () => {
    const ipc = fakeIpc();
    const adapter = new Adapter(ipc as any, new KeyRegistry());
    adapter.handleKeyDown("s0");
    adapter.handleKeyUp("s0");
    expect(ipc.sendKeyDown).toHaveBeenCalledWith("s0");
    expect(ipc.sendKeyUp).toHaveBeenCalledWith("s0");
  });
});
