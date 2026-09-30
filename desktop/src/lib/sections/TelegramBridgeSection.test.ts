import { afterEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import { setLang } from "../i18n.svelte";
import { FIELD_HELP } from "../help";
import { parseBridges } from "../bridgeSettings";
import { reactiveProps } from "../testProps.svelte";
import TelegramBridgeSection from "./TelegramBridgeSection.svelte";

const SECRET = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";

function tgBridge(over: Record<string, unknown> = {}, tg: Record<string, unknown> = {}) {
  return {
    offered: true, connected: true, revision: 0, set: false, source: "none", settings: null,
    telegram: {
      offered: true, revision: 3,
      settings: {
        enabled: true, chat_id: "-100123", message_thread_id: 7, interactive: true,
        allowed_user_ids: [42, 43], prompt_max_chars: 900, only_when_away: 5, language: "cs", sound: false,
      },
      status: {
        token: "file", active: true, inbound: "ok", last_error: null, last_sent_at_ms: 1_700_000_000_000,
        recent_chats: [
          { chat_id: "-100999", title: "Ops", type: "supergroup", message_thread_id: 12, topic_name: "Alerts" },
          { chat_id: "555", title: "Me", type: "private", message_thread_id: null, topic_name: null },
        ],
      },
      ...tg,
    },
    ...over,
  };
}

const BRIDGES = () => parseBridges({
  m4: tgBridge(),
  old: { offered: true, connected: true, revision: 1, set: true, source: "bridge", settings: {} },
  fresh: tgBridge({}, { revision: 0, settings: null, status: { token: null, active: false, inbound: "off", last_error: null, last_sent_at_ms: null, recent_chats: [] } }),
});

type Res = { status: number; body: Record<string, unknown> };
const ok = (revision = 4): Res => ({ status: 200, body: { ok: true, revision } });

let cleanup: (() => void) | null = null;
afterEach(() => {
  cleanup?.();
  cleanup = null;
  setLang("en");
});

function render(props: Record<string, unknown> = {}) {
  const target = document.createElement("div");
  document.body.appendChild(target);
  const call = "call" in props ? props.call : vi.fn(async () => ok());
  const instance = mount(TelegramBridgeSection, {
    target,
    props: { bridges: BRIDGES(), localTelegram: null, call, onReload: async () => {}, initialTarget: "m4", ...props, call },
  });
  flushSync();
  cleanup = () => { unmount(instance); target.remove(); };
  return { target, call: call as ReturnType<typeof vi.fn> };
}

const text = (el: Element | null) => el?.textContent?.replace(/\s+/g, " ").trim() ?? "";
function field(root: HTMLElement, key: string): HTMLInputElement | HTMLSelectElement {
  const label = Array.from(root.querySelectorAll<HTMLElement>(".fieldlabel")).find((l) => text(l).includes(key));
  if (!label) throw new Error(`no field ${key}`);
  return label.closest("label")!.querySelector("input, select") as HTMLInputElement | HTMLSelectElement;
}
async function type(el: HTMLInputElement | HTMLSelectElement, value: string) {
  el.value = value;
  el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
  flushSync();
}
const q = <T extends HTMLElement>(root: HTMLElement, sel: string) => root.querySelector<T>(sel)!;
const tick = () => new Promise((r) => setTimeout(r, 0));

describe("target picker", () => {
  it("lists This Mac and only bridges that offer telegram_config", () => {
    const { target } = render();
    const opts = Array.from(q<HTMLSelectElement>(target, "select[data-tg-target]").options).map((o) => o.value);
    expect(opts).toEqual(["", "fresh", "m4"]);
  });

  it("This Mac shows no bridge form", () => {
    const { target } = render({ initialTarget: "" });
    expect(target.querySelector("[data-tg-form]")).toBeNull();
    expect(text(target)).toContain("This Mac");
  });

  it("renders nothing when no bridge offers Telegram config", () => {
    const { target } = render({ bridges: parseBridges({ old: { offered: true, connected: true, revision: 1, set: true, source: "bridge", settings: {} } }) });
    expect(target.querySelector("[data-tg-section]")).toBeNull();
  });
});

describe("bridge form", () => {
  it("shows the bridge's settings", () => {
    const { target } = render();
    expect((field(target, "chat_id") as HTMLInputElement).value).toBe("-100123");
    expect((field(target, "message_thread_id") as HTMLInputElement).value).toBe("7");
    expect((field(target, "allowed_user_ids") as HTMLInputElement).value).toBe("42, 43");
    expect((field(target, "prompt_max_chars") as HTMLInputElement).value).toBe("900");
    expect((field(target, "only_when_away") as HTMLInputElement).value).toBe("5");
    expect((field(target, "language") as HTMLSelectElement).value).toBe("cs");
  });

  it("uses defaults when the bridge has no document", () => {
    const { target } = render({ initialTarget: "fresh" });
    expect((field(target, "chat_id") as HTMLInputElement).value).toBe("");
    expect((field(target, "prompt_max_chars") as HTMLInputElement).value).toBe("1200");
    expect((field(target, "language") as HTMLSelectElement).value).toBe("en");
    expect((field(target, "enabled") as HTMLInputElement).checked).toBe(false);
    expect((field(target, "sound") as HTMLInputElement).checked).toBe(true);
  });

  it("saves the edited document against the current revision", async () => {
    const call = vi.fn(async () => ok(4));
    const { target } = render({ call });
    await type(field(target, "chat_id"), "-100555");
    await type(field(target, "allowed_user_ids"), "1, 2");
    q<HTMLButtonElement>(target, "[data-action='tg-save']").click();
    await vi.waitFor(() => expect(call).toHaveBeenCalled());
    expect(call).toHaveBeenCalledWith("m4", "", {
      base_revision: 3,
      settings: {
        enabled: true, chat_id: "-100555", message_thread_id: 7, interactive: true,
        allowed_user_ids: [1, 2], prompt_max_chars: 900, only_when_away: 5, language: "cs", sound: false,
      },
    });
  });

  it("omits an empty forum topic and refuses a non-numeric allow-list without calling", async () => {
    const call = vi.fn(async () => ok());
    const { target } = render({ call, initialTarget: "fresh" });
    q<HTMLButtonElement>(target, "[data-action='tg-save']").click();
    await vi.waitFor(() => expect(call).toHaveBeenCalled());
    await tick();
    const sent = (call.mock.calls[0] as unknown[])[2] as { base_revision: number; settings: Record<string, unknown> };
    expect(sent.base_revision).toBe(0);
    expect("message_thread_id" in sent.settings).toBe(false);
    call.mockClear();
    await type(field(target, "allowed_user_ids"), "1, abc");
    q<HTMLButtonElement>(target, "[data-action='tg-save']").click();
    await tick();
    expect(call).not.toHaveBeenCalled();
    expect(q(target, "[data-tg-message]").textContent).toContain("allowed_user_ids");
  });

  it("409 reloads the config and says it changed elsewhere", async () => {
    const call = vi.fn(async () => ({ status: 409, body: { ok: false, error: "stale_revision" } }));
    const onReload = vi.fn(async () => {});
    const { target } = render({ call, onReload });
    q<HTMLButtonElement>(target, "[data-action='tg-save']").click();
    await vi.waitFor(() => expect(onReload).toHaveBeenCalled());
    expect(text(q(target, "[data-tg-message]"))).toContain("Changed elsewhere");
  });

  it("422 shows the bridge's messages", async () => {
    const call = vi.fn(async () => ({ status: 422, body: { ok: false, error: "invalid", messages: ["interactive requires a non-empty allowed_user_ids"] } }));
    const { target } = render({ call });
    q<HTMLButtonElement>(target, "[data-action='tg-save']").click();
    await vi.waitFor(() => expect(text(q(target, "[data-tg-message]"))).toContain("interactive requires"));
  });

  it("after a save the next save is based on the returned revision", async () => {
    const call = vi.fn(async () => ok(9));
    const { target } = render({ call });
    q<HTMLButtonElement>(target, "[data-action='tg-save']").click();
    await vi.waitFor(() => expect(call).toHaveBeenCalledTimes(1));
    await tick();
    q<HTMLButtonElement>(target, "[data-action='tg-save']").click();
    await vi.waitFor(() => expect(call).toHaveBeenCalledTimes(2));
    expect(((call.mock.calls[1] as unknown[])[2] as { base_revision: number }).base_revision).toBe(9);
  });
});

describe("token row", () => {
  it("shows status only, never a value, with a password input for Set", async () => {
    const { target } = render();
    const row = q(target, "[data-tg-token]");
    expect(text(row)).toContain("set (file)");
    expect(row.querySelector("input")).toBeNull();
    q<HTMLButtonElement>(target, "[data-action='tg-token-set']").click();
    flushSync();
    const input = q<HTMLInputElement>(target, "[data-tg-token-input]");
    expect(input.type).toBe("password");
    expect(input.value).toBe("");
  });

  it("says not set when there is no token", () => {
    const { target } = render({ initialTarget: "fresh" });
    expect(text(q(target, "[data-tg-token]"))).toContain("not set");
    expect(q<HTMLButtonElement>(target, "[data-action='tg-token-clear']").disabled).toBe(true);
  });

  it("Set posts the token to /token and never keeps it", async () => {
    const call = vi.fn(async () => ({ status: 200, body: { ok: true } }));
    const { target } = render({ call });
    q<HTMLButtonElement>(target, "[data-action='tg-token-set']").click();
    flushSync();
    await type(q<HTMLInputElement>(target, "[data-tg-token-input]"), SECRET);
    q<HTMLButtonElement>(target, "[data-action='tg-token-save']").click();
    await vi.waitFor(() => expect(call).toHaveBeenCalledWith("m4", "token", { action: "set", token: SECRET }));
    await tick();
    expect(target.innerHTML).not.toContain(SECRET);
    expect(target.querySelector("[data-tg-token-input]")).toBeNull();
  });

  it("Set reports an invalid token", async () => {
    const call = vi.fn(async () => ({ status: 422, body: { ok: false, error: "invalid" } }));
    const { target } = render({ call });
    q<HTMLButtonElement>(target, "[data-action='tg-token-set']").click();
    flushSync();
    await type(q<HTMLInputElement>(target, "[data-tg-token-input]"), "nope");
    q<HTMLButtonElement>(target, "[data-action='tg-token-save']").click();
    await vi.waitFor(() => expect(text(q(target, "[data-tg-message]"))).toContain("not a valid bot token"));
  });

  it("Clear posts action clear", async () => {
    const call = vi.fn(async () => ({ status: 200, body: { ok: true } }));
    const { target } = render({ call });
    q<HTMLButtonElement>(target, "[data-action='tg-token-clear']").click();
    await vi.waitFor(() => expect(call).toHaveBeenCalledWith("m4", "token", { action: "clear" }));
  });

  it("an env token cannot be cleared or replaced here and says why", () => {
    const bridges = parseBridges({ m4: tgBridge({}, { status: { token: "env", active: true, inbound: "ok", last_error: null, last_sent_at_ms: null, recent_chats: [] } }) });
    const { target } = render({ bridges });
    expect(text(q(target, "[data-tg-token]"))).toContain("set (env)");
    const clear = q<HTMLButtonElement>(target, "[data-action='tg-token-clear']");
    expect(clear.disabled).toBe(true);
    expect(clear.title).toContain("HERDECK_TELEGRAM_TOKEN");
    expect(q<HTMLButtonElement>(target, "[data-action='tg-token-set']").disabled).toBe(true);
  });

  it("env_locked from the server is explained", async () => {
    const call = vi.fn(async () => ({ status: 422, body: { ok: false, error: "env_locked" } }));
    const { target } = render({ call });
    q<HTMLButtonElement>(target, "[data-action='tg-token-clear']").click();
    await vi.waitFor(() => expect(text(q(target, "[data-tg-message]"))).toContain("environment"));
  });
});

describe("status line", () => {
  it("shows active, inbound, last sent and last error", () => {
    const bridges = parseBridges({ m4: tgBridge({}, { status: { token: "file", active: true, inbound: "disabled", last_error: "HTTP 429", last_sent_at_ms: 1_700_000_000_000, recent_chats: [] } }) });
    const { target } = render({ bridges });
    const s = text(q(target, "[data-tg-status]"));
    expect(s).toContain("active");
    expect(s).toContain("disabled");
    expect(s).toContain("HTTP 429");
    expect(q(target, "[data-tg-last-sent]").textContent).not.toBe("");
  });

  it("shows inactive and no error", () => {
    const { target } = render({ initialTarget: "fresh" });
    expect(text(q(target, "[data-tg-status]"))).toContain("inactive");
    expect(target.querySelector("[data-tg-last-error]")).toBeNull();
  });
});

describe("recent chats", () => {
  it("clicking a chat fills chat_id and message_thread_id", async () => {
    const { target } = render();
    const chats = target.querySelectorAll<HTMLButtonElement>("[data-tg-chat]");
    expect(chats.length).toBe(2);
    chats[0].click();
    flushSync();
    expect((field(target, "chat_id") as HTMLInputElement).value).toBe("-100999");
    expect((field(target, "message_thread_id") as HTMLInputElement).value).toBe("12");
    chats[1].click();
    flushSync();
    expect((field(target, "chat_id") as HTMLInputElement).value).toBe("555");
    expect((field(target, "message_thread_id") as HTMLInputElement).value).toBe("");
  });

  it("explains an empty list", () => {
    const { target } = render({ initialTarget: "fresh" });
    expect(target.querySelector("[data-tg-chat]")).toBeNull();
    expect(text(q(target, "[data-tg-chats-empty]"))).toContain("Write to the bot");
  });

  it("tells how a bot in privacy mode sees a group topic, in en and cs", () => {
    for (const lang of ["en", "cs"] as const) {
      setLang(lang);
      const { target } = render({ initialTarget: "fresh" });
      const hint = text(q(target, "[data-tg-chats-empty]"));
      expect(hint).toContain("/start@");
      expect(hint).toContain("/setprivacy");
      expect(hint).toContain("admin");
      cleanup?.();
      cleanup = null;
    }
  });
});

describe("test button", () => {
  it("calls /test and shows success", async () => {
    const call = vi.fn(async () => ({ status: 200, body: { ok: true } }));
    const { target } = render({ call });
    q<HTMLButtonElement>(target, "[data-action='tg-test']").click();
    await vi.waitFor(() => expect(call).toHaveBeenCalledWith("m4", "test", {}));
    await vi.waitFor(() => expect(text(q(target, "[data-tg-test-result]"))).toContain("sent"));
  });

  it("shows the failure text", async () => {
    const call = vi.fn(async () => ({ status: 200, body: { ok: false, error: "chat not found" } }));
    const { target } = render({ call });
    q<HTMLButtonElement>(target, "[data-action='tg-test']").click();
    await vi.waitFor(() => expect(text(q(target, "[data-tg-test-result]"))).toContain("chat not found"));
  });

  it("maps 503 to bridge offline", async () => {
    const call = vi.fn(async () => ({ status: 503, body: { ok: false, error: "disconnected" } }));
    const { target } = render({ call });
    q<HTMLButtonElement>(target, "[data-action='tg-test']").click();
    await vi.waitFor(() => expect(text(q(target, "[data-tg-test-result]"))).toContain("not connected"));
  });
});

describe("move from this Mac", () => {
  const LOCAL = { token_env: "TG_TOKEN", chat_id: "-1001", message_thread_id: 4, interactive: true, allowed_user_ids: [9], prompt_max_chars: 800, only_when_away: 10 };

  it("is offered only with a local Telegram config", () => {
    expect(render({ localTelegram: null }).target.querySelector("[data-action='tg-move']")).toBeNull();
    cleanup?.();
    expect(render({ localTelegram: LOCAL }).target.querySelector("[data-action='tg-move']")).not.toBeNull();
  });

  it("puts the local fields (base = current revision) then sets the token from local", async () => {
    const call = vi.fn(async (_id: string, sub: string) => (sub === "" ? ok(4) : { status: 200, body: { ok: true } }));
    const { target } = render({ call, localTelegram: LOCAL });
    q<HTMLButtonElement>(target, "[data-action='tg-move']").click();
    await vi.waitFor(() => expect(call).toHaveBeenCalledTimes(2));
    expect(call.mock.calls[0]).toEqual(["m4", "", {
      base_revision: 3,
      settings: {
        enabled: true, chat_id: "-1001", message_thread_id: 4, interactive: true,
        allowed_user_ids: [9], prompt_max_chars: 800, only_when_away: 10, language: "cs", sound: false,
      },
    }]);
    expect(call.mock.calls[1]).toEqual(["m4", "token", { action: "set", from_local: true }]);
    await vi.waitFor(() => expect(target.querySelector("[data-tg-moved]")).not.toBeNull());
    expect(text(q(target, "[data-tg-moved]"))).toContain("stops sending Telegram");
  });

  it("uses base revision 0 for an unset bridge", async () => {
    const call = vi.fn(async (_id: string, sub: string) => (sub === "" ? ok(1) : { status: 200, body: { ok: true } }));
    const { target } = render({ call, localTelegram: LOCAL, initialTarget: "fresh" });
    q<HTMLButtonElement>(target, "[data-action='tg-move']").click();
    await vi.waitFor(() => expect(call).toHaveBeenCalled());
    expect((call.mock.calls[0] as unknown[])[2]).toMatchObject({ base_revision: 0 });
  });

  it("no_local_token tells the user to enter the token on the bridge", async () => {
    const call = vi.fn(async (_id: string, sub: string) => (sub === "" ? ok(4) : { status: 422, body: { ok: false, error: "no_local_token" } }));
    const { target } = render({ call, localTelegram: LOCAL });
    q<HTMLButtonElement>(target, "[data-action='tg-move']").click();
    await vi.waitFor(() => expect(text(q(target, "[data-tg-message]"))).toContain("enter the token on the bridge"));
    expect(target.querySelector("[data-tg-moved]")).toBeNull();
  });

  it("a failed put does not send the token and 409 reloads", async () => {
    const call = vi.fn(async () => ({ status: 409, body: { ok: false, error: "stale_revision" } }));
    const onReload = vi.fn(async () => {});
    const { target } = render({ call, localTelegram: LOCAL, onReload });
    q<HTMLButtonElement>(target, "[data-action='tg-move']").click();
    await vi.waitFor(() => expect(onReload).toHaveBeenCalled());
    expect(call).toHaveBeenCalledTimes(1);
  });
});

describe("browser preview / language", () => {
  it("without a transport the actions are disabled", () => {
    const { target } = render({ call: null });
    expect(q<HTMLButtonElement>(target, "[data-action='tg-save']").disabled).toBe(true);
    expect(q<HTMLButtonElement>(target, "[data-action='tg-test']").disabled).toBe(true);
  });

  it("labels carry the catalog tooltip in en and cs and the UI is translated", () => {
    for (const lang of ["en", "cs"] as const) {
      setLang(lang);
      const { target } = render();
      const help = FIELD_HELP[lang].telegram_bridge;
      for (const key of ["chat_id", "message_thread_id", "interactive", "allowed_user_ids", "prompt_max_chars", "only_when_away", "language", "sound", "enabled"]) {
        const label = Array.from(target.querySelectorAll<HTMLElement>(".fieldlabel")).find((l) => text(l).includes(key))!;
        expect(label.title, `${lang}/${key}`).toBe(help[key]);
      }
      expect(text(q(target, "[data-action='tg-save']"))).toBe(lang === "en" ? "Save Telegram settings" : "Uložit nastavení Telegramu");
      cleanup?.();
      cleanup = null;
    }
  });
});

describe("refreshing the bridge's status", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("Refresh re-reads the config and has a tooltip in en and cs", async () => {
    for (const lang of ["en", "cs"] as const) {
      setLang(lang);
      const onReload = vi.fn(async () => {});
      const { target } = render({ onReload });
      const button = q<HTMLButtonElement>(target, "[data-action='tg-refresh']");
      expect(text(button)).toBe(lang === "en" ? "Refresh" : "Obnovit");
      expect(button.title).toContain(lang === "en" ? "status" : "stav");
      button.click();
      await vi.waitFor(() => expect(onReload).toHaveBeenCalledTimes(1));
      cleanup?.();
      cleanup = null;
    }
  });

  it("a finished test re-reads the status (last sent / last error)", async () => {
    const onReload = vi.fn(async () => {});
    const call = vi.fn(async () => ({ status: 200, body: { ok: false, error: "chat not found" } }));
    const { target } = render({ call, onReload });
    q<HTMLButtonElement>(target, "[data-action='tg-test']").click();
    await vi.waitFor(() => expect(text(q(target, "[data-tg-test-result]"))).toContain("chat not found"));
    await vi.waitFor(() => expect(onReload).toHaveBeenCalled());
  });

  it("polls every 3 s while a bridge is shown, stops for This Mac and on unmount", async () => {
    vi.useFakeTimers();
    const onReload = vi.fn(async () => {});
    const { target } = render({ onReload });
    await vi.advanceTimersByTimeAsync(2_900);
    expect(onReload).toHaveBeenCalledTimes(0);
    await vi.advanceTimersByTimeAsync(200);
    expect(onReload).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(3_000);
    expect(onReload).toHaveBeenCalledTimes(2);
    await type(q<HTMLSelectElement>(target, "select[data-tg-target]"), "");
    await vi.advanceTimersByTimeAsync(10_000);
    expect(onReload).toHaveBeenCalledTimes(2);
    await type(q<HTMLSelectElement>(target, "select[data-tg-target]"), "fresh");
    await vi.advanceTimersByTimeAsync(3_000);
    expect(onReload).toHaveBeenCalledTimes(3);
    cleanup?.();
    cleanup = null;
    await vi.advanceTimersByTimeAsync(10_000);
    expect(onReload).toHaveBeenCalledTimes(3);
  });

  it("a slow reload is not overlapped by the next poll", async () => {
    vi.useFakeTimers();
    let release: () => void = () => {};
    const onReload = vi.fn(() => new Promise<void>((r) => { release = r; }));
    render({ onReload });
    await vi.advanceTimersByTimeAsync(9_000);
    expect(onReload).toHaveBeenCalledTimes(1);
    release();
    await vi.advanceTimersByTimeAsync(3_000);
    expect(onReload).toHaveBeenCalledTimes(2);
  });

  it("the delayed re-read after a token change does not fire after unmount", async () => {
    vi.useFakeTimers();
    const onReload = vi.fn(async () => {});
    const call = vi.fn(async () => ({ status: 200, body: { ok: true } }));
    const { target } = render({ call, onReload, initialTarget: "m4" });
    q<HTMLButtonElement>(target, "[data-action='tg-token-clear']").click();
    await vi.advanceTimersByTimeAsync(0);
    expect(call).toHaveBeenCalledWith("m4", "token", { action: "clear" });
    const before = onReload.mock.calls.length;
    cleanup?.();
    cleanup = null;
    await vi.advanceTimersByTimeAsync(5_000);
    expect(onReload).toHaveBeenCalledTimes(before);
  });
});

