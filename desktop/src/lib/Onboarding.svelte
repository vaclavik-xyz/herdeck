<script lang="ts">
  // First-run (and re-onboarding) card for the floating-deck window. A thin
  // template over onboardingClient.ts: it binds form state and calls
  // transport.connect; all decision/parse logic lives in the client. The token
  // field is a plain password input whose value goes straight into the connect
  // request and is never read back.
  import type {
    SetupStatus,
    SetupTransport,
    ConnectRequest,
  } from "./onboardingClient";
  import { connectErrorMessage } from "./onboardingClient";

  let {
    view,
    status,
    transport,
    onConnected,
    onDismiss = undefined,
  }: {
    view: "welcome" | "reconnect";
    status: SetupStatus | null;
    transport: SetupTransport | null;
    onConnected: () => void;
    onDismiss?: (() => void) | undefined;
  } = $props();

  let showRemote = $state(false);
  let url = $state("");
  let token = $state("");
  let serverId = $state("");
  let busy = $state(false);
  let error = $state<string | null>(null);

  const localAvailable = $derived(status?.localHerdrAvailable === true);
  const savedAvailable = $derived(status?.savedRemoteAvailable === true);

  async function run(req: ConnectRequest): Promise<void> {
    if (!transport || busy) return;
    busy = true;
    error = null;
    const r = await transport.connect(req);
    busy = false;
    if (r.ok) {
      onConnected();
    } else {
      error = connectErrorMessage(r.error, status?.socketPath);
    }
  }

  function connectLocal(): void {
    void run({ choice: "local" });
  }
  function connectDemo(): void {
    void run({ choice: "demo" });
  }
  function connectSaved(): void {
    void run({ choice: "saved" });
  }
  function connectRemote(): void {
    const u = url.trim();
    if (!u || !token) {
      error = "Vyplň URL i token.";
      return;
    }
    const req: ConnectRequest = { choice: "remote", url: u, token };
    const id = serverId.trim();
    if (id) (req as { id?: string }).id = id;
    void run(req);
  }
</script>

<section class="onboarding">
  {#if view === "reconnect"}
    <h1>herdr neběží</h1>
    <p class="lead">Lokální připojení je zapamatované, ale herdr teď neběží.</p>
    {#if savedAvailable}
      <p class="lead">Máš uložené spojení.</p>
      <div class="actions">
        <button class="primary" disabled={busy} onclick={connectSaved}>Připojit k uloženému spojení</button>
      </div>
    {/if}
    <div class="actions">
      <button class="primary" disabled={busy} onclick={connectLocal}>Zkusit znovu</button>
      <button class="link" disabled={busy} onclick={() => (showRemote = !showRemote)}>
        Připojit vzdáleně…
      </button>
    </div>
  {:else}
    <h1>Připojit herdeck</h1>
    {#if savedAvailable}
      <p class="lead">Máš uložené spojení.</p>
      <div class="actions">
        <button class="primary" disabled={busy} onclick={connectSaved}>Připojit k uloženému spojení</button>
      </div>
    {/if}
    {#if localAvailable}
      <p class="lead ok">✓ herdr běží lokálně</p>
      <div class="actions">
        <button class="primary" disabled={busy} onclick={connectLocal}>Připojit</button>
        <button class="link" disabled={busy} onclick={() => (showRemote = !showRemote)}>
          Vzdálený herdr…
        </button>
      </div>
    {:else}
      <p class="lead">herdr nebyl lokálně nalezen — spusť ho, nebo se připoj vzdáleně.</p>
      <div class="actions">
        <button class="primary" disabled={busy} onclick={() => (showRemote = true)}>
          Vzdálený herdr…
        </button>
      </div>
    {/if}
  {/if}

  {#if showRemote || (view === "welcome" && !localAvailable)}
    <form class="remote" onsubmit={(e) => { e.preventDefault(); connectRemote(); }}>
      <label>URL<input type="text" placeholder="ws(s)://host:8788" bind:value={url} /></label>
      <label>Token<input type="password" bind:value={token} /></label>
      <label class="adv">ID (volitelné)<input type="text" placeholder="herdr" bind:value={serverId} /></label>
      <button class="primary" type="submit" disabled={busy}>Připojit</button>
    </form>
  {/if}

  <div class="footer">
    {#if view === "welcome"}
      <button class="link" disabled={busy} onclick={connectDemo}>Prozkoumat demo</button>
    {/if}
    {#if onDismiss}
      <button class="link dismiss" disabled={busy} onclick={onDismiss}>← zpět na deck</button>
    {/if}
  </div>

  {#if error}<p class="error" role="alert">{error}</p>{/if}
</section>

<style>
  .onboarding {
    box-sizing: border-box;
    padding: 24px 18px;
    background: #0b0b0d;
    color: #e7ecf3;
    font: 13px/1.4 system-ui, -apple-system, sans-serif;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  h1 {
    margin: 0;
    font-size: 17px;
  }
  .lead {
    margin: 0;
    color: #8b97a4;
  }
  .lead.ok {
    color: #3fb950;
  }
  .actions,
  .footer {
    display: flex;
    gap: 10px;
    align-items: center;
    flex-wrap: wrap;
  }
  .remote {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 10px;
    border-radius: 10px;
    background: #17171b;
  }
  .remote label {
    display: flex;
    flex-direction: column;
    gap: 3px;
    font-size: 11px;
    color: #8b97a4;
  }
  .remote input {
    padding: 6px 8px;
    border-radius: 6px;
    border: 1px solid #2a2a2e;
    background: #0b0b0d;
    color: #e7ecf3;
    font: inherit;
  }
  button.primary {
    padding: 7px 14px;
    border: none;
    border-radius: 7px;
    background: #2563eb;
    color: #fff;
    font: inherit;
    cursor: pointer;
  }
  button.primary:disabled {
    opacity: 0.5;
    cursor: default;
  }
  button.link {
    border: none;
    background: none;
    color: #5af;
    cursor: pointer;
    font: inherit;
    padding: 4px 0;
  }
  button.link.dismiss {
    margin-left: auto;
    color: #8b97a4;
  }
  .error {
    margin: 0;
    color: #f0883e;
  }
</style>
