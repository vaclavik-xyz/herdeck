// Telegram alerts sent BY the bridge (spec T9): the editor's model of one
// bridge's Telegram document, its status and the three calls that change it
// (`POST /bridge-telegram/<id>[/token|/test]`, via the Tauri shell).
//
// Framework-free. The bot token is never read here: the browser only ever
// SENDS one (typed by the user, or "from_local" resolved by the runtime) and
// gets back a status flag ("env" | "file" | null).
import type { BridgeShared } from "./bridgeSettings";

type Rec = Record<string, unknown>;

function rec(v: unknown): Rec {
  return v != null && typeof v === "object" && !Array.isArray(v) ? (v as Rec) : {};
}

/** Mirrors bridge_telegram.TelegramSettings (defaults and ranges). */
export interface TelegramDoc {
  enabled: boolean;
  chat_id: string;
  message_thread_id: number | null;
  interactive: boolean;
  allowed_user_ids: number[];
  prompt_max_chars: number;
  only_when_away: number;
  language: string;
  sound: boolean;
}

export const TELEGRAM_DEFAULTS: TelegramDoc = {
  enabled: false, chat_id: "", message_thread_id: null, interactive: false, allowed_user_ids: [],
  prompt_max_chars: 1200, only_when_away: 0, language: "en", sound: true,
};
export const TELEGRAM_LANGUAGES = ["en", "cs"] as const;

export interface RecentChat {
  chat_id: string;
  title: string;
  type: string;
  message_thread_id: number | null;
  topic_name: string | null;
}

export interface TelegramStatus {
  token: "env" | "file" | null;
  active: boolean;
  inbound: "off" | "ok" | "disabled";
  last_error: string | null;
  last_sent_at_ms: number | null;
  recent_chats: RecentChat[];
}

export interface BridgeTelegram {
  offered: boolean;
  /** 0 = never set. */
  revision: number;
  settings: Rec | null;
  status: TelegramStatus;
}

function parseChat(v: unknown): RecentChat | null {
  const c = rec(v);
  if (typeof c.chat_id !== "string" || c.chat_id === "") return null;
  return {
    chat_id: c.chat_id,
    title: typeof c.title === "string" ? c.title : "",
    type: typeof c.type === "string" ? c.type : "",
    message_thread_id: typeof c.message_thread_id === "number" ? c.message_thread_id : null,
    topic_name: typeof c.topic_name === "string" ? c.topic_name : null,
  };
}

/** One bridge's `telegram` block of `GET /config` `bridges[id]`. */
export function parseBridgeTelegram(raw: unknown): BridgeTelegram {
  const v = rec(raw);
  const s = rec(v.status);
  const settings = v.settings != null && typeof v.settings === "object" && !Array.isArray(v.settings) ? (v.settings as Rec) : null;
  return {
    offered: v.offered === true,
    revision: typeof v.revision === "number" ? v.revision : 0,
    settings,
    status: {
      token: s.token === "env" || s.token === "file" ? s.token : null,
      active: s.active === true,
      inbound: s.inbound === "ok" || s.inbound === "disabled" ? s.inbound : "off",
      last_error: typeof s.last_error === "string" && s.last_error !== "" ? s.last_error : null,
      last_sent_at_ms: typeof s.last_sent_at_ms === "number" ? s.last_sent_at_ms : null,
      recent_chats: Array.isArray(s.recent_chats) ? s.recent_chats.map(parseChat).filter((c): c is RecentChat => c != null) : [],
    },
  };
}

/** Connected bridges that offer `telegram_config` (the picker's entries). */
export function telegramIds(bridges: Record<string, BridgeShared>): string[] {
  return Object.entries(bridges).filter(([, b]) => b.connected && b.telegram?.offered === true).map(([id]) => id).sort();
}

