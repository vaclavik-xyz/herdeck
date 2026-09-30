// ConfigApp has no general test suite yet — this file is a narrow, growing
// harness, not exhaustive coverage. It mocks the Tauri bridge once and each
// describe block below drives one specific behavior through it:
// - deck_always_on_top: Apply must re-apply it live, the same way it already
//   re-registers the hotkey — see docs/superpowers/plans/2026-07-28-window-roles.md
//   and task-6-report.md.
// - the top bar's deck-toggle control: the app window's own show/hide gesture
//   for the deck, kept in step with the tray and the hotkey via
//   "deck-visibility-changed" — see task-7-report.md.
// - the sidebar version: it must come from package.json, not a literal.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import { setLang } from "./lib/i18n.svelte";
import { setHealthProblems } from "./lib/healthState.svelte";
import { healthProblems } from "./lib/healthStatus";

const { invokeMock, listenMock } = vi.hoisted(() => ({ invokeMock: vi.fn(), listenMock: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ invoke: invokeMock }));
vi.mock("@tauri-apps/api/event", () => ({ listen: listenMock }));

import ConfigApp from "./ConfigApp.svelte";
import pkg from "../package.json" with { type: "json" };

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
  // visibility_in_both_languages in plan_tests.rs) — this is the half of that claim
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

describe("ConfigApp sidebar version", () => {
  // This drifted once: the sidebar hard-coded v0.1.1 while the app shipped 0.2.0,
  // and nothing caught it because scripts/set-version.py (and the parity test that
  // backs it) only knows about the manifests, not a string inside a component.
  // The fix was to stop having a literal at all — vite.config.ts injects
  // __APP_VERSION__ from package.json — and this pins that it stays that way.
  it("shows the version from package.json, not a hard-coded string", () => {
    const { target, cleanup } = renderConfigApp();
    try {
      const shown = target.querySelector(".sidebar-version span")?.textContent?.trim();
      expect(shown).toBe(`v${pkg.version}`);
    } finally {
      cleanup();
    }
  });

  it("renders the injected constant rather than a literal in the source", () => {
    // A literal that merely happens to equal package.json today would pass the
    // test above and drift again at the next release.
    // vitest runs from desktop/, and import.meta.url is not a file: URL under jsdom.
    const source = readFileSync(resolve(process.cwd(), "src/ConfigApp.svelte"), "utf8");
    // Scope this to the markup line itself. A pattern spanning the rest of the
    // file would also fail on an unrelated "v1.2.3" in a help string, a comment
    // or the <style> block, and blame the sidebar for it.
    const line = source
      .split("\n")
      .find((l) => l.includes('class="sidebar-version"'));
    expect(line).toBeDefined();
    expect(line).toContain("__APP_VERSION__");
    expect(line).not.toMatch(/v\d+\.\d+\.\d+/);
  });
});

async function openSection(target: HTMLElement, label: string): Promise<void> {
  Array.from(target.querySelectorAll<HTMLButtonElement>(".sidebar button"))
    .find((b) => b.textContent?.includes(label))!.click();
  flushSync();
  await vi.waitFor(() => expect(target.querySelector(".loading-card")).toBeNull());
}

function clickApply(target: HTMLElement): void {
  Array.from(target.querySelectorAll<HTMLButtonElement>(".savebar button"))
    .find((b) => b.title.startsWith("Save the config"))!.click();
}

