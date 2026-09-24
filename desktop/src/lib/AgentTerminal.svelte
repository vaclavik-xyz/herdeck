<script lang="ts">
  // Read-only live view of an agent's pane inside the agent card. The runtime
  // relays the bridge's `observe` stream (src/herdeck/deckapp/agent_term.py)
  // and this long-polls it. Observation stops when this unmounts (card closed
  // or terminal toggled off) and while the window is hidden; the runtime also
  // reaps a session nobody polls, so a frozen WebView cannot leak one.
  import { onMount } from "svelte";
  import { TERM_POLL_MS, type AgentRef, type AgentTransport } from "./agentCardClient";
  import { defineMessages, fmt, locale } from "./i18n.svelte";
  import { createXterm, frameBytes, type TerminalFactory, type TerminalHandle } from "./xtermLoader";

  const M = defineMessages({
    en: {
      label: "Live terminal (read-only)",
      connecting: "Connecting…",
      live: "Live",
      paused: "Paused while the window is hidden",
      ended: "Preview ended: {reason}",
      ended_plain: "Preview ended",
      restart: "Restart",
      load_failed: "Couldn't load the terminal: {message}",
      "reason.disconnected": "the server is disconnected",
      "reason.invalid": "this agent has no terminal preview",
      "reason.unsupported": "this bridge doesn't offer live previews (update it)",
      "reason.gone": "the agent is gone",
      "reason.stopped": "stopped",
      "reason.limit": "too many live previews on this bridge",
      "reason.identity": "the pane now runs a different agent",
      "reason.no_stream": "herdr sent no stream (needs herdr 0.7.3 or newer)",
      "reason.no_herdr": "herdr was not found on the bridge host",
      "reason.ended": "the stream ended",
      "reason.too_large": "a terminal frame was too large",
    },
    cs: {
      label: "Živý terminál (jen pro čtení)",
      connecting: "Připojuji…",
      live: "Živě",
      paused: "Pozastaveno, dokud je okno skryté",
      ended: "Náhled skončil: {reason}",
      ended_plain: "Náhled skončil",
      restart: "Spustit znovu",
      load_failed: "Terminál se nepodařilo načíst: {message}",
      "reason.disconnected": "server je odpojený",
      "reason.invalid": "tento agent nemá náhled terminálu",
      "reason.unsupported": "tento bridge živé náhledy nenabízí (aktualizuj ho)",
      "reason.gone": "agent už neexistuje",
      "reason.stopped": "zastaveno",
      "reason.limit": "na tomto bridge běží příliš mnoho živých náhledů",
      "reason.identity": "v panelu teď běží jiný agent",
      "reason.no_stream": "herdr neposlal stream (potřebuje herdr 0.7.3 nebo novější)",
      "reason.no_herdr": "herdr na hostiteli bridge nebyl nalezen",
      "reason.ended": "stream skončil",
      "reason.too_large": "snímek terminálu byl příliš velký",
    },
  });
  const m = $derived(M[locale.lang]);
  type Key = keyof typeof M.en;

  let {
    transport,
    agent,
    createTerminal = createXterm,
    doc = typeof document === "undefined" ? undefined : document,
  }: {
    transport: AgentTransport;
    agent: AgentRef;
    createTerminal?: TerminalFactory;
    doc?: Pick<Document, "hidden" | "addEventListener" | "removeEventListener">;
  } = $props();

  let host = $state<HTMLElement | undefined>(undefined);
  let phase = $state<"connecting" | "live" | "paused" | "ended">("connecting");
  let reason = $state("");
  let term: TerminalHandle | null = null;
  let session: string | null = null;
  let generation = 0;
  let alive = true;

  // The bridge/runtime close reasons this UI knows (bridge._run_observe,
  // agent_term.py); anything else is shown as sent.
  const KNOWN_REASONS: Record<string, string> = {
    "too many live previews": "limit",
    "agent identity changed": "identity",
    "no stream from herdr (needs herdr >= 0.7.3)": "no_stream",
    "herdr binary not found on the bridge host": "no_herdr",
    "stream ended": "ended",
    "terminal frame too large": "too_large",
  };

  function reasonText(code: string): string {
    const key = `reason.${KNOWN_REASONS[code] ?? code}` as Key;
    return key in m ? m[key] : code;
  }

  function end(text: string): void {
    phase = "ended";
    reason = text;
  }

  /** Stop the remote observation (idempotent) and abandon any poll in flight. */
  function stop(): void {
    generation++;
    const id = session;
    session = null;
    if (id) void transport.termClose(id);
  }

  async function start(): Promise<void> {
    stop();
    const gen = generation;
    phase = "connecting";
    reason = "";
    try {
      if (!term) {
        if (!host) return;
        term = await createTerminal(host);
      }
    } catch (e) {
      if (alive) end(fmt(m.load_failed, { message: String(e) }));
      return;
    }
    if (!alive || gen !== generation) return;
    term.fit();
    const opened = await transport.termOpen(agent, term.cols, term.rows);
    if (!alive || gen !== generation) {
      // Closed (or restarted) while opening: never leave that one running.
      if (opened.ok) void transport.termClose(opened.id);
      return;
    }
    if (!opened.ok) {
      end(reasonText(opened.outcome.code));
      return;
    }
    session = opened.id;
    phase = "live";
    let after = 0;
    while (alive && gen === generation) {
      const polled = await transport.termPoll(opened.id, after, TERM_POLL_MS);
      if (!alive || gen !== generation || !term) return;
      if (polled.kind === "gone") {
        session = null;
        end("");
        return;
      }
      if (polled.kind === "error") {
        stop();
        end(polled.message);
        return;
      }
      if (polled.gap) term.reset();
      for (const f of polled.frames) {
        if (f.cols !== term.cols || f.rows !== term.rows) term.resize(f.cols, f.rows);
        if (f.full) term.reset();
        term.write(frameBytes(f.data));
      }
      after = polled.next;
      if (polled.closed) {
        session = null; // the runtime already dropped it
        end(reasonText(polled.closed));
        return;
      }
    }
  }

  function onVisibility(): void {
    if (!doc) return;
    if (doc.hidden) {
      if (phase === "live" || phase === "connecting") {
        stop();
        phase = "paused";
      }
    } else if (phase === "paused") {
      void start();
    }
  }

  onMount(() => {
    doc?.addEventListener("visibilitychange", onVisibility);
    if (doc?.hidden) phase = "paused";
    else void start();
    return () => {
      alive = false;
      doc?.removeEventListener("visibilitychange", onVisibility);
      stop();
      term?.dispose();
      term = null;
    };
  });
