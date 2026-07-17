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
  import { connectErrorMessage, shouldAutoReconnect } from "./onboardingClient";
  import { defineMessages, fmt, locale } from "./i18n.svelte";

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

  const LM = defineMessages({
    en: {
      connecting: "Connecting…",
      reconnect_h1: "herdr is not running",
      reconnect_lead: "The local connection is remembered, but herdr is not running right now.",
      reconnect_hint: "Start {herdr} in a terminal (socket: {socket}). I'll reconnect automatically once it's up.",
      retry: "Try again",
      connect_saved: "Connect to the saved connection",
      connect_remote_toggle: "Connect remotely…",
      welcome_h1: "Connect herdeck",
      local_ok: "✓ herdr is running locally",
      connect_local: "Connect locally",
      connect: "Connect",
      remote_toggle: "Remote herdr…",
      no_local: "herdr was not found locally — start it, or connect remotely below.",
      url: "URL",
      token: "Token",
      id_optional: "ID (optional)",
      fill_url_token: "Fill in both URL and token.",
      demo: "Explore the demo",
      back_to_deck: "← back to the deck",
      sessions_h: "Active sessions",
      sessions_hint: "Choose any local sessions and optionally keep the saved remote bridge.",
      saved_remote: "Saved remote bridge",
      apply_connections: "Apply connections",
      running: "running",
      stopped: "not running",
    },
    cs: {
      connecting: "Připojuji…",
      reconnect_h1: "herdr neběží",
      reconnect_lead: "Lokální připojení je zapamatované, ale herdr teď neběží.",
      reconnect_hint: "Spusť {herdr} v terminálu (socket: {socket}). Jakmile naběhne, připojím se automaticky.",
      retry: "Zkusit znovu",
      connect_saved: "Připojit k uloženému spojení",
      connect_remote_toggle: "Připojit vzdáleně…",
      welcome_h1: "Připojit herdeck",
      local_ok: "✓ herdr běží lokálně",
      connect_local: "Připojit lokálně",
      connect: "Připojit",
      remote_toggle: "Vzdálený herdr…",
      no_local: "herdr nebyl lokálně nalezen — spusť ho, nebo se připoj vzdáleně níže.",
      url: "URL",
      token: "Token",
      id_optional: "ID (volitelné)",
      fill_url_token: "Vyplň URL i token.",
      demo: "Prozkoumat demo",
      back_to_deck: "← zpět na deck",
      sessions_h: "Aktivní sessions",
      sessions_hint: "Vyber libovolné lokální sessions a případně ponech uložený vzdálený bridge.",
      saved_remote: "Uložený vzdálený bridge",
      apply_connections: "Použít připojení",
      running: "běží",
      stopped: "neběží",
    },
  });
  const lm = $derived(LM[locale.lang]);

  let showRemote = $state(false);
  let url = $state("");
  let token = $state("");
  let serverId = $state("");
  // WHICH action is connecting — the pressed button shows "Připojuji…" instead
  // of the whole card just greying out for a multi-second probe.
  let busyAction = $state<string | null>(null);
  const busy = $derived(busyAction != null);
  let error = $state<string | null>(null);
  let selectedSessions = $state<string[]>([]);
  let includeSaved = $state(false);
  let seededSessions = $state(false);

  const localAvailable = $derived(status?.localHerdrAvailable === true);
  const savedAvailable = $derived(status?.savedRemoteAvailable === true);
  const localSessions = $derived(status?.localSessions ?? []);

  $effect(() => {
    if (!status || seededSessions) return;
    selectedSessions = status.localSessions
      .filter((session) => session.selected)
      .map((session) => session.name);
    includeSaved = status.savedRemoteAvailable
      && (status.mode === "remote" || status.mode === "mixed");
    seededSessions = true;
  });

  // Latch (not derive) the remote form open when there is no local herdr: a
  // derived condition made the form vanish mid-typing when herdr appeared
  // during the 2.5s /setup poll.
  $effect(() => {
    if (view === "welcome" && status != null && !localAvailable) showRemote = true;
  });

  // The user already CHOSE local — when herdr (re)appears, reconnect without
  // demanding a click. Gated on the PERSISTED choice rather than the current
  // view: the moment the socket exists the backend reports reason=first_run,
  // so the parent flips this card to "welcome" before a view-gated effect
  // could ever fire. A manual re-onboarding session (onDismiss present) is
  // the user's explicit request to change things — never auto-connect there.
  let autoReconnectTried = $state(false);
  $effect(() => {
    if (
      shouldAutoReconnect({
        view,
        choice: status?.choice ?? null,
        localAvailable,
        busy,
        tried: autoReconnectTried,
        manual: onDismiss != null,
      })
    ) {
      autoReconnectTried = true;
      const remembered = status?.localSessions
        .filter((session) => session.selected)
        .map((session) => session.name) ?? [];
      if (remembered.length > 0) {
        void run(
          {
            choice: "sessions",
            sessions: remembered,
            include_saved: status?.savedRemoteAvailable === true
              && (status?.mode === "remote" || status?.mode === "mixed"),
          },
          "sessions",
        );
      } else {
        connectLocal();
      }
    }
  });

  async function run(req: ConnectRequest, action: string): Promise<void> {
    if (!transport || busy) return;
    busyAction = action;
    error = null;
    const r = await transport.connect(req);
    busyAction = null;
    if (r.ok) {
      onConnected();
    } else {
      error = connectErrorMessage(r.error, status?.socketPath, locale.lang);
    }
  }

  function connectLocal(): void {
    void run({ choice: "local" }, "local");
  }
  function connectDemo(): void {
    void run({ choice: "demo" }, "demo");
  }
  function connectSaved(): void {
    void run({ choice: "saved" }, "saved");
  }
  function connectRemote(): void {
    const u = url.trim();
    if (!u || !token) {
      error = lm.fill_url_token;
      return;
    }
    const req: ConnectRequest = { choice: "remote", url: u, token };
    const id = serverId.trim();
    if (id) (req as { id?: string }).id = id;
    void run(req, "remote");
  }
  function toggleSession(name: string, selected: boolean): void {
    selectedSessions = selected
      ? [...new Set([...selectedSessions, name])]
      : selectedSessions.filter((item) => item !== name);
  }
  function applyConnections(): void {
    void run(
      {
        choice: "sessions",
        sessions: selectedSessions,
        include_saved: includeSaved,
      },
      "sessions",
    );
  }

  function focusOnMount(node: HTMLInputElement): void {
    node.focus();
  }

  const label = (idle: string, action: string): string =>
    busyAction === action ? lm.connecting : idle;
