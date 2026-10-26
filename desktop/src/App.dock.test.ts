// The floating (deck-role) window picks its content from <html data-floating>
// (stamped by Rust before first paint): the mini dock by default, the
// hardware mirror for "deck". App reads role and content once, at
// construction, so each test sets the attributes only around mount().
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, unmount } from "svelte";
import { setLang } from "./lib/i18n.svelte";

const { invokeMock, listenMock, emitMock } = vi.hoisted(() => ({
  invokeMock: vi.fn(),
  listenMock: vi.fn(),
  emitMock: vi.fn(),
}));
vi.mock("@tauri-apps/api/core", () => ({ invoke: invokeMock }));
vi.mock("@tauri-apps/api/event", () => ({ listen: listenMock, emit: emitMock }));

import App from "./App.svelte";

async function fakeInvoke(cmd: string, args?: Record<string, unknown>): Promise<unknown> {
  switch (cmd) {
    case "get_discovery":
      return { url: "ws://127.0.0.1:1", host: "127.0.0.1", port: 1, source: "test" };
    case "setup_status":
      return { mode: "mock", connected: true };
    case "agent_call":
      if (typeof args?.path === "string" && args.path.startsWith("/agent/list")) {
        if (args.path === "/agent/list") {
          return { status: 200, body: { version: 1, language: "en", connected: true, agents: [] } };
        }
        return new Promise(() => {}); // the long-poll never answers here
      }
      return { status: 404, body: null };
    case "dock_layout":
      return { side: "right", stripX: 0, stripY: 0 };
    default:
      return null;
  }
}

function renderDeckWindow(floating: "dock" | "deck" | null) {
  const target = document.createElement("div");
  document.body.appendChild(target);
  document.documentElement.dataset.windowRole = "deck";
  if (floating) document.documentElement.dataset.floating = floating;
  const instance = mount(App, { target });
  delete document.documentElement.dataset.windowRole;
  delete document.documentElement.dataset.floating;
  return { target, cleanup: () => { unmount(instance); target.remove(); } };
}

beforeEach(() => {
  setLang("en");
  Object.defineProperty(window, "__TAURI_INTERNALS__", { value: {}, configurable: true });
  invokeMock.mockReset();
  invokeMock.mockImplementation(fakeInvoke);
  listenMock.mockReset();
  listenMock.mockImplementation(() => Promise.resolve(() => {}));
  emitMock.mockReset();
});

afterEach(() => {
  delete (window as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
});

describe("the floating window's content", () => {
  it("is the mini dock by default", async () => {
    const { target, cleanup } = renderDeckWindow(null);
    try {
      await vi.waitFor(() => expect(target.querySelector(".mini-dock")).not.toBeNull());
      expect(target.querySelector(".shell")).toBeNull();
      await vi.waitFor(() =>
        expect(invokeMock).toHaveBeenCalledWith("dock_layout", expect.objectContaining({ cardWidth: 0 })),
      );
      await vi.waitFor(() =>
        expect(target.querySelector("button.neutral")?.getAttribute("title")).toBe("No active agents"),
      );
      target.querySelector<HTMLButtonElement>("button.neutral")!.click();
      expect(invokeMock).toHaveBeenCalledWith("show_app");
    } finally {
      cleanup();
    }
  });

  it("is the hardware mirror when floating = deck", async () => {
    const { target, cleanup } = renderDeckWindow("deck");
    try {
      await vi.waitFor(() => expect(target.querySelector(".shell")).not.toBeNull());
      expect(target.querySelector(".mini-dock")).toBeNull();
      expect(invokeMock).not.toHaveBeenCalledWith("dock_layout", expect.anything());
    } finally {
      cleanup();
    }
  });
});
