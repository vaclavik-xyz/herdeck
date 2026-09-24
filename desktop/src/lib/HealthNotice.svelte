<script lang="ts">
  // A small, non-blocking status line that explains a dark deck: version
  // mismatches (app ≠ runtime, runtime ≠ bridge), a bridge that dropped or
  // rejected its token, a D200 that disconnected or is held by another
  // runtime. Polls the runtime /health through the Rust shell (never the
  // token in JS) and renders nothing while everything is fine.
  import Banner from "./Banner.svelte";
  import { healthLine } from "./healthStatus";
  import { defineMessages, locale } from "./i18n.svelte";
  import { visibilityGatedLoop } from "./pollGate";

  let { fetchHealth = null, intervalMs = 5000 }: {
    // `invoke("check_health")`; null (no discovery yet) keeps it silent.
    fetchHealth?: (() => Promise<unknown>) | null;
    intervalMs?: number;
  } = $props();

  const LM = defineMessages({
    en: {
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
    },
    cs: {
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
    },
  });

  let health = $state<unknown>(null);
  const line = $derived(healthLine(health, LM[locale.lang]));

  $effect(() => {
    const fetch = fetchHealth;
    if (!fetch) {
      health = null;
      return;
    }
    const loop = visibilityGatedLoop(async () => {
      try {
        health = await fetch();
      } catch {
        health = null; // runtime not reachable: DeckView already says offline
      }
    }, () => intervalMs);
    return () => loop.stop();
  });
</script>

{#if line}
  <div class="health-notice">
    <Banner kind="warning" message={line} />
  </div>
{/if}

<style>
  .health-notice { font: var(--t-label); }
</style>
