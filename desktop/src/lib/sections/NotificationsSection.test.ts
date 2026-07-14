import { describe, expect, it } from "vitest";
import { flushSync, mount, unmount } from "svelte";

import { parseConfig } from "../configClient";
import NotificationsSection from "./NotificationsSection.svelte";
import NotificationsSectionHarness from "./NotificationsSectionHarness.svelte";

function inputFor(target: HTMLElement, label: string): HTMLInputElement {
  const fieldLabel = Array.from(target.querySelectorAll(".fieldlabel")).find(
    (node) => node.textContent?.trim() === label,
  );
  const input = fieldLabel?.parentElement?.querySelector("input");
  if (!(input instanceof HTMLInputElement)) throw new Error(`missing input for ${label}`);
  return input;
}

function overrideFor(target: HTMLElement, label: string): HTMLElement {
  const field = Array.from(target.querySelectorAll(".override")).find(
    (node) => node.querySelector(":scope > .label")?.textContent?.trim() === label,
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
      expect(draft.message_thread_id).toBeNull();
      expect(draft.allowed_user_ids).toEqual([]);
    } finally {
      unmount(instance);
    }
  });
});
