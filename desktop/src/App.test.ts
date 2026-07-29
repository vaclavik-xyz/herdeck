// The manual "check for updates" tray item exists because the SILENT mount
// check gave a user no way to tell "you are up to date" from "the check
// broke and nobody told you" (see updateClient.ts's runUpdateCheck). This
// harness mounts the real App component — role defaults to "app" (no
// data-window-role attribute in jsdom, same fallback App.svelte itself uses)
// — with a mocked Tauri bridge, and drives both the mount-time check and the
// tray's "check-for-updates" event through it, the same way ConfigApp.test.ts
// drives deck-visibility-changed.
//
// The reconciliation itself (sticky availableUpdate + transient notice +
// monotonic checkSeq) is a pure reducer tested in isolation in
// updateState.test.ts — what belongs HERE is the wiring: does the mount
// effect actually call it, does the tray listener, does the broadcast reach
// the other window, does the DOM show what the reducer says.
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

// listen<unknown>'s type parameter is compile-time only — nothing stops a
// malformed payload (a future emitter, a stray broadcast from somewhere
// else) from reaching the listener at runtime. asUpdateCheckState is the
// actual guard; this drives the registered callback directly with a bad
// payload, the one thing a real cross-window emit can't be made to do from
// this test file.
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
      // inside UpdateBanner's `availableUpdate.version` if it were assigned
      // as-is.
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

