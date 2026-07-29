// The manual "check for updates" tray item exists because the SILENT mount
// check gave a user no way to tell "you are up to date" from "the check
// broke and nobody told you" (see updateClient.ts's runUpdateCheck). This
// harness mounts the real App component — role defaults to "app" (no
// data-window-role attribute in jsdom, same fallback App.svelte itself uses)
// — with a mocked Tauri bridge, and drives both the mount-time check and the
// tray's "check-for-updates" event through it, the same way ConfigApp.test.ts
// drives deck-visibility-changed.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, unmount } from "svelte";
import { setLang } from "./lib/i18n.svelte";

const { invokeMock, listenMock, emitMock } = vi.hoisted(() => ({
  invokeMock: vi.fn(),
  listenMock: vi.fn(),
  emitMock: vi.fn(),
}));
vi.mock("@tauri-apps/api/core", () => ({ invoke: invokeMock }));
vi.mock("@tauri-apps/api/event", () => ({ listen: listenMock, emit: emitMock }));

import App from "./App.svelte";

function rawConfig() {
  return {
    base: { servers: [], desktop: { deck_always_on_top: false } },
    profiles: {},
    local: {},
    secrets: {},
    active_profile: "default",
  };
}

// update_check's outcome is the one thing each test below varies; everything
// else is fixed so the app reaches a settled "deck" state (no onboarding
// card in the way of the banner) regardless.
function mockInvoke(updateCheck: () => unknown) {
  return async (cmd: string): Promise<unknown> => {
    switch (cmd) {
      case "get_discovery":
        return { url: "ws://127.0.0.1:1", host: "127.0.0.1", port: 1, source: "test" };
      case "setup_status":
        return { mode: "mock", connected: true };
      case "config_read":
        return rawConfig();
      case "config_validate":
      case "config_write":
        return { errors: [] };
      case "update_check":
        return updateCheck();
      default:
        return null;
    }
  };
}

let target: HTMLElement;

beforeEach(() => {
  setLang("en");
  // Same gate ConfigApp.test.ts sets: without it the read-only browser-preview
  // path takes over instead of the real invoke-backed one.
  Object.defineProperty(window, "__TAURI_INTERNALS__", { value: {}, configurable: true });
  invokeMock.mockReset();
  listenMock.mockReset();
  listenMock.mockImplementation(() => Promise.resolve(() => {}));
  // A real Tauri `emit` reaches every window's listeners, including the
  // sender's own — so the mock delivers to every callback registered (via
  // listenMock) for that event name, across however many instances are
  // mounted, the same way two real windows sharing one process would.
  emitMock.mockReset();
  emitMock.mockImplementation(async (event: string, payload: unknown) => {
    for (const [name, cb] of listenMock.mock.calls) {
      if (name === event) (cb as (ev: { payload: unknown }) => void)({ payload });
    }
  });
  target = document.createElement("div");
  document.body.appendChild(target);
});

