import { afterEach, describe, expect, it } from "vitest";
import {
  HEALTH_GRACE_MS, bySeverity, configErrorSection, healthProblems, worstSeverity,
  type HealthProblem,
} from "./healthStatus";
import { maintenanceBadge } from "./healthState.svelte";
import { NOTICE_MESSAGES, problemText } from "./noticeMessages";
import { dismiss, isDismissed, pruneDismissals, readDismissals, writeDismissals, DISMISSALS_KEY } from "./noticeDismissals";

const NOW = 10_000_000;
const kinds = (raw: unknown, now = NOW) => healthProblems(raw, now).map((p) => p.kind);
const text = (p: HealthProblem, lang: "en" | "cs" = "en") => problemText(p, NOTICE_MESSAGES[lang], NOW);

describe("healthProblems", () => {
  it("is empty for a healthy runtime and for an old runtime without the fields", () => {
    expect(healthProblems({ ok: true }, NOW)).toEqual([]);
    expect(
      healthProblems({
        version: "0.8.1",
        app_version: "0.8.1",
        servers: { local: { connected: true, bridge_version: "0.8.1", since: NOW - 1 } },
        d200: { connected: true, last_frame_at: NOW },
      }, NOW),
    ).toEqual([]);
  });

  it("orders by severity: errors, then warnings, then info — payload order within one", () => {
    const problems = healthProblems({
      version: "0.9.1",
      app_version: "0.9.0",
      config_error: "invalid grid 'wide'",
      servers: {
        newer: { connected: true, bridge_version: "0.9.2" },
        m4: { connected: true, bridge_version: "0.8.9", self_update: true, managed: true },
        box: { connected: false, since: 0, ever_connected: true, last_error: "token rejected (close 4401)" },
      },
      d200: { connected: false, last_frame_at: 1, since: 0 },
    }, NOW);
    expect(problems.map((p) => [p.severity, p.kind])).toEqual([
      ["error", "config_error"],
      ["error", "bridge_token"],
      ["warning", "runtime_mismatch"],
      ["warning", "bridge_mismatch"],
      ["warning", "d200_down"],
      ["info", "bridge_newer"],
    ]);
    expect(worstSeverity(problems)).toBe("error");
    expect(worstSeverity(problems.filter((p) => p.severity !== "error"))).toBe("warning");
    expect(worstSeverity([])).toBeNull();
  });

  it("maps each problem to the one action that fixes it", () => {
    const actions = Object.fromEntries(healthProblems({
      version: "0.9.1",
      app_version: "0.9.0",
      config_error: "x",
      servers: { m4: { connected: true, bridge_version: "0.8.9", self_update: true, managed: true } },
      d200: { lock_owner: 5 },
    }, NOW).map((p) => [p.kind, p.action]));
    expect(actions).toEqual({
      config_error: { kind: "fix_config" },
      runtime_mismatch: { kind: "restart_runtime" },
      bridge_mismatch: { kind: "update_bridge", serverId: "m4" },
      d200_locked: { kind: "restart_deck" },
    });
  });

  it("offers no inline update for a bridge it cannot update", () => {
    const action = (s: Record<string, unknown>) =>
      healthProblems({ version: "0.9.1", servers: { m4: { connected: true, bridge_version: "0.8.9", ...s } } }, NOW)[0].action;
    expect(action({ self_update: true, managed: false })).toBeNull();
    expect(action({ self_update: true, managed: null })).toBeNull();
    expect(action({ self_update: false, managed: true })).toBeNull();
  });

  it("explains a dropped bridge only after the grace period", () => {
    expect(kinds({ servers: { local: { connected: false, since: NOW - 1000, ever_connected: true } } })).toEqual([]);
    expect(kinds({ servers: { box: { connected: false, since: NOW - HEALTH_GRACE_MS, ever_connected: true } } }))
      .toEqual(["bridge_down"]);
  });

  it("stays quiet about a server that never connected (an unused T3), unless its token was rejected", () => {
    const unused = { servers: { t3: { connected: false, ever_connected: false, since: NOW - 3_600_000, last_error: "T3 unavailable" } } };
    expect(kinds(unused)).toEqual([]);
    const badToken = { servers: { t3: { connected: false, ever_connected: false, since: NOW - 60_000, last_error: "token rejected (close 4401)" } } };
    expect(kinds(badToken)).toEqual(["bridge_token"]);
  });

  it("reports a D200 only once it has been driven, and a foreign lock owner", () => {
    expect(kinds({ d200: { connected: false, last_frame_at: null, since: 0 } })).toEqual([]);
    expect(kinds({ d200: { connected: false, last_frame_at: 5, since: NOW - 2 * 3_600_000 } })).toEqual(["d200_down"]);
    expect(kinds({ d200: { connected: false, lock_owner: 4242 } })).toEqual(["d200_locked"]);
  });

  it("keys dismissal on content that does not tick with the clock", () => {
    const outage = { servers: { box: { connected: false, since: NOW - 60_000, ever_connected: true } } };
    const a = healthProblems(outage, NOW)[0];
    const b = healthProblems(outage, NOW + 120_000)[0];
    expect(a.key).toBe(b.key);
    expect(a.content).toBe(b.content);
    const next = healthProblems({ servers: { box: { connected: false, since: NOW + 1, ever_connected: true } } }, NOW + 60_000)[0];
    expect(next.key).toBe(a.key);
    expect(next.content).not.toBe(a.content);
  });

  it("keeps raw backend text out of the sentence (it goes to title=)", () => {
    const [p] = healthProblems({ config_error: "bridge token for server 'local' not found" }, NOW);
    expect(text(p)).not.toContain("not found");
    expect(p.detail).toBe("bridge token for server 'local' not found");
  });
});

