import { afterEach, describe, expect, it } from "vitest";
import { flushSync, mount, tick, unmount } from "svelte";
import StatisticsSection from "./StatisticsSection.svelte";
import { setLang } from "../i18n.svelte";

const H = 3_600_000;
const M = 60_000;

function report(group: string, range: number) {
  return {
    ok: true,
    code: "ok",
    servers: ["m4"],
    missing: [],
    range_days: range,
    group_by: group,
    truncated: false,
    total: {
      working_ms: 5 * H, blocked_ms: 40 * M, idle_ms: H, waiting_ms: 0, done_ms: 0,
      blocked_count: 4, answered_count: 3, done_count: 2, answer_median_ms: 5 * M, answer_p90_ms: 20 * M,
    },
    groups: [
      { key: "herdeck", label: "herdeck", working_ms: 4 * H, blocked_ms: 30 * M, blocked_count: 3, answered_count: 2, done_count: 2, answer_median_ms: 5 * M, answer_p90_ms: 20 * M },
      { key: "api", label: "api", working_ms: H, blocked_ms: 10 * M, blocked_count: 1, answered_count: 1 },
    ],
    days: Array.from({ length: range }, (_, i) => ({
      day: new Date(Date.UTC(2026, 8, 24 - range + 1 + i)).toISOString().slice(0, 10),
      working_ms: H * (i + 1),
      blocked_ms: 10 * M,
    })),
  };
}

type Invoke = (cmd: string, args?: Record<string, unknown>) => Promise<unknown>;

function defaultAnswer(path: string): unknown {
  const q = new URLSearchParams(path.split("?")[1]);
  return { status: 200, body: report(q.get("group") ?? "agent", Number(q.get("range") ?? 7)) };
}

function fake(answer: (path: string) => unknown = defaultAnswer) {
  const paths: string[] = [];
  const invoke: Invoke = async (cmd, args) => {
    if (cmd !== "maintenance_call") throw new Error(`unexpected ${cmd}`);
    const path = String(args?.path);
    paths.push(path);
    return answer(path);
  };
  return { invoke, paths };
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

async function render(invoke: Invoke | null, lang: "en" | "cs" = "en") {
  cleanup?.();
  setLang(lang);
  const target = document.createElement("div");
  document.body.appendChild(target);
  const instance = mount(StatisticsSection, { target, props: { invoke } });
  cleanup = () => { unmount(instance); target.remove(); };
  await settle();
  return target;
}

describe("StatisticsSection", () => {
  it("loads the default range and shows the summary, chart and table", async () => {
    const { invoke, paths } = fake();
    const t = await render(invoke);
    expect(paths).toEqual(["/stats?range=7&group=agent"]);
    const metric = (name: string) => t.querySelector(`[data-metric="${name}"] strong`)?.textContent;
    expect(metric("waited")).toBe("40m");
    expect(metric("answers")).toBe("3");
    expect(metric("median")).toBe("5m");
    expect(metric("done")).toBe("2");
    expect(t.querySelector('[data-metric="median"] small')?.textContent).toBe("p90 20m");
    expect(t.querySelectorAll("[data-chart] g[data-day]")).toHaveLength(7);
    expect(t.querySelectorAll("[data-groups] tbody tr")).toHaveLength(2);
    expect(t.querySelector('[data-group="herdeck"] td')?.textContent).toBe("herdeck");
  });

  it("re-queries when the range or grouping changes", async () => {
    const { invoke, paths } = fake();
    const t = await render(invoke);
    t.querySelector<HTMLButtonElement>('button[data-range="30"]')!.click();
    await settle();
    const select = t.querySelector<HTMLSelectElement>("[data-group-select]")!;
    select.value = "repo";
    select.dispatchEvent(new Event("change"));
    await settle();
    expect(paths).toEqual(["/stats?range=7&group=agent", "/stats?range=30&group=agent", "/stats?range=30&group=repo"]);
    expect(t.querySelectorAll("[data-chart] g[data-day]")).toHaveLength(30);
    t.querySelector<HTMLButtonElement>('button[data-action="refresh"]')!.click();
    await settle();
    expect(paths).toHaveLength(4);
  });

  it("explains an old bridge, an error and a missing runtime in both languages", async () => {
    const old = fake(() => ({ status: 200, body: { ok: false, code: "unsupported", missing: [] } }));
    let t = await render(old.invoke);
    expect(t.querySelector("[data-stats-unavailable]")?.textContent).toContain("Update the bridges");
    t = await render(old.invoke, "cs");
    expect(t.querySelector("[data-stats-unavailable]")?.textContent).toContain("Aktualizuj bridge");
    t = await render(fake(() => ({ status: 500, body: null })).invoke);
    expect(t.querySelector("[data-stats-error]")?.textContent).toContain("HTTP 500");
    t = await render(null, "cs");
    expect(t.textContent).toContain("běžící runtime");
  });

  it("gives every control a tooltip in English and Czech", async () => {
    for (const lang of ["en", "cs"] as const) {
      const t = await render(fake().invoke, lang);
      const titled = [
        t.querySelector(".range"),
        t.querySelector(".group-select"),
        t.querySelector('button[data-action="refresh"]'),
        ...t.querySelectorAll(".metric"),
      ];
      for (const el of titled) expect(el?.getAttribute("title")?.length ?? 0).toBeGreaterThan(10);
      if (lang === "cs") expect(t.querySelector('[data-metric="waited"] span')?.textContent).toBe("Agenti na tebe čekali");
    }
  });

  it("reports bridges left out of the merge", async () => {
    const { invoke } = fake(() => ({ status: 200, body: { ...report("agent", 7), missing: [{ server_id: "mb", reason: "timeout" }] } }));
    const t = await render(invoke);
    expect(t.querySelector("[data-missing]")?.textContent).toContain("mb (no answer)");
  });
});