// The behaviour a manual re-check has when an update is ALREADY showing —
// the state a user is most likely to click "Check for updates" from. This is
// where the original defect lived: a manual check used to report NOTHING at
// all here (see the review at .superpowers/sdd/update-check-review.md,
// Finding 1). The fix is structural, not a guard: `notice` and
// `availableUpdate` are different fields (updateState.ts), so a transient
// outcome renders on top of the sticky one without needing to compare
// against it.
describe("App update check on top of an already-found update", () => {
  it("reports a failure from a manual re-check, and the available update survives underneath", async () => {
    vi.useFakeTimers();
    try {
      const check = vi
        .fn()
        .mockResolvedValueOnce({ version: "0.2.0", current_version: "0.1.0" }) // automatic
        .mockRejectedValueOnce(new Error("offline")); // manual re-check
      invokeMock.mockImplementation(mockInvoke(check));
      const { target, cleanup } = render();
      try {
        await vi.advanceTimersByTimeAsync(0);
        expect(target.textContent).toContain("Herdeck 0.2.0 is available.");

        registeredListener("check-for-updates")!();
        await vi.advanceTimersByTimeAsync(0);

        // The original defect: nothing changed here at all — not while
        // checking, not on failure. Now the failure is reported...
        expect(target.textContent).toContain("Update check failed: offline");
        // ...temporarily covering the install button (scoped to the update
        // banner's own live region — an unscoped "button" query would find
        // ConfigApp's unrelated "Show deck" button instead)...
        expect(target.querySelector<HTMLButtonElement>('[role="status"] button')).toBeNull();

        // ...but the update itself was never touched underneath: once the
        // notice auto-dismisses, the install action is exactly where it was.
        await vi.advanceTimersByTimeAsync(8000);
        expect(target.textContent).toContain("Herdeck 0.2.0 is available.");
        expect(target.querySelector<HTMLButtonElement>('[role="status"] button')?.textContent).toBe("Install and restart");
      } finally {
        cleanup();
      }
    } finally {
      vi.useRealTimers();
    }
  });

  it("a manual re-check confirming up-to-date retracts a stale available update", async () => {
    // Not a regression: a confirmed up-to-date means the release was pulled
    // or already applied, so the install button really has nothing left to
    // do — see Finding 7 in the review.
    const check = vi
      .fn()
      .mockResolvedValueOnce({ version: "0.2.0", current_version: "0.1.0" })
      .mockResolvedValueOnce(null);
    invokeMock.mockImplementation(mockInvoke(check));
    const { target, cleanup } = render();
    try {
      await vi.waitFor(() => expect(target.textContent).toContain("Herdeck 0.2.0 is available."));

      registeredListener("check-for-updates")!();
      await vi.waitFor(() => expect(target.textContent).toContain("Herdeck is up to date."));
      expect(target.textContent).not.toContain("is available");
    } finally {
      cleanup();
    }
  });

  it("shows 'checking' during a manual re-check, temporarily covering an available update", async () => {
    let resolveSecond: (v: unknown) => void = () => {};
    const check = vi
      .fn()
      .mockResolvedValueOnce({ version: "0.2.0", current_version: "0.1.0" })
      .mockImplementationOnce(() => new Promise((resolve) => { resolveSecond = resolve; }));
    invokeMock.mockImplementation(mockInvoke(check));
    const { target, cleanup } = render();
    try {
      await vi.waitFor(() => expect(target.textContent).toContain("Herdeck 0.2.0 is available."));

      registeredListener("check-for-updates")!();
      await vi.waitFor(() => expect(check).toHaveBeenCalledTimes(2));
      expect(target.textContent).toContain("Checking for updates");
      expect(target.textContent).not.toContain("is available");

      // Resolves with the same version: the update reappears once
      // "checking" clears, exactly as it was before the re-check.
      resolveSecond({ version: "0.2.0", current_version: "0.1.0" });
      await vi.waitFor(() => expect(target.textContent).toContain("Herdeck 0.2.0 is available."));
    } finally {
      cleanup();
    }
  });

  // The accepted cost of the simplification (see the review): a check that
  // was straddling an install resolution — in flight when it landed — is
  // dropped outright by the monotonic epoch, even if it would have reported
  // a genuinely newer version. The user re-checks. This is deliberately
  // different from three earlier commits' behaviour (which tried to
  // preserve exactly this case with a generation counter and a per-version
  // blacklist) — pinned here so the trade-off stays a decision, not a
  // silent regression.
  it("(accepted cost) drops a straddling check's fresh discovery once an install resolution lands", async () => {
    // Starting a manual check on top of an already-showing update means
    // "checking" now covers the install button (the fix above) — so the
    // resolution below is driven directly through the broadcast listener,
    // the same path installUpdate's own resolution reaches the OTHER window
    // through, rather than depending on which DOM button happens to be
    // reachable under the notice.
    let resolveManual: (v: unknown) => void = () => {};
    const check = vi
      .fn()
      .mockResolvedValueOnce({ version: "0.2.0", current_version: "0.1.0" }) // automatic
      .mockImplementationOnce(() => new Promise((resolve) => { resolveManual = resolve; })); // manual, deferred
    invokeMock.mockImplementation(mockInvoke(check));
    const { target, cleanup } = render();
    try {
      await vi.waitFor(() => expect(target.textContent).toContain("Herdeck 0.2.0 is available."));

      registeredListener("check-for-updates")!();
      await vi.waitFor(() => expect(check).toHaveBeenCalledTimes(2));

      const resultListener = listenMock.mock.calls.find(([name]) => name === "update-check-result");
      const deliver = resultListener![1] as (ev: { payload: unknown }) => void;
      deliver({ payload: null });
      flushSync();
      expect(target.textContent).toContain("Herdeck is up to date.");

      // The re-check that started BEFORE the resolution settles now, with a
      // genuinely newer version — dropped anyway: the epoch already moved
      // on while it was in flight.
      resolveManual({ version: "0.3.0", current_version: "0.1.0" });
      await new Promise((r) => setTimeout(r, 0));
      expect(target.textContent).not.toContain("0.3.0");
      expect(target.textContent).toContain("Herdeck is up to date.");
    } finally {
      cleanup();
    }
  });
});

