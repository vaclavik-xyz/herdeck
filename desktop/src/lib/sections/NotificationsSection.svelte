<script lang="ts">
  import { onMount } from "svelte";
  import { invoke } from "@tauri-apps/api/core";
  import BooleanField from "../fields/BooleanField.svelte";
  import NumberField from "../fields/NumberField.svelte";
  import TriStateListField from "../fields/TriStateListField.svelte";
  import TextField from "../fields/TextField.svelte";
  import SoundField from "../fields/SoundField.svelte";
  import TokenSecretField from "../fields/TokenSecretField.svelte";
  import OverrideField from "../fields/OverrideField.svelte";
  import {
    commandTransport as cfgTransport, getAt, setAt, removeAt, listFieldState, setListField,
    secretFlag, type ListFieldState, type ConfigPayload,
    inheritedFor, inheritedForPath, overrideValue, overrideValuePath, overrideState,
    setOverride, clearOverride, setOverridePath, clearOverridePath, updateBaseTelegram,
  } from "../configClient";
  import FieldGroup from "./FieldGroup.svelte";
  import { defineMessages, fieldHelp, fmt, locale, t } from "../i18n.svelte";
  import defaults from "../configDefaults.json";

  let { payload = $bindable(), onChange, onError, reloadRev = 0, editProfile = null }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void; reloadRev?: number; editProfile?: string | null } = $props();

  const cfg = cfgTransport((cmd, args) => invoke(cmd, args));

  const SEC = "notifications";
  const overlay = $derived(editProfile != null && editProfile !== "default");
  const prof = $derived(editProfile ?? "");
  const NOTIF_DEFAULTS: Record<string, boolean> = {
    enabled: defaults.notifications.enabled,
    sound: defaults.notifications.sound,
    banner_actions: defaults.notifications.banner_actions,
    banner_prompt: defaults.notifications.banner_prompt,
    skip_focused: defaults.notifications.skip_focused,
  };
  const NOTIF_LIST_DEFAULTS: Record<string, string[]> = {
    on: [...defaults.notifications.on],
    backends: [...defaults.notifications.backends],
  };
  const TELEGRAM_DEFAULTS: Record<string, unknown> = defaults.notifications.telegram;
  const SOUNDS_DEFAULTS: Record<string, string> = defaults.notifications.sounds as Record<string, string>;

  // Tooltips for every field (current language) — required for each labelled
  // field (enforced by sections.help.test.ts); catalog lives in help.ts.
  const HELP = $derived(fieldHelp("notifications"));

  const LM = defineMessages({
    en: {
      group_telegram: "Telegram bot",
      group_sounds: "Per-event sounds",
      sounds_hint:
        "Empty = default (blocked: Glass, done: Hero). Any macOS system sound name, e.g. Basso, Funk, Ping, Submarine.",
      sounds_hint_overlay:
        "Empty = inherit the base/default value. Any macOS system sound name, e.g. Basso, Funk, Ping, Submarine.",
      tg_hint: "Empty field = inherit (a token is never saved blank).",
      none: "(none)",
      origin_own: "custom",
      origin_inherited: "inherited",
      origin_unset: "unset",
      save_token_failed: "saving token '{name}' failed (HTTP {code})",
      clear_token_failed: "deleting token '{name}' failed (HTTP {code})",
      sound_default: "(default: {name})",
      sound_inherit: "(inherit)",
      test_sound: "Play a test notification with this sound",
      test_failed: "test notification failed: {e}",
      permission_denied: "macOS notifications are turned off for Herdeck, so no banner will appear. Allow them in System Settings → Notifications → Herdeck.",
      event_off_blocked: "blocked notifications are off: 'blocked' is not in on, so this sound never plays.",
      event_off_done: "done notifications are off: 'done' is not in on, so this sound never plays.",
      event_enable: "Add '{event}' to on",
      event_enable_title: "Add '{event}' to the on list so this event notifies again",
    },
    cs: {
      group_telegram: "Telegram bot",
      group_sounds: "Zvuky dle stavu",
      sounds_hint:
        "Prázdné = výchozí (blocked: Glass, done: Hero). Libovolný systémový zvuk macOS, např. Basso, Funk, Ping, Submarine.",
      sounds_hint_overlay:
        "Prázdné = zdědit z base/výchozí. Libovolný systémový zvuk macOS, např. Basso, Funk, Ping, Submarine.",
      tg_hint: "Prázdné pole = zdědit (token se nikdy neuloží prázdný).",
      none: "(nic)",
      origin_own: "vlastní",
      origin_inherited: "zděděno",
      origin_unset: "nenastaveno",
      save_token_failed: "uložení tokenu '{name}' selhalo (HTTP {code})",
      clear_token_failed: "smazání tokenu '{name}' selhalo (HTTP {code})",
      sound_default: "(výchozí: {name})",
      sound_inherit: "(zdědit)",
      test_sound: "Přehrát testovací notifikaci s tímto zvukem",
      test_failed: "testovací notifikace selhala: {e}",
      permission_denied: "Notifikace macOS má Herdeck vypnuté, takže se žádný banner neukáže. Povol je v Nastavení systému → Oznámení → Herdeck.",
      event_off_blocked: "Upozornění blocked jsou vypnutá: 'blocked' není v on, takže tenhle zvuk nikdy nezazní.",
      event_off_done: "Upozornění done jsou vypnutá: 'done' není v on, takže tenhle zvuk nikdy nezazní.",
      event_enable: "Přidat '{event}' do on",
      event_enable_title: "Přidá '{event}' do seznamu on, aby tahle událost zase upozorňovala",
    },
  });
  const lm = $derived(LM[locale.lang]);

  // C4 shell commands. Each degrades on its own: an older shell, a plain
  // browser or a non-macOS build yields an empty sound list (free-text field)
  // and an unknown permission (no warning).
  let soundNames = $state<string[]>([]);
  let permission = $state<boolean | null>(null);
  onMount(() => {
    void invoke("notification_sounds")
      .then((names) => {
        soundNames = Array.isArray(names) ? names.filter((n): n is string => typeof n === "string") : [];
      })
      .catch(() => {});
    void invoke("notification_permission")
      .then((granted) => { permission = typeof granted === "boolean" ? granted : null; })
      .catch(() => {});
  });

  async function testSound(key: "blocked" | "done", name: string): Promise<void> {
    const sound = name.trim() || SOUNDS_DEFAULTS[key] || null;
    try {
      await invoke("test_notification", { sound });
    } catch (e) {
      onError(fmt(lm.test_failed, { e: e instanceof Error ? e.message : String(e) }));
    }
  }

  const enabled = $derived((getAt(payload, "base", "notifications", "enabled") as boolean) ?? NOTIF_DEFAULTS.enabled);
  const sound = $derived((getAt(payload, "base", "notifications", "sound") as boolean) ?? NOTIF_DEFAULTS.sound);
  const bannerActions = $derived((getAt(payload, "base", "notifications", "banner_actions") as boolean) ?? NOTIF_DEFAULTS.banner_actions);
  const skipFocused = $derived((getAt(payload, "base", "notifications", "skip_focused") as boolean) ?? NOTIF_DEFAULTS.skip_focused);
  const bannerPrompt = $derived((getAt(payload, "base", "notifications", "banner_prompt") as boolean) ?? NOTIF_DEFAULTS.banner_prompt);
  const on = $derived((getAt(payload, "base", "notifications", "on") as string[]) ?? NOTIF_LIST_DEFAULTS.on);
  const onState = $derived(listFieldState(payload, "base", "notifications", "on"));
  const backends = $derived((getAt(payload, "base", "notifications", "backends") as string[]) ?? NOTIF_LIST_DEFAULTS.backends);
  const backendsState = $derived(listFieldState(payload, "base", "notifications", "backends"));

  // Per-event sound names ([notifications.sounds]); empty field = the default.
  const sounds = $derived(((): { blocked: string; done: string } => {
    const v = getAt(payload, "base", "notifications", "sounds");
    const s = v != null && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
    // Trim hand-written TOML values too — whitespace would silently kill the sound.
    return { blocked: String(s.blocked ?? "").trim(), done: String(s.done ?? "").trim() };
  })());

  function setSounds(key: "blocked" | "done", v: string): void {
    const s = { ...((getAt(payload, "base", "notifications", "sounds") as Record<string, unknown> | undefined) ?? {}) };
    if (v.trim() === "") delete s[key]; // blank field reverts to the default sound
    else s[key] = v.trim(); // a stray space would silently kill the osascript sound
    // An emptied map is absent rather than `{}` — the backend treats a present
    // table as an explicit override, mirroring updateBaseTelegram's pruning.
    payload = Object.keys(s).length === 0
      ? removeAt(payload, "base", "notifications", "sounds")
      : setAt(payload, "base", "notifications", "sounds", s);
    onChange();
  }

  const telegram = $derived(((): {
    token_env: string;
    chat_id: string;
    message_thread_id: number | null;
    interactive: boolean;
    allowed_user_ids: number[] | string;
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
        : typeof t.allowed_user_ids === "string" ? t.allowed_user_ids : [],
      prompt_max_chars: typeof t.prompt_max_chars === "number"
        ? t.prompt_max_chars
        : TELEGRAM_DEFAULTS.prompt_max_chars as number,
    };
  })());

  function set(key: string, value: unknown): void {
    payload = setAt(payload, "base", "notifications", key, value);
    onChange();
  }
  // `on`/`backends` tri-state: absent → backend defaults (["blocked","done"]/["macos"]), [] → none, custom → list.
  function setTri(key: string, state: ListFieldState, list: string[]): void {
    payload = setListField(payload, "base", "notifications", key, state, list);
    onChange();
  }
  function setTelegram(field: string, v: unknown): void {
    payload = updateBaseTelegram(payload, field, v);
    onChange();
  }
  function parseIntegerList(raw: string): number[] | string {
    if (raw.trim() === "") return [];
    const values = raw.split(",").map((part) => part.trim());
    if (values.some((part) => !/^-?\d+$/.test(part))) return raw;
    const parsed = values.map(Number);
    return parsed.every(Number.isSafeInteger) ? parsed : raw;
  }
  function setBaseAllowedUsers(raw: string): void {
    setTelegram("allowed_user_ids", parseIntegerList(raw));
  }
  function integerListText(value: unknown): string {
    return Array.isArray(value) ? value.join(", ") : typeof value === "string" ? value : "";
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

  // --- per-event sound vs `on` ---
  // A sound field is always shown, but its event only fires when it is in the
  // effective `on` list; warn inline instead of letting a configured sound
  // silently never play (the "done sound but no done alert" trap).
  type NotifyEvent = "blocked" | "done";
  // Bumped when the enable button rewrites `on`, so the list widget drops a
  // stale local draft and shows the new list.
  let onRev = $state(0);
  function hasEvent(list: string[], event: NotifyEvent): boolean {
    return list.some((item) => String(item).trim() === event);
  }
  const effectiveOn = $derived(
    !overlay ? on : overrideState(payload, prof, SEC, "on") === "default" ? effectiveList("on") : ovList("on"),
  );
  const effectiveBackends = $derived(
    !overlay ? backends : overrideState(payload, prof, SEC, "backends") === "default" ? effectiveList("backends") : ovList("backends"),
  );
  // Only meaningful when macOS alerts can fire at all: with notifications
  // disabled or no "macos" backend, the sound is moot either way.
  const soundsLive = $derived(
    (overlay ? scBool("enabled") : enabled) && effectiveBackends.some((b) => String(b).trim() === "macos"),
  );
  function eventOff(event: NotifyEvent): boolean {
    return soundsLive && !hasEvent(effectiveOn, event);
  }
  function enableEvent(event: NotifyEvent): void {
    const next = [...effectiveOn.filter((item) => String(item).trim() !== ""), event];
    onRev += 1;
    if (overlay) setOvList("on", "custom", next);
    else setTri("on", "custom", next);
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
  function tgInheritedRaw(k: string): unknown {
    return inheritedForPath(payload, prof, tgPath(k)) ?? TELEGRAM_DEFAULTS[k];
  }
  function tgFieldState(k: string): "inherit" | "override" {
    return overrideValuePath(payload, prof, tgPath(k)) === undefined ? "inherit" : "override";
  }
  function tgInheritedDisplay(k: string): string {
    const value = tgInheritedRaw(k);
    if (k === "message_thread_id" && value === 0) return lm.none;
    if (Array.isArray(value)) return value.length > 0 ? value.join(", ") : lm.none;
    return value == null ? lm.none : String(value);
  }
  function tgNumber(k: string): number | null {
    const value = tgRaw(k);
    if (k === "message_thread_id" && value === 0) return null;
    return typeof value === "number" ? value : null;
  }
  function tgBoolean(k: string): boolean { return tgRaw(k) === true; }
  function tgIntegerList(k: string): string {
    return integerListText(tgRaw(k));
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
      profiles: setOverridePath(payload.profiles, prof, tgPath(k), value),
    };
    onChange();
  }
  function setTgFieldState(k: string, state: "inherit" | "override"): void {
    payload = {
      ...payload,
      profiles: state === "inherit"
        ? clearOverridePath(payload.profiles, prof, tgPath(k))
        : setOverridePath(payload.profiles, prof, tgPath(k), tgInheritedRaw(k)),
    };
    onChange();
  }
  function setTgAllowedUsers(raw: string): void {
    setTgScalar("allowed_user_ids", parseIntegerList(raw));
  }

  // --- overlay per-event sounds (nested dict, per-subfield via path) ---
  function soPath(k: string): string[] {
    return [SEC, "sounds", k];
  }
  function soValue(k: string): string {
    const v = overrideValuePath(payload, prof, soPath(k));
    return v !== undefined ? String(v).trim() : String(inheritedForPath(payload, prof, soPath(k)) ?? "").trim();
  }
  function soInheritedRaw(k: string): unknown {
    return inheritedForPath(payload, prof, soPath(k)) ?? SOUNDS_DEFAULTS[k];
  }
  function soState(k: string): "inherit" | "override" {
    return overrideValuePath(payload, prof, soPath(k)) === undefined ? "inherit" : "override";
  }
  function soInheritedDisplay(k: string): string {
    const value = soInheritedRaw(k);
    return value == null ? "" : String(value);
  }
  function setSo(k: string, v: string): void {
    const clean = v.trim(); // whitespace would silently kill the osascript sound
    payload = {
      ...payload,
      profiles:
        clean === ""
          ? clearOverridePath(payload.profiles, prof, soPath(k))
          : setOverridePath(payload.profiles, prof, soPath(k), clean),
    };
    onChange();
  }
  function setSoState(k: string, state: "inherit" | "override"): void {
    payload = {
      ...payload,
      profiles:
        state === "inherit"
          ? clearOverridePath(payload.profiles, prof, soPath(k))
          : setOverridePath(payload.profiles, prof, soPath(k), soInheritedRaw(k)),
    };
    onChange();
  }
</script>

{#snippet eventOffWarning(event: NotifyEvent)}
  {#if eventOff(event)}
    <p class="event-off" role="status" data-event-off={event}>
      <span>{event === "done" ? lm.event_off_done : lm.event_off_blocked}</span>
      <button type="button" class="event-enable" title={fmt(lm.event_enable_title, { event })} onclick={() => enableEvent(event)}>{fmt(lm.event_enable, { event })}</button>
    </p>
  {/if}
{/snippet}

{#if permission === false}
  <p class="permission-warning" role="alert">{lm.permission_denied}</p>
{/if}
{#if overlay}
  <OverrideField label="enabled" help={HELP.enabled} state={scState("enabled")} inheritedDisplay={scHint("enabled")} onstate={(s) => setScState("enabled", s)}>
    <BooleanField label="" value={scBool("enabled")} onchange={(v) => setSc("enabled", v)} />
  </OverrideField>
  <OverrideField label="sound" help={HELP.sound} state={scState("sound")} inheritedDisplay={scHint("sound")} onstate={(s) => setScState("sound", s)}>
    <BooleanField label="" value={scBool("sound")} onchange={(v) => setSc("sound", v)} />
  </OverrideField>
  <TriStateListField label="on" help={HELP.on} state={overrideState(payload, prof, SEC, "on")} list={ovList("on")} customSeed={effectiveList("on")} inheritLabel={t("widget.inherit")} inheritHint={`${t("widget.inherited")} ${listHint("on")}`} resetKey={`${prof}:${reloadRev}:${onRev}:notifications:on`} onchange={(s, l) => setOvList("on", s, l)} />
  <OverrideField label="banner_actions" help={HELP.banner_actions} state={scState("banner_actions")} inheritedDisplay={scHint("banner_actions")} onstate={(s) => setScState("banner_actions", s)}>
    <BooleanField label="" value={scBool("banner_actions")} onchange={(v) => setSc("banner_actions", v)} />
  </OverrideField>
  <OverrideField label="banner_prompt" help={HELP.banner_prompt} state={scState("banner_prompt")} inheritedDisplay={scHint("banner_prompt")} onstate={(s) => setScState("banner_prompt", s)}>
    <BooleanField label="" value={scBool("banner_prompt")} onchange={(v) => setSc("banner_prompt", v)} />
  </OverrideField>
  <OverrideField label="skip_focused" help={HELP.skip_focused} state={scState("skip_focused")} inheritedDisplay={scHint("skip_focused")} onstate={(s) => setScState("skip_focused", s)}>
    <BooleanField label="" value={scBool("skip_focused")} onchange={(v) => setSc("skip_focused", v)} />
  </OverrideField>
  <TriStateListField label="backends" help={HELP.backends} state={overrideState(payload, prof, SEC, "backends")} list={ovList("backends")} customSeed={effectiveList("backends")} inheritLabel={t("widget.inherit")} inheritHint={`${t("widget.inherited")} ${listHint("backends")}`} resetKey={`${prof}:${reloadRev}:notifications:backends`} onchange={(s, l) => setOvList("backends", s, l)} />
  <FieldGroup title={lm.group_sounds}>
    <p class="hint">{lm.sounds_hint_overlay}</p>
    {@render eventOffWarning("blocked")}
    <OverrideField label="sounds_blocked" help={HELP.sounds_blocked} state={soState("blocked")} inheritedDisplay={soInheritedDisplay("blocked")} onstate={(s) => setSoState("blocked", s)}>
      <SoundField label="" value={soValue("blocked")} options={soundNames} defaultLabel={lm.sound_inherit} testLabel={lm.test_sound} onchange={(v) => setSo("blocked", v)} ontest={() => void testSound("blocked", soValue("blocked") || soInheritedDisplay("blocked"))} />
    </OverrideField>
    {@render eventOffWarning("done")}
    <OverrideField label="sounds_done" help={HELP.sounds_done} state={soState("done")} inheritedDisplay={soInheritedDisplay("done")} onstate={(s) => setSoState("done", s)}>
      <SoundField label="" value={soValue("done")} options={soundNames} defaultLabel={lm.sound_inherit} testLabel={lm.test_sound} onchange={(v) => setSo("done", v)} ontest={() => void testSound("done", soValue("done") || soInheritedDisplay("done"))} />
    </OverrideField>
  </FieldGroup>
  <FieldGroup title={lm.group_telegram}>
    <p class="hint">{lm.tg_hint}</p>
    <TokenSecretField
      label={`token_env (${tgOrigin("token_env")})`}
      help={HELP.token}
      value={tgValue("token_env")}
      flag={secretFlag(payload, tgValue("token_env"))}
      oninput={(v) => setTg("token_env", v)}
      onset={(val) => setSecret(tgValue("token_env"), val)}
      onclear={() => clearSecret(tgValue("token_env"))}
    />
    <TextField label={`chat_id (${tgOrigin("chat_id")})`} help={HELP.chat_id} value={tgValue("chat_id")} oninput={(v) => setTg("chat_id", v)} />
    <OverrideField label="message_thread_id" help={HELP.message_thread_id} state={tgFieldState("message_thread_id")} inheritedDisplay={tgInheritedDisplay("message_thread_id")} onstate={(s) => setTgFieldState("message_thread_id", s)}>
      <NumberField label="" int value={tgNumber("message_thread_id")} onchange={(v) => setTgScalar("message_thread_id", v ?? 0)} />
    </OverrideField>
    <OverrideField label="interactive" help={HELP.interactive} state={tgFieldState("interactive")} inheritedDisplay={tgInheritedDisplay("interactive")} onstate={(s) => setTgFieldState("interactive", s)}>
      <BooleanField label="" value={tgBoolean("interactive")} onchange={(v) => setTgScalar("interactive", v)} />
    </OverrideField>
    <OverrideField label="allowed_user_ids" help={HELP.allowed_user_ids} state={tgFieldState("allowed_user_ids")} inheritedDisplay={tgInheritedDisplay("allowed_user_ids")} onstate={(s) => setTgFieldState("allowed_user_ids", s)}>
      <TextField label="" value={tgIntegerList("allowed_user_ids")} oninput={setTgAllowedUsers} />
    </OverrideField>
    <OverrideField label="prompt_max_chars" help={HELP.prompt_max_chars} state={tgFieldState("prompt_max_chars")} inheritedDisplay={tgInheritedDisplay("prompt_max_chars")} onstate={(s) => setTgFieldState("prompt_max_chars", s)}>
      <NumberField label="" int value={tgNumber("prompt_max_chars")} onchange={(v) => setTgScalar("prompt_max_chars", v)} />
    </OverrideField>
  </FieldGroup>
{:else}
  <BooleanField label="enabled" help={HELP.enabled} value={enabled} onchange={(v) => set("enabled", v)} />
  <BooleanField label="sound" help={HELP.sound} value={sound} onchange={(v) => set("sound", v)} />
  <TriStateListField label="on" help={HELP.on} state={onState} list={on} customSeed={NOTIF_LIST_DEFAULTS.on} defaultHint={NOTIF_LIST_DEFAULTS.on.join(" · ")} resetKey={`base:${reloadRev}:${onRev}:notifications:on`} onchange={(s, l) => setTri("on", s, l)} />
  <BooleanField label="banner_actions" help={HELP.banner_actions} value={bannerActions} onchange={(v) => set("banner_actions", v)} />
  <BooleanField label="banner_prompt" help={HELP.banner_prompt} value={bannerPrompt} onchange={(v) => set("banner_prompt", v)} />
  <BooleanField label="skip_focused" help={HELP.skip_focused} value={skipFocused} onchange={(v) => set("skip_focused", v)} />
  <TriStateListField label="backends" help={HELP.backends} state={backendsState} list={backends} customSeed={NOTIF_LIST_DEFAULTS.backends} defaultHint={NOTIF_LIST_DEFAULTS.backends.join(" · ")} resetKey={`base:${reloadRev}:notifications:backends`} onchange={(s, l) => setTri("backends", s, l)} />
  <FieldGroup title={lm.group_sounds}>
    <p class="hint">{lm.sounds_hint}</p>
    {@render eventOffWarning("blocked")}
    <SoundField label="sounds_blocked" help={HELP.sounds_blocked} value={sounds.blocked} options={soundNames} placeholder={SOUNDS_DEFAULTS.blocked} defaultLabel={fmt(lm.sound_default, { name: SOUNDS_DEFAULTS.blocked })} testLabel={lm.test_sound} onchange={(v) => setSounds("blocked", v)} ontest={() => void testSound("blocked", sounds.blocked)} />
    {@render eventOffWarning("done")}
    <SoundField label="sounds_done" help={HELP.sounds_done} value={sounds.done} options={soundNames} placeholder={SOUNDS_DEFAULTS.done} defaultLabel={fmt(lm.sound_default, { name: SOUNDS_DEFAULTS.done })} testLabel={lm.test_sound} onchange={(v) => setSounds("done", v)} ontest={() => void testSound("done", sounds.done)} />
  </FieldGroup>
  <FieldGroup title={lm.group_telegram}>
    <TokenSecretField label="token_env" help={HELP.token} value={telegram.token_env} flag={secretFlag(payload, telegram.token_env)} oninput={(v) => setTelegram("token_env", v)} onset={(val) => setSecret(telegram.token_env, val)} onclear={() => clearSecret(telegram.token_env)} />
    <TextField label="chat_id" help={HELP.chat_id} value={telegram.chat_id} oninput={(v) => setTelegram("chat_id", v)} />
    <NumberField label="message_thread_id" help={HELP.message_thread_id} int value={telegram.message_thread_id} onchange={(v) => setTelegram("message_thread_id", v)} />
    <BooleanField label="interactive" help={HELP.interactive} value={telegram.interactive} onchange={(v) => setTelegram("interactive", v)} />
    <TextField label="allowed_user_ids" help={HELP.allowed_user_ids} value={integerListText(telegram.allowed_user_ids)} oninput={setBaseAllowedUsers} />
    <NumberField label="prompt_max_chars" help={HELP.prompt_max_chars} int value={telegram.prompt_max_chars} onchange={(v) => setTelegram("prompt_max_chars", v)} />
  </FieldGroup>
{/if}

<style>
  .hint { margin: 0 0 var(--s3); color: var(--text-dim); font: var(--t-help); }
  .event-off {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--s2);
    margin: 0 0 var(--s2);
    padding: var(--s2) var(--s3);
    border: 1px solid color-mix(in srgb, var(--st-blocked) 45%, var(--line));
    border-radius: var(--r-control);
    background: color-mix(in srgb, var(--st-blocked) 12%, var(--canvas));
    color: var(--text);
    font: var(--t-help);
  }
  .event-off span { flex: 1 1 16em; }
  .event-enable {
    flex: none;
    min-height: 28px;
    padding: 0 var(--s3);
    border: 1px solid var(--line-strong);
    border-radius: var(--r-control);
    background: var(--panel-raised);
    color: var(--text);
    cursor: pointer;
  }
  .event-enable:hover { background: var(--key); }
  .permission-warning {
    margin: 0 0 var(--s3);
    padding: var(--s2) var(--s3);
    border: 1px solid color-mix(in srgb, var(--st-blocked) 45%, var(--line));
    border-radius: var(--r-control);
    background: color-mix(in srgb, var(--st-blocked) 12%, var(--canvas));
    color: var(--text);
    font: var(--t-help);
  }
</style>