</script>

<section class="onboarding">
  {#if view === "reconnect"}
    <h1>{lm.reconnect_h1}</h1>
    <p class="lead">{lm.reconnect_lead}</p>
    <p class="hint">
      {fmt(lm.reconnect_hint, { herdr: "herdr", socket: status?.socketPath ?? "?" })}
    </p>
    <div class="actions">
      <button class="primary" disabled={busy} onclick={connectLocal}>
        {label(lm.retry, "local")}
      </button>
      {#if savedAvailable}
        <button class="ghost" disabled={busy} onclick={connectSaved}>
          {label(lm.connect_saved, "saved")}
        </button>
      {/if}
      <button class="link" disabled={busy} onclick={() => (showRemote = !showRemote)}>
        {lm.connect_remote_toggle}
      </button>
    </div>
  {:else}
    <h1>{lm.welcome_h1}</h1>
    {#if localAvailable}
      <p class="lead ok">{lm.local_ok}</p>
      <div class="actions">
        {#if savedAvailable}
          <button class="primary" disabled={busy} onclick={connectSaved}>
            {label(lm.connect_saved, "saved")}
          </button>
          <button class="ghost" disabled={busy} onclick={connectLocal}>
            {label(lm.connect_local, "local")}
          </button>
        {:else}
          <button class="primary" disabled={busy} onclick={connectLocal}>
            {label(lm.connect, "local")}
          </button>
        {/if}
        <button class="link" disabled={busy} onclick={() => (showRemote = !showRemote)}>
          {lm.remote_toggle}
        </button>
      </div>
    {:else}
      <p class="lead">{lm.no_local}</p>
      {#if savedAvailable}
        <div class="actions">
          <button class="ghost" disabled={busy} onclick={connectSaved}>
            {label(lm.connect_saved, "saved")}
          </button>
        </div>
      {/if}
      <!-- the latched-open remote form below IS the primary action here — the
           old extra 'Vzdálený herdr…' primary above it visibly did nothing -->
    {/if}
  {/if}

  {#if localSessions.length > 0 || savedAvailable}
    <section class="sessions" aria-labelledby="sessions-heading">
      <h2 id="sessions-heading">{lm.sessions_h}</h2>
      <p>{lm.sessions_hint}</p>
      <div class="session-list">
        {#each localSessions as session (session.name)}
          <label class:unavailable={!session.available}>
            <input
              type="checkbox"
              checked={selectedSessions.includes(session.name)}
              onchange={(event) =>
                toggleSession(session.name, (event.currentTarget as HTMLInputElement).checked)}
            />
            <span class:online={session.available} class="dot"></span>
            <span>
              <strong>{session.name}</strong>
              <small>{session.available ? lm.running : lm.stopped}</small>
            </span>
          </label>
        {/each}
        {#if savedAvailable}
          <label>
            <input type="checkbox" bind:checked={includeSaved} />
            <span class:online={status?.mode === "remote" || status?.mode === "mixed"} class="dot"></span>
            <span><strong>{lm.saved_remote}</strong><small>Tailscale</small></span>
          </label>
        {/if}
      </div>
      <button class="primary" disabled={busy} onclick={applyConnections}>
        {label(lm.apply_connections, "sessions")}
      </button>
    </section>
  {/if}

  {#if showRemote || (view === "welcome" && status != null && !localAvailable)}
    <form class="remote" onsubmit={(e) => { e.preventDefault(); connectRemote(); }}>
      <label>{lm.url}<input type="text" placeholder="ws(s)://host:8788" bind:value={url} use:focusOnMount /></label>
      <label>{lm.token}<input type="password" bind:value={token} /></label>
      <label class="adv">{lm.id_optional}<input type="text" placeholder="herdr" bind:value={serverId} /></label>
      <button class="primary" type="submit" disabled={busy}>
        {label(lm.connect, "remote")}
      </button>
    </form>
  {/if}

  {#if error}<p class="error" role="alert">{error}</p>{/if}

  <div class="footer">
    <button class="link" disabled={busy} onclick={connectDemo}>
      {label(lm.demo, "demo")}
    </button>
    {#if onDismiss}
      <button class="link dismiss" disabled={busy} onclick={onDismiss}>{lm.back_to_deck}</button>
    {/if}
  </div>
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
  .hint {
    margin: 0;
    color: #6b7785;
    font-size: 12px;
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
  .sessions {
    padding: 10px;
    border: 1px solid #263142;
    border-radius: 10px;
    background: #11151b;
  }
  .sessions h2 { margin: 0; font-size: 13px; color: #dce7f5; }
  .sessions > p { margin: 3px 0 9px; color: #78879a; font-size: 11px; }
  .session-list { display: flex; flex-wrap: wrap; gap: 7px; margin-bottom: 9px; }
  .session-list label {
    display: grid;
    grid-template-columns: auto 7px 1fr;
    align-items: center;
    gap: 6px;
    min-width: 115px;
    padding: 7px 8px;
    border: 1px solid #2a3546;
    border-radius: 8px;
    background: #171d26;
    cursor: pointer;
  }
  .session-list label:has(input:checked) { border-color: #3979bd; }
  .session-list label.unavailable { opacity: .58; }
  .session-list input { margin: 0; accent-color: #58a6ff; }
  .session-list .dot { width: 7px; height: 7px; border-radius: 50%; background: #6b7280; }
  .session-list .dot.online { background: #3fb950; box-shadow: 0 0 0 2px #1f4d2b; }
  .session-list span:last-child { display: flex; flex-direction: column; min-width: 0; }
  .session-list strong { font: 600 11px/1.2 ui-monospace, SFMono-Regular, monospace; overflow: hidden; text-overflow: ellipsis; }
  .session-list small { color: #78879a; font-size: 9px; }
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
  button.ghost {
    padding: 6px 13px;
    border: 1px solid #2a3f66;
    border-radius: 7px;
    background: none;
    color: #9db8e8;
    font: inherit;
    cursor: pointer;
  }
  button.ghost:disabled {
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
