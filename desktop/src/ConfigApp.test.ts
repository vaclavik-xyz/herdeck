// ConfigApp has no general test suite yet — this file is a narrow, growing
// harness, not exhaustive coverage. It mocks the Tauri bridge once and each
// describe block below drives one specific behavior through it:
// - deck_always_on_top: Apply must re-apply it live, the same way it already
//   re-registers the hotkey — see docs/superpowers/plans/2026-07-28-window-roles.md
//   and task-6-report.md.
// - the top bar's deck-toggle control: the app window's own show/hide gesture
//   for the deck, kept in step with the tray and the hotkey via
//   "deck-visibility-changed" — see task-7-report.md.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import { setLang } from "./lib/i18n.svelte";

const { invokeMock, listenMock } = vi.hoisted(() => ({ invokeMock: vi.fn(), listenMock: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ invoke: invokeMock }));
vi.mock("@tauri-apps/api/event", () => ({ listen: listenMock }));

import ConfigApp from "./ConfigApp.svelte";

function rawConfig() {
  return {
    base: { servers: [], desktop: { deck_always_on_top: false } },
    profiles: {},
    local: {},
    secrets: {},
    active_profile: "default",
  };
}

function mockInvoke(cmd: string): unknown {
  switch (cmd) {
    case "get_discovery":
      return { url: "ws://127.0.0.1:1", host: "127.0.0.1", port: 1, source: "test" };
    case "config_read":
      return rawConfig();
    case "config_validate":
    case "config_write":
      return { errors: [] };
    default:
      return null;
  }
}

let target: HTMLElement;

beforeEach(() => {
  setLang("en");
  // browserMode (the read-only design-preview path) is gated on this global's
  // absence — set it so these tests run the real invoke-backed path.
  Object.defineProperty(window, "__TAURI_INTERNALS__", { value: {}, configurable: true });
  invokeMock.mockReset();
  invokeMock.mockImplementation(async (cmd: string) => mockInvoke(cmd));
  listenMock.mockReset();
  listenMock.mockImplementation(() => Promise.resolve(() => {}));
  target = document.createElement("div");
  document.body.appendChild(target);
});

// The handler ConfigApp registered for one event name, so a test can fire it
// directly — simulating the tray, the hotkey, or ⌘W changing the deck WITHOUT
// a click in this window.
function registeredListener(event: string): ((ev: { payload: unknown }) => void) | undefined {
  const call = listenMock.mock.calls.find(([name]) => name === event);
  return call?.[1] as ((ev: { payload: unknown }) => void) | undefined;
}