/** The document as the form edits it: stored values over the defaults. */
export function docFrom(settings: Rec | null): TelegramDoc {
  const s = settings ?? {};
  const d = TELEGRAM_DEFAULTS;
  return {
    enabled: typeof s.enabled === "boolean" ? s.enabled : d.enabled,
    chat_id: typeof s.chat_id === "string" ? s.chat_id : d.chat_id,
    message_thread_id: typeof s.message_thread_id === "number" ? s.message_thread_id : null,
    interactive: typeof s.interactive === "boolean" ? s.interactive : d.interactive,
    allowed_user_ids: Array.isArray(s.allowed_user_ids) ? s.allowed_user_ids.filter((n): n is number => typeof n === "number") : [],
    prompt_max_chars: typeof s.prompt_max_chars === "number" ? s.prompt_max_chars : d.prompt_max_chars,
    only_when_away: typeof s.only_when_away === "number" ? s.only_when_away : d.only_when_away,
    language: typeof s.language === "string" ? s.language : d.language,
    sound: typeof s.sound === "boolean" ? s.sound : d.sound,
  };
}

/** What goes over the wire: `message_thread_id` is left out when unset
 *  (TOML has no null; the bridge's own writer does the same). */
export function docToWire(doc: TelegramDoc): Rec {
  const out: Rec = { ...doc, allowed_user_ids: [...doc.allowed_user_ids] };
  if (doc.message_thread_id == null) delete out.message_thread_id;
  return out;
}

/** "1, 2" -> [1, 2]; "" -> []; anything not a list of positive integers -> null. */
export function parseAllowedUsers(raw: string): number[] | null {
  if (raw.trim() === "") return [];
  const parts = raw.split(",").map((p) => p.trim());
  if (!parts.every((p) => /^[1-9]\d*$/.test(p))) return null;
  const ids = parts.map(Number);
  return ids.every(Number.isSafeInteger) ? ids : null;
}

/** This Mac's `[notifications.telegram]` as a bridge document. Only the fields
 *  both sides have are copied (chat_id, message_thread_id, interactive,
 *  allowed_user_ids, prompt_max_chars, only_when_away); moving turns the bridge
 *  side on. `language` and `sound` have no local counterpart and stay as the
 *  bridge has them (`current`). The token never passes through here. */
export function localToBridgeSettings(local: Rec, current: TelegramDoc): Rec {
  const doc: TelegramDoc = { ...current, allowed_user_ids: [...current.allowed_user_ids], enabled: true };
  if (typeof local.chat_id === "string") doc.chat_id = local.chat_id;
  if (typeof local.message_thread_id === "number") doc.message_thread_id = local.message_thread_id;
  if (typeof local.interactive === "boolean") doc.interactive = local.interactive;
  if (Array.isArray(local.allowed_user_ids)) doc.allowed_user_ids = local.allowed_user_ids.filter((n): n is number => typeof n === "number");
  if (typeof local.prompt_max_chars === "number") doc.prompt_max_chars = local.prompt_max_chars;
  if (typeof local.only_when_away === "number") doc.only_when_away = local.only_when_away;
  return docToWire(doc);
}

// --- calls ---------------------------------------------------------------------

export type TelegramSub = "" | "token" | "test";
/** `POST /bridge-telegram/<id>[/token|/test]` → the runtime's `{status, body}`. */
export type TelegramCallFn = (serverId: string, sub: TelegramSub, body: Rec) => Promise<unknown>;
export interface TelegramReply {
  /** HTTP status (0 = the runtime was unreachable). */
  status: number;
  body: Rec;
}

/** Never throws. Note: the body sent may hold a typed token; it is not kept. */
export async function callTelegram(call: TelegramCallFn, serverId: string, sub: TelegramSub, body: Rec): Promise<TelegramReply> {
  try {
    const r = rec(await call(serverId, sub, body));
    return { status: typeof r.status === "number" ? r.status : 0, body: rec(r.body) };
  } catch {
    return { status: 0, body: {} };
  }
}

export type TokenErrorKind = "invalid" | "env_locked" | "io_error" | "no_local_token" | "failed" | "offline" | "timeout" | "unreachable";

/** null = success. */
export function tokenErrorKind(r: TelegramReply): TokenErrorKind | null {
  if (r.status === 200 && r.body.ok === true) return null;
  if (r.status === 0) return "unreachable";
  if (r.status === 503) return "offline";
  if (r.status === 504) return "timeout";
  const e = r.body.error;
  return e === "invalid" || e === "env_locked" || e === "io_error" || e === "no_local_token" ? e : "failed";
}
