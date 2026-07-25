import { describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import ConfirmRemoveButton from "./ConfirmRemoveButton.svelte";

describe("ConfirmRemoveButton", () => {
  it("requires a second press before running the destructive action", () => {
    const target = document.createElement("div");
    const onconfirm = vi.fn();
    const instance = mount(ConfirmRemoveButton, { target, props: { title: "Remove server", onconfirm } });
    try {
      const button = target.querySelector("button") as HTMLButtonElement;
      button.click();
      flushSync();
      expect(onconfirm).not.toHaveBeenCalled();
      expect(button.textContent).toBe("Remove?");
      button.click();
      flushSync();
      expect(onconfirm).toHaveBeenCalledOnce();
    } finally {
      unmount(instance);
    }
  });

  it("disarms automatically", () => {
    vi.useFakeTimers();
    const target = document.createElement("div");
    const instance = mount(ConfirmRemoveButton, { target, props: { title: "Remove server", onconfirm: () => {} } });
    try {
      const button = target.querySelector("button") as HTMLButtonElement;
      button.click();
      vi.advanceTimersByTime(4000);
      flushSync();
      expect(button.textContent).toBe("×");
    } finally {
      unmount(instance);
      vi.useRealTimers();
    }
  });
});
