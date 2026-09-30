import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it, vi } from "vitest";
import {
  FALLBACK_TARGET,
  SHARED_NOTIFICATION_KEYS,
  SHARED_USAGE_KEYS,
  SHARED_WHOLE_SECTIONS,
  composeShared,
  defaultTarget,
  extractShared,
  keepSaved,
  parseBridges,
  putBridgeSettings,
  resolveTarget,
  saveDrafts,
  splitShared,
  stableJson,
  targetIds,
  targetMode,
} from "./bridgeSettings";
import { parseConfig, type ConfigPayload } from "./configClient";

// The TS key lists mirror src/herdeck/shared_settings.py. Read the Python
// tuples so a key added on one side only fails here (the bridge rejects any
// key it does not know, so a drifted list would break adoption).
function pythonTuple(name: string): string[] {
  const src = readFileSync(resolve(__dirname, "../../../src/herdeck/shared_settings.py"), "utf8");
  const m = src.match(new RegExp(`^${name} = \\(([^)]*)\\)`, "m"));
  if (!m) throw new Error(`${name} not found in shared_settings.py`);
  return [...m[1].matchAll(/"([^"]+)"/g)].map((x) => x[1]);
}

function bridge(over: Record<string, unknown> = {}) {
  return {
    offered: true, connected: true, revision: 3, updated_at_ms: 1, updated_by: "mac",
    set: true, source: "bridge",
    settings: { notifications: { on: ["done"], done_min_work: 5, done_short_delay: 0, remind_after: 0, subagents_done: false }, safety: { approve_always: false, require_confirm_for: [] } },
    ...over,
  };
}

function payload(extra: Record<string, unknown> = {}): ConfigPayload {
  const p = parseConfig({
    base: {
      notifications: { enabled: true, on: ["blocked"], remind_after: 7 },
      usage: { providers: ["claude"], alert_at: [80] },
      macros: [{ label: "go", text: "continue" }],
      view: { language: "en" },
    },
    profiles: { night: { notifications: { on: ["done"], sound: false }, safety: { approve_always: true } } },
    local: {},
    secrets: {},
    ...extra,
  });
  if (!p) throw new Error("bad payload");
  return p;
}

describe("shared key lists", () => {
  it("mirror src/herdeck/shared_settings.py exactly", () => {
    expect([...SHARED_NOTIFICATION_KEYS]).toEqual(pythonTuple("SHARED_NOTIFICATION_KEYS"));
    expect([...SHARED_USAGE_KEYS]).toEqual(pythonTuple("SHARED_USAGE_KEYS"));
    expect([...SHARED_WHOLE_SECTIONS]).toEqual(pythonTuple("SHARED_WHOLE_SECTIONS"));
  });
});

describe("parseConfig bridges", () => {
  it("parses bridges and shared_overlay_ignored, defaulting to empty", () => {
    const p = payload({ bridges: { m4: bridge() }, shared_overlay_ignored: ["m4", 3] });
    expect(p.bridges.m4).toMatchObject({ offered: true, connected: true, revision: 3, set: true, source: "bridge" });
    expect(p.sharedOverlayIgnored).toEqual(["m4"]);
    const bare = payload();
    expect(bare.bridges).toEqual({});
    expect(bare.sharedOverlayIgnored).toEqual([]);
  });

  it("treats a set bridge without a document as unset", () => {
    expect(parseBridges({ m4: bridge({ settings: null }) }).m4.set).toBe(false);
  });
});

describe("extractShared", () => {
  it("keeps only shared keys that are present (like extract_shared)", () => {
    expect(extractShared(payload().base)).toEqual({
      notifications: { on: ["blocked"], remind_after: 7 },
      usage: { alert_at: [80] },
      macros: [{ label: "go", text: "continue" }],
    });
  });
});

describe("compose / split", () => {
  it("shows the bridge document in base and hides profile overlays of shared keys", () => {
    const settings = parseBridges({ m4: bridge() }).m4.settings!;
    const view = composeShared(payload(), settings);
    expect(view.base.notifications).toEqual({ enabled: true, on: ["done"], remind_after: 0, done_min_work: 5, done_short_delay: 0, subagents_done: false });
    expect(view.base.usage).toEqual({ providers: ["claude"] });
    expect(view.base.macros).toBeUndefined();
    expect(view.base.safety).toEqual({ approve_always: false, require_confirm_for: [] });
    expect(view.profiles.night).toEqual({ notifications: { sound: false } });
  });

  it("splits an edit back into the untouched local shared keys + the bridge document", () => {
    const original = payload();
    const settings = parseBridges({ m4: bridge() }).m4.settings!;
    const view = composeShared(original, settings);
    const edited: ConfigPayload = {
      ...view,
      base: { ...view.base, notifications: { ...(view.base.notifications as object), enabled: false, done_min_work: 9 } },
    };
    const { payload: local, shared } = splitShared(original, edited);
    expect(local.base.notifications).toEqual({ enabled: false, on: ["blocked"], remind_after: 7 });
    expect(local.base.macros).toEqual([{ label: "go", text: "continue" }]);
    expect(local.base.safety).toBeUndefined();
    expect(local.profiles.night).toEqual({ notifications: { on: ["done"], sound: false }, safety: { approve_always: true } });
    expect((shared.notifications as Record<string, unknown>).done_min_work).toBe(9);
    expect(shared.safety).toEqual({ approve_always: false, require_confirm_for: [] });
    expect(stableJson(splitShared(original, view).payload)).toBe(stableJson(original));
  });
});

