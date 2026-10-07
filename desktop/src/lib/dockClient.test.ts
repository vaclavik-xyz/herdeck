import { describe, expect, it, vi } from "vitest";
import {
  DOCK_POLL_MS,
  DOCK_RETRY_MS,
  dockNextDelay,
  dockTransport,
  parseDockAgent,
  parseDockList,
  projectIconUrl,
} from "./dockClient";

const ROW = {
  server_id: "studio",
  pane_id: "p1",
  agent_type: "claude",
  title: "Česká bankovní API",
  repo: "macdoktor-crm",
  workspace: "macDoktor CRM",
  branch: "feat/bank",
  tab: "",
  status: "blocked",
  since_s: 260,
  waiting_on: "",
  subagents: 0,
  offline: false,
  icon: "0123456789abcdef",
  monogram: "M",
};

describe("parseDockAgent", () => {
  it("maps a full row", () => {
    expect(parseDockAgent(ROW)).toEqual({
      serverId: "studio",
      paneId: "p1",
      agentType: "claude",
      title: "Česká bankovní API",
      repo: "macdoktor-crm",
      workspace: "macDoktor CRM",
      branch: "feat/bank",
      tab: "",
      status: "blocked",
      sinceS: 260,
      waitingOn: "",
      subagents: 0,
      offline: false,
      icon: "0123456789abcdef",
      monogram: "M",
    });
  });

  it("rejects malformed entries", () => {
    expect(parseDockAgent(null)).toBeNull();
    expect(parseDockAgent("x")).toBeNull();
    expect(parseDockAgent({ ...ROW, pane_id: "" })).toBeNull();
    expect(parseDockAgent({ ...ROW, server_id: 3 })).toBeNull();
    expect(parseDockAgent({ ...ROW, status: "sleeping" })).toBeNull();
    expect(parseDockAgent({ ...ROW, since_s: -1 })).toBeNull();
    expect(parseDockAgent({ ...ROW, since_s: "260" })).toBeNull();
    expect(parseDockAgent({ ...ROW, since_s: Number.NaN })).toBeNull();
  });

  it("keeps only a well-formed icon hash and a short monogram", () => {
    expect(parseDockAgent({ ...ROW, icon: "../config" })?.icon).toBeNull();
    expect(parseDockAgent({ ...ROW, icon: "0123456789ABCDEF" })?.icon).toBeNull();
    expect(parseDockAgent({ ...ROW, icon: null })?.icon).toBeNull();
    expect(parseDockAgent({ ...ROW, monogram: "" })?.monogram).toBe("?");
    expect(parseDockAgent({ ...ROW, monogram: "ABC" })?.monogram).toBe("A");
    expect(parseDockAgent({ ...ROW, subagents: "x", offline: "yes" })).toMatchObject({
      subagents: 0,
      offline: false,
    });
  });
});

describe("parseDockList", () => {
  it("parses the envelope and drops only the bad rows", () => {
    const list = parseDockList({
      version: 42,
      language: "cs",
      connected: true,
      agents: [ROW, { ...ROW, status: "nope" }, { ...ROW, pane_id: "p2" }],
    });
    expect(list?.version).toBe(42);
    expect(list?.language).toBe("cs");
    expect(list?.connected).toBe(true);
    expect(list?.agents.map((a) => a.paneId)).toEqual(["p1", "p2"]);
  });

  it("coerces an unknown language to en and rejects a broken envelope", () => {
    expect(parseDockList({ version: 1, language: "de", connected: false, agents: [] })?.language).toBe("en");
    expect(parseDockList(null)).toBeNull();
    expect(parseDockList({ version: "1", agents: [] })).toBeNull();
    expect(parseDockList({ version: -1, agents: [] })).toBeNull();
    expect(parseDockList({ version: 1, agents: {} })).toBeNull();
  });
});

describe("projectIconUrl", () => {
  it("builds a scheme URL for a hash and nothing else", () => {
    expect(projectIconUrl("herdeck://localhost", "0123456789abcdef")).toBe(
      "herdeck://localhost/project-icon/0123456789abcdef",
    );
    expect(projectIconUrl("http://herdeck.localhost", "0123456789abcdef")).toBe(
      "http://herdeck.localhost/project-icon/0123456789abcdef",
    );
    expect(projectIconUrl("herdeck://localhost", "../panel")).toBeNull();
  });
});

describe("dockNextDelay", () => {
  it("retries slowly after a failure", () => {
    expect(dockNextDelay({ failed: true, longPolled: true, changed: false, elapsedMs: 5 })).toBe(DOCK_RETRY_MS);
  });
  it("starts long-polling right after the first plain poll", () => {
    expect(dockNextDelay({ failed: false, longPolled: false, changed: true, elapsedMs: 5 })).toBe(0);
  });
  it("falls back to the retry cadence when the runtime did not hold the request", () => {
    expect(dockNextDelay({ failed: false, longPolled: true, changed: false, elapsedMs: 10 })).toBe(DOCK_RETRY_MS);
  });
  it("re-arms at once after a change or a quiet timeout, with a small floor", () => {
    expect(dockNextDelay({ failed: false, longPolled: true, changed: true, elapsedMs: 10 })).toBe(40);
    expect(dockNextDelay({ failed: false, longPolled: true, changed: false, elapsedMs: 20000 })).toBe(0);
  });
});

describe("dockTransport", () => {
  it("polls plainly first, through agent_call", async () => {
    const invoke = vi.fn().mockResolvedValue({ status: 200, body: { version: 1, language: "en", connected: true, agents: [ROW] } });
    const list = await dockTransport(invoke, "herdeck://localhost").list(null, DOCK_POLL_MS);
    expect(invoke).toHaveBeenCalledWith("agent_call", { method: "GET", path: "/agent/list" });
    expect(list?.agents).toHaveLength(1);
  });

  it("long-polls within the agent_call proxy cap", async () => {
    // agent_card.rs AGENT_WAIT_MAX_MS = 20000; the proxy times out at wait + 6 s.
    expect(DOCK_POLL_MS).toBeLessThanOrEqual(20000);
    const invoke = vi.fn().mockResolvedValue({ status: 200, body: { version: 2, language: "en", connected: true, agents: [] } });
    await dockTransport(invoke, "herdeck://localhost").list(7, DOCK_POLL_MS);
    expect(invoke).toHaveBeenCalledWith("agent_call", {
      method: "GET",
      path: `/agent/list?after=7&wait_ms=${DOCK_POLL_MS}`,
      waitMs: DOCK_POLL_MS,
    });
  });

  it("answers null for an HTTP error, a broken body or a failed invoke", async () => {
    const t404 = dockTransport(vi.fn().mockResolvedValue({ status: 404, body: null }), "x");
    expect(await t404.list(null, 0)).toBeNull();
    const tBad = dockTransport(vi.fn().mockResolvedValue({ status: 200, body: "nope" }), "x");
    expect(await tBad.list(null, 0)).toBeNull();
    const tErr = dockTransport(vi.fn().mockRejectedValue(new Error("down")), "x");
    expect(await tErr.list(null, 0)).toBeNull();
  });

  it("builds icon URLs on the given scheme base", () => {
    const t = dockTransport(vi.fn(), "herdeck://localhost");
    expect(t.iconUrl("0123456789abcdef")).toBe("herdeck://localhost/project-icon/0123456789abcdef");
    expect(t.iconUrl("bad")).toBeNull();
  });
});
