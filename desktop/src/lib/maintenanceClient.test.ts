import { describe, expect, it } from "vitest";
import {
  fetchMaintenance, parseDeckOutcome, parseMaintenance, powerCycleDeck, restartDeck, runBridgeUpdate,
  compareVersions, runtimeOrigin, runtimeService, serverSegment, versionRows, type MaintenanceStatus,
  hookState, parseHooksSummary, runHooksAction,
} from "./maintenanceClient";
import {
  MAINTENANCE_MESSAGES, bridgeUpdateText, d200StateText, deckOutcomeText, powerCycleReasonText,
  hookStateText, hooksOutcomeText,
} from "./maintenanceMessages";
import { rawStatus } from "./maintenanceFixture";

function status(over: Record<string, unknown> = {}): MaintenanceStatus {
  const s = parseMaintenance(rawStatus(over));
  if (!s) throw new Error("fixture");
  return s;
}

describe("parseMaintenance", () => {
  it("shapes the runtime payload and the shell's app facts", () => {
    const s = status();
    expect(s.version).toBe("0.9.1");
    expect(s.d200.state).toBe("connected");
    expect(s.d200.powerCycle).toMatchObject({ available: true, hub: "20-1", port: 2 });
    expect(s.servers).toEqual([
      { id: "m4", managed: true, selfUpdate: true, connected: true, bridgeVersion: "0.8.9", protocolSupported: null, lastError: null, everConnected: true, hooks: null },
    ]);
    expect(s.app?.bundle).toBe("/Applications/herdeck.app");
  });

  it("tolerates junk", () => {
    expect(parseMaintenance(null)).toBeNull();
    expect(parseMaintenance([])).toBeNull();
    const s = parseMaintenance({ d200: { state: "exploded" } });
    expect(s?.d200.state).toBe("unknown");
    expect(s?.servers).toEqual([]);
    expect(s?.app).toBeNull();
  });
});

describe("runtimeOrigin", () => {
  it("tells this app's service from another bundle's and a checkout's", () => {
    expect(runtimeOrigin(status())).toBe("service_this_app");
    expect(runtimeOrigin(status({
      service: { installed: true, program: "/Users/me/Downloads/herdeck.app/Contents/Resources/herdeck-deckapp/herdeck-deckapp", from_app: true },
    }))).toBe("service_other_app");
    expect(runtimeOrigin(status({
      service: { installed: true, program: "/Users/me/herdeck/.venv/bin/python", from_app: false },
    }))).toBe("service_checkout");
  });

  it("is self_spawned for the app's own child and attached otherwise", () => {
    const notService = { frozen: true, spawned_by_app: true, is_service: false };
    expect(runtimeOrigin(status({ process: notService, app: { version: "0.9.1", spawned_runtime: true } }))).toBe("self_spawned");
    expect(runtimeOrigin(status({ process: notService, app: { version: "0.9.1", spawned_runtime: false } }))).toBe("attached");
    // installed unit, but this process is not it (hand-written launcher)
    expect(runtimeOrigin(status({ process: { is_service: false } }))).toBe("attached");
  });
});

describe("versionRows", () => {
  it("flags every version that differs from the runtime's", () => {
    const rows = versionRows(status({ app: { version: "0.9.2" } }));
    expect(rows.map((r) => [r.id, r.version, r.mismatch])).toEqual([
      ["app", "0.9.2", true],
      ["runtime", "0.9.1", false],
      ["m4", "0.8.9", true],
    ]);
    expect(versionRows(status({ servers: { a: { bridge_version: "0.9.1" } } }))[2].mismatch).toBe(false);
  });
});

describe("compareVersions", () => {
  it("compares dotted numbers, not strings", () => {
    expect(compareVersions("0.9.1", "0.10.0")).toBe(-1);
    expect(compareVersions("1.0", "1.0.0")).toBe(0);
    expect(compareVersions("v2.1.0", "2.0.9")).toBe(1);
    expect(compareVersions("dev", "1.0")).toBeNull();
  });
});

describe("serverSegment", () => {
  it("encodes every character the Rust allow-list refuses", () => {
    expect(serverSegment("m4")).toBe("m4");
    expect(serverSegment("local:personal")).toBe("local%3Apersonal");
    expect(serverSegment("a b/c")).toBe("a%20b%2Fc");
    expect(serverSegment("x!'()*")).toBe("x%21%27%28%29%2A");
  });
});