describe("problem sentences (en + cs)", () => {
  it("reads like a human sentence in both languages", () => {
    const [down] = healthProblems({ servers: { local: { connected: false, since: NOW - 3 * 60_000, ever_connected: true } } }, NOW);
    expect(text(down, "en")).toBe("Bridge local is disconnected (3 min)");
    expect(text(down, "cs")).toBe("Bridge local je odpojený (3 min)");
    const [mismatch] = healthProblems({ version: "0.10.0", app_version: "0.10.1" }, NOW);
    expect(text(mismatch, "cs")).toBe("Runtime 0.10.0 se liší od aplikace 0.10.1 — restartuj runtime");
    expect(text(mismatch, "en")).toBe("Runtime 0.10.0 differs from the app 0.10.1 — restart the runtime");
    const [token] = healthProblems({ servers: { t3: { connected: false, since: NOW - 60_000, last_error: "token rejected" } } }, NOW);
    expect(text(token, "cs")).toBe("Bridge t3 odmítl token (1 min)");
  });
});

describe("configErrorSection", () => {
  it("sends Fix config… to the section the error most likely lives in", () => {
    expect(configErrorSection("bridge token for server 'local' not found")).toBe("servers");
    expect(configErrorSection("invalid grid 'wide'")).toBe("deck");
    expect(configErrorSection("[view].language must be en or cs")).toBe("view");
    expect(configErrorSection("something odd")).toBe("maintenance");
  });
});

describe("bySeverity", () => {
  it("is stable within one severity", () => {
    const items = [
      { severity: "info" as const, n: 1 },
      { severity: "error" as const, n: 2 },
      { severity: "info" as const, n: 3 },
      { severity: "warning" as const, n: 4 },
      { severity: "error" as const, n: 5 },
    ];
    expect(bySeverity(items).map((i) => i.n)).toEqual([2, 5, 4, 1, 3]);
  });
});

describe("maintenanceBadge", () => {
  it("counts real problems (not info facts) and takes the worst colour", () => {
    const problems = healthProblems({
      version: "0.9.1",
      app_version: "0.9.0",
      servers: { newer: { bridge_version: "0.9.2" }, box: { connected: false, since: 0, ever_connected: true } },
    }, NOW);
    expect(maintenanceBadge(problems)).toEqual({ count: 2, severity: "warning" });
    expect(maintenanceBadge(healthProblems({ config_error: "x", version: "1", app_version: "2" }, NOW)))
      .toEqual({ count: 2, severity: "error" });
    expect(maintenanceBadge(healthProblems({ version: "0.9.1", servers: { n: { bridge_version: "0.9.2" } } }, NOW))).toBeNull();
    expect(maintenanceBadge([])).toBeNull();
  });
});

describe("notice dismissals", () => {
  afterEach(() => localStorage.clear());

  it("hide a problem until its content changes, persisted in localStorage", () => {
    const p = { key: "bridge_link:box", content: "down|1" };
    let d = readDismissals();
    expect(isDismissed(d, p)).toBe(false);
    d = dismiss(d, p);
    writeDismissals(d);
    expect(isDismissed(readDismissals(), p)).toBe(true);
    expect(isDismissed(readDismissals(), { ...p, content: "down|2" })).toBe(false);
  });

  it("forget dismissals of problems that are gone", () => {
    const d = { a: "1", b: "2" };
    expect(pruneDismissals(d, ["a"])).toEqual({ a: "1" });
    expect(pruneDismissals(d, ["a", "b"])).toBe(d);
  });

  it("survive broken or throwing storage", () => {
    localStorage.setItem(DISMISSALS_KEY, "{not json");
    expect(readDismissals()).toEqual({});
    const throwing = { getItem: () => { throw new Error("denied"); }, setItem: () => { throw new Error("full"); } };
    expect(readDismissals(throwing)).toEqual({});
    expect(() => writeDismissals({ a: "1" }, throwing)).not.toThrow();
  });
});
