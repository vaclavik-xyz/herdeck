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

  // keyGen: a stable closure-captured counter used by the derived below.
  // This lets us generate unique monotone keys without $effect timing issues.
  let keyGen = 0;

  // serversWithKeys is re-derived on every servers change. It expands/contracts
  // the keys array synchronously so the keyed {#each} never sees undefined keys.
  // We hold the prior key array in a captured variable so stable rows keep their key.
  let prevKeys: number[] = [];
  const serversWithKeys = $derived.by<Array<{ s: ServerRecord; key: number }>>(() => {
    const ss = serversOf(payload);
    const next: number[] = [];
    for (let i = 0; i < ss.length; i++) {
      next.push(prevKeys[i] !== undefined ? prevKeys[i] : keyGen++);
    }
    prevKeys = next;
    return ss.map((s, i) => ({ s, key: next[i] }));
  });

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
