<script lang="ts">
  // A small, non-blocking status line that explains a dark deck: a config
  // that does not load (the runtime then shows an error, never demo agents), version
  // mismatches (app ≠ runtime, runtime ≠ bridge), a bridge that dropped or
  // rejected its token, a D200 that disconnected or is held by another
  // runtime. Polls the runtime /health through the Rust shell (never the
  // token in JS) and renders nothing while everything is fine. With `invoke`
  // it also offers the fix inline: "Update bridge" on a bridge version
  // mismatch, "Restart deck" on a D200 problem, "Open Maintenance" otherwise.
  import Banner from "./Banner.svelte";
  import type { InvokeFn } from "./deckClient";
  import { healthActions, healthItems, type HealthAction } from "./healthStatus";
  import { defineMessages, fmt, locale } from "./i18n.svelte";
  import { restartDeck, runBridgeUpdate } from "./maintenanceClient";
  import { MAINTENANCE_MESSAGES, bridgeUpdateText, deckOutcomeText } from "./maintenanceMessages";
  import { visibilityGatedLoop } from "./pollGate";

  let { fetchHealth = null, intervalMs = 5000, invoke = null, onOpenMaintenance = null }: {
    // `invoke("check_health")`; null (no discovery yet) keeps it silent.
    fetchHealth?: (() => Promise<unknown>) | null;
    intervalMs?: number;
    // Tauri `invoke` for the inline actions; null = no buttons.
    invoke?: InvokeFn | null;
    onOpenMaintenance?: (() => void) | null;
  } = $props();

  const LM = defineMessages({
    en: {
      config_error: "config error: {error} — see Maintenance",
      runtime_mismatch: "runtime {runtime} ≠ app {app} — restart the runtime",
      bridge_mismatch: "bridge {id} {bridge} ≠ runtime {runtime} — update the bridge",
      bridge_protocol: "bridge {id} speaks a newer protocol — update the runtime",
      bridge_token: "bridge {id}: token rejected {since}",
      bridge_down: "bridge {id}: disconnected {since}",
      d200_down: "D200: disconnected {since}",
      d200_locked: "D200: driven by another runtime (pid {pid})",
      seconds: "{n} s",
      minutes: "{n} min",
      hours: "{n} h",
      update_bridge_id: "Update bridge {id}",
    },
    cs: {
      config_error: "chyba configu: {error} — viz Údržba",
      runtime_mismatch: "runtime {runtime} ≠ aplikace {app} — restartuj runtime",
      bridge_mismatch: "bridge {id} {bridge} ≠ runtime {runtime} — aktualizuj bridge",
      bridge_protocol: "bridge {id} mluví novějším protokolem — aktualizuj runtime",
      bridge_token: "bridge {id}: token odmítnut {since}",
      bridge_down: "bridge {id}: odpojeno {since}",
      d200_down: "D200: odpojeno {since}",
      d200_locked: "D200: ovládá ho jiný runtime (pid {pid})",
      seconds: "{n} s",
      minutes: "{n} min",
      hours: "{n} h",
      update_bridge_id: "Aktualizovat bridge {id}",
    },
  });
  const lm = $derived(LM[locale.lang]);
  const mm = $derived(MAINTENANCE_MESSAGES[locale.lang]);

  let health = $state<unknown>(null);
  const items = $derived(healthItems(health, lm));
  const line = $derived(items.map((item) => item.text).join(" · "));
  const actions = $derived(invoke ? healthActions(items) : []);

  let busy = $state("");
  let result = $state<{ ok: boolean; text: string } | null>(null);
  let loop: ReturnType<typeof visibilityGatedLoop> | null = null;

  function actionKey(a: HealthAction): string {
    return a.kind === "update_bridge" ? `update:${a.serverId}` : a.kind;
  }
  function actionLabel(a: HealthAction): string {
    if (a.kind === "update_bridge") return fmt(lm.update_bridge_id, { id: a.serverId });
    return a.kind === "restart_deck" ? mm.restart_deck : mm.open_maintenance;
  }

  async function run(a: HealthAction): Promise<void> {
    const call = invoke;
    if (!call || busy) return;
    if (a.kind === "open_maintenance") {
      if (onOpenMaintenance) onOpenMaintenance();
      else void call("open_maintenance").catch(() => {});
      return;
    }
    busy = actionKey(a);
    result = null;
    try {
      if (a.kind === "restart_deck") {
        const o = deckOutcomeText(await restartDeck(call), mm);
        result = { ok: o.ok, text: o.command ? `${o.text} ${o.command}` : o.text };
      } else {
        const view = await runBridgeUpdate(call, a.serverId, (v) => {
          result = { ok: v.ok, text: bridgeUpdateText(v, mm).text };
        });
        const t = bridgeUpdateText(view, mm);
        result = { ok: view.ok, text: t.command ? `${t.text} ${t.command}` : t.text };
      }
    } finally {
      busy = "";
      loop?.kick();
    }
  }

  $effect(() => {
    const fetch = fetchHealth;
    if (!fetch) {
      health = null;
      return;
    }
    const current = visibilityGatedLoop(async () => {
      try {
        health = await fetch();
      } catch {
        health = null; // runtime not reachable: DeckView already says offline
      }
    }, () => intervalMs);
    loop = current;
    return () => { current.stop(); if (loop === current) loop = null; };
  });
</script>

{#if line || result}
  <div class="health-notice">
    {#if line}<Banner kind="warning" message={line} />{/if}
    {#if actions.length > 0}
      <div class="health-actions">
        {#each actions as action (actionKey(action))}
          <button type="button" data-action={actionKey(action)} disabled={busy !== ""} onclick={() => run(action)}>
            {busy === actionKey(action) ? mm.service_running : actionLabel(action)}
          </button>
        {/each}
      </div>
    {/if}
    {#if result}<p class="health-result" class:bad={!result.ok} role="status">{result.text}</p>{/if}
  </div>
{/if}

<style>
  .health-notice { font: var(--t-label); }
  .health-actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; }
  .health-actions button { background: transparent; border: 1px solid var(--st-blocked); color: var(--st-blocked); border-radius: 5px; padding: 2px 8px; cursor: pointer; font: inherit; }
  .health-actions button:disabled { opacity: .6; cursor: default; }
  .health-result { margin: 4px 0 0; color: var(--st-working); user-select: text; }
  .health-result.bad { color: var(--st-blocked); }
</style>