describe("ConfigApp Apply outcome banner", () => {
  it("shows the hotkey registration error instead of 'saved'", async () => {
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === "reload_hotkey") throw "accelerator CmdOrCtrl+Shift+D is already taken";
      return mockInvoke(cmd);
    });
    const { target, cleanup } = renderConfigApp();
    try {
      await openSection(target, "Window");
      const checkbox = target.querySelector<HTMLInputElement>(".content input[type='checkbox']")!;
      checkbox.checked = true;
      checkbox.dispatchEvent(new Event("change", { bubbles: true }));
      flushSync();
      clickApply(target);
      await vi.waitFor(() => {
        expect(target.querySelector(".savebar")?.textContent).toContain("already taken");
      });
      expect(target.querySelector(".savebar")?.textContent).not.toMatch(/\bsaved\s*$/);
    } finally {
      cleanup();
    }
  });

  it("asks for a restart when a startup-only key changed", async () => {
    const { target, cleanup } = renderConfigApp();
    try {
      await openSection(target, "Deck");
      const input = Array.from(target.querySelectorAll<HTMLLabelElement>(".content label.field"))
        .find((l) => l.querySelector("[data-config-key='deck']"))!.querySelector("input")!;
      input.value = "web";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      flushSync();
      clickApply(target);
      await vi.waitFor(() => {
        expect(target.querySelector(".savebar")?.textContent).toContain("Restart Herdeck");
      });
      expect(target.querySelector(".savebar")?.textContent).toContain("deck");
    } finally {
      cleanup();
    }
  });
});

describe("ConfigApp status chrome and navigation a11y", () => {
  it("marks the active section with aria-current", () => {
    const { target, cleanup } = renderConfigApp();
    try {
      const current = () => Array.from(target.querySelectorAll<HTMLButtonElement>(".sidebar button[aria-current='page']"));
      expect(current().map((b) => b.textContent?.trim())).toEqual(["Overview"]);
      Array.from(target.querySelectorAll<HTMLButtonElement>(".sidebar button"))
        .find((b) => b.textContent?.includes("Window"))!.click();
      flushSync();
      expect(current().map((b) => b.textContent?.trim())).toEqual(["Window"]);
    } finally {
      cleanup();
    }
  });

  it("shows no remote-servers pill for a local-only setup", async () => {
    const { target, cleanup } = renderConfigApp();
    try {
      await new Promise((r) => setTimeout(r, 0));
      flushSync();
      // The runtime pill is there; the remote one would only read "0/0 · not ready".
      expect(target.querySelector(".topbar .status-pill")).not.toBeNull();
      expect(target.querySelector(".secondary-status")).toBeNull();
    } finally {
      cleanup();
    }
  });

  it("gives the overview's icon-only connections button a title", () => {
    const { target, cleanup } = renderConfigApp();
    try {
      const button = target.querySelector<HTMLButtonElement>(".connection-card .icon-button");
      expect(button?.title).toBe("Open connections");
    } finally {
      cleanup();
    }
  });
});

describe("ConfigApp Maintenance health badge", () => {
  afterEach(() => setHealthProblems([]));

  it("shows the problem count, coloured by the worst, on the Maintenance entry", () => {
    setHealthProblems(healthProblems({
      version: "0.10.0",
      app_version: "0.10.1",
      config_error: "invalid grid",
      servers: { newer: { bridge_version: "0.10.2" } },
    }));
    const { target, cleanup } = renderConfigApp();
    try {
      const entry = Array.from(target.querySelectorAll<HTMLButtonElement>(".sidebar button"))
        .find((b) => b.textContent?.includes("Maintenance"))!;
      const badge = entry.querySelector<HTMLElement>("[data-health-badge]");
      // config error + runtime mismatch; the newer bridge is only info
      expect(badge?.textContent).toBe("2");
      expect(badge?.classList.contains("error")).toBe(true);
      expect(badge?.getAttribute("title")).toBe("2 health problem(s) — see Maintenance");
    } finally {
      cleanup();
    }
  });

  it("shows no badge when nothing is wrong", () => {
    setHealthProblems([]);
    const { target, cleanup } = renderConfigApp();
    try {
      expect(target.querySelector("[data-health-badge]")).toBeNull();
    } finally {
      cleanup();
    }
  });
});

