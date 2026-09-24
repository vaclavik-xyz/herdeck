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
  it("shows the runtime's config error in English and Czech", async () => {
    const status = rawStatus({ config_error: "bridge token for server 'local' not found" });
    let t = await render(fake(status).invoke);
    expect(t.querySelector("[data-config-error]")?.textContent).toContain(
      "The config does not load: bridge token for server 'local' not found",
    );
    cleanup?.();
    t = await render(fake(status).invoke, "cs");
    expect(t.querySelector("[data-config-error]")?.textContent).toContain(
      "Config nejde načíst: bridge token for server 'local' not found",
    );
    cleanup?.();
    cleanup = null;
    t = await render(fake(rawStatus()).invoke);
    expect(t.querySelector("[data-config-error]")).toBeNull();
  });

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
    expect(g.calls.find((c) => c.cmd === "runtime_service")?.args).toEqual({ action: "install", env: [], replace: false });
    expect(t.querySelector('[data-note="service"]')?.textContent).toBe("Done.");
  });

  it("never offers a plain install over a checkout unit: replacing it needs a confirm naming its program", async () => {
    const program = "/Users/me/herdeck/.venv/bin/python";
    const ok = () => ({ ok: true, exit_code: 0, timed_out: false, stdout: "", stderr: "" });
    // origin service_checkout, and a checkout unit next to a self-spawned runtime
    for (const process of [{ is_service: true }, { is_service: false }]) {
      const status = rawStatus({ process, service: { installed: true, program, from_app: false } });
      const g = fake(status, { runtime_service: ok });
      const t = await render(g.invoke);
      expect(t.querySelector('button[data-action="install"]')).toBeNull();
      expect(t.querySelector('button[data-action="uninstall"]')).toBeNull();
      expect(button(t, "restart-runtime").disabled).toBe(true);
      expect(t.querySelector('[data-hint="not-ours"]')?.textContent).toContain(program);
      button(t, "replace").click();
      flushSync();
      const confirm = t.querySelector('[data-confirm="replace"]')?.textContent ?? "";
      expect(confirm).toContain(program);
      expect(confirm).toContain("--config");
      button(t, "confirm").click();
      await settle();
      expect(g.calls.find((c) => c.cmd === "runtime_service")?.args).toEqual({ action: "install", env: [], replace: true });
      cleanup?.();
      cleanup = null;
    }
  });

  it("names another app's bundle in the remove confirm and sends replace", async () => {
    const program = "/Users/me/Downloads/herdeck.app/Contents/Resources/herdeck-deckapp/herdeck-deckapp";
    const g = fake(rawStatus({ service: { installed: true, program, from_app: true } }), {
      runtime_service: () => ({ ok: true, exit_code: 0, timed_out: false, stdout: "", stderr: "" }),
    });
    const t = await render(g.invoke, "cs");
    expect(t.querySelector("[data-origin]")?.getAttribute("data-origin")).toBe("service_other_app");
    expect(button(t, "restart-runtime").disabled).toBe(true);
    button(t, "uninstall").click();
    flushSync();
    expect(t.querySelector('[data-confirm="uninstall"]')?.textContent).toContain(program);
    button(t, "confirm").click();
    await settle();
    expect(g.calls.find((c) => c.cmd === "runtime_service")?.args).toEqual({ action: "uninstall", env: [], replace: true });
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
    expect(t.querySelector('[data-note="bridge"]')?.textContent).toBe("Bridge m4 is updated to 0.9.1.");
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

  it("offers Update bridge only to a managed, self-updating bridge behind the runtime", async () => {
    const servers = {
      old: { managed: true, self_update: true, connected: true, bridge_version: "0.8.9" },
      same: { managed: true, self_update: true, connected: true, bridge_version: "0.9.1" },
      newer: { managed: true, self_update: true, connected: true, bridge_version: "0.10.0" },
      hand: { managed: false, self_update: true, connected: true, bridge_version: "0.8.9" },
      t3: { managed: null, self_update: false, connected: true, bridge_version: null },
      legacy: { managed: true, self_update: false, connected: true, bridge_version: "0.8.9" },
    };
    const t = await render(fake(rawStatus({ servers })).invoke);
    const has = (id: string) => t.querySelector(`[data-server="${id}"] button[data-action="update-bridge"]`) != null;
    expect(["old", "same", "newer", "hand", "t3", "legacy"].map(has)).toEqual([true, false, false, false, false, false]);
    expect(t.querySelector('[data-server="hand"] [data-offer="install_managed"]')).not.toBeNull();
    expect(t.querySelector('[data-server="hand"] .command code')?.textContent)
      .toBe("herdeck-service install bridge --managed --version 0.9.1");
    expect(t.querySelector('[data-server="t3"] [data-offer="unknown"]')?.textContent).toContain("Install type unknown");
    expect(t.querySelector('[data-server="legacy"] [data-offer="unsupported"]')).not.toBeNull();
  });

  it("greys out a server that never connected as not in use (en + cs)", async () => {
    const status = rawStatus({
      servers: {
        m4: { managed: true, self_update: true, connected: true, bridge_version: "0.9.1", ever_connected: true },
        "t3-headless": { managed: null, self_update: false, connected: false, ever_connected: false, last_error: "T3 unavailable or incompatible" },
      },
    });
    let t = await render(fake(status).invoke);
    const row = t.querySelector<HTMLElement>('[data-server="t3-headless"]')!;
    expect(row.hasAttribute("data-unused")).toBe(true);
    expect(row.querySelector(".unused-label")?.textContent).toBe("not in use");
    expect(row.textContent).not.toContain("T3 unavailable");
    expect(t.querySelector('[data-server="m4"]')?.hasAttribute("data-unused")).toBe(false);
    cleanup?.();
    t = await render(fake(status).invoke, "cs");
    expect(t.querySelector('[data-server="t3-headless"] .unused-label')?.textContent).toBe("nepoužívá se");
  });

  const hooksSummary = (claude: Record<string, unknown> = {}, codex: Record<string, unknown> = {}) => ({
    claude: { installed: false, file: "/Users/me/.claude/settings.json", error: null, ...claude },
    codex: { installed: false, file: "/Users/me/.codex/hooks.json", error: null, needs_trust: false, features_hooks_enabled: false, ...codex },
  });
  const withHooks = (hooks: unknown, over: Record<string, unknown> = {}) =>
    rawStatus({ servers: { m4: { managed: true, self_update: true, connected: true, bridge_version: "0.9.1", hooks, ...over } } });
  const hookRow = (t: HTMLElement, agent: string) => t.querySelector<HTMLElement>(`[data-hooks="m4"] [data-agent="${agent}"]`)!;

  it("shows each agent's subagent tracking state (en + cs)", async () => {
    const status = withHooks(hooksSummary({ installed: true }, { installed: true, needs_trust: true }));
    let t = await render(fake(status).invoke);
    expect(hookRow(t, "claude").dataset.state).toBe("installed");
    expect(hookRow(t, "claude").querySelector("[data-hook-state]")?.textContent).toBe("installed");
    expect(hookRow(t, "codex").dataset.state).toBe("enable_features");
    expect(hookRow(t, "codex").textContent).toContain("[features] hooks = true in /Users/me/.codex/config.toml");
    const toggle = button(t, "hooks-claude");
    expect(toggle.getAttribute("aria-checked")).toBe("true");
    expect(toggle.getAttribute("title")).toBe("Remove herdeck's subagent hooks for Claude Code from m4");
    cleanup?.();
    t = await render(fake(withHooks(hooksSummary({}, { installed: true, needs_trust: true, features_hooks_enabled: true }))).invoke, "cs");
    expect(hookRow(t, "claude").textContent).toContain("nenainstalováno");
    expect(hookRow(t, "codex").textContent).toContain("/hooks");
    expect(button(t, "hooks-claude").getAttribute("title")).toContain("Nainstalovat");
  });

  it("confirms before installing, naming the file and the backup, then reports the outcome", async () => {
    const posted: unknown[] = [];
    const f = fake(withHooks(hooksSummary()), {
      "POST /maintenance/servers/m4/hooks": (args) => {
        posted.push(args?.body);
        return { status: 200, body: { ok: true, code: "ok", message: "", agents: hooksSummary({ installed: true }) } };
      },
    });
    const t = await render(f.invoke);
    button(t, "hooks-claude").click();
    await settle();
    expect(posted).toEqual([]);
    const confirm = t.querySelector('[data-confirm="hooks-install"]')!;
    expect(confirm.textContent).toContain("/Users/me/.claude/settings.json");
    expect(confirm.textContent).toContain("backup");
    button(t, "hooks-confirm").click();
    await settle();
    expect(posted).toEqual([{ action: "install", agents: ["claude"] }]);
    expect(t.querySelector('[data-note="hooks"]')?.textContent).toBe("Done. Running agents pick it up after a restart.");
  });

  it("asks to uninstall an installed agent, can be cancelled, and shows refusals", async () => {
    const posted: unknown[] = [];
    const f = fake(withHooks(hooksSummary({}, { installed: true, features_hooks_enabled: true })), {
      "POST /maintenance/servers/m4/hooks": (args) => {
        posted.push(args?.body);
        return { status: 200, body: { ok: false, code: "readonly", message: "read-only token", agents: null } };
      },
    });
    const t = await render(f.invoke, "cs");
    button(t, "hooks-codex").click();
    await settle();
    expect(t.querySelector('[data-confirm="hooks-uninstall"]')?.textContent).toContain("záloha");
    button(t, "hooks-cancel").click();
    await settle();
    expect(t.querySelector('[data-confirm^="hooks-"]')).toBeNull();
    button(t, "hooks-codex").click();
    await settle();
    button(t, "hooks-confirm").click();
    await settle();
    expect(posted).toEqual([{ action: "uninstall", agents: ["codex"] }]);
    const note = t.querySelector('[data-note="hooks"]')!;
    expect(note.classList.contains("bad")).toBe(true);
    expect(note.textContent).toContain("jen pro čtení");
  });

  it("offers the OpenCode plugin only when the bridge reports it, with a plugin confirmation", async () => {
    let t = await render(fake(withHooks(hooksSummary())).invoke);
    expect(t.querySelector('[data-hooks="m4"] [data-agent="opencode"]')).toBeNull();
    cleanup?.();
    const plugin = "/Users/me/.config/opencode/plugins/herdeck-subagents.js";
    const posted: unknown[] = [];
    const f = fake(withHooks({ ...hooksSummary(), opencode: { installed: false, file: plugin, error: null } }), {
      "POST /maintenance/servers/m4/hooks": (args) => {
        posted.push(args?.body);
        return { status: 200, body: { ok: true, code: "ok", message: "", agents: null } };
      },
    });
    t = await render(f.invoke);
    expect(hookRow(t, "opencode").dataset.state).toBe("not_installed");
    expect(button(t, "hooks-opencode").getAttribute("title")).toBe("Install herdeck's subagent hooks for OpenCode on m4");
    button(t, "hooks-opencode").click();
    await settle();
    const confirm = t.querySelector('[data-confirm="hooks-install"]')!;
    expect(confirm.textContent).toContain(`subagent plugin on m4? herdeck writes ${plugin}`);
    button(t, "hooks-confirm").click();
    await settle();
    expect(posted).toEqual([{ action: "install", agents: ["opencode"] }]);
    cleanup?.();
    t = await render(fake(withHooks({ ...hooksSummary(), opencode: { installed: true, file: plugin, error: null } })).invoke, "cs");
    button(t, "hooks-opencode").click();
    await settle();
    expect(t.querySelector('[data-confirm="hooks-uninstall"]')?.textContent).toContain("Odebrat plugin pro subagenty OpenCode");
  });

  it("explains a bridge that does not report hooks and hides the row while disconnected", async () => {
    let t = await render(fake(withHooks(null)).invoke);
    expect(t.querySelector('[data-hooks="m4"] [data-hooks-unavailable]')?.textContent).toContain("Subagent tracking");
    expect(t.querySelector('[data-action="hooks-claude"]')).toBeNull();
    cleanup?.();
    t = await render(fake(withHooks(hooksSummary(), { connected: false })).invoke);
    expect(t.querySelector('[data-hooks="m4"]')).toBeNull();
  });

  it("does not offer installing over a hook file it cannot use", async () => {
    const t = await render(fake(withHooks(hooksSummary({ error: "settings.json is not valid JSON; not changed" }))).invoke);
    expect(hookRow(t, "claude").dataset.state).toBe("error");
    expect(hookRow(t, "claude").textContent).toContain("not valid JSON");
    expect(button(t, "hooks-claude").disabled).toBe(true);
  });

  it("reports an unreachable runtime", async () => {
    const t = await render(async () => { throw new Error("sidecar not ready"); });
    expect(t.querySelector('[role="alert"]')?.textContent).toContain("sidecar not ready");
  });
});