describe("targets", () => {
  const bridges = parseBridges({
    old: { offered: false, connected: true, revision: 0, set: false, source: "none", settings: null },
    unset: bridge({ set: false, revision: 0, settings: null, source: "none" }),
    gone: bridge({ connected: false, offered: false, source: "cache" }),
    m4: bridge(),
    mb: bridge({ revision: 9 }),
  });

  it("lists connected bridges offering settings plus adopted offline ones", () => {
    expect(targetIds(bridges)).toEqual(["unset", "gone", "m4", "mb"]);
  });

  it("defaults to the first adopted connected bridge, else This Mac", () => {
    expect(defaultTarget(bridges)).toBe("m4");
    expect(defaultTarget(parseBridges({ unset: bridge({ set: false, settings: null }) }))).toBe(FALLBACK_TARGET);
    expect(resolveTarget("mb", bridges)).toBe("mb");
    expect(resolveTarget(FALLBACK_TARGET, bridges)).toBe(FALLBACK_TARGET);
    expect(resolveTarget("old", bridges)).toBe("m4");
    expect(resolveTarget(null, bridges)).toBe("m4");
  });

  it("classifies the target", () => {
    expect(targetMode(bridges, FALLBACK_TARGET)).toBe("fallback");
    expect(targetMode(bridges, "unset")).toBe("unset");
    expect(targetMode(bridges, "gone")).toBe("offline");
    expect(targetMode(bridges, "m4")).toBe("adopted");
    expect(targetMode(bridges, "nope")).toBe("fallback");
  });
});

describe("saving", () => {
  it("maps HTTP statuses to outcomes and never throws", async () => {
    const put = vi.fn()
      .mockResolvedValueOnce({ status: 200, body: { ok: true, revision: 4 } })
      .mockResolvedValueOnce({ status: 409, body: { ok: false, error: "stale_revision", messages: [], revision: 5 } })
      .mockResolvedValueOnce({ status: 422, body: { ok: false, error: "invalid", messages: ["macros[0].label must be a non-empty string"] } })
      .mockRejectedValueOnce(new Error("sidecar not ready"));
    expect(await putBridgeSettings(put, "m4", 3, {})).toMatchObject({ ok: true, revision: 4, error: null });
    expect(await putBridgeSettings(put, "m4", 3, {})).toMatchObject({ ok: false, status: 409, error: "stale_revision" });
    expect(await putBridgeSettings(put, "m4", 3, {})).toMatchObject({ error: "invalid", messages: ["macros[0].label must be a non-empty string"] });
    expect(await putBridgeSettings(put, "m4", 3, {})).toMatchObject({ ok: false, status: 0, error: "unreachable" });
    expect(put).toHaveBeenCalledWith("m4", { base_revision: 3, settings: {} });
  });

  it("apply to all: one put per adopted bridge, each with its own revision", async () => {
    const bridges = parseBridges({ m4: bridge({ revision: 3 }), mb: bridge({ revision: 9 }), gone: bridge({ connected: false }) });
    const put = vi.fn(async (id: string) => (id === "mb"
      ? { status: 409, body: { ok: false, error: "stale_revision" } }
      : { status: 200, body: { ok: true, revision: 4 } }));
    const doc = { safety: { approve_always: true, require_confirm_for: [] } };
    const results = await saveDrafts(put, bridges, { m4: { baseRevision: 3, settings: doc } }, "m4");
    expect(put.mock.calls).toEqual([
      ["m4", { base_revision: 3, settings: doc }],
      ["mb", { base_revision: 9, settings: doc }],
    ]);
    expect(results.map((r) => [r.serverId, r.ok, r.error])).toEqual([["m4", true, null], ["mb", false, "stale_revision"]]);
  });

  it("without apply to all only the drafted bridge is written", async () => {
    const bridges = parseBridges({ m4: bridge(), mb: bridge({ revision: 9 }) });
    const put = vi.fn(async () => ({ status: 200, body: { ok: true, revision: 4 } }));
    await saveDrafts(put, bridges, { mb: { baseRevision: 9, settings: {} } }, null);
    expect(put.mock.calls).toEqual([["mb", { base_revision: 9, settings: {} }]]);
  });
});

describe("keepSaved", () => {
  const doc = { safety: { approve_always: true, require_confirm_for: [] } };
  const ok = (id: string, revision: number) => ({ serverId: id, ok: true, status: 200, revision, error: null, messages: [], sent: doc });

  it("never goes back to a revision older than a save just made", () => {
    const stale = parseBridges({ m4: bridge({ revision: 3 }), fresh: bridge({ set: false, revision: 0, settings: null, source: "none" }) });
    const out = keepSaved(stale, [ok("m4", 4), ok("fresh", 1)]);
    expect(out.m4).toMatchObject({ revision: 4, settings: doc, set: true, source: "bridge" });
    expect(out.fresh).toMatchObject({ revision: 1, settings: doc, set: true });
    expect(stale.m4.revision, "input untouched").toBe(3);
  });

  it("keeps a re-read that already caught up, and ignores failed puts", () => {
    const caught = parseBridges({ m4: bridge({ revision: 5 }) });
    expect(keepSaved(caught, [ok("m4", 4)]).m4.revision).toBe(5);
    const failed = { ...ok("m4", 9), ok: false, error: "invalid" as const };
    expect(keepSaved(caught, [failed]).m4.revision).toBe(5);
  });
});