// Bridge shared settings (issue #116, spec S5-S7): the shared sections edit
// the selected TARGET — a bridge's own document (saved with
// POST /bridge-settings/<id> through `config_bridge_settings`) or This Mac's
// config.toml (the fallback, saved exactly like today).
describe("ConfigApp bridge shared settings", () => {
  const BRIDGE_DOC = {
    safety: { approve_always: false, require_confirm_for: ["act_force"] },
    macros: [],
  };

  function sharedBridge(over: Record<string, unknown> = {}) {
    return {
      offered: true, connected: true, revision: 3, updated_at_ms: 1, updated_by: "mac", set: true,
      source: "bridge", settings: BRIDGE_DOC, ...over,
    };
  }

  function sharedConfig(extra: Record<string, unknown> = {}) {
    return {
      ...rawConfig(),
      base: {
        servers: [{ id: "m4", url: "ws://m4:8788", token_env: "T" }, { id: "mb", url: "ws://mb:8788", token_env: "T" }],
        safety: { approve_always: true },
      },
      bridges: { m4: sharedBridge() },
      shared_overlay_ignored: [],
      ...extra,
    };
  }

  function useConfig(raw: () => Record<string, unknown>, put: (serverId: string, body: unknown) => unknown = () => ({ status: 200, body: { ok: true, revision: 4 } })) {
    invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
      if (cmd === "config_read") return raw();
      if (cmd === "config_bridge_settings") return put(args!.serverId as string, args!.body);
      return mockInvoke(cmd);
    });
  }

  async function openSection(target: HTMLElement, label: string): Promise<void> {
    await vi.waitFor(() => expect(invokeMock.mock.calls.some(([cmd]) => cmd === "config_read")).toBe(true));
    const nav = Array.from(target.querySelectorAll<HTMLButtonElement>(".sidebar button"))
      .find((b) => b.textContent?.trim() === label);
    nav!.click();
    flushSync();
    await vi.waitFor(() => expect(target.querySelector(".loading-card")).toBeNull());
  }

  function approveAlways(target: HTMLElement): HTMLInputElement {
    const input = target.querySelector<HTMLElement>(".content [data-config-key='approve_always']")?.parentElement?.querySelector("input");
    if (!(input instanceof HTMLInputElement)) throw new Error("approve_always not rendered");
    return input;
  }

  function toggle(input: HTMLInputElement): void {
    input.checked = !input.checked;
    input.dispatchEvent(new Event("change", { bubbles: true }));
    flushSync();
  }

  function clickApply(target: HTMLElement): void {
    const apply = Array.from(target.querySelectorAll<HTMLButtonElement>(".savebar button"))
      .find((b) => b.title.startsWith("Save the config"));
    expect(apply!.disabled, "Apply must be enabled by a bridge edit").toBe(false);
    apply!.click();
  }

  function pick(target: HTMLElement, value: string): void {
    const select = target.querySelector<HTMLSelectElement>("[data-shared-target-select]")!;
    select.value = value;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    flushSync();
  }

  const bridgeCalls = () => invokeMock.mock.calls.filter(([cmd]) => cmd === "config_bridge_settings");

  it("edits the first adopted bridge by default and Apply saves it there, not in config.toml", async () => {
    useConfig(() => sharedConfig());
    const { target, cleanup } = renderConfigApp();
    try {
      await openSection(target, "Safety");
      expect(target.querySelector<HTMLSelectElement>("[data-shared-target-select]")!.value).toBe("m4");
      expect(approveAlways(target).checked, "shows the bridge's value, not config.toml's").toBe(false);
      toggle(approveAlways(target));
      clickApply(target);
      await vi.waitFor(() => expect(bridgeCalls()).toHaveLength(1));
      expect(bridgeCalls()[0][1]).toEqual({
        serverId: "m4",
        body: { base_revision: 3, settings: { safety: { approve_always: true, require_confirm_for: ["act_force"] }, macros: [] } },
      });
      expect(invokeMock).not.toHaveBeenCalledWith("config_write", expect.anything());
    } finally {
      cleanup();
    }
  });

  it("This Mac (fallback) edits config.toml exactly like today", async () => {
    useConfig(() => sharedConfig());
    const { target, cleanup } = renderConfigApp();
    try {
      await openSection(target, "Safety");
      pick(target, "");
      expect(approveAlways(target).checked).toBe(true);
      toggle(approveAlways(target));
      clickApply(target);
      await vi.waitFor(() => expect(invokeMock).toHaveBeenCalledWith("config_write", expect.anything()));
      const body = invokeMock.mock.calls.find(([cmd]) => cmd === "config_write")![1].body;
      expect(body.base.safety).toEqual({ approve_always: false });
      expect(bridgeCalls()).toHaveLength(0);
    } finally {
      cleanup();
    }
  });

  it("a stale revision (409) reloads that bridge's settings and says so", async () => {
    let reads = 0;
    useConfig(
      () => {
        reads += 1;
        return sharedConfig(reads > 1 ? { bridges: { m4: sharedBridge({ revision: 5 }) } } : {});
      },
      () => ({ status: 409, body: { ok: false, error: "stale_revision", messages: [], revision: 5 } }),
    );
    const { target, cleanup } = renderConfigApp();
    try {
      await openSection(target, "Safety");
      toggle(approveAlways(target));
      clickApply(target);
      await vi.waitFor(() => expect(target.querySelector("[data-shared-results]")).not.toBeNull());
      expect(reads).toBeGreaterThan(1);
      expect(target.querySelector("[data-shared-results]")!.textContent).toContain("m4: changed elsewhere — reloaded");
      expect(approveAlways(target).checked, "the draft is dropped for the reloaded value").toBe(false);
      expect(target.querySelector(".dirty"), "nothing left unsaved").toBeNull();
    } finally {
      cleanup();
    }
  });

  it("Apply to all bridges posts once per adopted bridge with its own revision and lists failures", async () => {
    useConfig(
      () => sharedConfig({ bridges: { m4: sharedBridge(), mb: sharedBridge({ revision: 9 }) } }),
      (id) => (id === "mb"
        ? { status: 503, body: { ok: false, error: "disconnected", messages: ["bridge mb is not connected"] } }
        : { status: 200, body: { ok: true, revision: 4 } }),
    );
    const { target, cleanup } = renderConfigApp();
    try {
      await openSection(target, "Safety");
      const all = target.querySelector<HTMLInputElement>("[data-action='apply-all']")!;
      toggle(all);
      toggle(approveAlways(target));
      clickApply(target);
      await vi.waitFor(() => expect(target.querySelector("[data-shared-results]")).not.toBeNull());
      expect(bridgeCalls().map(([, a]) => [a.serverId, a.body.base_revision])).toEqual([["m4", 3], ["mb", 9]]);
      const rows = Array.from(target.querySelectorAll("[data-shared-results] li")).map((li) => li.textContent?.trim());
      expect(rows).toEqual(["m4: saved", "mb: bridge not connected"]);
    } finally {
      cleanup();
    }
  });

  it("an offline target is read-only with a notice", async () => {
    useConfig(() => sharedConfig({ bridges: { m4: sharedBridge({ connected: false, offered: false, source: "cache" }) } }));
    const { target, cleanup } = renderConfigApp();
    try {
      await openSection(target, "Safety");
      expect(target.querySelector<HTMLSelectElement>("[data-shared-target-select]")!.value, "offline is never the default").toBe("");
      pick(target, "m4");
      expect(target.querySelector("[data-shared-offline]")!.textContent).toContain("Bridge offline — showing last known settings");
      expect(approveAlways(target).checked).toBe(false);
      expect(approveAlways(target).matches(":disabled")).toBe(true);
    } finally {
      cleanup();
    }
  });

  it("hides profile-overlay controls of shared fields on an adopted target and explains why", async () => {
    useConfig(() => sharedConfig({
      active_profile: "night",
      profiles: { night: { safety: { approve_always: true } } },
      shared_overlay_ignored: ["m4"],
    }));
    const { target, cleanup } = renderConfigApp();
    try {
      await openSection(target, "Safety");
      expect(target.querySelector(".content .override"), "no overlay controls on the bridge").toBeNull();
      expect(target.querySelector("[data-overlay-hidden]")).not.toBeNull();
      expect(target.querySelector("[data-overlay-ignored]")).not.toBeNull();
      pick(target, "");
      expect(target.querySelector(".content .override"), "the fallback keeps profile overlays").not.toBeNull();
    } finally {
      cleanup();
    }
  });

  // The bridge answers a put before it broadcasts the new document, so the
  // re-read right after Apply can still carry the old revision.
  it("a save stays visible when the immediate re-read still predates it", async () => {
    useConfig(() => sharedConfig(), () => ({ status: 200, body: { ok: true, revision: 4 } }));
    const { target, cleanup } = renderConfigApp();
    try {
      await openSection(target, "Safety");
      toggle(approveAlways(target));
      clickApply(target);
      await vi.waitFor(() => expect(target.querySelector("[data-shared-results]")).not.toBeNull());
      expect(approveAlways(target).checked, "the saved value, not the stale re-read").toBe(true);
      expect(target.querySelector(".dirty")).toBeNull();
      toggle(approveAlways(target));
      clickApply(target);
      await vi.waitFor(() => expect(bridgeCalls()).toHaveLength(2));
      expect(bridgeCalls()[1][1].body.base_revision, "based on the saved revision").toBe(4);
    } finally {
      cleanup();
    }
  });

  it("adoption moves the SAVED config.toml base, not unsaved edits, and is not offered again", async () => {
    useConfig(
      () => sharedConfig({ bridges: { m4: sharedBridge({ set: false, revision: 0, settings: null, source: "none" }) } }),
      () => ({ status: 200, body: { ok: true, revision: 1 } }),
    );
    const { target, cleanup } = renderConfigApp();
    try {
      await openSection(target, "Safety");
      pick(target, "m4");
      toggle(approveAlways(target)); // unsaved local edit: approve_always false
      target.querySelector<HTMLButtonElement>("[data-action='adopt']")!.click();
      await vi.waitFor(() => expect(bridgeCalls()).toHaveLength(1));
      expect(bridgeCalls()[0][1]).toEqual({ serverId: "m4", body: { base_revision: 0, settings: { safety: { approve_always: true } } } });
      await vi.waitFor(() => expect(target.querySelector("[data-action='adopt']"), "the stale re-read must not bring the button back").toBeNull());
    } finally {
      cleanup();
    }
  });

  // A local Apply reloads the runtime, which reconnects every bridge: right
  // after config_write the bridges read as disconnected for a moment.
  it("a combined Apply puts the bridge before config.toml and recovers the bridge after the reload", async () => {
    let reconnecting = false;
    let readsAfterWrite = 0;
    const order: string[] = [];
    invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
      if (cmd === "config_read") {
        if (reconnecting) {
          readsAfterWrite += 1;
          if (readsAfterWrite >= 3) reconnecting = false;
          return sharedConfig({ bridges: { m4: sharedBridge({ connected: false, offered: false, source: "cache" }) } });
        }
        return sharedConfig();
      }
      if (cmd === "config_bridge_settings") {
        order.push("bridge");
        if (reconnecting) return { status: 503, body: { ok: false, error: "disconnected", messages: [] } };
        return { status: 200, body: { ok: true, revision: 4 } };
      }
      if (cmd === "config_write") {
        order.push("write");
        reconnecting = true;
        return { errors: [] };
      }
      return mockInvoke(cmd);
    });
    const { target, cleanup } = renderConfigApp();
    try {
      await openSection(target, "Safety");
      toggle(approveAlways(target)); // bridge draft
      pick(target, "");
      toggle(approveAlways(target)); // config.toml edit
      pick(target, "m4");
      clickApply(target);
      await vi.waitFor(() => expect(order).toEqual(["bridge", "write"]));
      await vi.waitFor(() => expect(target.querySelector(".dirty")).toBeNull());
      const rows = Array.from(target.querySelectorAll("[data-shared-results] li")).map((li) => li.textContent?.trim());
      expect(rows).toEqual(["m4: saved"]);
      expect(target.querySelector<HTMLSelectElement>("[data-shared-target-select]")!.value).toBe("m4");
      expect(approveAlways(target).checked, "the saved bridge value survives the post-write reload").toBe(true);
      await vi.waitFor(
        () => expect(target.querySelector("[data-shared-offline]"), "the bridge section recovers").toBeNull(),
        { timeout: 5000 },
      );
      expect(approveAlways(target).checked).toBe(true);
    } finally {
      cleanup();
    }
  });

  it("a rejected config.toml save never shows 'saved' after a successful bridge put", async () => {
    invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
      if (cmd === "config_read") return sharedConfig();
      if (cmd === "config_bridge_settings") return { status: 200, body: { ok: true, revision: 4 } };
      if (cmd === "config_write") return { errors: ["safety.approve_always: bad"] };
      return mockInvoke(cmd);
    });
    const { target, cleanup } = renderConfigApp();
    try {
      await openSection(target, "Safety");
      toggle(approveAlways(target)); // bridge draft
      pick(target, "");
      toggle(approveAlways(target)); // config.toml edit
      clickApply(target);
      await vi.waitFor(() => expect(invokeMock).toHaveBeenCalledWith("config_write", expect.anything()));
      await vi.waitFor(() => expect(target.querySelector("[data-shared-results]")).not.toBeNull());
      await new Promise((r) => setTimeout(r, 50));
      expect(target.querySelector(".banner.success"), "config.toml was not saved").toBeNull();
    } finally {
      cleanup();
    }
  });

  it("shows no target picker when no bridge offers shared settings", async () => {
    useConfig(() => sharedConfig({ bridges: { m4: sharedBridge({ offered: false, set: false, settings: null }) } }));
    const { target, cleanup } = renderConfigApp();
    try {
      await openSection(target, "Safety");
      expect(target.querySelector("[data-shared-target]")).toBeNull();
    } finally {
      cleanup();
    }
  });
});

