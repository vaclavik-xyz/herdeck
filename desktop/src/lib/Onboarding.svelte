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
  import ArrowLeft from "phosphor-svelte/lib/ArrowLeft";
  import CheckCircle from "phosphor-svelte/lib/CheckCircle";
  import CloudArrowUp from "phosphor-svelte/lib/CloudArrowUp";
  import GearSix from "phosphor-svelte/lib/GearSix";
  import { connectErrorMessage, hasConnectionInventory, shouldAutoReconnect } from "./onboardingClient";
  import { defineMessages, fmt, locale } from "./i18n.svelte";

  let {
    view,
    status,
    transport,
    onConnected,
    onDismiss = undefined,
    onOpenSettings = undefined,
    manual = false,
    variant = "compact",
  }: {
    view: "welcome" | "reconnect";
    status: SetupStatus | null;
    transport: SetupTransport | null;
    onConnected: () => void;
    onDismiss?: (() => void) | undefined;
    onOpenSettings?: (() => void) | undefined;
    manual?: boolean;
    variant?: "compact" | "desktop";
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
      local_ok: "herdr is running locally",
      connect_local: "Connect locally",
      connect: "Connect",
      remote_toggle: "Add remote connection…",
      no_local: "herdr was not found locally. Start it, or connect remotely below.",
      url: "URL",
      token: "Token",
      id_optional: "ID (optional)",
      fill_url_token: "Fill in both URL and token.",
      demo: "Explore the demo",
      back_to_deck: "Back to Herdeck",
      open_settings: "Open settings",
      sessions_h: "Connections",
      sessions_hint: "Choose the local sessions and saved bridge Herdeck should monitor.",
      saved_remote: "Saved remote bridge",
      apply_connections: "Save and connect",
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
      local_ok: "herdr běží lokálně",
      connect_local: "Připojit lokálně",
      connect: "Připojit",
      remote_toggle: "Přidat vzdálené připojení…",
      no_local: "herdr nebyl lokálně nalezen. Spusť ho, nebo se připoj vzdáleně níže.",
      url: "URL",
      token: "Token",
      id_optional: "ID (volitelné)",
      fill_url_token: "Vyplň URL i token.",
      demo: "Prozkoumat demo",
      back_to_deck: "Zpět do Herdecku",
      open_settings: "Otevřít nastavení",
      sessions_h: "Připojení",
      sessions_hint: "Vyber lokální sessions a uložený bridge, které má Herdeck sledovat.",
      saved_remote: "Uložený vzdálený bridge",
      apply_connections: "Uložit a připojit",
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
  const hasInventory = $derived(hasConnectionInventory(status));

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
        manual: manual || onDismiss != null,
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

<section class="onboarding" class:desktop={variant === "desktop"}>
  {#if view === "reconnect"}
    <h1>{lm.reconnect_h1}</h1>
    <p class="lead">{lm.reconnect_lead}</p>
    <p class="hint">
      {fmt(lm.reconnect_hint, { herdr: "herdr", socket: status?.socketPath ?? "?" })}
    </p>
    {#if !hasInventory}
      <div class="actions">
        <button class="primary" disabled={busy} onclick={connectLocal}>
          {label(lm.retry, "local")}
        </button>
        <button class="link" disabled={busy} onclick={() => (showRemote = !showRemote)}>
          {lm.connect_remote_toggle}
        </button>
      </div>
    {/if}
  {:else}
    <h1>{lm.welcome_h1}</h1>
    {#if localAvailable}
      <p class="lead ok"><CheckCircle size={16} weight="fill" />{lm.local_ok}</p>
      {#if !hasInventory}
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
      {/if}
    {:else}
      <p class="lead">{lm.no_local}</p>
      {#if savedAvailable && !hasInventory}
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
      <div class="section-heading">
        <div><h2 id="sessions-heading">{lm.sessions_h}</h2><p>{lm.sessions_hint}</p></div>
        <span class="connection-count">{selectedSessions.length + (includeSaved ? 1 : 0)}</span>
      </div>
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
              <small>{session.available ? lm.running : lm.stopped} · {session.serverId}</small>
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
      <div class="session-actions">
        <button class="primary" disabled={busy} onclick={applyConnections}>
          {label(lm.apply_connections, "sessions")}
        </button>
        <button class="ghost remote-toggle" disabled={busy} onclick={() => (showRemote = !showRemote)}>
          <CloudArrowUp size={15} />{lm.remote_toggle}
        </button>
      </div>
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
    {#if onOpenSettings}
      <button class="link dismiss" disabled={busy} onclick={onOpenSettings}>
        <GearSix size={14} />{lm.open_settings}
      </button>
    {/if}
    {#if onDismiss}
      <button class="link dismiss" disabled={busy} onclick={onDismiss}>
        <ArrowLeft size={14} />{lm.back_to_deck}
      </button>
    {/if}
  </div>
</section>

<style>
  .onboarding {
    box-sizing: border-box;
    padding: 24px 20px;
    background: var(--canvas);
    color: var(--text);
    font: var(--t-body);
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .onboarding.desktop {
    min-width: 0;
    padding: 38px 36px 30px;
    background: var(--sidebar);
  }
  h1 {
    margin: 0;
    font-size: 18px;
    font-weight: 680;
    letter-spacing: -.025em;
  }
  .lead {
    margin: 0;
    color: var(--text-dim);
  }
  .lead.ok {
    display: flex;
    align-items: center;
    gap: 7px;
    color: var(--st-working);
  }
  .hint {
    margin: 0;
    color: var(--text-faint);
    font-size: 12px;
  }
  .actions,
  .footer {
    display: flex;
    gap: 10px;
    align-items: center;
    flex-wrap: wrap;
  }
  .section-heading {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 18px;
    margin-bottom: 12px;
  }
  .section-heading > div {
    min-width: 0;
  }
  .connection-count {
    min-width: 24px;
    padding: 2px 7px;
    border-radius: 999px;
    background: var(--key);
    color: var(--text-dim);
    font: 600 10px/1.5 var(--font-mono);
    text-align: center;
  }
  .remote {
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 14px;
    border: 1px solid var(--line);
    border-radius: 10px;
    background: var(--panel);
  }
  .sessions {
    padding-top: 4px;
  }
  .sessions h2 { margin: 0; font-size: 13px; color: var(--text); }
  .sessions p { margin: 3px 0 0; color: var(--text-dim); font-size: 11px; }
  .session-list {
    overflow: hidden;
    margin-bottom: 12px;
    border: 1px solid var(--line-strong);
    border-radius: 10px;
    background: var(--field);
  }
  .session-list label {
    display: grid;
    grid-template-columns: auto 7px 1fr;
    align-items: center;
    gap: 9px;
    min-height: 52px;
    padding: 8px 11px;
    border-bottom: 1px solid var(--line);
    cursor: pointer;
    transition: background .14s ease;
  }
  .session-list label:last-child { border-bottom: 0; }
  .session-list label:hover { background: var(--panel-raised); }
  .session-list label:has(input:checked) { background: var(--accent-soft); }
  .session-list label.unavailable { opacity: .62; }
  .session-list input { margin: 0; accent-color: var(--accent); }
  .session-list .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--st-unknown); }
  .session-list .dot.online { background: var(--st-working); }
  .session-list span:last-child { display: flex; flex-direction: column; min-width: 0; }
  .session-list strong { font: 600 11px/1.25 var(--font-mono); overflow: hidden; text-overflow: ellipsis; }
  .session-list small { margin-top: 2px; color: var(--text-dim); font-size: 9px; }
  .session-actions {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .remote label {
    display: flex;
    flex-direction: column;
    gap: 3px;
    font-size: 11px;
    color: var(--text-dim);
  }
  .remote input {
    padding: 6px 8px;
    border-radius: 7px;
    border: 1px solid var(--line-strong);
    background: var(--field);
    color: var(--text);
    font: inherit;
  }
  button.primary {
    min-height: 32px;
    padding: 7px 14px;
    border: 1px solid var(--accent-strong);
    border-radius: 7px;
    background: var(--accent);
    color: var(--canvas);
    font: inherit;
    font-weight: 680;
    cursor: pointer;
  }
  button.ghost {
    min-height: 32px;
    padding: 6px 13px;
    border: 1px solid var(--line-strong);
    border-radius: 7px;
    background: none;
    color: var(--text);
    font: inherit;
    cursor: pointer;
  }
  button.remote-toggle {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  button.link {
    border: none;
    background: none;
    color: var(--accent-strong);
    cursor: pointer;
    font: inherit;
    padding: 4px 0;
  }
  button.link.dismiss {
    display: inline-flex;
    margin-left: auto;
    align-items: center;
    gap: 5px;
    color: var(--text-dim);
  }
  .error {
    margin: 0;
    color: var(--st-offline-text);
  }
  button:focus-visible,
  input:focus-visible {
    outline: 2px solid var(--accent-strong);
    outline-offset: 2px;
  }
  button:active:not(:disabled) {
    transform: translateY(1px);
  }
  @media (max-width: 760px) {
    .onboarding.desktop {
      padding: 24px;
    }
    .session-actions {
      align-items: stretch;
      flex-direction: column;
    }
    .session-actions button {
      justify-content: center;
      width: 100%;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .session-list label {
      transition: none;
    }
  }
</style>
