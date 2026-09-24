<script lang="ts">
  // Maintenance: what an agent used to do from a terminal to keep herdeck
  // running — versions, where the runtime comes from (and making it a service
  // of this app), the D200 (restart / USB power-cycle) and bridge updates.
  // Not a config section: it reads GET /maintenance through the Rust
  // `maintenance_call` proxy and acts through it, `runtime_service` and
  // `open_log`. All texts: maintenanceMessages.ts (en + cs).
  import { onDestroy } from "svelte";
  import type { InvokeFn } from "../deckClient";
  import { fmt, locale } from "../i18n.svelte";
  import { visibilityGatedLoop } from "../pollGate";
  import {
    fetchMaintenance, restartDeck, powerCycleDeck, runBridgeUpdate, runtimeService, openLog,
    runtimeOrigin, unitOwner, versionRows, bridgeOffer, managedBridgeCommand,
    type BridgeUpdateView, type MaintenanceStatus, type ServiceAction,
  } from "../maintenanceClient";
  import {
    MAINTENANCE_MESSAGES, bridgeUpdateText, d200StateText, deckOutcomeText, durationText,
    originText, powerCycleReasonText,
  } from "../maintenanceMessages";

  let { invoke = null, pollMs = 5000, updateWaitMs = undefined }: {
    // Tauri `invoke`; null (browser preview / no runtime yet) shows nothing live.
    invoke?: InvokeFn | null;
    pollMs?: number;
    updateWaitMs?: number;
  } = $props();

  const lm = $derived(MAINTENANCE_MESSAGES[locale.lang]);

  let status = $state<MaintenanceStatus | null>(null);
  let loadError = $state("");
  let alive = true;
  onDestroy(() => { alive = false; });

  async function refresh(): Promise<void> {
    const call = invoke;
    if (!call) return;
    const result = await fetchMaintenance(call);
    if (!alive) return;
    if (result.kind === "ok") {
      status = result.status;
      loadError = "";
    } else {
      loadError = result.message;
    }
  }

  let loop: ReturnType<typeof visibilityGatedLoop> | null = null;
  $effect(() => {
    const call = invoke;
    if (!call) return;
    const current = visibilityGatedLoop(refresh, () => pollMs);
    loop = current;
    return () => { current.stop(); if (loop === current) loop = null; };
  });

  const origin = $derived(status ? runtimeOrigin(status) : null);
  const owner = $derived(status ? unitOwner(status) : "none");
  const program = $derived(status?.service.program ?? "?");
  const rows = $derived(status ? versionRows(status) : []);

  // --- runtime service ---
  let serviceBusy = $state<ServiceAction | null>(null);
  let serviceNote = $state<{ ok: boolean; text: string } | null>(null);
  let confirming = $state<"install" | "uninstall" | null>(null);

  async function service(action: ServiceAction, replace = false): Promise<void> {
    const call = invoke;
    if (!call || serviceBusy) return;
    confirming = null;
    serviceBusy = action;
    serviceNote = null;
    const r = await runtimeService(call, action, { replace });
    if (!alive) return;
    serviceBusy = null;
    serviceNote = r.ok
      ? { ok: true, text: lm.service_ok }
      : r.timedOut
        ? { ok: false, text: fmt(lm.service_timeout, { detail: r.detail }) }
        : { ok: false, text: fmt(lm.service_failed, { code: r.exitCode ?? "–", detail: r.detail }) };
    loop?.kick();
  }

  let logNote = $state("");
  async function open(kind: "runtime" | "app"): Promise<void> {
    const call = invoke;
    if (!call) return;
    const r = await openLog(call, kind);
    if (!alive) return;
    logNote = r.ok ? "" : fmt(lm.log_failed, { detail: r.detail });
  }

  // --- deck ---
  let deckBusy = $state<"restart" | "cycle" | null>(null);
  let deckNote = $state<{ ok: boolean; text: string; command: string | null } | null>(null);
  async function deck(kind: "restart" | "cycle"): Promise<void> {
    const call = invoke;
    if (!call || deckBusy) return;
    deckBusy = kind;
    deckNote = null;
    const outcome = kind === "restart" ? await restartDeck(call) : await powerCycleDeck(call);
    if (!alive) return;
    deckBusy = null;
    deckNote = deckOutcomeText(outcome, lm);
    loop?.kick();
  }

  // --- bridges ---
  let updates = $state<Record<string, { running: boolean; view: BridgeUpdateView | null }>>({});
  async function updateBridge(id: string): Promise<void> {
    const call = invoke;
    if (!call || updates[id]?.running) return;
    updates[id] = { running: true, view: null };
    const view = await runBridgeUpdate(call, id, (v) => {
      if (alive) updates[id] = { running: v.code === "pending", view: v };
    }, { isCancelled: () => !alive, waitMs: updateWaitMs });
    if (!alive) return;
    updates[id] = { running: false, view };
    loop?.kick();
  }

  // --- copy ---
  let copied = $state("");
  async function copy(command: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(command);
      copied = command;
    } catch {
      copied = "";
    }
  }

  function uptime(s: number | null): string {
    return s == null ? "?" : durationText(s * 1000);
  }
