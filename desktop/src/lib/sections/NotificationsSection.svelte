<script lang="ts">
  import { invoke } from "@tauri-apps/api/core";
  import BooleanField from "../fields/BooleanField.svelte";
  import NumberField from "../fields/NumberField.svelte";
  import TriStateListField from "../fields/TriStateListField.svelte";
  import TextField from "../fields/TextField.svelte";
  import TokenSecretField from "../fields/TokenSecretField.svelte";
  import OverrideField from "../fields/OverrideField.svelte";
  import {
    commandTransport as cfgTransport, getAt, setAt, listFieldState, setListField,
    secretFlag, type ListFieldState, type ConfigPayload,
    inheritedFor, inheritedForPath, overrideValue, overrideValuePath, overrideState,
    setOverride, clearOverride, setOverridePath, clearOverridePath, updateBaseTelegram,
  } from "../configClient";
  import { defineMessages, fieldHelp, fmt, locale, t } from "../i18n.svelte";

  let { payload = $bindable(), onChange, onError, reloadRev = 0, editProfile = null }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void; reloadRev?: number; editProfile?: string | null } = $props();

  const cfg = cfgTransport((cmd, args) => invoke(cmd, args));

  const SEC = "notifications";
  const overlay = $derived(editProfile != null && editProfile !== "default");
  const prof = $derived(editProfile ?? "");
  // Mirror of backend defaults (settings._notifications_config) — keep in sync.
  const NOTIF_DEFAULTS: Record<string, boolean> = { enabled: false, sound: true };
  const NOTIF_LIST_DEFAULTS: Record<string, string[]> = { on: ["blocked"], backends: ["macos"] };
  const TELEGRAM_DEFAULTS: Record<string, unknown> = {
    message_thread_id: null,
    interactive: false,
    allowed_user_ids: [],
    prompt_max_chars: 1200,
  };

  // Tooltips for every field (current language) — required for each labelled
  // field (enforced by sections.help.test.ts); catalog lives in help.ts.
  const HELP = $derived(fieldHelp("notifications"));

  const LM = defineMessages({
    en: {
      heading: "Notifications",
      tg_hint: "Empty field = inherit (a token is never saved blank).",
      none: "(none)",
      origin_own: "custom",
      origin_inherited: "inherited",
      origin_unset: "unset",
      save_token_failed: "saving token '{name}' failed (HTTP {code})",
      clear_token_failed: "deleting token '{name}' failed (HTTP {code})",
    },
    cs: {
      heading: "Notifikace",
      tg_hint: "Prázdné pole = zdědit (token se nikdy neuloží prázdný).",
      none: "(nic)",
      origin_own: "vlastní",
      origin_inherited: "zděděno",
      origin_unset: "nenastaveno",
      save_token_failed: "uložení tokenu '{name}' selhalo (HTTP {code})",
      clear_token_failed: "smazání tokenu '{name}' selhalo (HTTP {code})",
    },
  });
  const lm = $derived(LM[locale.lang]);

  const enabled = $derived((getAt(payload, "base", "notifications", "enabled") as boolean) ?? false);
  const sound = $derived((getAt(payload, "base", "notifications", "sound") as boolean) ?? true);
  const on = $derived((getAt(payload, "base", "notifications", "on") as string[]) ?? NOTIF_LIST_DEFAULTS.on);
  const onState = $derived(listFieldState(payload, "base", "notifications", "on"));
  const backends = $derived((getAt(payload, "base", "notifications", "backends") as string[]) ?? NOTIF_LIST_DEFAULTS.backends);
  const backendsState = $derived(listFieldState(payload, "base", "notifications", "backends"));

  const telegram = $derived(((): {
    token_env: string;
    chat_id: string;
    message_thread_id: number | null;
    interactive: boolean;
    allowed_user_ids: number[];
    prompt_max_chars: number;
  } => {
    const v = getAt(payload, "base", "notifications", "telegram");
    const t = v != null && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
    return {
      token_env: String(t.token_env ?? ""),
      chat_id: String(t.chat_id ?? ""),
      message_thread_id: typeof t.message_thread_id === "number" ? t.message_thread_id : null,
      interactive: t.interactive === true,
      allowed_user_ids: Array.isArray(t.allowed_user_ids)
        ? t.allowed_user_ids.filter((value): value is number => typeof value === "number")
        : [],
      prompt_max_chars: typeof t.prompt_max_chars === "number" ? t.prompt_max_chars : 1200,
    };
  })());

  function set(key: string, value: unknown): void {
    payload = setAt(payload, "base", "notifications", key, value);
    onChange();
  }
  // `on`/`backends` tri-state: absent → backend defaults (["blocked"]/["macos"]), [] → none, custom → list.
  function setTri(key: string, state: ListFieldState, list: string[]): void {
    payload = setListField(payload, "base", "notifications", key, state, list);
    onChange();
  }
  function setTelegram(field: string, v: unknown): void {
    payload = updateBaseTelegram(payload, field, v);
    onChange();
  }
  function parseIntegerList(raw: string): number[] | null {
    if (raw.trim() === "") return [];
    const values = raw.split(",").map((part) => part.trim());
    if (values.some((part) => !/^-?\d+$/.test(part))) return null;
    const parsed = values.map(Number);
    return parsed.every(Number.isSafeInteger) ? parsed : null;
  }
  function setBaseAllowedUsers(raw: string): void {
    const parsed = parseIntegerList(raw);
    if (parsed !== null) setTelegram("allowed_user_ids", parsed);
  }
  async function setSecret(name: string, value: string): Promise<void> {
    const code = await cfg.setSecret(name, value);
    if (code === 204) {
      payload = { ...payload, secrets: { ...payload.secrets, [name]: { set: true, source: "keychain" } } };
    } else {
      onError(fmt(lm.save_token_failed, { name, code }));
    }
  }
  async function clearSecret(name: string): Promise<void> {
    const code = await cfg.clearSecret(name);
    if (code === 204) {
      payload = { ...payload, secrets: { ...payload.secrets, [name]: { set: false, source: null } } };
    } else {
      onError(fmt(lm.clear_token_failed, { name, code }));
    }
  }

  // --- overlay scalar (enabled/sound) ---
  function scHint(key: string): string { const v = inheritedFor(payload, prof, SEC, key); return String(v ?? NOTIF_DEFAULTS[key]); }
  function scState(key: string): "inherit" | "override" { return overrideState(payload, prof, SEC, key) === "default" ? "inherit" : "override"; }
  function scBool(key: string): boolean { const v = overrideValue(payload, prof, SEC, key); return v === undefined ? Boolean(inheritedFor(payload, prof, SEC, key) ?? NOTIF_DEFAULTS[key]) : Boolean(v); }
  function setScState(key: string, s: "inherit" | "override"): void {
    payload = { ...payload, profiles: s === "inherit" ? clearOverride(payload.profiles, prof, SEC, key) : setOverride(payload.profiles, prof, SEC, key, inheritedFor(payload, prof, SEC, key) ?? NOTIF_DEFAULTS[key]) };
    onChange();
  }
  function setSc(key: string, v: unknown): void { payload = { ...payload, profiles: setOverride(payload.profiles, prof, SEC, key, v) }; onChange(); }

  // --- overlay list (on/backends) ---
  function listHint(key: string): string { const v = inheritedFor(payload, prof, SEC, key) ?? NOTIF_LIST_DEFAULTS[key]; return Array.isArray(v) ? v.join(" · ") : lm.none; }
  function effectiveList(key: string): string[] { const v = inheritedFor(payload, prof, SEC, key) ?? NOTIF_LIST_DEFAULTS[key]; return Array.isArray(v) ? v as string[] : []; }
  function ovList(key: string): string[] { const v = overrideValue(payload, prof, SEC, key); return Array.isArray(v) ? (v as string[]) : []; }
  function setOvList(key: string, state: ListFieldState, list: string[]): void {
    payload = { ...payload, profiles: state === "default" ? clearOverride(payload.profiles, prof, SEC, key) : setOverride(payload.profiles, prof, SEC, key, state === "empty" ? [] : list) };
    onChange();
  }

  // --- overlay telegram (nested dict, per-subfield via path) ---
  function tgPath(k: string): string[] { return [SEC, "telegram", k]; }
  // Effective telegram subfield value (own override → inherited → ""). NO inherit/override
  // toggle: a blank token_env is poison (backend reads it as an env-var name), so we never
  // persist a blank override — a cleared field reverts to inheriting, mirroring base setTelegram.
  function tgValue(k: string): string {
    const v = overrideValuePath(payload, prof, tgPath(k));
    return v !== undefined ? String(v) : String(inheritedForPath(payload, prof, tgPath(k)) ?? "");
  }
  function tgRaw(k: string): unknown {
    const own = overrideValuePath(payload, prof, tgPath(k));
    if (own !== undefined) return own;
    return inheritedForPath(payload, prof, tgPath(k)) ?? TELEGRAM_DEFAULTS[k];
  }
  function tgNumber(k: string): number | null {
    const value = tgRaw(k);
    return typeof value === "number" ? value : null;
  }
  function tgBoolean(k: string): boolean { return tgRaw(k) === true; }
  function tgIntegerList(k: string): string {
    const value = tgRaw(k);
    return Array.isArray(value) ? value.join(", ") : "";
  }
  function tgOrigin(k: string): string {
    if (overrideValuePath(payload, prof, tgPath(k)) !== undefined) return lm.origin_own;
    return inheritedForPath(payload, prof, tgPath(k)) != null ? lm.origin_inherited : lm.origin_unset;
  }
  function setTg(k: string, v: string): void {
    payload = {
      ...payload,
      profiles: v.trim() === ""
        ? clearOverridePath(payload.profiles, prof, tgPath(k))
        : setOverridePath(payload.profiles, prof, tgPath(k), v),
    };
    onChange();
  }
  function setTgScalar(k: string, value: unknown): void {
    payload = {
      ...payload,
      profiles: value === null
        ? clearOverridePath(payload.profiles, prof, tgPath(k))
        : setOverridePath(payload.profiles, prof, tgPath(k), value),
    };
    onChange();
  }
  function setTgAllowedUsers(raw: string): void {
    const parsed = parseIntegerList(raw);
    if (parsed !== null) setTgScalar("allowed_user_ids", parsed);
  }
