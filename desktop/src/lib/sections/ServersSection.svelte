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

  // Per-row stable keys. Plain (non-$state) variables — only serversWithKeys
  // (which is $derived) needs to be reactive; rowKeys is its private bookkeeping.
  let nextKey = 0;
  // Initialize eagerly so the first render already has correct keys.
  let rowKeys: number[] = serversOf(payload).map(() => nextKey++);

  // Pure derived: reads payload → servers → zips with rowKeys.
  // rowKeys is plain non-reactive state mutated before payload is reassigned
  // in add/remove, so the derived always sees a consistent rowKeys on re-run.
  // For external reloads that skip add/remove (Discard/Apply), reconcile here
  // by reading and locally extending/truncating — writing only to local vars,
  // not $state, which Svelte 5 permits inside $derived.by.
  const serversWithKeys = $derived.by<Array<{ s: ServerRecord; key: number }>>(() => {
    const ss = serversOf(payload);
    // Grow for external reload that adds rows.
    while (rowKeys.length < ss.length) rowKeys.push(nextKey++);
    // Shrink for external reload that removes rows.
    if (rowKeys.length > ss.length) rowKeys = rowKeys.slice(0, ss.length);
    return ss.map((s, i) => ({ s, key: rowKeys[i] }));
  });

  function set(i: number, field: "id" | "url" | "token_env", v: string): void {
    payload = updateServer(payload, i, field, v);
    onChange();
  }
  function add(): void {
    rowKeys = [...rowKeys, nextKey++]; // stable key for the new tail row
    payload = addServer(payload); // triggers derived re-run with keys already updated
    onChange();
  }
  function remove(i: number): void {
    rowKeys = rowKeys.filter((_, k) => k !== i); // drop key at index i first
    payload = removeServer(payload, i); // then trigger derived re-run
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
