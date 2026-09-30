import { afterEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import { setLang } from "../i18n.svelte";
import { FIELD_HELP } from "../help";
import { FALLBACK_TARGET, parseBridges, type PutOutcome } from "../bridgeSettings";
import SharedTarget from "./SharedTarget.svelte";

function bridge(over: Record<string, unknown> = {}) {
  return {
    offered: true, connected: true, revision: 3, updated_at_ms: 1, updated_by: "mac", set: true, source: "bridge",
    settings: { safety: { approve_always: true, require_confirm_for: [] } },
    ...over,
  };
}

const BRIDGES = parseBridges({
  old: { offered: false, connected: true, revision: 0, set: false, source: "none", settings: null },
  fresh: bridge({ set: false, revision: 0, settings: null, source: "none" }),
  gone: bridge({ connected: false, offered: false, source: "cache" }),
  m4: bridge(),
});

let cleanup: (() => void) | null = null;
afterEach(() => {
  cleanup?.();
  cleanup = null;
  setLang("en");
});

function render(props: Record<string, unknown> = {}) {
  const target = document.createElement("div");
  document.body.appendChild(target);
  const instance = mount(SharedTarget, {
    target,
    props: {
      bridges: BRIDGES,
      target: "m4",
      onTarget: () => {},
      applyAll: false,
      onApplyAll: () => {},
      editingProfile: false,
      overlayIgnored: [],
      results: [],
      baseConfig: {},
      put: null,
      onAdopted: () => {},
      ...props,
    },
  });
  flushSync();
  cleanup = () => {
    unmount(instance);
    target.remove();
  };
  return target;
}

const text = (el: Element) => el.textContent?.replace(/\s+/g, " ").trim() ?? "";

describe("SharedTarget picker", () => {
  it("lists connected bridges offering settings, adopted offline ones and This Mac (fallback)", () => {
    const target = render();
    const select = target.querySelector<HTMLSelectElement>("select[data-shared-target-select]")!;
    const options = Array.from(select.options).map((o) => [o.value, o.textContent?.trim()]);
    expect(options.map(([v]) => v)).toEqual([FALLBACK_TARGET, "fresh", "gone", "m4"]);
    expect(options[0][1]).toBe("This Mac (fallback)");
    expect(select.value).toBe("m4");
  });

  it("reports a picked target", () => {
    const onTarget = vi.fn();
    const target = render({ onTarget });
    const select = target.querySelector<HTMLSelectElement>("select[data-shared-target-select]")!;
    select.value = FALLBACK_TARGET;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    expect(onTarget).toHaveBeenCalledWith(FALLBACK_TARGET);
  });

  it("labels the picker with a translated help tooltip in both languages", () => {
    for (const lang of ["en", "cs"] as const) {
      setLang(lang);
      const target = render();
      const label = target.querySelector<HTMLElement>(".fieldlabel")!;
      expect(label.title).toBe(FIELD_HELP[lang].shared.target);
      cleanup?.();
      cleanup = null;
    }
  });
});

describe("SharedTarget adoption", () => {
  it("offers adoption for an unset bridge and posts base_revision 0 with the shared part of the base config", async () => {
    const put = vi.fn(async () => ({ status: 200, body: { ok: true, revision: 1 } }));
    const onAdopted = vi.fn();
    const base = {
      notifications: { enabled: false, on: ["done"], done_min_work: 4 },
      usage: { providers: ["claude"], alert_at: [90] },
      macros: [{ label: "go", text: "continue" }],
      view: { language: "en" },
    };
    const target = render({ target: "fresh", put, onAdopted, baseConfig: base });
    const button = target.querySelector<HTMLButtonElement>("[data-action='adopt']")!;
    expect(text(button)).toBe("Move these settings to bridge fresh");
    button.click();
    await vi.waitFor(() => expect(onAdopted).toHaveBeenCalled());
    expect(put).toHaveBeenCalledWith("fresh", {
      base_revision: 0,
      settings: {
        notifications: { on: ["done"], done_min_work: 4 },
        usage: { alert_at: [90] },
        macros: [{ label: "go", text: "continue" }],
      },
    });
    expect(onAdopted.mock.calls[0][0]).toMatchObject({ serverId: "fresh", ok: true });
  });

  it("shows a failed adoption", async () => {
    const put = vi.fn(async () => ({ status: 409, body: { ok: false, error: "stale_revision", revision: 2 } }));
    const target = render({ target: "fresh", put });
    target.querySelector<HTMLButtonElement>("[data-action='adopt']")!.click();
    await vi.waitFor(() => expect(target.querySelector("[data-adopt-error]")).not.toBeNull());
    expect(text(target.querySelector("[data-adopt-error]")!)).toContain("changed elsewhere");
  });

  it("does not offer adoption for an adopted or fallback target", () => {
    expect(render().querySelector("[data-action='adopt']")).toBeNull();
    cleanup?.();
    expect(render({ target: FALLBACK_TARGET }).querySelector("[data-action='adopt']")).toBeNull();
  });
});

describe("SharedTarget status", () => {
  it("an offline target shows the last-known notice", () => {
    const target = render({ target: "gone" });
    expect(text(target.querySelector("[data-shared-offline]")!)).toBe("Bridge offline — showing last known settings");
  });

  it("explains hidden profile overlays on an adopted target, and warns when the active profile's are ignored", () => {
    let target = render({ editingProfile: true });
    expect(target.querySelector("[data-overlay-hidden]")).not.toBeNull();
    expect(target.querySelector("[data-overlay-ignored]")).toBeNull();
    cleanup?.();
    target = render({ overlayIgnored: ["m4"] });
    expect(text(target.querySelector("[data-overlay-ignored]")!)).toContain("m4");
    cleanup?.();
    target = render({ target: FALLBACK_TARGET, editingProfile: true });
    expect(target.querySelector("[data-overlay-hidden]")).toBeNull();
  });

  it("lists which fields follow the target in a mixed section", () => {
    const target = render({ sharedKeys: ["alert_at", "alert_reset"] });
    expect(text(target.querySelector("[data-shared-keys]")!)).toContain("alert_at, alert_reset");
  });
});

describe("SharedTarget apply to all", () => {
  it("is offered only with at least two adopted connected bridges", () => {
    expect(render().querySelector("[data-action='apply-all']")).toBeNull();
    cleanup?.();
    const two = parseBridges({ m4: bridge(), mb: bridge({ revision: 9 }) });
    const onApplyAll = vi.fn();
    const target = render({ bridges: two, onApplyAll });
    const box = target.querySelector<HTMLInputElement>("[data-action='apply-all']")!;
    box.checked = true;
    box.dispatchEvent(new Event("change", { bubbles: true }));
    expect(onApplyAll).toHaveBeenCalledWith(true);
  });

  it("lists per-bridge results, a stale one as changed elsewhere and reloaded", () => {
    const results: PutOutcome[] = [
      { serverId: "m4", ok: true, status: 200, revision: 4, error: null, messages: [], sent: {} },
      { serverId: "mb", ok: false, status: 409, revision: 10, error: "stale_revision", messages: [], sent: {} },
      { serverId: "x", ok: false, status: 422, revision: null, error: "invalid", messages: ["macros[0].label must be a non-empty string"], sent: {} },
    ];
    const target = render({ results });
    const rows = Array.from(target.querySelectorAll("[data-shared-results] li")).map(text);
    expect(rows).toEqual([
      "m4: saved",
      "mb: changed elsewhere — reloaded",
      "x: rejected: macros[0].label must be a non-empty string",
    ]);
  });

  it("speaks Czech", () => {
    setLang("cs");
    const target = render({ target: "gone", results: [{ serverId: "mb", ok: false, status: 409, revision: 1, error: "stale_revision", messages: [], sent: {} }] });
    expect(text(target.querySelector("[data-shared-offline]")!)).not.toContain("Bridge offline");
    expect(text(target.querySelector("[data-shared-results] li")!)).toBe("mb: změněno jinde — načteno znovu");
    expect(target.querySelector<HTMLSelectElement>("select")!.options[0].textContent?.trim()).toBe("Tento Mac (záloha)");
  });
});