</script>

{#snippet commandBox(command: string)}
  <div class="command">
    <code>{command}</code>
    <button type="button" class="copy" title={lm.copy_title} aria-label={lm.copy_title} onclick={() => copy(command)}>
      {copied === command ? lm.copied : lm.copy}
    </button>
  </div>
{/snippet}

<div class="maintenance">
  <div class="toolbar">
    {#if loadError}<p class="note bad" role="alert">{fmt(lm.unreachable, { error: loadError })}</p>{/if}
    {#if !status && !loadError}<p class="note">{lm.loading}</p>{/if}
    <button type="button" onclick={() => loop?.kick()} disabled={!invoke}>{lm.refresh}</button>
  </div>

  {#if status}
    <section class="block" aria-labelledby="mt-versions">
      <h3 id="mt-versions">{lm.versions}</h3>
      <p class="hint">{lm.versions_hint}</p>
      <dl class="versions">
        {#each rows as row (row.kind + row.id)}
          <div class:mismatch={row.mismatch} data-row={row.kind === "bridge" ? `bridge:${row.id}` : row.kind}>
            <dt>{row.kind === "app" ? lm.app : row.kind === "runtime" ? lm.runtime : fmt(lm.bridge, { id: row.id })}</dt>
            <dd>
              {row.version ?? lm.unknown}
              {#if row.mismatch}<span class="flag">⚠ {lm.mismatch}</span>{/if}
            </dd>
          </div>
        {/each}
      </dl>
    </section>

    <section class="block" aria-labelledby="mt-runtime">
      <h3 id="mt-runtime">{lm.runtime_heading}</h3>
      <p class="state" data-origin={origin}>{originText(origin ?? "attached", status.service.program, status.pid, lm)}</p>
      <p class="hint">{fmt(lm.pid_uptime, { pid: status.pid ?? "?", uptime: uptime(status.uptimeS) })}</p>
      <div class="actions">
        {#if owner !== "this_app"}
          {@const replacing = owner === "other_app" || owner === "checkout"}
          {#if confirming === "install"}
            <span class="confirm-text" data-confirm={replacing ? "replace" : "install"}>{replacing ? fmt(lm.replace_confirm, { program }) : lm.install_confirm}</span>
            <button type="button" class="primary" data-action="confirm" onclick={() => service("install", replacing)}>{lm.confirm}</button>
            <button type="button" onclick={() => (confirming = null)}>{lm.cancel}</button>
          {:else}
            <button type="button" class="primary" data-action={replacing ? "replace" : "install"} disabled={!status.app?.bundledRuntime || serviceBusy != null} onclick={() => (confirming = "install")}>{replacing ? lm.replace_service : lm.install_service}</button>
          {/if}
        {/if}
        <button type="button" data-action="restart-runtime" disabled={owner !== "this_app" || serviceBusy != null} title={owner === "this_app" ? undefined : owner === "none" ? lm.restart_runtime_na : fmt(lm.restart_not_ours, { program })} onclick={() => service("restart")}>{lm.restart_runtime}</button>
        {#if owner === "this_app" || owner === "other_app"}
          {#if confirming === "uninstall"}
            <span class="confirm-text" data-confirm="uninstall">{owner === "other_app" ? fmt(lm.uninstall_other_confirm, { program }) : lm.uninstall_confirm}</span>
            <button type="button" data-action="confirm" onclick={() => service("uninstall", owner === "other_app")}>{lm.confirm}</button>
            <button type="button" onclick={() => (confirming = null)}>{lm.cancel}</button>
          {:else}
            <button type="button" data-action="uninstall" disabled={serviceBusy != null} onclick={() => (confirming = "uninstall")}>{lm.uninstall_service}</button>
          {/if}
        {/if}
        <button type="button" data-action="runtime-log" disabled={!status.logs.runtime} onclick={() => open("runtime")}>{lm.open_runtime_log}</button>
        <button type="button" data-action="app-log" onclick={() => open("app")}>{lm.open_app_log}</button>
      </div>
      {#if owner !== "this_app"}
        <p class="hint">{status.app?.bundledRuntime ? lm.install_hint : lm.install_dev}</p>
      {/if}
      {#if owner === "none"}<p class="hint">{lm.restart_runtime_na}</p>{:else if owner !== "this_app"}<p class="hint" data-hint="not-ours">{fmt(lm.restart_not_ours, { program })}</p>{/if}
      {#if !status.logs.runtime}<p class="hint">{lm.no_runtime_log}</p>{/if}
      {#if serviceBusy}<p class="note">{lm.service_running}</p>{/if}
      {#if serviceNote}<p class="note" class:bad={!serviceNote.ok} data-note="service">{serviceNote.text}</p>{/if}
      {#if logNote}<p class="note bad">{logNote}</p>{/if}
    </section>

    <section class="block" aria-labelledby="mt-deck">
      <h3 id="mt-deck">{lm.deck_heading}</h3>
      <p class="state" data-d200={status.d200.state}>{d200StateText(status.d200, lm)}</p>
      {#if status.d200.usbLocation}<p class="hint">{fmt(lm.usb_location, { location: status.d200.usbLocation })}</p>{/if}
      {#if status.d200.lastError && !status.d200.connected}<p class="hint">{fmt(lm.last_error, { error: status.d200.lastError })}</p>{/if}
      <div class="actions">
        <button type="button" data-action="restart-deck" disabled={deckBusy != null || !status.d200.supervised} onclick={() => deck("restart")}>{lm.restart_deck}</button>
        <button type="button" data-action="power-cycle" disabled={deckBusy != null || !status.d200.powerCycle.available} onclick={() => deck("cycle")}>{lm.power_cycle}</button>
      </div>
      {#if !status.d200.powerCycle.available && status.d200.powerCycle.reason}
        <p class="hint" data-reason={status.d200.powerCycle.reason}>{powerCycleReasonText(status.d200.powerCycle.reason, lm)}</p>
      {/if}
      {#if deckBusy}<p class="note">{lm.service_running}</p>{/if}
      {#if deckNote}
        <p class="note" class:bad={!deckNote.ok} data-note="deck">{deckNote.text}</p>
        {#if deckNote.command}{@render commandBox(deckNote.command)}{/if}
      {/if}
    </section>

    <section class="block" aria-labelledby="mt-bridges">
      <h3 id="mt-bridges">{lm.bridges_heading}</h3>
      {#each status.servers as server (server.id)}
        {@const upd = updates[server.id]}
        {@const text = upd?.view ? bridgeUpdateText(upd.view, lm) : null}
        {@const offer = bridgeOffer(server, status.version)}
        <div class="bridge" data-server={server.id}>
          <div class="bridge-head">
            <strong>{server.id}</strong>
            <span class:ok={server.connected === true}>{server.connected ? lm.connected : lm.disconnected}</span>
            <span class:flag={server.bridgeVersion != null && status.version != null && server.bridgeVersion !== status.version}>{server.bridgeVersion ?? lm.unknown}</span>
            <span class="dim">{server.managed === true ? lm.managed : server.managed === false ? lm.not_managed : lm.managed_unknown}</span>
            {#if offer === "update" || upd?.running}
              <button
                type="button"
                data-action="update-bridge"
                disabled={upd?.running === true || server.connected !== true}
                title={fmt(lm.update_bridge_title, { id: server.id, version: status.version ?? "?" })}
                onclick={() => updateBridge(server.id)}
              >{upd?.running ? lm.updating : lm.update_bridge}</button>
            {/if}
          </div>
          {#if !upd?.view}
            {#if offer === "install_managed"}
              <p class="hint" data-offer="install_managed">{lm.upd_not_managed}</p>
              {@render commandBox(managedBridgeCommand(status.version))}
            {:else if offer === "unknown" || offer === "unsupported"}
              <p class="hint" data-offer={offer}>{offer === "unknown" ? lm.offer_unknown : lm.offer_unsupported}</p>
            {/if}
          {/if}
          {#if server.lastError && server.connected !== true}<p class="hint">{fmt(lm.last_error, { error: server.lastError })}</p>{/if}
          {#if upd?.view && upd.view.progress.length > 0}
            <ol class="progress">
              {#each upd.view.progress as step (step.seq)}<li><span class="dim">{step.stage}</span> {step.message}</li>{/each}
            </ol>
          {/if}
          {#if text}
            <p class="note" class:bad={upd?.view?.ok === false} data-note="bridge">{text.text}</p>
            {#if text.command}{@render commandBox(text.command)}{/if}
            {#if upd?.view?.output}<pre class="output">{upd.view.output}</pre>{/if}
          {/if}
        </div>
      {:else}
        <p class="hint">{lm.no_bridges}</p>
      {/each}
    </section>
  {/if}
</div>

<style>
  .maintenance { display: flex; flex-direction: column; gap: var(--s4); }
  .toolbar { display: flex; align-items: center; justify-content: flex-end; gap: var(--s3); }
  .toolbar .note { margin-right: auto; }
  .block { border: 1px solid var(--line); border-radius: var(--r-panel); background: var(--panel); padding: var(--s4) var(--s5); }
  h3 { margin: 0 0 var(--s2); color: var(--text); font: var(--t-h2); }
  .hint { margin: var(--s1) 0; color: var(--text-dim); font: var(--t-help); }
  .state { margin: 0 0 var(--s1); color: var(--text); }
  .note { margin: var(--s2) 0 0; color: var(--st-working); font: var(--t-help); }
  .note.bad { color: var(--st-blocked); }
  .versions { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: var(--s2); margin: var(--s2) 0 0; }
  .versions div { border: 1px solid var(--line); border-radius: var(--r-control); padding: var(--s2) var(--s3); background: var(--field); }
  .versions div.mismatch { border-color: var(--st-blocked); }
  .versions dt { color: var(--text-dim); font-size: 10px; }
  .versions dd { margin: 0; color: var(--text); font: var(--t-mono); }
  .flag { color: var(--st-blocked); }
  .dim { color: var(--text-dim); }
  .ok { color: var(--st-working); }
  .actions { display: flex; flex-wrap: wrap; align-items: center; gap: var(--s2); margin-top: var(--s3); }
  .confirm-text { color: var(--text); font: var(--t-help); }
  .bridge { border-top: 1px solid var(--line); padding: var(--s3) 0; }
  .bridge:first-of-type { border-top: 0; }
  .bridge-head { display: flex; flex-wrap: wrap; align-items: center; gap: var(--s3); }
  .bridge-head button { margin-left: auto; }
  .progress { margin: var(--s2) 0 0; padding-left: var(--s5); color: var(--text); font: var(--t-mono); font-size: 11px; }
  .output { max-height: 160px; overflow: auto; margin: var(--s2) 0 0; padding: var(--s2); background: var(--field); border-radius: var(--r-control); font: var(--t-mono); font-size: 11px; white-space: pre-wrap; }
  .command { display: flex; align-items: center; gap: var(--s2); margin-top: var(--s2); }
  .command code { flex: 1; min-width: 0; overflow-x: auto; padding: var(--s2); background: var(--field); border-radius: var(--r-control); font: var(--t-mono); white-space: nowrap; }
  button {
    min-height: 30px;
    padding: 0 var(--s3);
    border: 1px solid var(--line-strong);
    border-radius: var(--r-control);
    background: var(--field);
    color: var(--text-dim);
    cursor: pointer;
  }
  button:hover:not(:disabled) { color: var(--text); background: var(--panel-raised); }
  button:disabled { opacity: .55; cursor: default; }
  button.primary { border-color: var(--accent-strong); color: var(--text); }
</style>