type Call = { cmd: string; args?: Record<string, unknown> };
function fakeInvoke(replies: unknown[]): { invoke: (cmd: string, args?: Record<string, unknown>) => Promise<unknown>; calls: Call[] } {
  const calls: Call[] = [];
  return {
    calls,
    invoke: async (cmd, args) => {
      calls.push({ cmd, args });
      const next = replies.shift();
      if (next instanceof Error) throw next;
      return next;
    },
  };
}

describe("fetchMaintenance", () => {
  it("GETs /maintenance through maintenance_call", async () => {
    const f = fakeInvoke([{ status: 200, body: rawStatus() }]);
    const r = await fetchMaintenance(f.invoke);
    expect(r.kind).toBe("ok");
    expect(f.calls[0]).toEqual({ cmd: "maintenance_call", args: { method: "GET", path: "/maintenance" } });
  });

  it("reports HTTP and transport failures", async () => {
    expect(await fetchMaintenance(fakeInvoke([{ status: 403, body: null }]).invoke)).toEqual({ kind: "error", message: "HTTP 403" });
    const r = await fetchMaintenance(fakeInvoke([new Error("sidecar not ready")]).invoke);
    expect(r).toEqual({ kind: "error", message: "Error: sidecar not ready" });
  });
});

describe("deck actions", () => {
  it("POST the exact routes and keep the outcome details", async () => {
    const f = fakeInvoke([
      { status: 200, body: { ok: true, outcome: "reopened" } },
      { status: 200, body: { ok: false, outcome: "needs_admin", command: "sudo uhubctl -l 20-1 -p 2 -a cycle -d 2", error: "Permission denied" } },
    ]);
    expect((await restartDeck(f.invoke)).outcome).toBe("reopened");
    const cycle = await powerCycleDeck(f.invoke);
    expect(cycle).toMatchObject({ ok: false, outcome: "needs_admin", command: "sudo uhubctl -l 20-1 -p 2 -a cycle -d 2" });
    expect(f.calls.map((c) => c.args?.path)).toEqual(["/maintenance/deck/restart", "/maintenance/deck/power-cycle"]);
    expect(f.calls[0].args?.method).toBe("POST");
  });

  it("maps a non-200 answer and an unreachable runtime", async () => {
    expect(parseDeckOutcome({ status: 403, body: null })).toMatchObject({ ok: false, outcome: "http", error: "HTTP 403" });
    expect(await restartDeck(fakeInvoke([new Error("down")]).invoke)).toMatchObject({ outcome: "unreachable" });
  });
});

describe("runBridgeUpdate", () => {
  it("POSTs, then long-polls with after=<next> until the outcome, accumulating progress", async () => {
    const f = fakeInvoke([
      { status: 200, body: { ok: true, code: "pending", message: "update running", target: "0.9.1", output: "", progress: [{ seq: 1, stage: "download", message: "wheel" }], next: 1 } },
      { status: 200, body: { ok: true, code: "pending", message: "update running", target: "0.9.1", output: "", progress: [{ seq: 2, stage: "install", message: "pip" }], next: 2 } },
      { status: 200, body: { ok: true, code: "updated", message: "updated to 0.9.1; restarting", target: "0.9.1", output: "", progress: [{ seq: 3, stage: "verify", message: "ok" }], next: 3 } },
    ]);
    const seen: string[] = [];
    const view = await runBridgeUpdate(f.invoke, "local:m4", (v) => seen.push(v.code), { waitMs: 5000 });
    expect(view.code).toBe("updated");
    expect(view.progress.map((p) => p.stage)).toEqual(["download", "install", "verify"]);
    expect(seen).toEqual(["pending", "pending", "updated"]);
    expect(f.calls.map((c) => [c.args?.method, c.args?.path])).toEqual([
      ["POST", "/maintenance/servers/local%3Am4/update"],
      ["GET", "/maintenance/servers/local%3Am4/update?after=1&wait_ms=5000"],
      ["GET", "/maintenance/servers/local%3Am4/update?after=2&wait_ms=5000"],
    ]);
    expect(f.calls[0].args?.body).toEqual({ wait_ms: 5000 });
  });

  it("stops at once on a final code and when cancelled", async () => {
    const done = await runBridgeUpdate(fakeInvoke([{ status: 200, body: { ok: false, code: "not_managed", message: "no", progress: [], next: 0 } }]).invoke, "m4");
    expect(done.code).toBe("not_managed");
    const f = fakeInvoke([{ status: 200, body: { ok: true, code: "pending", progress: [], next: 0 } }]);
    const cancelled = await runBridgeUpdate(f.invoke, "m4", () => {}, { isCancelled: () => true });
    expect(cancelled.code).toBe("pending");
    expect(f.calls).toHaveLength(1);
  });

  it("maps HTTP errors and transport errors", async () => {
    expect((await runBridgeUpdate(fakeInvoke([{ status: 404, body: null }]).invoke, "x")).code).toBe("http");
    expect((await runBridgeUpdate(fakeInvoke([new Error("gone")]).invoke, "x")).code).toBe("unreachable");
  });
});

