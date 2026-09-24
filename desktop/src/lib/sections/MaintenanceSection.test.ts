import { afterEach, describe, expect, it } from "vitest";
import { flushSync, mount, tick, unmount } from "svelte";
import MaintenanceSection from "./MaintenanceSection.svelte";
import { rawStatus } from "../maintenanceFixture";
import { setLang } from "../i18n.svelte";

type Handler = (args: Record<string, unknown> | undefined) => unknown;

/** A fake Tauri invoke: GET /maintenance answers `status`, everything else
 *  goes to `routes[<method path>]` / `routes[<cmd>]`. */
function fake(status: Record<string, unknown>, routes: Record<string, Handler> = {}) {
  const calls: { cmd: string; args?: Record<string, unknown> }[] = [];
  const invoke = async (cmd: string, args?: Record<string, unknown>): Promise<unknown> => {
    calls.push({ cmd, args });
    if (cmd === "maintenance_call") {
      const key = `${args?.method} ${args?.path}`;
      if (key === "GET /maintenance") return { status: 200, body: status };
      const h = routes[key] ?? routes[`${args?.method} ${String(args?.path).split("?")[0]}`];
      if (h) return h(args);
      return { status: 404, body: null };
    }
    const h = routes[cmd];
    if (h) return h(args);
    throw new Error(`unexpected ${cmd}`);
  };
  return { invoke, calls };
}

let cleanup: (() => void) | null = null;
afterEach(() => {
  cleanup?.();
  cleanup = null;
  setLang("en");
});

async function settle(): Promise<void> {
  for (let i = 0; i < 12; i += 1) await tick();
  flushSync();
}

async function render(invoke: (cmd: string, args?: Record<string, unknown>) => Promise<unknown>, lang: "en" | "cs" = "en") {
  setLang(lang);
  const target = document.createElement("div");
  document.body.appendChild(target);
  const instance = mount(MaintenanceSection, { target, props: { invoke, pollMs: 600_000, updateWaitMs: 1000 } });
  cleanup = () => { unmount(instance); target.remove(); };
  await settle();
  return target;
}

const button = (t: HTMLElement, action: string): HTMLButtonElement => {
  const b = t.querySelector<HTMLButtonElement>(`button[data-action="${action}"]`);
  if (!b) throw new Error(`no button ${action}`);
  return b;
};

