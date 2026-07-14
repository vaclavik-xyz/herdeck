import { describe, expect, it } from "vitest";
import { mount, unmount } from "svelte";

import { parseConfig } from "../configClient";
import NotificationsSection from "./NotificationsSection.svelte";

function inputFor(target: HTMLElement, label: string): HTMLInputElement {
  const fieldLabel = Array.from(target.querySelectorAll(".fieldlabel")).find(
    (node) => node.textContent?.trim() === label,
  );
  const input = fieldLabel?.parentElement?.querySelector("input");
  if (!(input instanceof HTMLInputElement)) throw new Error(`missing input for ${label}`);
  return input;
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
});
