import { describe, it, expect } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import StatusRibbon from "./StatusRibbon.svelte";
import { DEFAULT_STATUS_COLORS } from "./statusColors";

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

// Scope note: jsdom applies no scoped Svelte styles, so every CSS-level
// invariant of this component (e.g. the resting runtime dot never inheriting
// the ready tone) is asserted from source in theme.test.ts, not here.
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

  it("marks empty statuses so a non-zero count is what the eye lands on", () => {
    const empty = { agents: 4, blocked: 0, working: 4, idle: 0, done: 0, waiting: 0 };
    const { target, cleanup } = render({ ready: true, summary: empty, labels: LABELS });
    try {
      const zeroed = (status: string) =>
        target.querySelector(`[data-status="${status}"]`)?.classList.contains("zero");
      expect(zeroed("working")).toBe(false); // 4 agents: full strength
      expect(zeroed("blocked")).toBe(true);
      expect(zeroed("waiting")).toBe(true);
      expect(zeroed("done")).toBe(true);
    } finally { cleanup(); }
  });

  it("colours each cell from the deck's own palette, defaulting to the backend colours", () => {
    const { target, cleanup } = render({ ready: true, summary: SUMMARY, labels: LABELS });
    try {
      const tone = (status: string) =>
        target.querySelector<HTMLElement>(`[data-status="${status}"]`)?.getAttribute("style");
      expect(tone("working")).toContain(`var(--st-${DEFAULT_STATUS_COLORS.working})`);
      expect(tone("blocked")).toContain(`var(--st-${DEFAULT_STATUS_COLORS.blocked})`);
    } finally { cleanup(); }
  });

  it("follows a status colour the user remapped in the config", () => {
    // A remapped [theme].colors.working repaints the physical key; the ribbon
    // has to move with it or the window and the deck disagree.
    const { target, cleanup } = render({
      ready: true,
      summary: SUMMARY,
      labels: LABELS,
      colors: { working: "pink" },
    });
    try {
      const working = target.querySelector<HTMLElement>('[data-status="working"]');
      expect(working?.getAttribute("style")).toContain("var(--st-pink)");
      // untouched statuses keep their defaults
      const done = target.querySelector<HTMLElement>('[data-status="done"]');
      expect(done?.getAttribute("style")).toContain(`var(--st-${DEFAULT_STATUS_COLORS.done})`);
    } finally { cleanup(); }
  });

  it("degrades an out-of-palette colour name to dim, like the backend does", () => {
    // The backend resolves an unknown name via COLORS.get(name, dim); a
    // hand-edited TOML must not leave the window rendering nothing.
    const { target, cleanup } = render({
      ready: true,
      summary: SUMMARY,
      labels: LABELS,
      colors: { working: "chartreuse" },
    });
    try {
      const working = target.querySelector<HTMLElement>('[data-status="working"]');
      expect(working?.getAttribute("style")).toContain("var(--st-dim)");
    } finally { cleanup(); }
  });

  it("does not accept an Object prototype key as a palette name", () => {
    const { target, cleanup } = render({
      ready: true,
      summary: SUMMARY,
      labels: LABELS,
      colors: { working: "constructor" },
    });
    try {
      const working = target.querySelector<HTMLElement>('[data-status="working"]');
      expect(working?.getAttribute("style")).toContain("var(--st-dim)");
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