describe("MaintenanceSection", () => {
  it("shows versions with the bridge mismatch flagged, and a service from this app", async () => {
    const t = await render(fake(rawStatus()).invoke);
    const bridge = t.querySelector('[data-row="bridge:m4"]');
    expect(bridge?.classList.contains("mismatch")).toBe(true);
    expect(t.querySelector('[data-row="runtime"]')?.classList.contains("mismatch")).toBe(false);
    expect(t.querySelector("[data-origin]")?.getAttribute("data-origin")).toBe("service_this_app");
    expect(t.textContent).toContain("updates together with the app");
    // already the service from this app: no install offer, restart + uninstall are
    expect(t.querySelector('[data-action="install"]')).toBeNull();
    expect(button(t, "restart-runtime").disabled).toBe(false);
    expect(button(t, "uninstall").disabled).toBe(false);
    expect(button(t, "runtime-log").disabled).toBe(false);
  });

  it("offers installing the service when the runtime runs inside the app", async () => {
    // app facts say this shell spawned it
    const status = rawStatus({ process: { is_service: false }, service: { installed: false }, logs: { runtime: null } });
    (status.app as Record<string, unknown>).spawned_runtime = true;
    const g = fake(status, { runtime_service: () => ({ ok: true, exit_code: 0, timed_out: false, stdout: "", stderr: "" }) });
    const t = await render(g.invoke);
    expect(t.querySelector("[data-origin]")?.getAttribute("data-origin")).toBe("self_spawned");
    expect(button(t, "restart-runtime").disabled).toBe(true);
    expect(button(t, "runtime-log").disabled).toBe(true);
    expect(t.textContent).toContain("d200_standard_writer");
    button(t, "install").click();
    flushSync();
    // two-step: confirm first
    const confirm = Array.from(t.querySelectorAll("button")).find((b) => b.textContent?.trim() === "Confirm");
    expect(confirm).toBeTruthy();
    confirm!.click();
    await settle();
    expect(g.calls.find((c) => c.cmd === "runtime_service")?.args).toEqual({ action: "install", env: [] });
    expect(t.querySelector('[data-note="service"]')?.textContent).toBe("Done.");
  });

  it("disables install in a dev build without a bundled runtime", async () => {
    const status = rawStatus({ process: { is_service: false }, service: { installed: false } });
    (status.app as Record<string, unknown>).bundled_runtime = null;
    const t = await render(fake(status).invoke);
    expect(button(t, "install").disabled).toBe(true);
    expect(t.textContent).toContain("dev build");
  });

  it("explains each D200 state and why power-cycle is unavailable", async () => {
    const status = rawStatus();
    status.d200 = {
      ...(status.d200 as object), connected: false, state: "not_on_usb", usb_present: false,
      power_cycle: { available: false, reason: "uhubctl_missing", uhubctl: null, hub: null, port: null, source: null },
    };
    const t = await render(fake(status).invoke);
    expect(t.querySelector("[data-d200]")?.textContent).toContain("not connected to USB");
    expect(button(t, "power-cycle").disabled).toBe(true);
    expect(t.querySelector('[data-reason="uhubctl_missing"]')?.textContent).toContain("brew install uhubctl");
  });

  it("restarts the deck and shows the outcome", async () => {
    const f = fake(rawStatus(), {
      "POST /maintenance/deck/restart": () => ({ status: 200, body: { ok: false, outcome: "locked_by", pid: 77 } }),
    });
    const t = await render(f.invoke);
    button(t, "restart-deck").click();
    await settle();
    expect(t.querySelector('[data-note="deck"]')?.textContent).toContain("pid 77");
  });

  it("shows the exact command with a copy button when power-cycle needs admin", async () => {
    const command = "sudo /opt/homebrew/bin/uhubctl -l 20-1 -p 2 -a cycle -d 2";
    const f = fake(rawStatus(), {
      "POST /maintenance/deck/power-cycle": () => ({ status: 200, body: { ok: false, outcome: "needs_admin", command, error: "Permission denied" } }),
    });
    const t = await render(f.invoke);
    button(t, "power-cycle").click();
    await settle();
    expect(t.querySelector(".command code")?.textContent).toBe(command);
    const copy = t.querySelector<HTMLButtonElement>(".command button.copy");
    expect(copy?.getAttribute("title")).toBe("Copy the command to the clipboard");
  });

  it("follows a bridge update's progress to its outcome", async () => {
    let polls = 0;
    const f = fake(rawStatus(), {
      "POST /maintenance/servers/m4/update": () => ({
        status: 200,
        body: { ok: true, code: "pending", message: "update running", progress: [{ seq: 1, stage: "download", message: "wheel" }], next: 1 },
      }),
      "GET /maintenance/servers/m4/update": () => {
        polls += 1;
        return {
          status: 200,
          body: { ok: true, code: "updated", message: "updated to 0.9.1; restarting", progress: [{ seq: 2, stage: "verify", message: "0.9.1" }], next: 2 },
        };
      },
    });
    const t = await render(f.invoke);
    const update = button(t, "update-bridge");
    expect(update.getAttribute("title")).toContain("0.9.1");
    update.click();
    await settle();
    expect(polls).toBe(1);
    const items = Array.from(t.querySelectorAll('[data-server="m4"] .progress li')).map((li) => li.textContent);
    expect(items).toEqual(["download wheel", "verify 0.9.1"]);
    expect(t.querySelector('[data-note="bridge"]')?.textContent).toContain("updated to 0.9.1");
  });

  it("explains not_managed with the one-time managed install command", async () => {
    const f = fake(rawStatus(), {
      "POST /maintenance/servers/m4/update": () => ({ status: 200, body: { ok: false, code: "not_managed", message: "not managed", progress: [], next: 0 } }),
    });
    const t = await render(f.invoke, "cs");
    button(t, "update-bridge").click();
    await settle();
    expect(t.querySelector('[data-note="bridge"]')?.textContent).toContain("spravovaná služba");
    expect(t.querySelector(".command code")?.textContent).toBe("herdeck-service install bridge --managed");
  });

  it("reports an unreachable runtime", async () => {
    const t = await render(async () => { throw new Error("sidecar not ready"); });
    expect(t.querySelector('[role="alert"]')?.textContent).toContain("sidecar not ready");
  });
});