describe("runtimeService", () => {
  it("passes the action and env and shapes the result", async () => {
    const f = fakeInvoke([{ ok: false, exit_code: 1, timed_out: false, stdout: "", stderr: "launchctl failed" }]);
    const r = await runtimeService(f.invoke, "restart");
    expect(f.calls[0]).toEqual({ cmd: "runtime_service", args: { action: "restart", env: [], replace: false } });
    expect(r).toEqual({ ok: false, exitCode: 1, timedOut: false, detail: "launchctl failed" });
    const refused = await runtimeService(fakeInvoke([new Error("no bundled runtime")]).invoke, "install");
    expect(refused.ok).toBe(false);
    expect(refused.detail).toContain("no bundled runtime");
  });
});

describe("outcome texts", () => {
  const DECK = ["reopened", "not_present", "failed", "locked_by", "timeout", "busy", "unsupported", "cycled", "needs_admin", "unavailable", "http", "unreachable"];
  const BRIDGE = ["updated", "pending", "not_managed", "downgrade", "readonly", "failed", "busy", "unsupported", "disconnected", "newer", "current", "http", "unreachable"];

  for (const lang of ["en", "cs"] as const) {
    const m = MAINTENANCE_MESSAGES[lang];
    it(`every deck and bridge outcome has its own sentence [${lang}]`, () => {
      const deckTexts = DECK.map((outcome) => deckOutcomeText({
        ok: outcome === "reopened", outcome, error: "e", command: null, reason: null, pid: 7, usbPresent: false, hub: "1-1", port: 3,
      }, m).text);
      expect(new Set(deckTexts).size).toBe(DECK.length);
      const bridgeTexts = BRIDGE.map((code) => bridgeUpdateText({ ok: false, code, message: "msg", target: null, output: "", progress: [], next: 0 }, m).text);
      expect(new Set(bridgeTexts).size).toBe(BRIDGE.length);
      for (const t of [...deckTexts, ...bridgeTexts]) expect(t).not.toMatch(/\{\w+\}/);
    });
  }

  it("needs_admin and not_managed carry the command to copy", () => {
    const m = MAINTENANCE_MESSAGES.en;
    const admin = deckOutcomeText({ ok: false, outcome: "needs_admin", error: null, command: "sudo uhubctl -l 1-1 -p 3 -a cycle -d 2", reason: null, pid: null, usbPresent: null, hub: null, port: null }, m);
    expect(admin.command).toBe("sudo uhubctl -l 1-1 -p 3 -a cycle -d 2");
    const managed = bridgeUpdateText({ ok: false, code: "not_managed", message: "", target: null, output: "", progress: [], next: 0 }, m);
    expect(managed.command).toBe("herdeck-service install bridge --managed");
  });

  it("names why a power-cycle is unavailable and the D200 state in words", () => {
    const m = MAINTENANCE_MESSAGES.en;
    expect(powerCycleReasonText("uhubctl_missing", m)).toContain("brew install uhubctl");
    expect(powerCycleReasonText("location_unknown", m)).toContain("usb_hub");
    const d = status().d200;
    expect(d200StateText({ ...d, state: "not_on_usb" }, m)).toContain("unplug and replug");
    expect(d200StateText({ ...d, state: "locked", lockOwner: 99 }, m)).toContain("pid 99");
    expect(d200StateText({ ...d, lastFrameAt: 1000 }, m, 6000)).toContain("5 s");
  });
});

