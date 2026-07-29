// The manual "check for updates" tray item exists because the SILENT mount
// check gave a user no way to tell "you are up to date" from "the check
// broke and nobody told you" (see updateClient.ts's runUpdateCheck). This
// harness mounts the real App component — role defaults to "app" (no
// data-window-role attribute in jsdom, same fallback App.svelte itself uses)
// — with a mocked Tauri bridge, and drives both the mount-time check and the
// tray's "check-for-updates" event through it, the same way ConfigApp.test.ts
// drives deck-visibility-changed.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
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

// The LAST registration wins when more than one instance is mounted (the
// deck-then-app tests below): Tauri's real `emit_to(APP_WINDOW, …)` only
// ever reaches the app window's own channel, and in every test here the app
// instance — the one actually meant to receive "check-for-updates" — is
// mounted after the deck. For the single-instance tests this is simply the
// only match.
function registeredListener(event: string): (() => void) | undefined {
  const calls = listenMock.mock.calls.filter(([name]) => name === event);
  const call = calls[calls.length - 1];
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

// listen<UpdateCheckState>'s type parameter is compile-time only — nothing
// stops a malformed payload (a future emitter, a stray broadcast from
// somewhere else) from reaching the listener at runtime. asUpdateCheckState
// is the actual guard; this drives the registered callback directly with a
// bad payload, the one thing a real cross-window emit can't be made to do
// from this test file.
describe("App update-check-result listener", () => {
  it("ignores a malformed broadcast instead of crashing on state.info", async () => {
    invokeMock.mockImplementation(mockInvoke(() => null));
    const { target, cleanup } = render();
    try {
      await vi.waitFor(() => {
        expect(listenMock.mock.calls.some(([name]) => name === "update-check-result")).toBe(true);
      });
      const call = listenMock.mock.calls.find(([name]) => name === "update-check-result");
      const cb = call![1] as (ev: { payload: unknown }) => void;

      // "available" with no `info` — exactly the shape that would throw
      // inside UpdateBanner's `state.info.version` if it were assigned as-is.
      expect(() => {
        cb({ payload: { kind: "available" } });
        flushSync(); // force the render synchronously so a bad assignment surfaces here
      }).not.toThrow();
      expect(target.querySelector(".banner"), "a rejected payload must not render").toBeNull();
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

  it("does not resize/notify the deck for a purely informational manual result", async () => {
    // The app window's manual check finds nothing new — that is only
    // interesting to the window that asked, not to a deck overlay that
    // never asked anything.
    const check = vi.fn().mockResolvedValue(null);
    invokeMock.mockImplementation(mockInvoke(check));
    const deck = renderWithRole("deck");
    try {
      const app = render();
      try {
        await vi.waitFor(() => expect(check).toHaveBeenCalledTimes(1));
        registeredListener("check-for-updates")!();
        await vi.waitFor(() => {
          expect(app.target.textContent).toContain("Herdeck is up to date.");
        });
        expect(deck.target.querySelector(".banner")).toBeNull();
      } finally {
        app.cleanup();
      }
    } finally {
      deck.cleanup();
    }
  });
});

// A later, lesser outcome must never erase a real, still-installable update:
// the check that found it does not stop being true just because a
// subsequent check found nothing new or failed outright.
describe("App update check does not downgrade a found update", () => {
  it("keeps an available update after a later manual check finds nothing new", async () => {
    const check = vi
      .fn()
      .mockResolvedValueOnce({ version: "0.2.0", current_version: "0.1.0" })
      .mockResolvedValueOnce(null);
    invokeMock.mockImplementation(mockInvoke(check));
    const { target, cleanup } = render();
    try {
      await vi.waitFor(() => {
        expect(target.textContent).toContain("Herdeck 0.2.0 is available.");
      });

      registeredListener("check-for-updates")!();
      await vi.waitFor(() => expect(check).toHaveBeenCalledTimes(2));
      // Give the second result's assignment a tick to (not) apply.
      await new Promise((r) => setTimeout(r, 0));

      expect(target.textContent).toContain("Herdeck 0.2.0 is available.");
      expect(target.textContent).not.toContain("up to date");
    } finally {
      cleanup();
    }
  });

  it("keeps an available update after a later manual check fails", async () => {
    const check = vi
      .fn()
      .mockResolvedValueOnce({ version: "0.2.0", current_version: "0.1.0" })
      .mockRejectedValueOnce(new Error("offline"));
    invokeMock.mockImplementation(mockInvoke(check));
    const { target, cleanup } = render();
    try {
      await vi.waitFor(() => {
        expect(target.textContent).toContain("Herdeck 0.2.0 is available.");
      });

      registeredListener("check-for-updates")!();
      await vi.waitFor(() => expect(check).toHaveBeenCalledTimes(2));
      await new Promise((r) => setTimeout(r, 0));

      expect(target.textContent).toContain("Herdeck 0.2.0 is available.");
      expect(target.textContent).not.toContain("Update check failed");
    } finally {
      cleanup();
    }
  });

  // The two tests above cover checks that never overlap (one fully settles,
  // THEN the next starts). Checks can genuinely overlap — a manual check
  // fired while the automatic mount check is still in flight, or a
  // double-clicked tray item — and a fix that only compares against what
  // THIS call started from (rather than the latest settled value) is
  // defeated by exactly that interleaving.
  it("keeps an available update found by a check that resolves before an overlapping, lesser one", async () => {
    let resolveAutomatic: (v: unknown) => void = () => {};
    let resolveManual: (v: unknown) => void = () => {};
    const check = vi
      .fn()
      .mockImplementationOnce(() => new Promise((resolve) => { resolveAutomatic = resolve; }))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveManual = resolve; }));
    invokeMock.mockImplementation(mockInvoke(check));
    const { target, cleanup } = render();
    try {
      // The automatic mount check is in flight (before = null)...
      await vi.waitFor(() => expect(check).toHaveBeenCalledTimes(1));
      // ...and a manual one starts on TOP of it, also capturing before = null.
      registeredListener("check-for-updates")!();
      await vi.waitFor(() => expect(check).toHaveBeenCalledTimes(2));

      // The automatic check (started FIRST) resolves FIRST, with the real update.
      resolveAutomatic({ version: "0.2.0", current_version: "0.1.0" });
      await vi.waitFor(() => {
        expect(target.textContent).toContain("Herdeck 0.2.0 is available.");
      });

      // The manual check (its OWN stale `before` snapshot was null) resolves
      // SECOND, with nothing new — it must not erase what the other found.
      resolveManual(null);
      await new Promise((r) => setTimeout(r, 0));

      expect(target.textContent).toContain("Herdeck 0.2.0 is available.");
      expect(target.textContent).not.toContain("up to date");
    } finally {
      cleanup();
    }
  });
});

// installUpdate's own resolution (the updater found nothing installable —
// typically something else already applied it) is the one thing that can
// retract an "available" banner besides a fresh check finding a newer one.
// It must clear it everywhere, not just in the window where install was
// clicked, since only one window (the app window) ever offers that button.
describe("App update install resolution clears both windows", () => {
  it("clears the banner in both windows when the install resolves to nothing installable", async () => {
    const check = vi.fn().mockResolvedValue({ version: "0.2.0", current_version: "0.1.0" });
    const baseInvoke = mockInvoke(check);
    invokeMock.mockImplementation(async (cmd: string) =>
      cmd === "update_install" ? false : baseInvoke(cmd),
    );
    const deck = renderWithRole("deck");
    try {
      const app = render();
      try {
        await vi.waitFor(() => {
          expect(app.target.textContent).toContain("Herdeck 0.2.0 is available.");
        });
        await vi.waitFor(() => {
          expect(deck.target.textContent).toContain("Herdeck 0.2.0 is available.");
        });

        const installButton = app.target.querySelector("button");
        expect(installButton?.textContent).toBe("Install and restart");
        installButton!.click();

        await vi.waitFor(() => {
          expect(app.target.querySelector(".banner"), "app window banner never cleared").toBeNull();
        });
        await vi.waitFor(() => {
          expect(deck.target.querySelector(".banner"), "deck window banner never cleared").toBeNull();
        });
      } finally {
        app.cleanup();
      }
    } finally {
      deck.cleanup();
    }
  });

  // The exact race the anti-downgrade guard has to survive: a check that was
  // ALREADY in flight when installUpdate legitimately resolved the update
  // away must not bring it back when it finally settles with a lesser
  // outcome. A guard that compares against a snapshot from when the check
  // STARTED (rather than the live state at the moment it resolves) gets this
  // backwards.
  it("does not resurrect an update that install already resolved away, from a check in flight before it", async () => {
    let resolveManual: (v: unknown) => void = () => {};
    const check = vi
      .fn()
      .mockResolvedValueOnce({ version: "0.2.0", current_version: "0.1.0" }) // automatic mount check
      .mockImplementationOnce(() => new Promise((resolve) => { resolveManual = resolve; })); // manual, deferred
    const baseInvoke = mockInvoke(check);
    invokeMock.mockImplementation(async (cmd: string) =>
      cmd === "update_install" ? false : baseInvoke(cmd),
    );
    const { target, cleanup } = render();
    try {
      await vi.waitFor(() => {
        expect(target.textContent).toContain("Herdeck 0.2.0 is available.");
      });

      // A manual re-check starts on top of the available update (no
      // "checking" placeholder — see the neighbouring describe block) and
      // is left in flight.
      registeredListener("check-for-updates")!();
      await vi.waitFor(() => expect(check).toHaveBeenCalledTimes(2));

      // The user installs WHILE that re-check is still pending, and the
      // updater finds nothing left to install.
      const installButton = target.querySelector("button");
      installButton!.click();
      await vi.waitFor(() => {
        expect(target.querySelector(".banner"), "install resolution never cleared the banner").toBeNull();
      });

      // NOW the re-check that started before the install finally resolves,
      // with nothing new — it must not resurrect what install just cleared.
      resolveManual(null);
      await new Promise((r) => setTimeout(r, 0));

      expect(
        target.querySelector(".banner"),
        "a stale in-flight check resurrected an update the install already resolved away",
      ).toBeNull();
    } finally {
      cleanup();
    }
  });

  it("does not broadcast a clear when the install succeeds (about to restart)", async () => {
    const check = vi.fn().mockResolvedValue({ version: "0.2.0", current_version: "0.1.0" });
    const baseInvoke = mockInvoke(check);
    invokeMock.mockImplementation(async (cmd: string) =>
      cmd === "update_install" ? true : baseInvoke(cmd),
    );
    const { target, cleanup } = render();
    try {
      await vi.waitFor(() => {
        expect(target.textContent).toContain("Herdeck 0.2.0 is available.");
      });
      emitMock.mockClear();

      const installButton = target.querySelector("button");
      installButton!.click();
      // installUpdate's async body needs a turn to run through.
      await new Promise((r) => setTimeout(r, 0));

      expect(emitMock).not.toHaveBeenCalledWith(expect.anything(), null);
    } finally {
      cleanup();
    }
  });

  // The "superseded by install" guard exists to keep a STALE lesser outcome
  // from un-clearing a resolved-away update — it must not go further and
  // swallow a genuinely NEW available update a check reports after that
  // same install resolution, which is real, current information.
  it("still applies a fresh available update found by a check resolving after an install resolution", async () => {
    let resolveManual: (v: unknown) => void = () => {};
    const check = vi
      .fn()
      .mockResolvedValueOnce({ version: "0.2.0", current_version: "0.1.0" }) // automatic mount check
      .mockImplementationOnce(() => new Promise((resolve) => { resolveManual = resolve; })); // manual, deferred
    const baseInvoke = mockInvoke(check);
    invokeMock.mockImplementation(async (cmd: string) =>
      cmd === "update_install" ? false : baseInvoke(cmd),
    );
    const { target, cleanup } = render();
    try {
      await vi.waitFor(() => {
        expect(target.textContent).toContain("Herdeck 0.2.0 is available.");
      });

      registeredListener("check-for-updates")!();
      await vi.waitFor(() => expect(check).toHaveBeenCalledTimes(2));

      const installButton = target.querySelector("button");
      installButton!.click();
      await vi.waitFor(() => {
        expect(target.querySelector(".banner")).toBeNull();
      });

      // The re-check that started before the install now resolves with a
      // NEWER available update — a fresh answer, not stale news, and must
      // still show up despite having started before the install resolved.
      resolveManual({ version: "0.3.0", current_version: "0.1.0" });
      await vi.waitFor(() => {
        expect(target.textContent).toContain("Herdeck 0.3.0 is available.");
      });
    } finally {
      cleanup();
    }
  });
});

// A live region only reliably announces a CONTENT change on an element that
// already existed — not a freshly created one with its first message already
// in it. UpdateBanner must therefore be mounted (and its role="status"
// wrapper present) from the very first render, before any check has settled.
describe("App update banner is a persistent live region", () => {
  it("keeps the same [role=status] node across the null -> available transition", async () => {
    let resolveCheck: (v: unknown) => void = () => {};
    const check = vi.fn().mockImplementationOnce(() => new Promise((resolve) => { resolveCheck = resolve; }));
    invokeMock.mockImplementation(mockInvoke(check));
    const { target, cleanup } = render();
    try {
      const region = target.querySelector('[role="status"]');
      expect(region, "no persistent live region before the first check settles").not.toBeNull();

      // Wait for the promise executor to have actually run (and captured
      // `resolveCheck`) before resolving it — otherwise this races ahead of
      // the real invoke call and resolves nothing.
      await vi.waitFor(() => expect(check).toHaveBeenCalledTimes(1));
      resolveCheck({ version: "0.2.0", current_version: "0.1.0" });
      await vi.waitFor(() => {
        expect(target.textContent).toContain("Herdeck 0.2.0 is available.");
      });

      expect(
        target.querySelector('[role="status"]'),
        "the live region was recreated instead of having its content mutated",
      ).toBe(region);
    } finally {
      cleanup();
    }
  });
});

// A manual re-check must not hide an already-actionable "available" banner
// behind "Checking for updates…" — `update_check` is a plain invoke with no
// timeout, so a hung re-check would otherwise strand the install button gone
// for the rest of the process's life.
describe("App update banner keeps the install action visible during a re-check", () => {
  it("does not show 'checking' when a manual re-check starts from an available update", async () => {
    let resolveSecond: (v: unknown) => void = () => {};
    const check = vi
      .fn()
      .mockResolvedValueOnce({ version: "0.2.0", current_version: "0.1.0" })
      .mockImplementationOnce(() => new Promise((resolve) => { resolveSecond = resolve; }));
    invokeMock.mockImplementation(mockInvoke(check));
    const { target, cleanup } = render();
    try {
      await vi.waitFor(() => {
        expect(target.textContent).toContain("Herdeck 0.2.0 is available.");
      });

      registeredListener("check-for-updates")!();
      await vi.waitFor(() => expect(check).toHaveBeenCalledTimes(2));
      // The re-check is now in flight (its promise never resolved above) —
      // the install button must still be there right now, not "checking".
      expect(target.textContent).toContain("Herdeck 0.2.0 is available.");
      expect(target.querySelector("button")?.textContent).toBe("Install and restart");
      expect(target.textContent).not.toContain("Checking for updates");

      resolveSecond(null); // let the in-flight check settle so nothing leaks
    } finally {
      cleanup();
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
