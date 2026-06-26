<script lang="ts">
  import { invoke } from "@tauri-apps/api/core";
  import BooleanField from "../fields/BooleanField.svelte";
  import TriStateListField from "../fields/TriStateListField.svelte";
  import TextField from "../fields/TextField.svelte";
  import TokenSecretField from "../fields/TokenSecretField.svelte";
  import {
    commandTransport as cfgTransport, getAt, setAt, removeAt, listFieldState, setListField,
    secretFlag, type ListFieldState, type ConfigPayload,
  } from "../configClient";

  let { payload = $bindable(), onChange, onError }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void } = $props();

  const cfg = cfgTransport((cmd, args) => invoke(cmd, args));

  const enabled = $derived((getAt(payload, "base", "notifications", "enabled") as boolean) ?? false);
  const sound = $derived((getAt(payload, "base", "notifications", "sound") as boolean) ?? true);
  const on = $derived((getAt(payload, "base", "notifications", "on") as string[]) ?? []);
  const onState = $derived(listFieldState(payload, "base", "notifications", "on"));
  const backends = $derived((getAt(payload, "base", "notifications", "backends") as string[]) ?? []);
  const backendsState = $derived(listFieldState(payload, "base", "notifications", "backends"));

  const telegram = $derived(((): { token_env: string; chat_id: string } => {
    const v = getAt(payload, "base", "notifications", "telegram");
    const t = v != null && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
    return { token_env: String(t.token_env ?? ""), chat_id: String(t.chat_id ?? "") };
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
  function setTelegram(field: "token_env" | "chat_id", v: string): void {
    const next = { ...telegram, [field]: v };
    if (next.token_env.trim() === "" && next.chat_id.trim() === "") {
      // Both cleared → drop the table entirely (no empty [telegram]).
      payload = removeAt(payload, "base", "notifications", "telegram");
    } else {
      // Omit a BLANK sub-field rather than writing `token_env = ""`: the backend token
      // collector would treat "" as an env-var name and crash validation outside the
      // normal error path. A telegram table is only fully valid with BOTH fields; a
      // partial one is ignored by the backend (with a warning) but kept here so the
      // half-entered value (e.g. chat_id typed before token_env) is not lost.
      const tg: Record<string, string> = {};
      if (next.token_env.trim() !== "") tg.token_env = next.token_env;
      if (next.chat_id.trim() !== "") tg.chat_id = next.chat_id;
      payload = setAt(payload, "base", "notifications", "telegram", tg);
    }
    onChange();
  }
  async function setSecret(name: string, value: string): Promise<void> {
    const code = await cfg.setSecret(name, value);
    if (code === 204) {
      payload = { ...payload, secrets: { ...payload.secrets, [name]: { set: true, source: "keychain" } } };
    } else {
      onError(`uložení tokenu '${name}' selhalo (HTTP ${code})`);
    }
  }
  async function clearSecret(name: string): Promise<void> {
    const code = await cfg.clearSecret(name);
    if (code === 204) {
      payload = { ...payload, secrets: { ...payload.secrets, [name]: { set: false, source: null } } };
    } else {
      onError(`smazání tokenu '${name}' selhalo (HTTP ${code})`);
    }
  }
</script>

<h2>Notifications</h2>
<BooleanField label="enabled" value={enabled} onchange={(v) => set("enabled", v)} />
<BooleanField label="sound" value={sound} onchange={(v) => set("sound", v)} />
<TriStateListField label="on" state={onState} list={on} onchange={(s, l) => setTri("on", s, l)} />
<TriStateListField label="backends" state={backendsState} list={backends} onchange={(s, l) => setTri("backends", s, l)} />

<fieldset class="tg">
  <legend>Telegram</legend>
  <TokenSecretField
    label="token"
    value={telegram.token_env}
    flag={secretFlag(payload, telegram.token_env)}
    oninput={(v) => setTelegram("token_env", v)}
    onset={(val) => setSecret(telegram.token_env, val)}
    onclear={() => clearSecret(telegram.token_env)}
  />
  <TextField label="chat_id" value={telegram.chat_id} oninput={(v) => setTelegram("chat_id", v)} />
</fieldset>

<style>
  h2 { margin: 0 0 8px; }
  .tg { border: 1px solid #2a2a30; border-radius: 6px; margin: 12px 0; padding: 8px 12px; }
  .tg legend { color: #ccc; }
</style>
