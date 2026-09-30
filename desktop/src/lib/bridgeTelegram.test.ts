import { describe, expect, it } from "vitest";
import { parseBridges } from "./bridgeSettings";
import {
  TELEGRAM_DEFAULTS, telegramIds, callTelegram, localToBridgeSettings, parseAllowedUsers, tokenErrorKind,
} from "./bridgeTelegram";

const tg = (over: Record<string, unknown> = {}) => ({ offered: true, revision: 2, settings: null, status: { token: null, active: false, inbound: "off", last_error: null, last_sent_at_ms: null, recent_chats: [] }, ...over });

describe("parseBridges telegram", () => {
  it("parses the telegram block and leaves bridges without one alone", () => {
    const b = parseBridges({
      a: { offered: false, connected: true, revision: 0, set: false, source: "none", settings: null, telegram: tg({ settings: { chat_id: "1" }, status: { token: "env", active: true, inbound: "ok", last_error: "x", last_sent_at_ms: 5, recent_chats: [{ chat_id: "9", title: "T", type: "group", message_thread_id: 3, topic_name: "n" }, { bad: 1 }] } }) },
      b: { offered: true, connected: true, revision: 1, set: true, source: "bridge", settings: {} },
    });
    expect(b.a.telegram).toMatchObject({ offered: true, revision: 2, settings: { chat_id: "1" } });
    expect(b.a.telegram!.status.token).toBe("env");
    expect(b.a.telegram!.status.recent_chats).toEqual([{ chat_id: "9", title: "T", type: "group", message_thread_id: 3, topic_name: "n" }]);
    expect("telegram" in b.b).toBe(false);
  });
  it("telegramIds lists connected offering bridges", () => {
    const b = parseBridges({
      a: { offered: false, connected: true, telegram: tg() },
      b: { offered: false, connected: false, telegram: tg() },
      c: { offered: false, connected: true, telegram: tg({ offered: false }) },
    });
    expect(telegramIds(b)).toEqual(["a"]);
  });
});

describe("helpers", () => {
  it("defaults mirror TelegramSettings", () => {
    expect(TELEGRAM_DEFAULTS).toEqual({ enabled: false, chat_id: "", message_thread_id: null, interactive: false, allowed_user_ids: [], prompt_max_chars: 1200, only_when_away: 0, language: "en", sound: true });
  });
  it("parseAllowedUsers", () => {
    expect(parseAllowedUsers("")).toEqual([]);
    expect(parseAllowedUsers("1, 2")).toEqual([1, 2]);
    expect(parseAllowedUsers("1, x")).toBeNull();
    expect(parseAllowedUsers("0")).toBeNull();
  });
  it("localToBridgeSettings maps only matching fields and keeps the bridge's others", () => {
    expect(localToBridgeSettings({ token_env: "T", chat_id: "5", message_thread_id: 2, interactive: true, allowed_user_ids: [1], prompt_max_chars: 500, only_when_away: 3 }, { ...TELEGRAM_DEFAULTS, language: "cs" }))
      .toEqual({ enabled: true, chat_id: "5", message_thread_id: 2, interactive: true, allowed_user_ids: [1], prompt_max_chars: 500, only_when_away: 3, language: "cs", sound: true });
    expect(localToBridgeSettings({ chat_id: "5" }, TELEGRAM_DEFAULTS)).toEqual({ enabled: true, chat_id: "5", interactive: false, allowed_user_ids: [], prompt_max_chars: 1200, only_when_away: 0, language: "en", sound: true });
  });
  it("tokenErrorKind", () => {
    expect(tokenErrorKind({ status: 422, body: { error: "env_locked" } })).toBe("env_locked");
    expect(tokenErrorKind({ status: 422, body: { error: "weird" } })).toBe("failed");
    expect(tokenErrorKind({ status: 503, body: {} })).toBe("offline");
    expect(tokenErrorKind({ status: 504, body: {} })).toBe("timeout");
    expect(tokenErrorKind({ status: 200, body: { ok: true } })).toBeNull();
  });
  it("callTelegram never throws", async () => {
    expect(await callTelegram(async () => { throw new Error("x"); }, "m4", "", {})).toMatchObject({ status: 0 });
    expect(await callTelegram(async () => ({ status: 200, body: { ok: true } }), "m4", "", {})).toEqual({ status: 200, body: { ok: true } });
  });
});
