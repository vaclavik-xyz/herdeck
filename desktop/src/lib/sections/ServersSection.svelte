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

  let { payload = $bindable(), onChange, onError }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void } = $props();

  const cfg = cfgTransport((cmd, args) => invoke(cmd, args));

  // Per-row stable keys. Plain (non-$state) — only serversWithKeys (the $derived)
  // needs to be reactive. rowKeys is its private bookkeeping.
  let nextKey = 0;
  let rowKeys: number[] = serversOf(payload).map(() => nextKey++);
  // expectedLen tracks the server count after local mutations so the derived can
  // detect an external payload replacement (Discard/Apply) and reset all keys.
  let expectedLen = rowKeys.length;

  const serversWithKeys = $derived.by<Array<{ s: ServerRecord; key: number }>>(() => {
    const ss = serversOf(payload);
    if (ss.length !== expectedLen) {
      // External reload (Discard / Apply): reset all keys so no stale state bleeds
      // between rows. TokenSecretField instances are fully remounted.
      rowKeys = ss.map(() => nextKey++);
      expectedLen = ss.length;
    }
    return ss.map((s, i) => ({ s, key: rowKeys[i] }));
  });

  function set(i: number, field: "id" | "url" | "token_env", v: string): void {
    payload = updateServer(payload, i, field, v);
    onChange();
  }
  function add(): void {
    rowKeys = [...rowKeys, nextKey++]; // stable key for the new tail row
    expectedLen = rowKeys.length;      // mark length as locally expected
    payload = addServer(payload);
    onChange();
  }
  function remove(i: number): void {
    rowKeys = rowKeys.filter((_, k) => k !== i); // drop key at index i
    expectedLen = rowKeys.length;                  // mark length as locally expected
    payload = removeServer(payload, i);
    onChange();
  }
  async function setSecret(name: string, value: string): Promise<void> {
    const code = await cfg.setSecret(name, value); // 204 on success
    if (code === 204) {
      payload = { ...payload, secrets: { ...payload.secrets, [name]: { set: true, source: "keychain" } } };
    } else {
      onError(`uložení tokenu '${name}' selhalo (HTTP ${code})`);
    }
  }
  async function clearSecret(name: string): Promise<void> {
    const code = await cfg.clearSecret(name); // 204 on success
    if (code === 204) {
      payload = { ...payload, secrets: { ...payload.secrets, [name]: { set: false, source: null } } };
    } else {
      onError(`smazání tokenu '${name}' selhalo (HTTP ${code})`);
    }
  }
</script>

<h2>Servers</h2>
{#each serversWithKeys as { s, key }, i (key)}
  <fieldset>
    <legend>{s.id || "(nový server)"} <button type="button" onclick={() => remove(i)}>×</button></legend>
    <TextField label="id" value={s.id} oninput={(v) => set(i, "id", v)} />
    <TextField label="url" value={s.url} oninput={(v) => set(i, "url", v)} />
    <TokenSecretField
      label="token"
      value={s.token_env}
      flag={secretFlag(payload, s.token_env)}
      oninput={(v) => set(i, "token_env", v)}
      onset={(val) => setSecret(s.token_env, val)}
      onclear={() => clearSecret(s.token_env)}
    />
  </fieldset>
{/each}
<button type="button" onclick={add}>+ přidat server</button>

<style>
  fieldset { border: 1px solid #2a2a30; border-radius: 6px; margin: 8px 0; padding: 8px 12px; }
  legend { color: #ccc; } legend button { color: #e05050; background: none; border: 0; cursor: pointer; }
  button { background: #1b1b1f; border: 1px solid #2a2a30; color: inherit; border-radius: 4px; padding: 4px 8px; cursor: pointer; }
  h2 { margin: 0 0 8px; }
</style>