describe("subagent hooks", () => {
  const raw = (claude: Record<string, unknown>, codex: Record<string, unknown>) => ({
    claude: { installed: false, file: "/h/.claude/settings.json", error: null, ...claude },
    codex: { installed: false, file: "/h/.codex/hooks.json", error: null, needs_trust: false, features_hooks_enabled: false, ...codex },
  });

  it("parses the summary and derives each agent's state", () => {
    expect(parseHooksSummary(null)).toBeNull();
    expect(parseHooksSummary([])).toBeNull();
    const cases: [Record<string, unknown>, Record<string, unknown>, string, string][] = [
      [{}, {}, "not_installed", "not_installed"],
      [{ installed: true }, { installed: true, needs_trust: true }, "installed", "enable_features"],
      [{}, { installed: true, needs_trust: true, features_hooks_enabled: true }, "not_installed", "needs_trust"],
      [{}, { installed: true, features_hooks_enabled: true }, "not_installed", "installed"],
      [{ error: "not valid JSON" }, { installed: true, features_hooks_enabled: null }, "error", "enable_features"],
    ];
    for (const [claude, codex, cs, xs] of cases) {
      const h = parseHooksSummary(raw(claude, codex));
      expect([hookState("claude", h), hookState("codex", h)]).toEqual([cs, xs]);
    }
    expect(hookState("claude", null)).toBe("unknown");
    expect(hookState("codex", parseHooksSummary({ claude: raw({}, {}).claude }))).toBe("unknown");
    const s = status({ servers: { m4: { connected: true, hooks: raw({ installed: true }, {}) } } });
    expect(s.servers[0].hooks?.claude).toEqual({
      installed: true, file: "/h/.claude/settings.json", error: null, needsTrust: false, featuresHooksEnabled: null,
    });
  });

  it("names the Codex config next to its hooks file", () => {
    const m = MAINTENANCE_MESSAGES.en;
    const h = parseHooksSummary(raw({}, { installed: true }));
    expect(hookStateText("codex", h, m)).toContain("[features] hooks = true in /h/.codex/config.toml");
    expect(hookStateText("claude", h, m)).toBe("not installed");
    expect(hookStateText("claude", parseHooksSummary(raw({ error: "broken" }, {})), MAINTENANCE_MESSAGES.cs)).toContain("broken");
  });

  it("posts the action for one agent and maps the outcome", async () => {
    const calls: unknown[] = [];
    const invoke = async (cmd: string, args?: Record<string, unknown>) => {
      calls.push({ cmd, args });
      return { status: 200, body: { ok: true, code: "ok", message: "", agents: raw({ installed: true }, {}) } };
    };
    const o = await runHooksAction(invoke, "local:b", "install", ["claude"]);
    expect(calls).toEqual([{
      cmd: "maintenance_call",
      args: { method: "POST", path: "/maintenance/servers/local%3Ab/hooks", body: { action: "install", agents: ["claude"] } },
    }]);
    expect(o.ok).toBe(true);
    expect(o.agents?.claude?.installed).toBe(true);
    expect(hooksOutcomeText(o, MAINTENANCE_MESSAGES.en).text).toContain("after a restart");
    const http = await runHooksAction(async () => ({ status: 404, body: null }), "x", "uninstall", ["codex"]);
    expect(http).toMatchObject({ ok: false, code: "http" });
    const down = await runHooksAction(async () => { throw new Error("gone"); }, "x", "install", ["codex"]);
    expect(down).toMatchObject({ ok: false, code: "unreachable" });
    const m = MAINTENANCE_MESSAGES.cs;
    for (const code of ["failed", "readonly", "unsupported", "disconnected", "timeout", "http", "unreachable", "weird"]) {
      const text = hooksOutcomeText({ ok: false, code, message: "msg", agents: null }, m);
      expect(text.ok).toBe(false);
      expect(text.text.length).toBeGreaterThan(5);
    }
  });
});