afterEach(() => {
  delete (window as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
  target.remove();
});

// Shared mount helper — every describe block below drives the same real
// component through the same mocked Tauri bridge set up in beforeEach.
function renderConfigApp(): { target: HTMLElement; cleanup: () => void } {
  const instance = mount(ConfigApp, { target, props: { interactive: true } });
  return { target, cleanup: () => unmount(instance) };
}

describe("ConfigApp Apply re-applies deck_always_on_top", () => {
  it("invokes reload_deck_always_on_top after a successful save, like reload_hotkey", async () => {
    const { target, cleanup } = renderConfigApp();
    try {
      const desktopNav = Array.from(target.querySelectorAll<HTMLButtonElement>(".sidebar button"))
        .find((b) => b.textContent?.includes("Window"));
      expect(desktopNav, "desktop nav item not found").toBeTruthy();
      desktopNav!.click();
      flushSync();

      await vi.waitFor(() => {
        expect(target.querySelector(".loading-card")).toBeNull();
      });

      const checkbox = target.querySelector<HTMLInputElement>(".content input[type='checkbox']");
      expect(checkbox, "deck_always_on_top checkbox not rendered").toBeTruthy();
      checkbox!.checked = true;
      checkbox!.dispatchEvent(new Event("change", { bubbles: true }));
      flushSync();

      const applyButton = Array.from(target.querySelectorAll<HTMLButtonElement>(".savebar button"))
        .find((b) => b.title.startsWith("Save the config"));
      expect(applyButton, "Apply button not found").toBeTruthy();
      applyButton!.click();

      await vi.waitFor(() => {
        expect(invokeMock).toHaveBeenCalledWith("reload_deck_always_on_top");
      });
      // The hotkey field wasn't touched, but Apply re-registers it unconditionally
      // today — confirms this test drives the real Apply path, not a stub of it.
      expect(invokeMock).toHaveBeenCalledWith("reload_hotkey");
    } finally {
      cleanup();
    }
  });
});

// Task 7: the app window's own deck toggle, beside the tray's `toggle_deck`
// item and the CmdOrCtrl+Shift+D hotkey — same command pair, same destination
// window, same labels. It must ALSO follow "deck-visibility-changed" so it
// stays honest when one of those other two paths changed the deck instead.
describe("ConfigApp top bar deck-toggle control", () => {
  it("offers a way to toggle the deck, translated, while the deck is hidden", async () => {
    const { target, cleanup } = renderConfigApp();
    try {
      const button = target.querySelector<HTMLButtonElement>("[data-action='toggle-deck']");
      expect(button, "the app window offers no way to toggle the deck").not.toBeNull();
      expect(button!.title, "icon-only control without a translated title").toBeTruthy();
      await vi.waitFor(() => {
        expect(button!.title).toBe("Show deck");
        expect(button!.textContent).toContain("Show deck");
      });
    } finally {
      cleanup();
    }
  });

  it("invokes show_deck when clicked while the deck is hidden", async () => {
    const { target, cleanup } = renderConfigApp();
    try {
      const button = target.querySelector<HTMLButtonElement>("[data-action='toggle-deck']");
      expect(button).not.toBeNull();
      button!.click();
      flushSync();

      await vi.waitFor(() => {
        expect(invokeMock).toHaveBeenCalledWith("show_deck");
      });
      expect(invokeMock).not.toHaveBeenCalledWith("hide_deck");
    } finally {
      cleanup();
    }
  });

  it("reads the deck's visibility on mount, then shows hide_deck and its label", async () => {
    invokeMock.mockImplementation(async (cmd: string) => (cmd === "deck_visible" ? true : mockInvoke(cmd)));
    const { target, cleanup } = renderConfigApp();
    try {
      await vi.waitFor(() => {
        expect(invokeMock).toHaveBeenCalledWith("deck_visible");
      });
      const button = target.querySelector<HTMLButtonElement>("[data-action='toggle-deck']");
      expect(button, "the app window offers no way to toggle the deck").not.toBeNull();
      await vi.waitFor(() => {
        expect(button!.title).toBe("Hide deck");
        expect(button!.textContent).toContain("Hide deck");
      });

      button!.click();
      flushSync();
      await vi.waitFor(() => {
        expect(invokeMock).toHaveBeenCalledWith("hide_deck");
      });
      expect(invokeMock).not.toHaveBeenCalledWith("show_deck");
    } finally {
      cleanup();
    }
  });

  // Tauri gives no ordering guarantee between a command reply and an event on
  // the same channel: the mount-time `deck_visible` snapshot can resolve
  // AFTER a real-time "deck-visibility-changed" already landed. A stale
  // snapshot winning that race would silently revert a true toggle.
  it("keeps a real-time event's value over a mount-time snapshot that resolves later", async () => {
    let resolveSnapshot: (v: boolean) => void = () => {};
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === "deck_visible") return new Promise<boolean>((resolve) => { resolveSnapshot = resolve; });
      return mockInvoke(cmd);
    });
    const { target, cleanup } = renderConfigApp();
    try {
      await vi.waitFor(() => {
        expect(invokeMock, "the snapshot was never requested").toHaveBeenCalledWith("deck_visible");
      });
      const button = target.querySelector<HTMLButtonElement>("[data-action='toggle-deck']");

      // The event lands WHILE the snapshot request is still in flight.
      registeredListener("deck-visibility-changed")!({ payload: true });
      flushSync();
      expect(button!.title).toBe("Hide deck");

      // The snapshot now resolves late, carrying the stale pre-event value.
      // A macrotask tick (not just a microtask or two) flushes every hop of
      // the mocked invoke's own `async` wrapping plus the component's chain.
      resolveSnapshot(false);
      await new Promise((r) => setTimeout(r, 0));
      flushSync();

      expect(button!.title, "a stale snapshot overwrote a real-time event").toBe("Hide deck");
    } finally {
      cleanup();
    }
  });

  // The one that matters: the tray, the hotkey, or ⌘W can change the deck's
  // visibility WITHOUT this window's button ever being clicked. The button
  // must still tell the truth — via the event, not a click.
  it("flips label and command to match, without a click, when deck-visibility-changed fires", async () => {
    const { target, cleanup } = renderConfigApp();
    try {
      await vi.waitFor(() => {
        expect(registeredListener("deck-visibility-changed"), "no deck-visibility-changed listener registered")
          .toBeTruthy();
      });
      const button = target.querySelector<HTMLButtonElement>("[data-action='toggle-deck']");
      await vi.waitFor(() => expect(button!.title).toBe("Show deck"));

      registeredListener("deck-visibility-changed")!({ payload: true });
      flushSync();

      expect(button!.title).toBe("Hide deck");
      expect(button!.textContent).toContain("Hide deck");
      // No click happened — the flip came from the event alone.
      expect(invokeMock).not.toHaveBeenCalledWith("hide_deck");
      expect(invokeMock).not.toHaveBeenCalledWith("show_deck");

      registeredListener("deck-visibility-changed")!({ payload: false });
      flushSync();
      expect(button!.title).toBe("Show deck");
    } finally {
      cleanup();
    }
  });

  // The commit claims the labels are byte-identical to the tray's
  // `toggle_deck_label` in BOTH languages (see toggle_deck_label_reflects_-
  // visibility_in_both_languages in lib.rs) — this is the half of that claim
  // an English-only assertion can't catch.
  it("flips to the Czech labels, translated, on the same event-driven flip", async () => {
    // The editor's effective language follows the loaded config's
    // [view].language (see the `setLang(langOf(effectiveLanguage(payload)))`
    // effect), which overrides any language set before mount — so the
    // config, not `setLang`, has to say "cs" for it to stick.
    invokeMock.mockImplementation(async (cmd: string) =>
      cmd === "config_read" ? { ...rawConfig(), base: { ...rawConfig().base, view: { language: "cs" } } } : mockInvoke(cmd),
    );
    const { target, cleanup } = renderConfigApp();
    try {
      await vi.waitFor(() => {
        expect(registeredListener("deck-visibility-changed"), "no deck-visibility-changed listener registered")
          .toBeTruthy();
      });
      const button = target.querySelector<HTMLButtonElement>("[data-action='toggle-deck']");
      await vi.waitFor(() => expect(button!.title).toBe("Zobrazit deck"));
      expect(button!.textContent).toContain("Zobrazit deck");

      registeredListener("deck-visibility-changed")!({ payload: true });
      flushSync();

      expect(button!.title).toBe("Skrýt deck");
      expect(button!.textContent).toContain("Skrýt deck");
    } finally {
      cleanup();
    }
  });
});
