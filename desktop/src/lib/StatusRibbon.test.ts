import { describe, it, expect } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import StatusRibbon from "./StatusRibbon.svelte";

const LABELS = {
  runtime: "Runtime", ready: "Ready", connecting: "Connecting",
  agents: "Agents", working: "Working", blocked: "Blocked",
  waiting: "Waiting", done: "Done",
};

const SUMMARY = { agents: 6, blocked: 1, working: 3, idle: 0, done: 2, waiting: 1 };

function render(props: Record<string, unknown>) {
  const target = document.createElement("div");
  document.body.appendChild(target);
  const instance = mount(StatusRibbon, { target, props });
  flushSync();
  return { target, cleanup: () => { unmount(instance); target.remove(); } };
}

describe("StatusRibbon", () => {
  it("shows one cell per agent status with its count", () => {
    const { target, cleanup } = render({ ready: true, summary: SUMMARY, labels: LABELS });
    try {
      const cells = Array.from(target.querySelectorAll("[data-status]"));
      expect(cells.map((c) => c.getAttribute("data-status")))
        .toEqual(["working", "blocked", "waiting", "done"]);
      expect(cells.map((c) => c.querySelector("strong")?.textContent))
        .toEqual(["3", "1", "1", "2"]);
    } finally { cleanup(); }
  });

  it("colours each cell from the deck's own status token", () => {
    const { target, cleanup } = render({ ready: true, summary: SUMMARY, labels: LABELS });
    try {
      const working = target.querySelector<HTMLElement>('[data-status="working"]');
      expect(working?.getAttribute("style")).toContain("var(--st-working)");
    } finally { cleanup(); }
  });

  it("reports the runtime as connecting when it is not ready", () => {
    const { target, cleanup } = render({
      ready: false,
      summary: { agents: 0, blocked: 0, working: 0, idle: 0, done: 0, waiting: 0 },
      labels: LABELS,
    });
    try {
      expect(target.querySelector(".runtime")?.textContent).toContain("Connecting");
      expect(target.querySelector(".runtime.ready")).toBeNull();
    } finally { cleanup(); }
  });
});
