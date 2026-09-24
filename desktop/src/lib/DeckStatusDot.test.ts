import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { flushSync, mount, tick, unmount } from "svelte";
import DeckStatusDot from "./DeckStatusDot.svelte";
import { setLang } from "./i18n.svelte";
import { DISMISSALS_KEY } from "./noticeDismissals";

let target: HTMLElement;
let cleanup: (() => void) | null = null;

beforeEach(() => {
  target = document.createElement("div");
  document.body.appendChild(target);
});
afterEach(() => {
  cleanup?.();
  cleanup = null;
  target.remove();
  localStorage.clear();
  setLang("en");
});

async function render(health: unknown, props: Record<string, unknown> = {}) {
  const instance = mount(DeckStatusDot, { target, props: { fetchHealth: async () => health, intervalMs: 600_000, ...props } });
  cleanup = () => unmount(instance);
  for (let i = 0; i < 8; i += 1) await tick();
  flushSync();
  return target.querySelector<HTMLButtonElement>("button.dot");
}

describe("DeckStatusDot", () => {
  it("shows nothing while the deck is fine", async () => {
    expect(await render({ version: "0.10.1", app_version: "0.10.1" })).toBeNull();
  });

  it("takes the worst severity and names the top problem in its tooltip", async () => {
    const dot = await render({
      version: "0.10.0",
      app_version: "0.10.1",
      servers: { box: { connected: false, since: 0, last_error: "token rejected" } },
    });
    expect(dot?.dataset.severity).toBe("error");
    expect(dot?.getAttribute("title")).toMatch(/^Bridge box rejected the token \(\d+ d\) \(\+1 more\)$/);
    // No notice rows in the deck window.
    expect(target.querySelector("[data-notice]")).toBeNull();
  });

  it("is a warning dot for a warning, info for a new app version alone", async () => {
    expect((await render({ d200: { lock_owner: 9 } }))?.dataset.severity).toBe("warning");
    cleanup?.();
    const dot = await render({}, { appUpdate: { version: "0.10.2", current_version: "0.10.1" } });
    expect(dot?.dataset.severity).toBe("info");
    expect(dot?.getAttribute("title")).toBe("Herdeck 0.10.2 is available");
  });

  it("opens Maintenance in the app window on click (Czech tooltip)", async () => {
    setLang("cs");
    const cmds: string[] = [];
    const dot = await render({ version: "0.10.0", app_version: "0.10.1" }, {
      invoke: async (cmd: string) => { cmds.push(cmd); return null; },
    });
    expect(dot?.getAttribute("title")).toBe("Runtime 0.10.0 se liší od aplikace 0.10.1 — restartuj runtime");
    dot!.click();
    expect(cmds).toEqual(["open_maintenance"]);
  });

  it("ignores problems dismissed in the app window", async () => {
    localStorage.setItem(DISMISSALS_KEY, JSON.stringify({ runtime_mismatch: "0.10.0|0.10.1" }));
    expect(await render({ version: "0.10.0", app_version: "0.10.1" })).toBeNull();
  });
});
