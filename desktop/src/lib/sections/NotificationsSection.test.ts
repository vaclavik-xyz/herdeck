import { describe, expect, it } from "vitest";
import { flushSync, mount, unmount } from "svelte";

import { parseConfig } from "../configClient";
import { setLang } from "../i18n.svelte";
import NotificationsSection from "./NotificationsSection.svelte";
import NotificationsSectionHarness from "./NotificationsSectionHarness.svelte";

function inputFor(target: HTMLElement, label: string): HTMLInputElement {
  const fieldLabel = target.querySelector<HTMLElement>(`[data-config-key="${label}"]`);
  const input = fieldLabel?.parentElement?.querySelector("input");
  if (!(input instanceof HTMLInputElement)) throw new Error(`missing input for ${label}`);
  return input;
}

function overrideFor(target: HTMLElement, label: string): HTMLElement {
  const field = Array.from(target.querySelectorAll(".override")).find(
    (node) => node.querySelector<HTMLElement>(":scope > [data-config-key]")?.dataset.configKey === label,
  );
  if (!(field instanceof HTMLElement)) throw new Error(`missing override for ${label}`);
  return field;
}

describe("NotificationsSection", () => {
  it("edits every advanced Telegram field without changing its type", () => {
    const target = document.createElement("div");
    const instance = mount(NotificationsSection, {
      target,
      props: {
        payload: parseConfig({
          base: {
            notifications: {
              telegram: {
                token_env: "TG",
                chat_id: "-1001",
                message_thread_id: 456,
                interactive: true,
                allowed_user_ids: [111, 222],
                prompt_max_chars: 777,
              },
            },
          },
        })!,
        onChange: () => {},
        onError: () => {},
      },
    });
    try {
      expect(inputFor(target, "message_thread_id").value).toBe("456");
      expect(inputFor(target, "interactive").checked).toBe(true);
      expect(inputFor(target, "allowed_user_ids").value).toBe("111, 222");
      expect(inputFor(target, "prompt_max_chars").value).toBe("777");
    } finally {
      unmount(instance);
    }
  });

  it("shows safe advanced Telegram defaults", () => {
    const target = document.createElement("div");
    const instance = mount(NotificationsSection, {
      target,
      props: { payload: parseConfig({})!, onChange: () => {}, onError: () => {} },
    });
    try {
      expect(inputFor(target, "message_thread_id").value).toBe("");
      expect(inputFor(target, "interactive").checked).toBe(false);
      expect(inputFor(target, "allowed_user_ids").value).toBe("");
      expect(inputFor(target, "prompt_max_chars").value).toBe("1200");
    } finally {
      unmount(instance);
    }
  });

  it("keeps an invalid allow-list visible and invalid in the draft payload", () => {
    const target = document.createElement("div");
    const instance = mount(NotificationsSectionHarness, {
      target,
      props: { initial: parseConfig({})! },
    });
    try {
      const input = inputFor(target, "allowed_user_ids");
      input.value = "111, nope";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      flushSync();

      expect(inputFor(target, "allowed_user_ids").value).toBe("111, nope");
      expect(target.querySelector(".allowed-payload")?.textContent).toBe('"111, nope"');
    } finally {
      unmount(instance);
    }
  });

  it("serializes a valid allow-list as integers", () => {
    const target = document.createElement("div");
    const instance = mount(NotificationsSectionHarness, {
      target,
      props: { initial: parseConfig({})! },
    });
    try {
      const input = inputFor(target, "allowed_user_ids");
      input.value = "111, 222";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      flushSync();

      expect(target.querySelector(".allowed-payload")?.textContent).toBe("[111,222]");
    } finally {
      unmount(instance);
    }
  });

  it("stores null and empty-list profile overrides distinctly from inherit", () => {
    const target = document.createElement("div");
    const instance = mount(NotificationsSectionHarness, {
      target,
      props: {
        initial: parseConfig({
          base: { notifications: { telegram: { message_thread_id: 456, allowed_user_ids: [111] } } },
          profiles: { night: {} },
        })!,
        editProfile: "night",
      },
    });
    try {
      const thread = overrideFor(target, "message_thread_id");
      (thread.querySelector(".seg button:nth-child(2)") as HTMLButtonElement).click();
      flushSync();
      const threadInput = thread.querySelector("input") as HTMLInputElement;
      threadInput.value = "";
      threadInput.dispatchEvent(new Event("change", { bubbles: true }));

      const users = overrideFor(target, "allowed_user_ids");
      (users.querySelector(".seg button:nth-child(2)") as HTMLButtonElement).click();
      flushSync();
      const usersInput = users.querySelector("input") as HTMLInputElement;
      usersInput.value = "";
      usersInput.dispatchEvent(new Event("input", { bubbles: true }));
      flushSync();

      const draft = JSON.parse(target.querySelector(".profile-telegram")?.textContent ?? "null");
      expect(draft.message_thread_id).toBe(0);
      expect(draft.allowed_user_ids).toEqual([]);
    } finally {
      unmount(instance);
    }
  });

  it("warns when a sound's event is not in the base on list and adds it back", () => {
    const target = document.createElement("div");
    const instance = mount(NotificationsSectionHarness, {
      target,
      props: { initial: parseConfig({ base: { notifications: { on: ["blocked"] } } })! },
    });
    try {
      expect(target.querySelector('[data-event-off="blocked"]')).toBeNull();
      const warning = target.querySelector<HTMLElement>('[data-event-off="done"]');
      expect(warning?.textContent).toContain("done notifications are off");
      warning!.querySelector<HTMLButtonElement>("button")!.click();
      flushSync();
      expect(JSON.parse(target.querySelector(".base-on")?.textContent ?? "null")).toEqual(["blocked", "done"]);
      expect(target.querySelector('[data-event-off="done"]')).toBeNull();
    } finally {
      unmount(instance);
    }
  });

  it("shows no event warning with the default on list (both events)", () => {
    const target = document.createElement("div");
    const instance = mount(NotificationsSectionHarness, {
      target,
      props: { initial: parseConfig({})! },
    });
    try {
      expect(target.querySelector("[data-event-off]")).toBeNull();
    } finally {
      unmount(instance);
    }
  });

  it("warns from the profile's effective on list and overrides it to re-enable", () => {
    const target = document.createElement("div");
    const instance = mount(NotificationsSectionHarness, {
      target,
      props: {
        initial: parseConfig({
          base: { notifications: { on: ["done"] } },
          profiles: { night: {} },
        })!,
        editProfile: "night",
      },
    });
    try {
      // Inherited from base: done on, blocked off.
      expect(target.querySelector('[data-event-off="done"]')).toBeNull();
      const warning = target.querySelector<HTMLElement>('[data-event-off="blocked"]');
      expect(warning?.textContent).toContain("blocked notifications are off");
      warning!.querySelector<HTMLButtonElement>("button")!.click();
      flushSync();
      expect(JSON.parse(target.querySelector(".profile-on")?.textContent ?? "null")).toEqual(["done", "blocked"]);
      // The base list is left alone; the profile now overrides it.
      expect(JSON.parse(target.querySelector(".base-on")?.textContent ?? "null")).toEqual(["done"]);
      expect(target.querySelector("[data-event-off]")).toBeNull();
    } finally {
      unmount(instance);
    }
  });

  it("warns in Czech too", () => {
    setLang("cs");
    const target = document.createElement("div");
    const instance = mount(NotificationsSectionHarness, {
      target,
      props: { initial: parseConfig({ base: { notifications: { on: [] } } })! },
    });
    try {
      expect(target.querySelector('[data-event-off="done"]')?.textContent).toContain("Upozornění done jsou vypnutá");
      expect(target.querySelector('[data-event-off="blocked"]')).not.toBeNull();
    } finally {
      unmount(instance);
      setLang("en");
    }
  });

  it("edits base per-event sounds and reverts blank fields to defaults", () => {
    const target = document.createElement("div");
    const instance = mount(NotificationsSectionHarness, {
      target,
      props: { initial: parseConfig({})! },
    });
    try {
      const done = inputFor(target, "sounds_done");
      done.value = "Pop";
      done.dispatchEvent(new Event("input", { bubbles: true }));
      flushSync();

      const blocked = inputFor(target, "sounds_blocked");
      blocked.value = "Basso";
      blocked.dispatchEvent(new Event("input", { bubbles: true }));
      flushSync();

      expect(JSON.parse(target.querySelector(".sounds-payload")?.textContent ?? "null")).toEqual({
        done: "Pop",
        blocked: "Basso",
      });

      // Clearing a field removes the key so the backend default applies again.
      blocked.value = "";
      blocked.dispatchEvent(new Event("input", { bubbles: true }));
      flushSync();
      expect(JSON.parse(target.querySelector(".sounds-payload")?.textContent ?? "null")).toEqual({
        done: "Pop",
      });

      // Clearing the remaining field too drops the sounds key entirely (the
      // backend treats a present table as an explicit override).
      done.value = "";
      done.dispatchEvent(new Event("input", { bubbles: true }));
      flushSync();
      expect(target.querySelector(".sounds-payload")?.textContent).toBe("");
    } finally {
      unmount(instance);
    }
  });

  it("keeps profile sound overrides as strings and clears them on blank input", () => {
    const target = document.createElement("div");
    const instance = mount(NotificationsSectionHarness, {
      target,
      props: {
        initial: parseConfig({
          profiles: { night: {} },
        })!,
        editProfile: "night",
      },
    });
    try {
      const done = overrideFor(target, "sounds_done");
      (done.querySelector(".seg button:nth-child(2)") as HTMLButtonElement).click();
      flushSync();
      const doneInput = done.querySelector("input") as HTMLInputElement;
      expect(doneInput.value).toBe("Hero"); // seeded with the inherited default
      doneInput.value = "Tink";
      doneInput.dispatchEvent(new Event("input", { bubbles: true }));
      flushSync();

      const draft = JSON.parse(target.querySelector(".profile-sounds")?.textContent ?? "null");
      expect(draft).toEqual({ done: "Tink" });

      // Whitespace is trimmed so the osascript sound name stays valid.
      doneInput.value = " Tink ";
      doneInput.dispatchEvent(new Event("input", { bubbles: true }));
      flushSync();
      expect(
        JSON.parse(target.querySelector(".profile-sounds")?.textContent ?? "null"),
      ).toEqual({ done: "Tink" });

      // A whitespace-only input counts as blank and clears the override.
      // an emptied sounds map drops out of the profile payload entirely.
      doneInput.value = "";
      doneInput.dispatchEvent(new Event("input", { bubbles: true }));
      flushSync();
      expect(target.querySelector(".profile-sounds")?.textContent).toBe("");
    } finally {
      unmount(instance);
    }
  });

  it("seeds a sound override from the inherited base value, not just defaults", () => {
    const target = document.createElement("div");
    const instance = mount(NotificationsSectionHarness, {
      target,
      props: {
        initial: parseConfig({
          base: { notifications: { sounds: { done: "Pop" } } },
          profiles: { night: {} },
        })!,
        editProfile: "night",
      },
    });
    try {
      const done = overrideFor(target, "sounds_done");
      // Inherit state shows the base value, not the Glass/Hero default.
      expect(done.textContent).toContain("Pop");
      // Toggling to override seeds the input with the inherited value.
      (done.querySelector(".seg button:nth-child(2)") as HTMLButtonElement).click();
      flushSync();
      const doneInput = done.querySelector("input") as HTMLInputElement;
      expect(doneInput.value).toBe("Pop");
    } finally {
      unmount(instance);
    }
  });
});
