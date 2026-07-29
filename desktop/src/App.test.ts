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

      // Written against the CURRENT clear shape ({resolvedAway: version}),
      // not the old bare `null` — asserting against a shape nothing emits
      // any more would pass vacuously even if a clear leaked out here.
      expect(emitMock).not.toHaveBeenCalledWith(
        "update-check-result",
        expect.objectContaining({ resolvedAway: expect.anything() }),
      );
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

  // The mirror case: a check straddling the resolution reports the SAME
  // version install just resolved away. That is not fresh news — it is
  // exactly the stale banner the resolution retracted, and reshowing it
  // would put the install button back for a click that only resolves
  // `false` again.
  it("does not resurrect the SAME version an install just resolved away", async () => {
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

      // The in-flight re-check resolves with the SAME version install just
      // resolved away — stale, not fresh.
      resolveManual({ version: "0.2.0", current_version: "0.1.0" });
      await new Promise((r) => setTimeout(r, 0));

      expect(
        target.querySelector(".banner"),
        "resurrected the same version install already resolved away",
      ).toBeNull();
    } finally {
      cleanup();
    }
  });

  // The reverse ordering: installUpdate captures the version it is actually
  // targeting BEFORE awaiting `updater.install()` — an awaited
  // download-and-verify that can take a while. If a genuinely NEWER update
  // lands (from a check settling) WHILE that install is still pending, the
  // eventual resolution must retract only the version it was actually asked
  // about, not whatever happens to be live once it finally settles.
  it("does not clear a newer update that lands while install() is still resolving an older one", async () => {
    let resolveInstall: (v: unknown) => void = () => {};
    const check = vi
      .fn()
      .mockResolvedValueOnce({ version: "0.2.0", current_version: "0.1.0" }) // automatic mount check
      .mockResolvedValueOnce({ version: "0.3.0", current_version: "0.1.0" }); // manual re-check
    const install = vi.fn().mockImplementationOnce(() => new Promise((resolve) => { resolveInstall = resolve; }));
    const baseInvoke = mockInvoke(check);
    invokeMock.mockImplementation(async (cmd: string) =>
      cmd === "update_install" ? install() : baseInvoke(cmd),
    );
    const { target, cleanup } = render();
    try {
      await vi.waitFor(() => {
        expect(target.textContent).toContain("Herdeck 0.2.0 is available.");
      });

      // Click install — its promise is left pending (install is "in flight").
      const installButton = target.querySelector("button");
      installButton!.click();
      await vi.waitFor(() => expect(install).toHaveBeenCalledTimes(1));

      // WHILE install() is still pending, a manual re-check settles with a
      // genuinely newer available update.
      registeredListener("check-for-updates")!();
      await vi.waitFor(() => {
        expect(target.textContent).toContain("Herdeck 0.3.0 is available.");
      });

      // NOW install() finally resolves false — for the 0.2.0 it was actually
      // asked about, not the 0.3.0 that has since appeared.
      resolveInstall(false);
      await new Promise((r) => setTimeout(r, 0));

      expect(
        target.textContent,
        "a newer update that landed mid-install was wrongly cleared away with the old one",
      ).toContain("Herdeck 0.3.0 is available.");
    } finally {
      cleanup();
    }
  });

  // Pins WHICH version gets recorded and blacklisted, not just that survival
  // happens to work out: two checks are in flight when the install resolves
  // (0.2.0) — one later reports that SAME version (must be rejected as
  // stale), the other a genuinely different one (must be accepted). If
  // recordResolvedAway ever recorded the wrong version (e.g. the live one at
  // resolution time instead of the captured target), one half of this would
  // flip silently while the other still passed.
  it("rejects a straddling check reporting the resolved version, accepts one reporting a different one", async () => {
    let resolveB: (v: unknown) => void = () => {};
    let resolveC: (v: unknown) => void = () => {};
    const check = vi
      .fn()
      .mockResolvedValueOnce({ version: "0.2.0", current_version: "0.1.0" }) // automatic mount check
      .mockImplementationOnce(() => new Promise((resolve) => { resolveB = resolve; })) // manual check B
      .mockImplementationOnce(() => new Promise((resolve) => { resolveC = resolve; })); // manual check C
    const baseInvoke = mockInvoke(check);
    invokeMock.mockImplementation(async (cmd: string) =>
      cmd === "update_install" ? false : baseInvoke(cmd),
    );
    const { target, cleanup } = render();
    try {
      await vi.waitFor(() => {
        expect(target.textContent).toContain("Herdeck 0.2.0 is available.");
      });

      // Both checks start (and are left in flight) before the install below.
      registeredListener("check-for-updates")!();
      await vi.waitFor(() => expect(check).toHaveBeenCalledTimes(2));
      registeredListener("check-for-updates")!();
      await vi.waitFor(() => expect(check).toHaveBeenCalledTimes(3));

      const installButton = target.querySelector("button");
      installButton!.click();
      await vi.waitFor(() => expect(target.querySelector(".banner")).toBeNull());

      // Check B resolves with the SAME version install just resolved away.
      resolveB({ version: "0.2.0", current_version: "0.1.0" });
      await new Promise((r) => setTimeout(r, 0));
      expect(target.querySelector(".banner"), "the resolved version was resurrected").toBeNull();

      // Check C resolves with a genuinely DIFFERENT version — real news,
      // unaffected by the earlier resolution's blacklist.
      resolveC({ version: "0.3.0", current_version: "0.1.0" });
      await vi.waitFor(() => {
        expect(target.textContent).toContain("Herdeck 0.3.0 is available.");
      });
    } finally {
      cleanup();
    }
  });

  // Two SEPARATE resolutions in sequence: if the retracted version were kept
  // in a single slot rather than an accumulating set, the second install
  // would overwrite the first's record, un-protecting it — a check that
  // straddled only the FIRST resolution would then slip through once the
  // slot no longer names what it retracted.
  it("keeps both versions blacklisted after two sequential install resolutions", async () => {
    let resolveStraddlerX: (v: unknown) => void = () => {};
    const check = vi
      .fn()
      .mockResolvedValueOnce({ version: "0.2.0", current_version: "0.1.0" }) // automatic mount check
      .mockImplementationOnce(() => new Promise((resolve) => { resolveStraddlerX = resolve; })) // check X
      .mockResolvedValueOnce({ version: "0.3.0", current_version: "0.1.0" }); // finds the newer update
    const baseInvoke = mockInvoke(check);
    invokeMock.mockImplementation(async (cmd: string) =>
      cmd === "update_install" ? false : baseInvoke(cmd),
    );
    const { target, cleanup } = render();
    try {
      await vi.waitFor(() => {
        expect(target.textContent).toContain("Herdeck 0.2.0 is available.");
      });

      // Check X starts and is left in flight through everything below.
      registeredListener("check-for-updates")!();
      await vi.waitFor(() => expect(check).toHaveBeenCalledTimes(2));

      // First install: resolves 0.2.0 away and records it.
      target.querySelector("button")!.click();
      await vi.waitFor(() => expect(target.querySelector(".banner")).toBeNull());

      // A fresh check (its own generation matches, so it is not superseded)
      // finds a genuinely newer update.
      registeredListener("check-for-updates")!();
      await vi.waitFor(() => {
        expect(target.textContent).toContain("Herdeck 0.3.0 is available.");
      });

      // Second install: resolves 0.3.0 away too, and must record IT
      // WITHOUT losing the earlier record of 0.2.0.
      target.querySelector("button")!.click();
      await vi.waitFor(() => expect(target.querySelector(".banner")).toBeNull());

      // Check X (in flight since before EITHER install) finally settles
      // with the FIRST resolved version — must still be rejected.
      resolveStraddlerX({ version: "0.2.0", current_version: "0.1.0" });
      await new Promise((r) => setTimeout(r, 0));

      expect(
        target.querySelector(".banner"),
        "the second install's resolution overwrote the first's record, un-protecting 0.2.0",
      ).toBeNull();
    } finally {
      cleanup();
    }
  });

  // installError offers the same retry action regardless of `state` (see
  // UpdateBanner), so installUpdate can run with NO live "available" state
  // at all — reached here through real UI interaction across both windows:
  // the app's install THROWS (installError set, its OWN state untouched),
  // then the DECK installs instead (resolves false, broadcasting the
  // resolution), which clears the APP's state to null while its error
  // stays. The app's error banner still offers a retry — clicked with no
  // live target, recordResolvedAway must not let that wipe the version the
  // deck's resolution already recorded.
  it("does not let a retry with no live target erase a previously recorded resolved-away version", async () => {
    // A check that starts NOW and is left in flight through everything
    // below — its `startGeneration` is captured before any resolution
    // happens, which is what makes it "straddle" them once it finally
    // settles (a check that started fresh AFTER the fact wouldn't: its own
    // generation would already match, bypassing the staleness check
    // entirely — this is the one shape that actually exercises it).
    let resolveStraddler: (v: unknown) => void = () => {};
    const check = vi
      .fn()
      .mockResolvedValueOnce({ version: "0.2.0", current_version: "0.1.0" }) // automatic mount check
      .mockImplementationOnce(() => new Promise((resolve) => { resolveStraddler = resolve; }));
    const baseInvoke = mockInvoke(check);
    let appInstallThrows = true;
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === "update_install") {
        if (appInstallThrows) {
          appInstallThrows = false;
          throw new Error("network error");
        }
        return false;
      }
      return baseInvoke(cmd);
    });
    const deck = renderWithRole("deck");
    try {
      const app = render();
      try {
        await vi.waitFor(() => expect(app.target.textContent).toContain("Herdeck 0.2.0 is available."));
        await vi.waitFor(() => expect(deck.target.textContent).toContain("Herdeck 0.2.0 is available."));

        registeredListener("check-for-updates")!();
        await vi.waitFor(() => expect(check).toHaveBeenCalledTimes(2));

        // The app's install throws: installError is set, but its OWN
        // updateState is untouched by a throw — still "available 0.2.0".
        app.target.querySelector("button")!.click();
        await vi.waitFor(() => expect(app.target.textContent).toContain("network error"));

        // The deck installs instead — resolves false, targeting the deck's
        // own live "0.2.0", and broadcasts that resolution to the app too.
        // Waiting for the DECK's own banner to clear (not the app's, which
        // still shows only the error regardless) confirms its installUpdate
        // — including the emit — ran to completion.
        deck.target.querySelector("button")!.click();
        await vi.waitFor(() => expect(deck.target.querySelector(".banner")).toBeNull());
        // The app still shows ONLY the error (it outranks state) —
        // updateState was cleared by the deck's broadcast, but installError
        // never was.
        expect(app.target.textContent).toContain("network error");

        // The app's error banner still offers the SAME retry action —
        // clicked with no live "available" state (targetVersion is null).
        const retryButton = app.target.querySelector("button");
        expect(retryButton?.textContent).toBe("Install and restart");
        retryButton!.click();
        await vi.waitFor(() => expect(app.target.textContent).toContain("Herdeck is up to date."));

        // NOW the straddling check (in flight since before either
        // resolution) settles with that SAME version — must still be
        // rejected, proving the no-target retry did not wipe the "0.2.0"
        // the deck's install had already recorded.
        resolveStraddler({ version: "0.2.0", current_version: "0.1.0" });
        await new Promise((r) => setTimeout(r, 0));

        expect(
          app.target.textContent,
          "a no-target retry erased the recorded version, letting a stale check resurrect it",
        ).toContain("Herdeck is up to date.");
        expect(app.target.textContent).not.toContain("is available");
      } finally {
        app.cleanup();
      }
    } finally {
      deck.cleanup();
    }
  });

  // A live "available" banner already IS the answer to "is there something
  // to install" — a no-target retry's "up to date" fallback must not
  // clobber one that appeared WHILE that retry's install() was still in
  // flight (e.g. a genuinely newer update arriving from the other window,
  // simulated here by driving the "update-check-result" listener directly).
  it("does not let a no-target retry's 'up to date' fallback clobber a newer update that lands mid-retry", async () => {
    const check = vi.fn().mockResolvedValueOnce({ version: "0.2.0", current_version: "0.1.0" });
    let resolveRetryInstall: (v: unknown) => void = () => {};
    const install = vi
      .fn()
      .mockImplementationOnce(async () => {
        throw new Error("network error");
      })
      .mockImplementationOnce(() => new Promise((resolve) => { resolveRetryInstall = resolve; }));
    const baseInvoke = mockInvoke(check);
    invokeMock.mockImplementation(async (cmd: string) => (cmd === "update_install" ? install() : baseInvoke(cmd)));
    const { target, cleanup } = render();
    try {
      await vi.waitFor(() => {
        expect(target.textContent).toContain("Herdeck 0.2.0 is available.");
      });

      // Install throws: installError is set, but the throw itself never
      // touches updateState — still "available 0.2.0".
      target.querySelector("button")!.click();
      await vi.waitFor(() => expect(target.textContent).toContain("network error"));

      // Simulate "resolved elsewhere" (the other window) clearing THIS
      // window's state to null while installError stays — driving the
      // listener directly, the same way the malformed-payload test does,
      // rather than mounting a second window just to reach this one step.
      const resultListener = listenMock.mock.calls.find(([name]) => name === "update-check-result");
      const deliver = resultListener![1] as (ev: { payload: unknown }) => void;
      deliver({ payload: { resolvedAway: "0.2.0" } });
      flushSync();
      expect(target.querySelector(".banner")?.textContent).toContain("network error");

      // Retry with no live target — its install() is left pending.
      target.querySelector("button")!.click();
      await vi.waitFor(() => expect(install).toHaveBeenCalledTimes(2));

      // WHILE that retry is still pending, a genuinely newer update lands
      // (the other window's own check, say).
      deliver({
        payload: { kind: "available", info: { version: "0.3.0", current_version: "0.1.0" } },
      });
      flushSync();
      await vi.waitFor(() => expect(target.textContent).toContain("Herdeck 0.3.0 is available."));

      // NOW the retry's install() finally resolves false — it targeted
      // nothing (targetVersion was null), and must not answer "up to date"
      // over an update that is genuinely still there.
      resolveRetryInstall(false);
      await new Promise((r) => setTimeout(r, 0));

      expect(
        target.textContent,
        "the no-target retry's fallback clobbered a newer update that landed mid-retry",
      ).toContain("Herdeck 0.3.0 is available.");
    } finally {
      cleanup();
    }
  });

  // installUpdate's resolution can happen in EITHER window (both render the
  // install button) — the generation guard is per-window state, so it must
  // be kept in sync through the SAME "update-check-result" broadcast an
  // available update travels over, or a check running in the window that
  // never installed anything is blind to what the other one just resolved.
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
        const deckInstallButton = deck.target.querySelector("button");
        deckInstallButton!.click();
        await vi.waitFor(() => expect(app.target.querySelector(".banner")).toBeNull());
        await vi.waitFor(() => expect(deck.target.querySelector(".banner")).toBeNull());

        // The app window's in-flight check (started before the DECK's
        // install resolved things) now settles with the same stale version.
        resolveManual({ version: "0.2.0", current_version: "0.1.0" });
        await new Promise((r) => setTimeout(r, 0));

        expect(
          app.target.querySelector(".banner"),
          "the app window resurrected what the deck's install cleared",
        ).toBeNull();
        expect(
          deck.target.querySelector(".banner"),
          "the deck window resurrected what its own install cleared",
        ).toBeNull();
      } finally {
        app.cleanup();
      }
    } finally {
      deck.cleanup();
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

  // installError outranks every updateState kind and used to be cleared
  // ONLY by installUpdate's own entry (a retry click) — ignored, it would
  // pin a red bar for the rest of the process's life. Same 8s window as
  // the two kinds above, tracked independently of updateState.
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