describe("a bridge flap while it is shown", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  const DOWN = () => parseBridges({
    m4: tgBridge({ connected: false }, { offered: false, revision: 0, settings: null, status: null }),
    fresh: tgBridge(),
  });

  function mountReactive(extra: Record<string, unknown> = {}) {
    const target = document.createElement("div");
    document.body.appendChild(target);
    const props = reactiveProps({
      bridges: BRIDGES(), localTelegram: null, call: vi.fn(async () => ok()),
      onReload: vi.fn(async () => {}), onPoll: vi.fn(async () => {}), initialTarget: "m4", ...extra,
    });
    const instance = mount(TelegramBridgeSection, { target, props });
    flushSync();
    cleanup = () => { unmount(instance); target.remove(); };
    return { target, props };
  }

  it("keeps the bridge picked, keeps polling and keeps unsaved edits", async () => {
    vi.useFakeTimers();
    const { target, props } = mountReactive();
    const onPoll = props.onPoll as ReturnType<typeof vi.fn>;
    await type(field(target, "chat_id"), "-555");
    props.bridges = DOWN();
    flushSync();
    expect(q<HTMLSelectElement>(target, "select[data-tg-target]").value).toBe("m4");
    expect(target.querySelector("[data-tg-this-mac]")).toBeNull();
    expect(text(q(target, "[data-tg-disconnected]"))).toContain("m4");
    expect(target.querySelector("[data-action='tg-refresh']")).not.toBeNull();
    await vi.advanceTimersByTimeAsync(3_000);
    expect(onPoll).toHaveBeenCalledTimes(1);
    props.bridges = BRIDGES(); // back, same revision
    flushSync();
    expect(target.querySelector("[data-tg-disconnected]")).toBeNull();
    expect(field(target, "chat_id").value).toBe("-555");
    await vi.advanceTimersByTimeAsync(3_000);
    expect(onPoll).toHaveBeenCalledTimes(2);
    expect(props.onReload).not.toHaveBeenCalled(); // polls are the quiet read
  });

  it("the whole section stays when the only bridge flaps", () => {
    const { target, props } = mountReactive({
      bridges: parseBridges({ m4: tgBridge() }),
    });
    props.bridges = parseBridges({ m4: tgBridge({ connected: false }, { offered: false }) });
    flushSync();
    expect(target.querySelector("[data-tg-disconnected]")).not.toBeNull();
  });

  it("a new revision from the bridge still reseeds the form", async () => {
    const { target, props } = mountReactive();
    await type(field(target, "chat_id"), "-555");
    const next = BRIDGES();
    next.m4.telegram = { ...next.m4.telegram!, revision: 9, settings: { ...next.m4.telegram!.settings!, chat_id: "-100777" } };
    props.bridges = next;
    flushSync();
    expect(field(target, "chat_id").value).toBe("-100777");
  });

  it("an explicit reload during a poll runs after it, never alongside", async () => {
    let release: () => void = () => {};
    const onPoll = vi.fn(() => new Promise<void>((r) => { release = r; }));
    vi.useFakeTimers();
    const { target, props } = mountReactive({ onPoll });
    await vi.advanceTimersByTimeAsync(3_000);
    expect(onPoll).toHaveBeenCalledTimes(1);
    q<HTMLButtonElement>(target, "[data-action='tg-save']").click();
    await vi.advanceTimersByTimeAsync(0);
    expect(props.call).toHaveBeenCalled();
    expect(props.onReload).not.toHaveBeenCalled();
    release();
    await vi.advanceTimersByTimeAsync(0);
    expect(props.onReload).toHaveBeenCalledTimes(1);
  });
});
