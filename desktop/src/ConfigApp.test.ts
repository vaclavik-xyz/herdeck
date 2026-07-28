// ConfigApp has no general test suite yet (nothing here mocks the Tauri
// bridge before this file). This test is scoped narrowly to the fix-round
// regression it exists to close: Apply must re-apply `deck_always_on_top`
// live, the same way it already re-registers the hotkey — see
// docs/superpowers/plans/2026-07-28-window-roles.md and task-6-report.md.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import { setLang } from "./lib/i18n.svelte";

const { invokeMock } = vi.hoisted(() => ({ invokeMock: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ invoke: invokeMock }));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn(() => Promise.resolve(() => {})) }));

import ConfigApp from "./ConfigApp.svelte";

function rawConfig() {
  return {
    base: { servers: [], desktop: { deck_always_on_top: false } },
    profiles: {},
    local: {},
    secrets: {},
    active_profile: "default",
  };
}

function mockInvoke(cmd: string): unknown {
  switch (cmd) {
    case "get_discovery":
      return { url: "ws://127.0.0.1:1", host: "127.0.0.1", port: 1, source: "test" };
    case "config_read":
      return rawConfig();
    case "config_validate":
    case "config_write":
      return { errors: [] };
    default:
      return null;
  }
}

describe("ConfigApp Apply re-applies deck_always_on_top", () => {
  let target: HTMLElement;

  beforeEach(() => {
    setLang("en");
    // browserMode (the read-only design-preview path) is gated on this global's
    // absence — set it so Apply actually runs the real invoke-backed path.
    Object.defineProperty(window, "__TAURI_INTERNALS__", { value: {}, configurable: true });
    invokeMock.mockReset();
    invokeMock.mockImplementation(async (cmd: string) => mockInvoke(cmd));
    target = document.createElement("div");
    document.body.appendChild(target);
  });

  afterEach(() => {
    delete (window as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
    target.remove();
  });

  it("invokes reload_deck_always_on_top after a successful save, like reload_hotkey", async () => {
    const instance = mount(ConfigApp, { target, props: { interactive: true } });
    try {
      const desktopNav = Array.from(target.querySelectorAll<HTMLButtonElement>(".sidebar button"))
        .find((b) => b.textContent?.includes("Window"));
      expect(desktopNav, "desktop nav item not found").toBeTruthy();
      desktopNav!.click();
      flushSync();

      await vi.waitFor(() => {
        expect(target.querySelector(".loading-card")).toBeNull();
      });

      const checkbox = target.querySelector<HTMLInputElement>(".content input[type='checkbox']");
      expect(checkbox, "deck_always_on_top checkbox not rendered").toBeTruthy();
      checkbox!.checked = true;
      checkbox!.dispatchEvent(new Event("change", { bubbles: true }));
      flushSync();

      const applyButton = Array.from(target.querySelectorAll<HTMLButtonElement>(".savebar button"))
        .find((b) => b.title.startsWith("Save the config"));
      expect(applyButton, "Apply button not found").toBeTruthy();
      applyButton!.click();

      await vi.waitFor(() => {
        expect(invokeMock).toHaveBeenCalledWith("reload_deck_always_on_top");
      });
      // The hotkey field wasn't touched, but Apply re-registers it unconditionally
      // today — confirms this test drives the real Apply path, not a stub of it.
      expect(invokeMock).toHaveBeenCalledWith("reload_hotkey");
    } finally {
      unmount(instance);
    }
  });
});
