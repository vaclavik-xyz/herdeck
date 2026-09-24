import { describe, it, expect } from "vitest";
import {
  agentCallTransport,
  formatDuration,
  formatSince,
  parseDetail,
  parseOutcome,
  subagentIndent,
} from "./agentCardClient";

describe("subagents", () => {
  it("parses rows, drops malformed ones and clamps durations", () => {
    const d = parseDetail({
      server_id: "prod",
      pane_id: "p0",
      subagents: [
        { id: "a", provider: "claude", type: "Explore", description: "x", model: "", depth: 2, status: "running", duration_s: 12.7 },
        { id: "b", status: "done", duration_s: -5, depth: "1" },
        { id: "c", status: "weird", duration_s: 1 },
        { status: "done" },
        null,
      ],
    });
    expect(d?.subagents).toEqual([
      { id: "a", provider: "claude", type: "Explore", description: "x", model: "", depth: 2, status: "running", durationS: 12 },
      { id: "b", provider: "", type: "", description: "", model: "", depth: null, status: "done", durationS: 0 },
    ]);
    expect(parseDetail({ server_id: "s", pane_id: "p" })?.subagents).toEqual([]);
  });

  it("formats durations and indents by depth", () => {
    expect(formatDuration(0)).toBe("0s");
    expect(formatDuration(59.9)).toBe("59s");
    expect(formatDuration(65)).toBe("1m 05s");
    expect(formatDuration(3600 + 5 * 60)).toBe("1h 05m");
    expect(formatDuration(-3)).toBe("0s");
    expect([null, 0, 1, 2, 3, 9].map(subagentIndent)).toEqual([0, 0, 0, 1, 2, 3]);
  });
});

describe("parseDetail", () => {
  it("maps the runtime's snake_case detail and drops malformed options", () => {
    const d = parseDetail({
      server_id: "prod",
      pane_id: "p0",
      agent_type: "claude",
      status: "blocked",
      since_s: 12,
      connected: true,
      prompt: "1. Yes",
      revision: "r",
      options: [{ key: "1", label: "Yes", id: "approve", kind: "option", confirm: false }, { label: "no key" }, null],
      can_stop: true,
      stop_confirm: true,
      can_text: true,
      can_focus: true,
    });
    expect(d?.serverId).toBe("prod");
    expect(d?.sinceS).toBe(12);
    expect(d?.options).toEqual([{ key: "1", label: "Yes", id: "approve", kind: "option", confirm: false }]);
    expect(d?.canStop && d.stopConfirm && d.canText && d.canFocus).toBe(true);
  });

  it("rejects a body without an agent identity", () => {
    expect(parseDetail({ pane_id: "p0" })).toBeNull();
    expect(parseDetail(null)).toBeNull();
  });
});

describe("parseOutcome", () => {
  it("keeps the runtime's outcome and maps transport statuses", () => {
    expect(parseOutcome(200, { ok: false, code: "readonly", message: "m" })).toEqual({
      ok: false, code: "readonly", message: "m",
    });
    expect(parseOutcome(404, null).code).toBe("gone");
    expect(parseOutcome(403, null).code).toBe("forbidden");
    expect(parseOutcome(400, null).code).toBe("invalid");
    expect(parseOutcome(500, null)).toEqual({ ok: false, code: "http", message: "500" });
  });
});

describe("formatSince", () => {
  it("uses the tile buckets", () => {
    expect(formatSince(null)).toBe("");
    expect(formatSince(42)).toBe("42s");
    expect(formatSince(300)).toBe("5m");
    expect(formatSince(7300)).toBe("2h");
  });
});

describe("agentCallTransport", () => {
  it("relays through agent_call with the route and never a token", async () => {
    const calls: [string, Record<string, unknown> | undefined][] = [];
    const invoke = async (cmd: string, args?: Record<string, unknown>) => {
      calls.push([cmd, args]);
      if (args?.method === "GET") {
        return { status: 200, body: { server_id: "prod", pane_id: "w 1", status: "idle" } };
      }
      return { status: 200, body: { ok: true, code: "sent", message: "" } };
    };
    const t = agentCallTransport(invoke);
    const r = await t.detail({ index: 3 }, true);
    expect(r.kind).toBe("ok");
    await t.detail({ serverId: "prod", paneId: "w 1" });
    const out = await t.act("text", { serverId: "prod", paneId: "w 1" }, { text: "hi" });
    expect(out.ok).toBe(true);
    expect(calls).toEqual([
      ["agent_call", { method: "GET", path: "/agent/detail?index=3&refresh=1", body: undefined }],
      ["agent_call", { method: "GET", path: "/agent/detail?server_id=prod&pane_id=w+1", body: undefined }],
      ["agent_call", {
        method: "POST",
        path: "/agent/text",
        body: { server_id: "prod", pane_id: "w 1", text: "hi" },
      }],
    ]);
    expect(JSON.stringify(calls)).not.toContain("token");
  });

  it("opens, long-polls (asking the proxy to hold) and closes a terminal session", async () => {
    const calls: Record<string, unknown>[] = [];
    const invoke = async (_cmd: string, args?: Record<string, unknown>) => {
      calls.push(args ?? {});
      if (args?.path === "/agent/term/open") return { status: 200, body: { ok: true, code: "open", id: "s1" } };
      if (String(args?.path).startsWith("/agent/term/poll")) {
        return {
          status: 200,
          body: { frames: [{ seq: 3, full: true, cols: 90, rows: 20, data: "aGk=" }, { bad: 1 }], next: 4, closed: null, gap: false },
        };
      }
      return { status: 200, body: { ok: true, code: "closed", message: "" } };
    };
    const t = agentCallTransport(invoke);
    expect(await t.termOpen({ serverId: "prod", paneId: "p0" }, 90, 20)).toEqual({ ok: true, id: "s1" });
    const polled = await t.termPoll("s1", 3, 12000);
    expect(polled).toEqual({
      kind: "frames",
      frames: [{ seq: 3, full: true, cols: 90, rows: 20, data: "aGk=" }],
      next: 4,
      closed: null,
      gap: false,
    });
    await t.termClose("s1");
    expect(calls[0]).toEqual({
      method: "POST", path: "/agent/term/open", body: { server_id: "prod", pane_id: "p0", cols: 90, rows: 20 },
    });
    expect(calls[1]).toEqual({
      method: "GET", path: "/agent/term/poll?id=s1&after=3&wait_ms=12000", body: undefined, waitMs: 12000,
    });
    expect(calls[2]).toEqual({ method: "POST", path: "/agent/term/close", body: { id: "s1" } });
    const refused = agentCallTransport(async () => ({ status: 200, body: { ok: false, code: "disconnected", message: "" } }));
    expect(await refused.termOpen({ serverId: "a", paneId: "b" }, 80, 24)).toEqual({
      ok: false, outcome: { ok: false, code: "disconnected", message: "" },
    });
    const gone = agentCallTransport(async () => ({ status: 404, body: null }));
    expect(await gone.termPoll("x", 0, 0)).toEqual({ kind: "gone" });
  });

  it("turns a 404 into 'gone' and a proxy failure into 'unreachable'", async () => {
    const gone = agentCallTransport(async () => ({ status: 404, body: null }));
    expect(await gone.detail({ index: 1 })).toEqual({ kind: "gone" });
    const broken = agentCallTransport(async () => {
      throw new Error("no runtime");
    });
    expect((await broken.detail({ index: 1 })).kind).toBe("error");
    expect((await broken.act("stop", { serverId: "a", paneId: "b" })).code).toBe("unreachable");
  });
});