afterEach(() => {
  delete (window as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
  delete document.documentElement.dataset.windowRole;
  target.remove();
});

function registeredListener(event: string): (() => void) | undefined {
  const call = listenMock.mock.calls.find(([name]) => name === event);
  return call?.[1] as (() => void) | undefined;
}

function render(): { target: HTMLElement; cleanup: () => void } {
  const instance = mount(App, { target });
  return { target, cleanup: () => unmount(instance) };
}

// Rust stamps `data-window-role` on `<html>` before first paint, and
// App.svelte reads it into a plain (non-reactive) `const role` once, at
// component construction — so setting the attribute only for the
// synchronous `mount()` call, then clearing it immediately after, gives this
// one instance the "deck" role without affecting any other mounted here.
function renderWithRole(role: "app" | "deck"): { target: HTMLElement; cleanup: () => void } {
  const t = document.createElement("div");
  document.body.appendChild(t);
  if (role === "deck") document.documentElement.dataset.windowRole = "deck";
  const instance = mount(App, { target: t });
  delete document.documentElement.dataset.windowRole;
  return { target: t, cleanup: () => { unmount(instance); t.remove(); } };
}

describe("App update check", () => {
  it("checks once on mount and stays silent when it fails", async () => {
    const check = vi.fn().mockRejectedValue(new Error("offline"));
    invokeMock.mockImplementation(mockInvoke(check));
    const { target, cleanup } = render();
    try {
      await vi.waitFor(() => expect(check).toHaveBeenCalledTimes(1));
      // Give the rejected check's .catch/assignment a tick to (not) render.
      await new Promise((r) => setTimeout(r, 0));
      expect(target.querySelector(".banner"), "a silent mount failure must render nothing").toBeNull();
    } finally {
      cleanup();
    }
  });

  it("shows an available update found by the automatic mount check", async () => {
    const check = vi.fn().mockResolvedValue({ version: "0.2.0", current_version: "0.1.0" });
    invokeMock.mockImplementation(mockInvoke(check));
    const { target, cleanup } = render();
    try {
      await vi.waitFor(() => {
        expect(target.textContent).toContain("Herdeck 0.2.0 is available.");
      });
    } finally {
      cleanup();
    }
  });

  it("registers a listener for the tray's check-for-updates event", async () => {
    invokeMock.mockImplementation(mockInvoke(() => null));
    const { cleanup } = render();
    try {
      await vi.waitFor(() => {
        expect(registeredListener("check-for-updates"), "no check-for-updates listener registered")
          .toBeTruthy();
      });
    } finally {
      cleanup();
    }
  });

  it("reports a failure on a manual (tray-triggered) check, unlike the silent mount check", async () => {
    const check = vi.fn().mockRejectedValue(new Error("network unreachable"));
    invokeMock.mockImplementation(mockInvoke(check));
    const { target, cleanup } = render();
    try {
      await vi.waitFor(() => expect(check).toHaveBeenCalledTimes(1));
      // The automatic check just failed silently (previous test covers this).
      expect(target.querySelector(".banner")).toBeNull();

      registeredListener("check-for-updates")!();

      await vi.waitFor(() => {
        expect(target.textContent).toContain("Update check failed: network unreachable");
      });
      expect(check).toHaveBeenCalledTimes(2);
    } finally {
      cleanup();
    }
  });

  it("reports up to date on a manual check that finds nothing", async () => {
    const check = vi.fn().mockResolvedValue(null);
    invokeMock.mockImplementation(mockInvoke(check));
    const { target, cleanup } = render();
    try {
      await vi.waitFor(() => expect(check).toHaveBeenCalledTimes(1));
      // Nothing to show yet: the automatic check found no update either, and
      // that outcome is not surfaced unless it was asked for.
      expect(target.querySelector(".banner")).toBeNull();

      registeredListener("check-for-updates")!();

      await vi.waitFor(() => {
        expect(target.textContent).toContain("Herdeck is up to date.");
      });
    } finally {
      cleanup();
    }
  });
});

// Both windows mount this same component. Only the app window may ever run
// the check itself (asserted directly here); the deck window's ability to
// SHOW a result it never checked for depends entirely on the
// "update-check-result" broadcast — this is the part that was previously
// just a comment's claim with nothing behind it.
describe("App update check across windows", () => {
  it("never runs its own check from the deck window", async () => {
    const check = vi.fn().mockResolvedValue(null);
    invokeMock.mockImplementation(mockInvoke(check));
    const { cleanup } = renderWithRole("deck");
    try {
      // There is nothing to wait FOR — this gives any stray promise chain a
      // turn before asserting its absence.
      await new Promise((r) => setTimeout(r, 0));
      expect(check).not.toHaveBeenCalled();
    } finally {
      cleanup();
    }
  });

  it("shows an update in the deck window that only the app window's check found", async () => {
    const check = vi.fn().mockResolvedValue({ version: "0.2.0", current_version: "0.1.0" });
    invokeMock.mockImplementation(mockInvoke(check));
    const deck = renderWithRole("deck");
    try {
      const app = render();
      try {
        await vi.waitFor(() => expect(check).toHaveBeenCalledTimes(1));
        await vi.waitFor(() => {
          expect(deck.target.textContent).toContain("Herdeck 0.2.0 is available.");
        });
        // Confirms the deck never ran a check of its own to get there.
        expect(check).toHaveBeenCalledTimes(1);
      } finally {
        app.cleanup();
      }
    } finally {
      deck.cleanup();
    }
  });
});

// "Up to date" and "failed" are dead ends nothing else ever clears — left
// alone they would pin the banner in place (stealing height from the
// content-fit deck window) for the rest of the process's life. "Available"
// stays until installed or dismissed by an install attempt; it must NOT be
// swept away on the same clock.
describe("App update banner auto-dismiss", () => {
  it("clears a failed manual check after a timeout", async () => {
    vi.useFakeTimers();
    try {
      const check = vi.fn().mockRejectedValue(new Error("offline"));
      invokeMock.mockImplementation(mockInvoke(check));
      const { target, cleanup } = render();
      try {
        await vi.advanceTimersByTimeAsync(0);
        registeredListener("check-for-updates")!();
        await vi.advanceTimersByTimeAsync(0);
        expect(target.textContent).toContain("Update check failed: offline");

        await vi.advanceTimersByTimeAsync(8000);
        expect(target.querySelector(".banner"), "the failed banner never cleared").toBeNull();
      } finally {
        cleanup();
      }
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not clear an available update on the same timeout", async () => {
    vi.useFakeTimers();
    try {
      const check = vi.fn().mockResolvedValue({ version: "0.2.0", current_version: "0.1.0" });
      invokeMock.mockImplementation(mockInvoke(check));
      const { target, cleanup } = render();
      try {
        await vi.advanceTimersByTimeAsync(0);
        expect(target.textContent).toContain("Herdeck 0.2.0 is available.");

        await vi.advanceTimersByTimeAsync(8000);
        expect(target.textContent, "an actionable available-update banner must not auto-dismiss")
          .toContain("Herdeck 0.2.0 is available.");
      } finally {
        cleanup();
      }
    } finally {
      vi.useRealTimers();
    }
  });
});