</script>

<h2>{lm.heading}{#if overlay} · overlay: {editProfile}{/if}</h2>
{#if overlay}
  <OverrideField label="enabled" help={HELP.enabled} state={scState("enabled")} inheritedDisplay={scHint("enabled")} onstate={(s) => setScState("enabled", s)}>
    <BooleanField label="" value={scBool("enabled")} onchange={(v) => setSc("enabled", v)} />
  </OverrideField>
  <OverrideField label="sound" help={HELP.sound} state={scState("sound")} inheritedDisplay={scHint("sound")} onstate={(s) => setScState("sound", s)}>
    <BooleanField label="" value={scBool("sound")} onchange={(v) => setSc("sound", v)} />
  </OverrideField>
  <TriStateListField label="on" help={HELP.on} state={overrideState(payload, prof, SEC, "on")} list={ovList("on")} customSeed={effectiveList("on")} inheritLabel={t("widget.inherit")} inheritHint={`${t("widget.inherited")} ${listHint("on")}`} resetKey={`${prof}:${reloadRev}:notifications:on`} onchange={(s, l) => setOvList("on", s, l)} />
  <TriStateListField label="backends" help={HELP.backends} state={overrideState(payload, prof, SEC, "backends")} list={ovList("backends")} customSeed={effectiveList("backends")} inheritLabel={t("widget.inherit")} inheritHint={`${t("widget.inherited")} ${listHint("backends")}`} resetKey={`${prof}:${reloadRev}:notifications:backends`} onchange={(s, l) => setOvList("backends", s, l)} />
  <fieldset class="tg">
    <legend>Telegram</legend>
    <p class="hint">{lm.tg_hint}</p>
    <TokenSecretField
      label={`token (${tgOrigin("token_env")})`}
      help={HELP.token}
      value={tgValue("token_env")}
      flag={secretFlag(payload, tgValue("token_env"))}
      oninput={(v) => setTg("token_env", v)}
      onset={(val) => setSecret(tgValue("token_env"), val)}
      onclear={() => clearSecret(tgValue("token_env"))}
    />
    <TextField label={`chat_id (${tgOrigin("chat_id")})`} help={HELP.chat_id} value={tgValue("chat_id")} oninput={(v) => setTg("chat_id", v)} />
    <NumberField label={`message_thread_id (${tgOrigin("message_thread_id")})`} help={HELP.message_thread_id} int value={tgNumber("message_thread_id")} onchange={(v) => setTgScalar("message_thread_id", v)} />
    <BooleanField label={`interactive (${tgOrigin("interactive")})`} help={HELP.interactive} value={tgBoolean("interactive")} onchange={(v) => setTgScalar("interactive", v)} />
    <TextField label={`allowed_user_ids (${tgOrigin("allowed_user_ids")})`} help={HELP.allowed_user_ids} value={tgIntegerList("allowed_user_ids")} oninput={setTgAllowedUsers} />
    <NumberField label={`prompt_max_chars (${tgOrigin("prompt_max_chars")})`} help={HELP.prompt_max_chars} int value={tgNumber("prompt_max_chars")} onchange={(v) => setTgScalar("prompt_max_chars", v)} />
  </fieldset>
{:else}
  <BooleanField label="enabled" help={HELP.enabled} value={enabled} onchange={(v) => set("enabled", v)} />
  <BooleanField label="sound" help={HELP.sound} value={sound} onchange={(v) => set("sound", v)} />
  <TriStateListField label="on" help={HELP.on} state={onState} list={on} customSeed={NOTIF_LIST_DEFAULTS.on} defaultHint={NOTIF_LIST_DEFAULTS.on.join(" · ")} resetKey={`base:${reloadRev}:notifications:on`} onchange={(s, l) => setTri("on", s, l)} />
  <TriStateListField label="backends" help={HELP.backends} state={backendsState} list={backends} customSeed={NOTIF_LIST_DEFAULTS.backends} defaultHint={NOTIF_LIST_DEFAULTS.backends.join(" · ")} resetKey={`base:${reloadRev}:notifications:backends`} onchange={(s, l) => setTri("backends", s, l)} />
  <fieldset class="tg">
    <legend>Telegram</legend>
    <TokenSecretField label="token" help={HELP.token} value={telegram.token_env} flag={secretFlag(payload, telegram.token_env)} oninput={(v) => setTelegram("token_env", v)} onset={(val) => setSecret(telegram.token_env, val)} onclear={() => clearSecret(telegram.token_env)} />
    <TextField label="chat_id" help={HELP.chat_id} value={telegram.chat_id} oninput={(v) => setTelegram("chat_id", v)} />
    <NumberField label="message_thread_id" help={HELP.message_thread_id} int value={telegram.message_thread_id} onchange={(v) => setTelegram("message_thread_id", v)} />
    <BooleanField label="interactive" help={HELP.interactive} value={telegram.interactive} onchange={(v) => setTelegram("interactive", v)} />
    <TextField label="allowed_user_ids" help={HELP.allowed_user_ids} value={telegram.allowed_user_ids.join(", ")} oninput={setBaseAllowedUsers} />
    <NumberField label="prompt_max_chars" help={HELP.prompt_max_chars} int value={telegram.prompt_max_chars} onchange={(v) => setTelegram("prompt_max_chars", v)} />
  </fieldset>
{/if}

<style>
  h2 { margin: 0 0 8px; }
  .tg { border: 1px solid #2a2a30; border-radius: 6px; margin: 12px 0; padding: 8px 12px; }
  .tg legend { color: #ccc; }
  .hint { color: #888; margin: 0 0 8px; }
</style>
