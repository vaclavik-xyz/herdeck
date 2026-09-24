import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { flushSync, mount, tick, unmount } from "svelte";
import NoticeList from "./NoticeList.svelte";
import Toasts from "./Toasts.svelte";
import { healthState } from "./healthState.svelte";
import { setLang } from "./i18n.svelte";
import { DISMISSALS_KEY } from "./noticeDismissals";
import { clearToasts, toasts } from "./toastStore.svelte";
import { rawStatus } from "./maintenanceFixture";

type Invoke = (cmd: string, args?: Record<string, unknown>) => Promise<unknown>;

let target: HTMLElement;
let cleanups: (() => void)[] = [];

beforeEach(() => {
  target = document.createElement("div");
  document.body.appendChild(target);
});
afterEach(() => {
  for (const c of cleanups.splice(0)) c();
  target.remove();
  clearToasts();
  localStorage.clear();
  setLang("en");
});

async function settle(): Promise<void> {
  for (let i = 0; i < 12; i += 1) await tick();
  flushSync();
}

function mountList(props: Record<string, unknown>) {
  const instance = mount(NoticeList, { target, props: { intervalMs: 600_000, ...props } });
  const toastHost = mount(Toasts, { target, props: {} });
  cleanups.push(() => { unmount(instance); unmount(toastHost); });
}

const rows = () => Array.from(target.querySelectorAll<HTMLElement>("[data-notice]"));
const since = (msAgo: number) => Date.now() - msAgo;

describe("NoticeList rows", () => {
  it("renders one compact row per problem, worst severity first, with a human sentence", async () => {
    mountList({
      fetchHealth: async () => ({
        version: "0.10.1",
        app_version: "0.10.1",
        servers: {
          local: { connected: false, since: since(3 * 60_000), ever_connected: true, last_error: "connection refused" },
          box: { connected: false, since: since(60_000), last_error: "token rejected (close 4401)" },
        },
      }),
    });
    await settle();
    expect(rows().map((r) => r.dataset.severity)).toEqual(["error", "warning"]);
    expect(rows()[0].querySelector(".text")?.textContent).toBe("Bridge box rejected the token (1 min)");
    // Raw backend text lives in title=, not in the sentence.
    expect(rows()[0].querySelector(".text")?.getAttribute("title")).toBe("token rejected (close 4401)");
    expect(rows()[1].querySelector(".text")?.textContent).toBe("Bridge local is disconnected (3 min)");
  });

  it("speaks Czech when [view].language is cs", async () => {
    setLang("cs");
    mountList({ fetchHealth: async () => ({ servers: { local: { connected: false, since: since(3 * 60_000), ever_connected: true } } }) });
    await settle();
    expect(rows()[0].querySelector(".text")?.textContent).toBe("Bridge local je odpojený (3 min)");
    expect(rows()[0].querySelector("[data-details]")?.textContent).toBe("Podrobnosti");
    expect(rows()[0].querySelector("[data-dismiss]")?.getAttribute("title")).toBe("Skrýt, dokud se to nezmění");
  });

  it("stays silent about a server that never connected", async () => {
    mountList({ fetchHealth: async () => ({ servers: { "t3-headless": { connected: false, ever_connected: false, since: 0 } } }) });
    await settle();
    expect(rows()).toEqual([]);
    expect(healthState.problems).toEqual([]);
  });

  it("dismisses a problem until its content changes, and remembers it across mounts", async () => {
    let payload: unknown = { servers: { local: { connected: false, since: 1000, ever_connected: true } } };
    mountList({ fetchHealth: async () => payload });
    await settle();
    rows()[0].querySelector<HTMLButtonElement>("[data-dismiss]")!.click();
    flushSync();
    expect(rows()).toEqual([]);
    expect(JSON.parse(localStorage.getItem(DISMISSALS_KEY) ?? "{}")).toEqual({ "bridge_link:local": "down|1000" });

    // A fresh window (restart) still hides the same outage.
    for (const c of cleanups.splice(0)) c();
    mountList({ fetchHealth: async () => payload });
    await settle();
    expect(rows()).toEqual([]);

    // A NEW outage (another `since`) is different content: it shows again.
    payload = { servers: { local: { connected: false, since: 2000, ever_connected: true } } };
    for (const c of cleanups.splice(0)) c();
    mountList({ fetchHealth: async () => payload });
    await settle();
    expect(rows().map((r) => r.dataset.notice)).toEqual(["bridge_link:local"]);
  });

  it("publishes the problems for the Maintenance badge, dismissed ones included", async () => {
    mountList({
      fetchHealth: async () => ({
        version: "0.10.0",
        app_version: "0.10.1",
        config_error: "invalid grid",
      }),
    });
    await settle();
    rows()[0].querySelector<HTMLButtonElement>("[data-dismiss]")!.click();
    flushSync();
    expect(rows()).toHaveLength(1);
    expect(healthState.problems.map((p) => p.kind)).toEqual(["config_error", "runtime_mismatch"]);
  });

  it("shows a new app version as an info row with Install, release notes and Later", async () => {
    let installs = 0;
    let later = 0;
    mountList({
      fetchHealth: async () => ({ servers: { local: { connected: false, since: 0, ever_connected: true } } }),
      appUpdate: { version: "0.10.2", current_version: "0.10.1" },
      onInstall: () => { installs += 1; },
      onLater: () => { later += 1; },
    });
    await settle();
    expect(rows().map((r) => r.dataset.notice)).toEqual(["bridge_link:local", "app_update"]);
    const row = rows()[1];
    expect(row.dataset.severity).toBe("info");
    expect(row.querySelector(".text")?.textContent).toBe("Herdeck 0.10.2 is available");
    expect(row.querySelector("a")?.getAttribute("href")).toContain("v0.10.2");
    row.querySelector<HTMLButtonElement>('[data-action="install_update"]')!.click();
    row.querySelector<HTMLButtonElement>('[aria-label="Later"]')!.click();
    expect([installs, later]).toEqual([1, 1]);
  });
});

