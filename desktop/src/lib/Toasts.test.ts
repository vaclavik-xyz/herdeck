import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import Toasts from "./Toasts.svelte";
import { setLang } from "./i18n.svelte";
import { TOAST_SUCCESS_MS, bridgeUpdateSteps, clearToasts, showToast, toasts } from "./toastStore.svelte";

const LABELS = { download: "Download", install: "Install", verify: "Verify", restart: "Restart" };

let target: HTMLElement;
let cleanup: (() => void) | null = null;

beforeEach(() => {
  target = document.createElement("div");
  document.body.appendChild(target);
  const instance = mount(Toasts, { target, props: {} });
  cleanup = () => unmount(instance);
});
afterEach(() => {
  cleanup?.();
  target.remove();
  clearToasts();
  vi.useRealTimers();
  setLang("en");
});

const shown = () => Array.from(target.querySelectorAll<HTMLElement>("[data-toast]")).map((t) => t.dataset.toast);

describe("Toasts", () => {
  it("auto-dismisses a success after 5 s", () => {
    vi.useFakeTimers();
    showToast({ id: "ok", kind: "success", text: "Done" });
    flushSync();
    expect(shown()).toEqual(["ok"]);
    vi.advanceTimersByTime(TOAST_SUCCESS_MS - 1);
    flushSync();
    expect(shown()).toEqual(["ok"]);
    vi.advanceTimersByTime(1);
    flushSync();
    expect(shown()).toEqual([]);
  });

  it("keeps an error until it is closed", () => {
    vi.useFakeTimers();
    showToast({ id: "bad", kind: "error", text: "Failed" });
    flushSync();
    vi.advanceTimersByTime(60_000);
    flushSync();
    expect(shown()).toEqual(["bad"]);
    const close = target.querySelector<HTMLButtonElement>('[data-toast="bad"] button.close')!;
    expect(close.getAttribute("title")).toBe("Close");
    close.click();
    flushSync();
    expect(shown()).toEqual([]);
  });

  it("replaces a progress toast in place with its outcome (same id)", () => {
    vi.useFakeTimers();
    showToast({ id: "a", kind: "progress", text: "Working…" });
    vi.advanceTimersByTime(60_000);
    showToast({ id: "a", kind: "success", text: "Done" });
    flushSync();
    expect(toasts.items.map((t) => [t.id, t.kind])).toEqual([["a", "success"]]);
    vi.advanceTimersByTime(TOAST_SUCCESS_MS);
    expect(toasts.items).toEqual([]);
  });

  it("closes in Czech too", () => {
    setLang("cs");
    showToast({ id: "bad", kind: "error", text: "Chyba" });
    flushSync();
    expect(target.querySelector('[data-toast="bad"] button.close')?.getAttribute("title")).toBe("Zavřít");
  });

  it("renders a stepper and a copyable command", () => {
    showToast({ id: "s", kind: "progress", text: "Updating", steps: bridgeUpdateSteps(["download", "install"], "pending", LABELS), command: "sudo x" });
    flushSync();
    const states = Array.from(target.querySelectorAll<HTMLElement>("[data-step]")).map((li) => `${li.textContent}:${li.dataset.state}`);
    expect(states).toEqual(["Download:done", "Install:active", "Verify:pending", "Restart:pending"]);
    expect(target.querySelector("code")?.textContent).toBe("sudo x");
    expect(target.querySelector("button.copy")?.getAttribute("title")).toBe("Copy the command to the clipboard");
  });
});

describe("bridgeUpdateSteps", () => {
  const states = (stages: string[], code: string) => bridgeUpdateSteps(stages, code, LABELS).map((s) => s.state);
  it("maps download → install → verify → restart", () => {
    expect(states([], "pending")).toEqual(["active", "pending", "pending", "pending"]);
    expect(states(["download", "download", "install"], "pending")).toEqual(["done", "active", "pending", "pending"]);
    expect(states(["download", "install", "verify"], "pending")).toEqual(["done", "done", "active", "pending"]);
    expect(states(["download", "install", "verify"], "updated")).toEqual(["done", "done", "done", "done"]);
    expect(states(["download", "install"], "failed")).toEqual(["done", "failed", "pending", "pending"]);
    expect(states(["something-new"], "pending")).toEqual(["active", "pending", "pending", "pending"]);
  });
});
