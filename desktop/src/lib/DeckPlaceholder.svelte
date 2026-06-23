<script lang="ts">
  // ============================================================================
  // PLACEHOLDER — slice 3 (Tauri shell) only.
  //
  // This component is NOT the real deck. Its sole job is to prove the WebView
  // loaded and can reach the Python sidecar over the injected loopback url+token
  // (it shows the /health source + connected state).
  //
  // SLICE 2 replaces this with the real <DeckView /> component (poll /state,
  // diff + GET /tile PNGs, click -> POST /press). Mount point: see App.svelte,
  // the block marked "PLACEHOLDER: DeckView mounts here".
  // ============================================================================
  import type { Discovery, HealthResult } from "./sidecar";

  let {
    status,
    discovery,
    health,
    error,
  }: {
    status: string;
    discovery: Discovery | null;
    health: HealthResult | null;
    error: string | null;
  } = $props();

  const dot = (ok: boolean) => (ok ? "●" : "○");
</script>

<section class="placeholder">
  <header>
    <span class="logo">herdeck</span>
    <span class="badge">shell · phase&nbsp;1</span>
  </header>

  <div class="status-line">
    <span class="connected" class:ok={!!health}>{dot(!!health)}</span>
    <span>{status}</span>
  </div>

  {#if discovery}
    <dl class="kv">
      <dt>source</dt>
      <dd>{health?.source ?? discovery.source}</dd>
      <dt>connected</dt>
      <dd>{health ? (health.connected ? "yes" : "no") : "…"}</dd>
      <dt>sidecar</dt>
      <dd class="mono">{discovery.url}</dd>
    </dl>
  {/if}

  {#if error}
    <p class="error">{error}</p>
  {/if}

  <!-- The real DeckView (slice 2) renders the tile grid here. -->
  <div class="deck-slot" aria-hidden="true">
    <span>DeckView · slice&nbsp;2</span>
  </div>
</section>

<style>
  .placeholder {
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
    padding: 0.75rem 0.85rem;
    font: 13px/1.4 system-ui, -apple-system, sans-serif;
    color: #e7ecf3;
  }
  header {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
  }
  .logo {
    font-weight: 700;
    letter-spacing: 0.02em;
  }
  .badge {
    font-size: 11px;
    color: #9aa6b2;
  }
  .status-line {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    color: #c5cfdb;
  }
  .connected {
    color: #6b7785;
  }
  .connected.ok {
    color: #3fb950;
  }
  .kv {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 0.15rem 0.6rem;
    margin: 0;
    font-size: 12px;
  }
  .kv dt {
    color: #8b97a4;
  }
  .kv dd {
    margin: 0;
    text-align: right;
  }
  .mono {
    font-family: ui-monospace, SFMono-Regular, monospace;
    font-size: 11px;
  }
  .error {
    margin: 0;
    color: #f0883e;
    font-size: 12px;
  }
  .deck-slot {
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 120px;
    border: 1px dashed #39414c;
    border-radius: 8px;
    color: #5a6470;
    font-size: 11px;
  }
</style>