describe("NoticeList actions", () => {
  it("opens Settings → Maintenance from Details and the right section from Fix config…", async () => {
    const opened: string[] = [];
    mountList({
      fetchHealth: async () => ({ config_error: "bridge token for server 'local' not found" }),
      invoke: async () => null,
      onOpenSection: (s: string) => opened.push(s),
    });
    await settle();
    const row = rows()[0];
    expect(row.querySelector('[data-action="fix_config"]')?.textContent?.trim()).toBe("Fix config…");
    row.querySelector<HTMLButtonElement>('[data-action="fix_config"]')!.click();
    row.querySelector<HTMLButtonElement>("[data-details]")!.click();
    expect(opened).toEqual(["servers", "maintenance"]);
  });

  it("asks the shell to open Maintenance when it is not in the settings window", async () => {
    const cmds: string[] = [];
    mountList({ fetchHealth: async () => ({ d200: { lock_owner: 5 } }), invoke: async (cmd: string) => { cmds.push(cmd); return null; } });
    await settle();
    rows()[0].querySelector<HTMLButtonElement>("[data-details]")!.click();
    await settle();
    expect(cmds).toContain("open_maintenance");
  });

  it("runs Update bridge with a stepper toast, then the success sentence", async () => {
    const steps: string[][] = [];
    let polls = 0;
    const invoke: Invoke = async (_cmd, args) => {
      if (args?.method === "POST") {
        return { status: 200, body: { ok: true, code: "pending", message: "running", target: "0.10.1", progress: [{ seq: 1, stage: "download", message: "wheel" }], next: 1 } };
      }
      polls += 1;
      if (polls === 1) {
        return { status: 200, body: { ok: true, code: "pending", message: "running", target: "0.10.1", progress: [{ seq: 2, stage: "install", message: "pip" }], next: 2 } };
      }
      return { status: 200, body: { ok: true, code: "updated", message: "updated to 0.10.1; restarting", target: "0.10.1", progress: [{ seq: 3, stage: "verify", message: "0.10.1" }], next: 3 } };
    };
    mountList({
      fetchHealth: async () => ({ version: "0.10.1", servers: { local: { connected: true, bridge_version: "0.10.0", self_update: true, managed: true } } }),
      invoke,
    });
    await settle();
    const button = rows()[0].querySelector<HTMLButtonElement>('[data-action="update:local"]')!;
    expect(button.textContent?.trim()).toBe("Update bridge");
    // Record every intermediate stepper state the toast shows.
    const seen = new Set<string>();
    const observer = new MutationObserver(() => {
      const t = target.querySelector('[data-toast="bridge-update:local"]');
      if (!t) return;
      const s = Array.from(t.querySelectorAll<HTMLElement>("[data-step]")).map((li) => `${li.dataset.step}:${li.dataset.state}`);
      const key = s.join(",");
      if (!seen.has(key)) { seen.add(key); steps.push(s); }
    });
    observer.observe(target, { subtree: true, childList: true, attributes: true, characterData: true });
    button.click();
    await settle();
    observer.disconnect();
    const toast = target.querySelector<HTMLElement>('[data-toast="bridge-update:local"]')!;
    expect(toast.dataset.kind).toBe("success");
    expect(toast.querySelector(".text")?.textContent).toBe("Bridge local is updated to 0.10.1");
    expect(Array.from(toast.querySelectorAll<HTMLElement>("[data-step]")).map((li) => li.dataset.state))
      .toEqual(["done", "done", "done", "done"]);
    expect(steps).toContainEqual(["download:done", "install:active", "verify:pending", "restart:pending"]);
  });

  it("runs Restart deck and reports the outcome as a toast (Czech)", async () => {
    setLang("cs");
    const invoke: Invoke = async () => ({ status: 200, body: { ok: true, outcome: "reopened" } });
    mountList({ fetchHealth: async () => ({ d200: { lock_owner: 5 } }), invoke });
    await settle();
    const button = rows()[0].querySelector<HTMLButtonElement>('[data-action="restart_deck"]')!;
    expect(button.textContent?.trim()).toBe("Restartovat deck");
    button.click();
    await settle();
    expect(toasts.items.map((t) => [t.kind, t.text])).toEqual([["success", "Deck byl znovu otevřen a překreslen."]]);
  });

  it("offers Restart runtime only when the runtime is this app's own service", async () => {
    const health = { version: "0.10.0", app_version: "0.10.1" };
    const ours = rawStatus(); // a service unit running this app's bundled runtime
    const checkout = rawStatus({
      service: { installed: true, label: "x", unit_path: "/u", program: "/Users/me/src/herdeck/.venv/bin/herdeck-deckapp", from_app: false },
    });
    const calls: string[] = [];
    const invoke = (status: unknown): Invoke => async (cmd) => {
      calls.push(cmd);
      if (cmd === "maintenance_call") return { status: 200, body: status };
      if (cmd === "runtime_service") return { ok: true, exit_code: 0 };
      return null;
    };
    mountList({ fetchHealth: async () => health, invoke: invoke(checkout) });
    await settle();
    expect(rows()[0].querySelector('[data-action="restart_runtime"]')).toBeNull();
    for (const c of cleanups.splice(0)) c();

    mountList({ fetchHealth: async () => health, invoke: invoke(ours) });
    await settle();
    const button = rows()[0].querySelector<HTMLButtonElement>('[data-action="restart_runtime"]')!;
    button.click();
    await settle();
    expect(calls).toContain("runtime_service");
    expect(toasts.items.map((t) => t.text)).toEqual(["The runtime was restarted"]);
  });
});
