// The C4 shell commands behind the Notifications section: the OS sound list,
// the test notification, and the permission state.
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";

const { invokeMock } = vi.hoisted(() => ({ invokeMock: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ invoke: invokeMock }));

import { parseConfig } from "../configClient";
import { setLang } from "../i18n.svelte";
import NotificationsSection from "./NotificationsSection.svelte";

function render(errors: string[] = []) {
  setLang("en");
  const target = document.createElement("div");
  const instance = mount(NotificationsSection, {
    target,
    props: {
      payload: parseConfig({ base: { notifications: { sounds: { done: "Ping" } } } })!,
      onChange: () => {},
      onError: (m: string) => errors.push(m),
    },
  });
  return { target, cleanup: () => unmount(instance) };
}

function soundRow(target: HTMLElement, key: string): HTMLElement {
  return target.querySelector<HTMLElement>(`[data-config-key="${key}"]`)!.parentElement!;
}

beforeEach(() => {
  invokeMock.mockReset();
});

describe("NotificationsSection native sounds", () => {
  it("offers the OS sounds as a select and plays a test with the effective sound", async () => {
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === "notification_sounds") return ["Glass", "Hero", "Ping"];
      if (cmd === "notification_permission") return true;
      return null;
    });
    const { target, cleanup } = render();
    try {
      await vi.waitFor(() => expect(soundRow(target, "sounds_blocked").querySelector("select")).not.toBeNull());
      const select = soundRow(target, "sounds_done").querySelector("select")!;
      expect(select.value).toBe("Ping");
      expect(Array.from(select.options).map((o) => o.value)).toEqual(["", "Glass", "Hero", "Ping"]);

      soundRow(target, "sounds_blocked").querySelector<HTMLButtonElement>("button.test")!.click();
      flushSync();
      await vi.waitFor(() => expect(invokeMock).toHaveBeenCalledWith("test_notification", { sound: "Glass" }));
      expect(target.querySelector(".permission-warning")).toBeNull();
    } finally {
      cleanup();
    }
  });

  it("falls back to free text and warns when notifications are denied", async () => {
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === "notification_sounds") return [];
      if (cmd === "notification_permission") return false;
      if (cmd === "test_notification") throw "not authorized";
      return null;
    });
    const errors: string[] = [];
    const { target, cleanup } = render(errors);
    try {
      await vi.waitFor(() => expect(target.querySelector(".permission-warning")).not.toBeNull());
      expect(soundRow(target, "sounds_done").querySelector("select")).toBeNull();
      expect(soundRow(target, "sounds_done").querySelector("input")!.value).toBe("Ping");
      soundRow(target, "sounds_done").querySelector<HTMLButtonElement>("button.test")!.click();
      await vi.waitFor(() => expect(errors).toEqual(["test notification failed: not authorized"]));
    } finally {
      cleanup();
    }
  });
});
