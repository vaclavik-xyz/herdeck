<script lang="ts">
  import { invoke } from "@tauri-apps/api/core";
  import TextField from "../fields/TextField.svelte";
  import TokenSecretField from "../fields/TokenSecretField.svelte";
  import {
    commandTransport as cfgTransport,
    serversOf,
    addServer,
    removeServer,
    updateServer,
    secretFlag,
    type ConfigPayload,
    type ServerRecord,
  } from "../configClient";
  import { defineMessages, fieldHelp, fmt, locale } from "../i18n.svelte";

  let { payload = $bindable(), onChange, onError }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void } = $props();

  const cfg = cfgTransport((cmd, args) => invoke(cmd, args));

  const servers = $derived(serversOf(payload));

  // Current-language tooltips for every field — required for each labelled
  // field (enforced by sections.help.test.ts); texts live in help.ts.
  const HELP = $derived(fieldHelp("servers"));

  const LM = defineMessages({
    en: {
      heading: "Servers",
      new_server: "(new server)",
      remove_server: "Remove server",
      add_server: "+ add server",
      save_token_failed: "saving token '{name}' failed (HTTP {code})",
      clear_token_failed: "clearing token '{name}' failed (HTTP {code})",
    },
    cs: {
      heading: "Servery",
      new_server: "(nový server)",
      remove_server: "Odebrat server",
      add_server: "+ přidat server",
      save_token_failed: "uložení tokenu '{name}' selhalo (HTTP {code})",
      clear_token_failed: "smazání tokenu '{name}' selhalo (HTTP {code})",
    },
  });
  const lm = $derived(LM[locale.lang]);

  function set(i: number, field: "id" | "url" | "token_env", v: string): void {
    payload = updateServer(payload, i, field, v);
    onChange();
  }
  function add(): void {
    payload = addServer(payload);
    onChange();
  }
  function remove(i: number): void {
    payload = removeServer(payload, i);
    onChange();
  }
  async function setSecret(name: string, value: string): Promise<void> {
    const code = await cfg.setSecret(name, value); // 204 on success
    if (code === 204) {
      payload = { ...payload, secrets: { ...payload.secrets, [name]: { set: true, source: "keychain" } } };
    } else {
      onError(fmt(lm.save_token_failed, { name, code }));
    }
  }
  async function clearSecret(name: string): Promise<void> {
    const code = await cfg.clearSecret(name); // 204 on success
    if (code === 204) {
      payload = { ...payload, secrets: { ...payload.secrets, [name]: { set: false, source: null } } };
    } else {
      onError(fmt(lm.clear_token_failed, { name, code }));
    }
  }
</script>

<h2>{lm.heading}</h2>
<!-- Index keying is correct here: this is an append / remove list (no row reordering),
     and the only per-row transient state (TokenSecretField's in-progress secret entry) is
     disposable. Editing a field keeps the same index → same DOM node → focus preserved.
     A stable-id apparatus would add complexity that 9 řez-4 sections would clone. -->
{#each servers as s, i (i)}
  <fieldset>
    <legend>{s.id || lm.new_server} <button type="button" title={lm.remove_server} onclick={() => remove(i)}>×</button></legend>
    <TextField label="id" help={HELP.id} value={s.id} oninput={(v) => set(i, "id", v)} />
    <TextField label="url" help={HELP.url} value={s.url} oninput={(v) => set(i, "url", v)} />
    <TokenSecretField
      label="token"
      help={HELP.token}
      value={s.token_env}
      flag={secretFlag(payload, s.token_env)}
      oninput={(v) => set(i, "token_env", v)}
      onset={(val) => setSecret(s.token_env, val)}
      onclear={() => clearSecret(s.token_env)}
    />
  </fieldset>
{/each}
<button type="button" onclick={add}>{lm.add_server}</button>

<style>
  fieldset { border: 1px solid #2a2a30; border-radius: 6px; margin: 8px 0; padding: 8px 12px; }
  legend { color: #ccc; } legend button { color: #e05050; background: none; border: 0; cursor: pointer; }
  button { background: #1b1b1f; border: 1px solid #2a2a30; color: inherit; border-radius: 4px; padding: 4px 8px; cursor: pointer; }
  h2 { margin: 0 0 8px; }
</style>