// The Telegram-on-the-bridge section polls the bridges every 3 s while a
// bridge is picked. That poll must be quiet: it updates `bridges` only — no
// reloadRev bump (would reset other sections' drafts every 3 s) and no
// "refresh failed" banner while the runtime is unreachable.
describe("ConfigApp bridge Telegram status poll", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  function telegramBridge() {
    return {
      offered: true, connected: true, revision: 0, set: false, source: "none", settings: null,
      telegram: {
        offered: true, revision: 1, settings: { enabled: false, chat_id: "" },
        status: { token: "file", active: false, inbound: "off", last_error: null, last_sent_at_ms: null, recent_chats: [] },
      },
    };
  }

  it("re-reads the config quietly: no reloadRev bump, no banner", async () => {
    let readFails = false;
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === "config_read") {
        if (readFails) throw new Error("sidecar down");
        return { ...rawConfig(), bridges: { m4: telegramBridge() } };
      }
      return mockInvoke(cmd);
    });
    const { target, cleanup } = renderConfigApp();
    try {
      await vi.waitFor(() => expect(target.querySelector(".loading-card")).toBeNull());
      const nav = Array.from(target.querySelectorAll<HTMLButtonElement>(".sidebar button"))
        .find((b) => b.textContent?.includes("Notifications"));
      nav!.click();
      flushSync();
      await vi.waitFor(() => expect(target.querySelector("select[data-tg-target]")).not.toBeNull());
      vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
      const select = target.querySelector<HTMLSelectElement>("select[data-tg-target]")!;
      select.value = "m4";
      select.dispatchEvent(new Event("change", { bubbles: true }));
      flushSync();
      const rev = target.querySelector<HTMLElement>(".content")!.dataset.reloadRev;
      const reads = () => invokeMock.mock.calls.filter(([c]) => c === "config_read").length;
      const before = reads();
      await vi.advanceTimersByTimeAsync(3_000);
      await vi.waitFor(() => expect(reads()).toBe(before + 1));
      flushSync();
      expect(target.querySelector<HTMLElement>(".content")!.dataset.reloadRev).toBe(rev);
      readFails = true;
      await vi.advanceTimersByTimeAsync(3_000);
      await vi.waitFor(() => expect(reads()).toBe(before + 2));
      flushSync();
      expect(target.textContent).not.toContain("config refresh from the sidecar failed");
      expect(target.querySelector<HTMLElement>(".content")!.dataset.reloadRev).toBe(rev);
    } finally {
      cleanup();
    }
  });
});