</script>

<div class="term">
  <div class="bar">
    <span class="label">{m.label}</span>
    <span class="state" class:live={phase === "live"} role="status">
      {phase === "live"
        ? m.live
        : phase === "paused"
          ? m.paused
          : phase === "connecting"
            ? m.connecting
            : reason
              ? fmt(m.ended, { reason })
              : m.ended_plain}
    </span>
    {#if phase === "ended"}
      <button type="button" onclick={() => void start()}>{m.restart}</button>
    {/if}
  </div>
  <div class="screen" bind:this={host} aria-label={m.label}></div>
</div>

<style>
  .term {
    display: grid;
    gap: var(--s1);
  }
  .bar {
    display: flex;
    align-items: center;
    gap: var(--s2);
    font: var(--t-help);
    color: var(--text-dim);
  }
  .label {
    flex: 1;
    color: var(--text-faint);
    font: var(--t-eyebrow);
    text-transform: uppercase;
    letter-spacing: .04em;
  }
  .state.live { color: var(--st-working); }
  .screen {
    height: 240px;
    padding: var(--s1);
    border: 1px solid var(--line);
    border-radius: var(--r-control);
    background: var(--field);
    overflow: hidden;
  }
  button {
    padding: 2px 8px;
    border: 1px solid var(--line-strong);
    border-radius: var(--r-control);
    background: var(--key);
    color: var(--text);
    font: var(--t-label);
    cursor: pointer;
  }
</style>
