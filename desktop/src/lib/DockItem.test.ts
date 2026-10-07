import { afterEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import DockItem from "./DockItem.svelte";
import { setLang } from "./i18n.svelte";
import { dockAgent } from "./dockFixtures";
import type { DockAgent } from "./dockClient";

function render(agent: DockAgent, iconSrc: string | null = null) {
  const handlers = {
    onenter: vi.fn(),
    onleave: vi.fn(),
    onfocus: vi.fn(),
    onblur: vi.fn(),
    onclick: vi.fn(),
  };
  const target = document.createElement("div");
  document.body.appendChild(target);
  const instance = mount(DockItem, { target, props: { agent, iconSrc, ...handlers } });
  flushSync();
  const button = target.querySelector<HTMLButtonElement>("button.item")!;
  return { target, button, handlers, cleanup: () => { unmount(instance); target.remove(); } };
}

afterEach(() => setLang("en"));

describe("DockItem", () => {
  it("shows the status as a ring class and names the agent and status", () => {
    const { button, cleanup } = render(dockAgent({ status: "blocked", title: "Bank API" }));
    try {
      expect(button.classList.contains("st-blocked")).toBe(true);
      expect(button.title).toBe("Bank API · Needs you");
      expect(button.getAttribute("aria-label")).toBe("Bank API · Needs you");
    } finally {
      cleanup();
    }
  });

  it("speaks Czech when the UI does", () => {
    setLang("cs");
    const { button, cleanup } = render(dockAgent({ status: "working", title: "Bank API" }));
    try {
      expect(button.title).toBe("Bank API · Pracuje");
    } finally {
      cleanup();
    }
  });

  it("marks an offline agent and says so", () => {
    const { button, target, cleanup } = render(dockAgent({ status: "working", offline: true }));
    try {
      expect(button.classList.contains("offline")).toBe(true);
      expect(button.title).toBe("Bank API · Server offline");
      expect(target.querySelector(".slash")).not.toBeNull();
    } finally {
      cleanup();
    }
  });

  it("puts a dot on a done agent", () => {
    const { target, cleanup } = render(dockAgent({ status: "done" }));
    try {
      expect(target.querySelector(".done-dot")).not.toBeNull();
    } finally {
      cleanup();
    }
  });

  it("shows the favicon and falls back to the monogram when it fails to load", () => {
    const src = "herdeck://localhost/project-icon/0123456789abcdef";
    const { target, cleanup } = render(dockAgent({ icon: "0123456789abcdef", monogram: "M" }), src);
    try {
      const img = target.querySelector<HTMLImageElement>("img.mark")!;
      expect(img.getAttribute("src")).toBe(src);
      img.dispatchEvent(new Event("error"));
      flushSync();
      expect(target.querySelector("img.mark")).toBeNull();
      expect(target.querySelector(".mono")?.textContent).toBe("M");
    } finally {
      cleanup();
    }
  });

  it("reports hover, focus and click with its offset in the strip", () => {
    const { button, handlers, cleanup } = render(dockAgent());
    try {
      button.dispatchEvent(new MouseEvent("mouseenter"));
      button.dispatchEvent(new MouseEvent("mouseleave"));
      button.dispatchEvent(new FocusEvent("focus"));
      button.dispatchEvent(new FocusEvent("blur"));
      button.click();
      expect(handlers.onenter).toHaveBeenCalledWith(0);
      expect(handlers.onleave).toHaveBeenCalledTimes(1);
      expect(handlers.onfocus).toHaveBeenCalledWith(0);
      expect(handlers.onblur).toHaveBeenCalledTimes(1);
      expect(handlers.onclick).toHaveBeenCalledWith(0);
    } finally {
      cleanup();
    }
  });
});
