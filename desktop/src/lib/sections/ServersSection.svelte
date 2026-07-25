<script lang="ts">
  import PlugsConnected from "phosphor-svelte/lib/PlugsConnected";
  import { invoke } from "@tauri-apps/api/core";
  import TextField from "../fields/TextField.svelte";
  import TokenSecretField from "../fields/TokenSecretField.svelte";
  import ConfirmRemoveButton from "../fields/ConfirmRemoveButton.svelte";
  import {
    commandTransport as cfgTransport,
    serversOf,
    addServer,
    removeServer,
    updateServer,
    setLocalSessionSelected,
    secretFlag,
    type ConfigPayload,
    type ServerRecord,
  } from "../configClient";
  import { defineMessages, fieldHelp, fmt, locale } from "../i18n.svelte";

  let { payload = $bindable(), onChange, onError }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void } = $props();

  const cfg = cfgTransport((cmd, args) => invoke(cmd, args));

  const servers = $derived(serversOf(payload));
  let removalRevision = $state(0);

  // Current-language tooltips for every field — required for each labelled
  // field (enforced by sections.help.test.ts); texts live in help.ts.
  const HELP = $derived(fieldHelp("servers"));

  const LM = defineMessages({
    en: {
      heading: "Servers",
      local_heading: "Local sessions",
      local_hint: "Available Herdr sockets on this Mac. Changes apply without restarting herdeck.",
      available: "available",
      offline: "not running",
      default_session: "default",
      remote_heading: "Remote bridges",
      new_server: "(new server)",
      remove_server: "Remove server",
      add_server: "+ add server",
      save_token_failed: "saving token '{name}' failed (HTTP {code})",
      clear_token_failed: "clearing token '{name}' failed (HTTP {code})",
    },
    cs: {
      heading: "Servery",
      local_heading: "Lokální sessions",
      local_hint: "Dostupné Herdr sockety na tomto Macu. Změny se projeví bez restartu herdecku.",
      available: "dostupná",
      offline: "neběží",
      default_session: "výchozí",
      remote_heading: "Vzdálené bridges",
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
    removalRevision += 1;
    onChange();
  }
  function selectLocal(name: string, selected: boolean): void {
    payload = setLocalSessionSelected(payload, name, selected);
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
<section class="local-sessions" aria-labelledby="local-sessions-heading">
  <div class="section-head">
    <div>
      <h3 id="local-sessions-heading">{lm.local_heading}</h3>
      <p>{lm.local_hint}</p>
    </div>
    <span class="socket-mark" aria-hidden="true"><PlugsConnected size={22} /></span>
  </div>
  <div class="session-rail">
    {#each payload.localSessions as session (session.name)}
      <label class:unavailable={!session.available}>
        <input
          type="checkbox"
          checked={session.selected}
          onchange={(event) =>
            selectLocal(session.name, (event.currentTarget as HTMLInputElement).checked)}
        />
        <span class:online={session.available} class="status-dot" aria-hidden="true"></span>
        <span class="session-copy">
          <strong>{session.name === "default" ? lm.default_session : session.name}</strong>
          <small>{session.available ? lm.available : lm.offline}</small>
        </span>
      </label>
    {/each}
  </div>
</section>

<h3 class="remote-heading">{lm.remote_heading}</h3>
<!-- Index keying is correct here: this is an append / remove list (no row reordering),
     and the only per-row transient state (TokenSecretField's in-progress secret entry) is
     disposable. Editing a field keeps the same index → same DOM node → focus preserved.
     A stable-id apparatus would add complexity that 9 řez-4 sections would clone. -->
{#each servers as s, i (i)}
  <fieldset>
    <legend>{s.id || lm.new_server} <ConfirmRemoveButton title={lm.remove_server} identity={`${s.id}\u0000${s.url}\u0000${s.token_env}`} resetKey={removalRevision} onconfirm={() => remove(i)} /></legend>
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
  .local-sessions {
    border: 1px solid var(--border, #2b323d);
    border-radius: var(--radius-panel, 10px);
    background: var(--surface, #151a21);
    padding: 13px 15px;
    margin-bottom: 16px;
  }
  .section-head { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
  .section-head h3, .remote-heading { margin: 0; color: #dce1e8; font-size: 14px; }
  .section-head p { margin: 3px 0 0; color: var(--muted, #939ca9); font-size: 11px; max-width: 430px; }
  .socket-mark { color: #8ba7df; font: 22px/1 ui-monospace, monospace; }
  .session-rail { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
  .session-rail label {
    min-width: 120px;
    display: grid;
    grid-template-columns: auto 8px 1fr;
    gap: 7px;
    align-items: center;
    border: 1px solid var(--border, #2b323d);
    border-radius: var(--radius-control, 7px);
    padding: 8px 10px;
    background: var(--field, #10141a);
    cursor: pointer;
  }
  .session-rail label:has(input:checked) { border-color: #6382c1; background: var(--signal-soft, rgb(111 145 217 / .14)); }
  .session-rail label.unavailable { opacity: .62; }
  .session-rail input { margin: 0; accent-color: #6f91d9; }
  .status-dot { width: 7px; height: 7px; border-radius: 50%; background: #6b7280; }
  .status-dot.online { background: var(--green, #55c68d); }
  .session-copy { display: flex; flex-direction: column; min-width: 0; }
  .session-copy strong { color: #dce1e8; font: 600 12px/1.2 ui-monospace, SFMono-Regular, monospace; overflow: hidden; text-overflow: ellipsis; }
  .session-copy small { color: var(--muted, #939ca9); font-size: 10px; }
  .remote-heading { margin: 0 0 8px; }
  fieldset { border: 1px solid #2a2a30; border-radius: 6px; margin: 8px 0; padding: 8px 12px; }
  legend { color: #ccc; }
  button { background: #1b1b1f; border: 1px solid #2a2a30; color: inherit; border-radius: 4px; padding: 4px 8px; cursor: pointer; }
  h2 { margin: 0 0 8px; }
</style>
