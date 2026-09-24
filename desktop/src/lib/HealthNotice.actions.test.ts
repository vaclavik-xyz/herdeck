import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { flushSync, mount, tick, unmount } from "svelte";
import HealthNotice from "./HealthNotice.svelte";
import { healthActions, healthItems, type HealthMessages } from "./healthStatus";
import { setLang } from "./i18n.svelte";

const M: HealthMessages = {
  runtime_mismatch: "runtime {runtime} ≠ app {app}",
  bridge_mismatch: "bridge {id} {bridge} ≠ runtime {runtime}",
  bridge_protocol: "bridge {id} protocol",
  bridge_token: "bridge {id}: token rejected {since}",
  bridge_down: "bridge {id}: disconnected {since}",
  d200_down: "D200: disconnected {since}",
  d200_locked: "D200 locked by {pid}",
  seconds: "{n} s",
  minutes: "{n} min",
  hours: "{n} h",
};
const NOW = 10_000_000;

describe("healthItems actions", () => {
  it("maps each problem to the action that addresses it", () => {
    const items = healthItems(
      {
        version: "0.9.1",
        app_version: "0.8.9",
        servers: {
          m4: { connected: true, bridge_version: "0.8.9", self_update: true, managed: true },
          ci: { connected: false, since: 0, ever_connected: true },
        },
        d200: { connected: false, last_frame_at: 1, since: 0 },
      },
      M,
      NOW,
    );
    expect(items.map((i) => i.action)).toEqual([
      { kind: "open_maintenance" },
      { kind: "update_bridge", serverId: "m4" },
      { kind: "open_maintenance" },
      { kind: "restart_deck" },
    ]);
    expect(healthActions(items)).toEqual([
      { kind: "open_maintenance" },
      { kind: "update_bridge", serverId: "m4" },
      { kind: "restart_deck" },
    ]);
  });
});

describe("HealthNotice actions", () => {
  let target: HTMLElement;
  beforeEach(() => {
    target = document.createElement("div");
    document.body.appendChild(target);
  });
  afterEach(() => {
    target.remove();
    setLang("en");
  });

  async function settle() {
    for (let i = 0; i < 10; i += 1) await tick();
    flushSync();
  }

  type Invoke = (cmd: string, args?: Record<string, unknown>) => Promise<unknown>;
  function mountWith(payload: unknown, invoke: Invoke, onOpenMaintenance: (() => void) | null = null) {
    return mount(HealthNotice, {
      target,
      props: { fetchHealth: async () => payload, intervalMs: 60_000, invoke, onOpenMaintenance },
    });
  }

  it("offers Update bridge on a bridge mismatch and runs it", async () => {
    const calls: Record<string, unknown>[] = [];
    const invoke: Invoke = async (_cmd, args) => {
      calls.push(args ?? {});
      return { status: 200, body: { ok: true, code: "updated", message: "updated to 0.9.1; restarting", progress: [], next: 0 } };
    };
    const instance = mountWith({ version: "0.9.1", servers: { m4: { connected: true, bridge_version: "0.8.9", self_update: true, managed: true } } }, invoke);
    await settle();
    const button = target.querySelector<HTMLButtonElement>('button[data-action="update:m4"]');
    expect(button?.textContent?.trim()).toBe("Update bridge m4");
    button!.click();
    await settle();
    expect(calls[0]).toMatchObject({ method: "POST", path: "/maintenance/servers/m4/update" });
    expect(target.querySelector(".health-result")?.textContent).toContain("updated to 0.9.1");
    unmount(instance);
  });

  it("sends a mismatch it cannot fix inline to Maintenance", () => {
    const actions = (s: Record<string, unknown>) =>
      healthItems({ version: "0.9.1", servers: { m4: { connected: true, bridge_version: "0.8.9", ...s } } }, M, NOW)
        .map((i) => i.action.kind);
    expect(actions({ self_update: true, managed: false })).toEqual(["open_maintenance"]);
    expect(actions({ self_update: true, managed: null })).toEqual(["open_maintenance"]);
    expect(actions({ self_update: false, managed: true })).toEqual(["open_maintenance"]);
    // a bridge NEWER than the runtime is never "updated" back
    expect(healthItems({ version: "0.9.1", servers: { m4: { bridge_version: "0.10.0", self_update: true, managed: true } } }, M, NOW)
      .map((i) => i.action.kind)).toEqual(["open_maintenance"]);
  });

  it("offers Restart deck on a D200 problem", async () => {
    const calls: Record<string, unknown>[] = [];
    const invoke: Invoke = async (_cmd, args) => {
      calls.push(args ?? {});
      return { status: 200, body: { ok: true, outcome: "reopened" } };
    };
    setLang("cs");
    const instance = mountWith({ d200: { lock_owner: 5 } }, invoke);
    await settle();
    const button = target.querySelector<HTMLButtonElement>('button[data-action="restart_deck"]');
    expect(button?.textContent?.trim()).toBe("Restartovat deck");
    button!.click();
    await settle();
    expect(calls[0]).toMatchObject({ method: "POST", path: "/maintenance/deck/restart" });
    expect(target.querySelector(".health-result")?.textContent).toContain("znovu otevřen");
    unmount(instance);
  });

  it("offers Open Maintenance otherwise, via the callback or the shell command", async () => {
    let opened = 0;
    const cmds: string[] = [];
    const invoke: Invoke = async (cmd) => { cmds.push(cmd); return null; };
    const payload = { version: "0.9.1", app_version: "0.8.9" };
    let instance = mountWith(payload, invoke, () => { opened += 1; });
    await settle();
    target.querySelector<HTMLButtonElement>('button[data-action="open_maintenance"]')!.click();
    expect(opened).toBe(1);
    unmount(instance);
    instance = mountWith(payload, invoke);
    await settle();
    target.querySelector<HTMLButtonElement>('button[data-action="open_maintenance"]')!.click();
    await settle();
    expect(cmds).toContain("open_maintenance");
    unmount(instance);
  });

  it("shows no buttons without invoke", async () => {
    const instance = mount(HealthNotice, {
      target,
      props: { fetchHealth: async () => ({ d200: { lock_owner: 5 } }), intervalMs: 60_000 },
    });
    await settle();
    expect(target.textContent).toContain("pid 5");
    expect(target.querySelector("button[data-action]")).toBeNull();
    unmount(instance);
  });
});