// installUpdate's own resolution (the updater found nothing installable —
// typically something else already applied it, or the release was pulled;
// a SUCCESSFUL install never reaches here, since it calls
// app.request_restart() Rust-side and the process ends) is the one thing
// that can retract an "available" banner besides a fresh check confirming
// up-to-date. It must clear it everywhere, not just in the window where
// install was clicked, since either window can offer that button.
describe("App update install resolution clears both windows", () => {
  it("clears the available update in both windows when the install resolves to nothing installable", async () => {
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

        const installButton = app.target.querySelector<HTMLButtonElement>('[role="status"] button');
        expect(installButton?.textContent).toBe("Install and restart");
        installButton!.click();

        // applyResolvedAway answers "up to date" (not silence) in BOTH
        // windows — the app locally, the deck via the broadcast.
        await vi.waitFor(() => {
          expect(app.target.textContent, "app window never answered").toContain("Herdeck is up to date.");
        });
        await vi.waitFor(() => {
          expect(deck.target.textContent, "deck window never learned of the resolution").toContain(
            "Herdeck is up to date.",
          );
        });
      } finally {
        app.cleanup();
      }
    } finally {
      deck.cleanup();
    }
  });

  it("does not broadcast a clear when the install succeeds (about to restart)", async () => {
    const check = vi.fn().mockResolvedValue({ version: "0.2.0", current_version: "0.1.0" });
    const baseInvoke = mockInvoke(check);
    const install = vi.fn().mockResolvedValue(true);
    invokeMock.mockImplementation(async (cmd: string) => (cmd === "update_install" ? install() : baseInvoke(cmd)));
    const { target, cleanup } = render();
    try {
      await vi.waitFor(() => {
        expect(target.textContent).toContain("Herdeck 0.2.0 is available.");
      });
      emitMock.mockClear();

      const installButton = target.querySelector("button");
      installButton!.click();
      // Positive check first: confirm installUpdate's async body actually
      // ran all the way through to the (successful) resolution, not just
      // that nothing happened at all — a `not.toHaveBeenCalledWith(…, null)`
      // alone would also pass if the install path silently never ran.
      // Waiting on `install` having been CALLED is not enough: that happens
      // synchronously on click, before its promise (and the code after
      // awaiting it) has settled. A macrotask boundary (not just a
      // microtask/`vi.waitFor`'s first synchronous check) is what actually
      // guarantees the whole async body — including the code after the
      // await — has run to completion.
      await new Promise((r) => setTimeout(r, 0));
      expect(install).toHaveBeenCalledTimes(1);
      expect(target.textContent, "a successful install must leave the banner as-is").toContain(
        "Herdeck 0.2.0 is available.",
      );

      expect(emitMock).not.toHaveBeenCalledWith("update-check-result", null);
    } finally {
      cleanup();
    }
  });

  // installUpdate's resolution can happen in EITHER window (both render the
  // install button) — the epoch is per-window state, so it must be kept in
  // sync through the SAME "update-check-result" broadcast an available
  // update travels over, or a check running in the window that never
  // installed anything is blind to what the other one just resolved.
  it("keeps a resolved-away update cleared when the app's check settles after the DECK's install", async () => {
    let resolveManual: (v: unknown) => void = () => {};
    const check = vi
      .fn()
      .mockResolvedValueOnce({ version: "0.2.0", current_version: "0.1.0" }) // app's automatic mount check
      .mockImplementationOnce(() => new Promise((resolve) => { resolveManual = resolve; })); // app's manual re-check, deferred
    const baseInvoke = mockInvoke(check);
    invokeMock.mockImplementation(async (cmd: string) =>
      cmd === "update_install" ? false : baseInvoke(cmd),
    );
    const deck = renderWithRole("deck");
    try {
      const app = render();
      try {
        await vi.waitFor(() => expect(app.target.textContent).toContain("Herdeck 0.2.0 is available."));
        await vi.waitFor(() => expect(deck.target.textContent).toContain("Herdeck 0.2.0 is available."));

        // A manual re-check starts in the APP window and is left in flight.
        registeredListener("check-for-updates")!();
        await vi.waitFor(() => expect(check).toHaveBeenCalledTimes(2));

        // The user installs from the DECK window instead.
        const deckInstallButton = deck.target.querySelector<HTMLButtonElement>('[role="status"] button');
        deckInstallButton!.click();
        await vi.waitFor(() => expect(deck.target.textContent).toContain("Herdeck is up to date."));

        // The app window's in-flight check (started before the DECK's
        // install resolved things) now settles with the same stale version
        // — dropped, since the deck's broadcast already moved the app's
        // own epoch on.
        resolveManual({ version: "0.2.0", current_version: "0.1.0" });
        await new Promise((r) => setTimeout(r, 0));

        expect(
          app.target.textContent,
          "the app window resurrected what the deck's install cleared",
        ).toContain("Herdeck is up to date.");
        expect(
          deck.target.textContent,
          "the deck window resurrected what its own install cleared",
        ).toContain("Herdeck is up to date.");
      } finally {
        app.cleanup();
      }
    } finally {
      deck.cleanup();
    }
  });

  // The mechanism that keeps a "checking" placeholder from being stranded
  // forever: an install resolution overwrites it outright (rather than
  // trying to preserve it), and bumps the epoch so the check's own eventual
  // result — now guaranteed superseded — is safely dropped instead of
  // silently applying over a state that has moved on.
  it("overwrites (rather than strands) a live 'checking' notice when an install resolves", async () => {
    let resolveManual: (v: unknown) => void = () => {};
    const check = vi
      .fn()
      .mockResolvedValueOnce({ version: "0.2.0", current_version: "0.1.0" })
      .mockImplementationOnce(() => new Promise((resolve) => { resolveManual = resolve; }));
    invokeMock.mockImplementation(mockInvoke(check));
    const { target, cleanup } = render();
    try {
      await vi.waitFor(() => expect(target.textContent).toContain("Herdeck 0.2.0 is available."));

      registeredListener("check-for-updates")!();
      await vi.waitFor(() => expect(check).toHaveBeenCalledTimes(2));
      expect(target.textContent).toContain("Checking for updates");
      // "checking" covers the install button entirely (notice replaces the
      // whole view) — so the resolution below is driven directly through
      // the broadcast listener, the same path installUpdate's own
      // resolution reaches the OTHER window through.
      const resultListener = listenMock.mock.calls.find(([name]) => name === "update-check-result");
      const deliver = resultListener![1] as (ev: { payload: unknown }) => void;
      deliver({ payload: null });
      flushSync();

      expect(target.textContent, "'checking' was left stranded instead of overwritten").toContain(
        "Herdeck is up to date.",
      );

      // The check itself then settles — its own outcome is safely dropped
      // (the epoch already moved on), not applied over the resolved state.
      resolveManual({ version: "0.2.0", current_version: "0.1.0" });
      await new Promise((r) => setTimeout(r, 0));
      expect(target.textContent).toContain("Herdeck is up to date.");
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

// "Up to date" and "failed" notices are dead ends nothing else ever clears —
// left alone they would pin the banner in place (stealing height from the
// content-fit deck window) for the rest of the process's life. The sticky
// "available" update stays until installed or retracted by a confirmed
// up-to-date; it must NOT be swept away on the same clock.
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

  // "checking" is an answer-in-progress, not a dead end — it must survive
  // the same 8s window that sweeps up-to-date/failed, since it always
  // resolves on its own once the check it stands for actually settles (see
  // isDismissableNotice in updateState.ts).
  it("does not clear a 'checking' notice on the same timeout", async () => {
    // The automatic mount check never shows "checking" (only a manual one
    // does — see beginCheck in updateState.ts), so this drives the tray
    // listener instead, same as the other manual-check tests.
    vi.useFakeTimers();
    try {
      const check = vi
        .fn()
        .mockResolvedValueOnce(null) // automatic mount check: up to date, silent
        .mockImplementationOnce(() => new Promise(() => {})); // manual re-check, never settles here
      invokeMock.mockImplementation(mockInvoke(check));
      const { target, cleanup } = render();
      try {
        await vi.advanceTimersByTimeAsync(0);
        expect(target.querySelector(".banner")).toBeNull();

        registeredListener("check-for-updates")!();
        await vi.advanceTimersByTimeAsync(0);
        expect(target.textContent).toContain("Checking for updates");

        await vi.advanceTimersByTimeAsync(8000);
        expect(target.textContent, "a 'checking' notice must not auto-dismiss on a timer")
          .toContain("Checking for updates");
      } finally {
        cleanup();
      }
    } finally {
      vi.useRealTimers();
    }
  });

  // installError outranks both `notice` and `availableUpdate`, and used to
  // be cleared ONLY by installUpdate's own entry (a retry click) — ignored,
  // it would pin a red bar for the rest of the process's life. Same 8s
  // window as the two kinds above, tracked independently of updateState.
  it("clears an install error after a timeout, revealing the update underneath again", async () => {
    vi.useFakeTimers();
    try {
      const check = vi.fn().mockResolvedValue({ version: "0.2.0", current_version: "0.1.0" });
      invokeMock.mockImplementation(async (cmd: string) => {
        if (cmd === "update_install") throw new Error("disk full");
        return mockInvoke(check)(cmd);
      });
      const { target, cleanup } = render();
      try {
        await vi.advanceTimersByTimeAsync(0);
        expect(target.textContent).toContain("Herdeck 0.2.0 is available.");

        target.querySelector("button")!.click();
        await vi.advanceTimersByTimeAsync(0);
        expect(target.textContent).toContain("disk full");

        await vi.advanceTimersByTimeAsync(8000);
        expect(target.textContent, "the install error never cleared").not.toContain("disk full");
        // installError merely masked the available update — it is still
        // there, and shows again once the error is out of the way.
        expect(target.textContent).toContain("Herdeck 0.2.0 is available.");
      } finally {
        cleanup();
      }
    } finally {
      vi.useRealTimers();
    }
  });
});
