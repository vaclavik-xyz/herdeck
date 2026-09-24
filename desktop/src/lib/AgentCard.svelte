<script lang="ts">
  // The agent card: one agent in full, opened from a deck tile (Option/Alt-click
  // or a long press in DeckView). The deck drill fits a blocked prompt into
  // three panel lines; this shows all of it, the same parsed options, a
  // free-text reply, Stop and Focus. Every action reports what the bridge said
  // (sent, stale prompt, read-only token, …) instead of failing silently.
  import { onMount } from "svelte";
  import X from "phosphor-svelte/lib/X";
  import {
    formatDuration,
    formatSince,
    subagentIndent,
    type SubagentRow,
    type ActionOutcome,
    type AgentAction,
    type AgentDetail,
    type AgentOption,
    type AgentRef,
    type AgentTarget,
    type AgentTransport,
  } from "./agentCardClient";
  import { defineMessages, fmt, locale } from "./i18n.svelte";
  import { visibilityGatedLoop, type GatedLoop } from "./pollGate";
  import AgentTerminal from "./AgentTerminal.svelte";
  import type { TerminalFactory } from "./xtermLoader";

  const M = defineMessages({
    en: {
      card_label: "Agent details",
      close: "Close agent card",
      loading: "Loading…",
      gone: "No agent here anymore — it left the fleet, the tile just changed, or this is the demo deck.",
      load_failed: "Couldn't load the agent: {message}",
      prompt: "Prompt",
      prompt_reading: "Reading the prompt…",
      prompt_none: "Not waiting for an answer.",
      options: "Answer",
      confirm: "Sure? Click again",
      reply: "Reply",
      reply_placeholder: "Type a reply — Enter sends, Shift+Enter adds a line",
      send: "Send",
      stop: "Stop",
      stop_title: "Interrupt the agent (sends its profile's stop keys)",
      focus: "Focus",
      focus_title: "Switch herdr to this pane and bring the terminal forward",
      terminal: "Live terminal",
      terminal_title: "Show or hide a read-only live view of the agent's pane",
      offline: "Server disconnected — actions are unavailable until it reconnects.",
      since: "for {since}",
      "status.blocked": "blocked",
      "status.working": "working",
      "status.idle": "idle",
      "status.done": "done",
      "status.waiting": "waiting",
      "status.unknown": "unknown",
      "out.sent": "Sent.",
      "out.focused": "Focused in herdr.",
      "out.pending": "Sent — the bridge hasn't confirmed yet.",
      "out.not_blocked": "The agent is no longer waiting for an answer.",
      "out.stale": "The prompt changed — read the new one before answering.",
      "out.identity_changed": "That pane now runs a different agent.",
      "out.readonly": "Read-only connection: this bridge doesn't allow actions ({message}).",
      "out.disconnected": "The server is disconnected.",
      "out.invalid": "The runtime refused this input.",
      "out.rejected": "The bridge refused it: {message}",
      "out.error": "Error: {message}",
      "out.gone": "The agent is gone.",
      "out.forbidden": "Refused: the runtime's access token changed.",
      "out.unreachable": "Couldn't reach the runtime.",
      "out.http": "The runtime answered HTTP {message}.",
      subagents: "Subagents",
      "sub.unnamed": "subagent",
      "sub.running": "running",
      "sub.done": "done",
      "sub.failed": "failed",
      "sub.stale": "no signal",
      "sub.running_title": "Running for {duration}",
      "sub.total_title": "Took {duration}",
      "sub.stale_title": "No heartbeat for over 10 minutes — it may have ended unnoticed ({duration} since start)",
    },
    cs: {
      card_label: "Detail agenta",
      close: "Zavřít kartu agenta",
      loading: "Načítám…",
      gone: "Agent tu už není — odešel z flotily, dlaždice se právě změnila, nebo jde o demo deck.",
      load_failed: "Agenta se nepodařilo načíst: {message}",
      prompt: "Dotaz",
      prompt_reading: "Čtu dotaz…",
      prompt_none: "Nečeká na odpověď.",
      options: "Odpověď",
      confirm: "Opravdu? Klikni znovu",
      reply: "Odpověď textem",
      reply_placeholder: "Napiš odpověď — Enter odešle, Shift+Enter přidá řádek",
      send: "Odeslat",
      stop: "Zastavit",
      stop_title: "Přeruší agenta (pošle stop klávesy jeho profilu)",
      focus: "Zaměřit",
      focus_title: "Přepne herdr na tento panel a přenese terminál do popředí",
      terminal: "Živý terminál",
      terminal_title: "Zobrazí nebo skryje živý náhled panelu agenta (jen pro čtení)",
      offline: "Server je odpojený — akce nejsou dostupné, dokud se znovu nepřipojí.",
      since: "{since}",
      "status.blocked": "blokován",
      "status.working": "pracuje",
      "status.idle": "nečinný",
      "status.done": "hotovo",
      "status.waiting": "čeká na pozadí",
      "status.unknown": "neznámý",
      "out.sent": "Odesláno.",
      "out.focused": "Zaměřeno v herdr.",
      "out.pending": "Odesláno — bridge zatím nepotvrdil.",
      "out.not_blocked": "Agent už nečeká na odpověď.",
      "out.stale": "Dotaz se změnil — před odpovědí si přečti nový.",
      "out.identity_changed": "V tom panelu teď běží jiný agent.",
      "out.readonly": "Připojení jen pro čtení: tento bridge nepovoluje akce ({message}).",
      "out.disconnected": "Server je odpojený.",
      "out.invalid": "Runtime tento vstup odmítl.",
      "out.rejected": "Bridge to odmítl: {message}",
      "out.error": "Chyba: {message}",
      "out.gone": "Agent už neexistuje.",
      "out.forbidden": "Odmítnuto: změnil se přístupový token runtime.",
      "out.unreachable": "Nepodařilo se spojit s runtime.",
      "out.http": "Runtime odpověděl HTTP {message}.",
      subagents: "Subagenti",
      "sub.unnamed": "subagent",
      "sub.running": "běží",
      "sub.done": "hotovo",
      "sub.failed": "selhal",
      "sub.stale": "bez signálu",
      "sub.running_title": "Běží {duration}",
      "sub.total_title": "Trval {duration}",
      "sub.stale_title": "Přes 10 minut bez známky života — možná skončil bez hlášení ({duration} od startu)",
    },
  });
  const m = $derived(M[locale.lang]);
  type Key = keyof typeof M.en;

  let {
    transport,
    target,
    onClose,
    pollMs = 1500,
    createTerminal = undefined,
  }: {
    transport: AgentTransport;
    target: AgentTarget;
    onClose: () => void;
    pollMs?: number;
    // Test seam: a fake terminal instead of the lazily loaded xterm.js.
    createTerminal?: TerminalFactory;
  } = $props();

  // The deck arms a destructive press for 5 s (orchestrator _CONFIRM_TTL_S).
  const CONFIRM_MS = 5000;

  let detail = $state<AgentDetail | null>(null);
  let phase = $state<"loading" | "ready" | "gone" | "error">("loading");
  let loadError = $state("");
  let busy = $state(false);
  let feedback = $state<ActionOutcome | null>(null);
  let armed = $state<string | null>(null);
  let armTimer: ReturnType<typeof setTimeout> | undefined;
  let reply = $state("");
  let showTerminal = $state(false);
  let ref: AgentRef | null = null;
  let alive = true;
  let loop: GatedLoop | null = null;
  let root = $state<HTMLElement | undefined>(undefined);
  // Live subagent durations: the runtime's figure at fetch time, advanced by a
  // 1 s local tick between polls (only while something is running).
  let fetchedAt = $state(Date.now());
  let now = $state(Date.now());

  async function load(refresh = false): Promise<void> {
    const first = ref === null;
    const r = await transport.detail(ref ?? target, refresh || first);
    if (!alive) return;
    if (r.kind === "ok") {
      detail = r.detail;
      fetchedAt = now = Date.now();
      ref = { serverId: r.detail.serverId, paneId: r.detail.paneId };
      phase = "ready";
    } else if (r.kind === "gone") {
      phase = "gone";
      detail = null;
    } else if (first) {
      phase = "error";
      loadError = r.message;
    }
    // A later transient failure keeps the last good detail on screen.
  }

  function outcomeText(o: ActionOutcome): string {
    const key = `out.${o.code}` as Key;
    const template = key in m ? m[key] : m["out.error"];
    return fmt(template, { message: o.message || o.code });
  }

  async function run(action: AgentAction, extra: Record<string, string> = {}): Promise<boolean> {
    if (!ref || busy) return false;
    busy = true;
    feedback = null;
    let out: ActionOutcome;
    try {
      out = await transport.act(action, ref, extra);
    } finally {
      busy = false;
    }
    if (!alive) return false;
    feedback = out;
    // After an answer the old prompt is spent (the runtime drops it): re-read
    // at once. After a stale refusal, likewise show what changed.
    void load(action === "answer" || out.code === "stale");
    return out.ok;
  }

  function disarm(): void {
    if (armTimer) clearTimeout(armTimer);
    armTimer = undefined;
    armed = null;
  }

  /** Two-step press for actions the deck would confirm; true = go ahead. */
  function confirmed(key: string, needed: boolean): boolean {
    if (!needed || armed === key) {
      disarm();
      return true;
    }
    disarm();
    armed = key;
    armTimer = setTimeout(disarm, CONFIRM_MS);
    return false;
  }

  function answer(o: AgentOption): void {
    if (!detail?.revision && detail?.backend !== "t3") return;
    if (!confirmed(`opt:${o.key}`, o.confirm)) return;
    void run("answer", { key: o.key, revision: detail?.revision ?? "" });
  }

  function stop(): void {
    if (!confirmed("stop", detail?.stopConfirm ?? true)) return;
    void run("stop");
  }

  async function send(): Promise<void> {
    const text = reply.trim();
    if (!text) return;
    if (await run("text", { text: reply })) reply = "";
  }

  function onReplyKey(e: KeyboardEvent): void {
    // Enter submits (herdeck-ctl `send` submits at once); Shift+Enter is a
    // newline; never submit mid IME composition.
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      void send();
    }
  }

  function onKey(e: KeyboardEvent): void {
    if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    }
  }

  function optionText(o: AgentOption): string {
    return o.kind === "option" ? `${o.key}. ${o.label}` : o.label;
  }

  const statusWord = $derived.by(() => {
    const key = `status.${detail?.status ?? "unknown"}` as Key;
    return key in m ? m[key] : detail?.status ?? "";
  });
  const place = $derived(
    detail ? [detail.repo, detail.branch].filter(Boolean).join(" · ") : "",
  );
  const where = $derived(
    detail ? [detail.workspace, detail.tab].filter(Boolean).join(" › ") : "",
  );
  const actionsOff = $derived(busy || !detail?.connected);
  const anyRunning = $derived(detail?.subagents.some((s) => s.status === "running") ?? false);

  $effect(() => {
    if (!anyRunning) return;
    const timer = setInterval(() => (now = Date.now()), 1000);
    return () => clearInterval(timer);
  });

  function subDuration(s: SubagentRow): number {
    return s.status === "running"
      ? s.durationS + Math.max(0, Math.floor((now - fetchedAt) / 1000))
      : s.durationS;
  }

  function subTitle(s: SubagentRow): string {
    const duration = formatDuration(subDuration(s));
    const key: Key =
      s.status === "running" ? "sub.running_title" : s.status === "stale" ? "sub.stale_title" : "sub.total_title";
    return fmt(m[key], { duration });
  }

  onMount(() => {
    loop = visibilityGatedLoop(() => load(), () => pollMs);
    root?.focus({ preventScroll: true }); // so Escape closes it at once
    return () => {
      alive = false;
      loop?.stop();
      disarm();
    };
  });
</script>

<div bind:this={root} class="agent-card" role="dialog" aria-label={m.card_label} tabindex="-1" onkeydown={onKey}>
  <header>
    <div class="who">
      {#if detail}
        <strong class="agent">{detail.displayAgent || detail.agentType}</strong>
        <span class="status st-{detail.status}">
          <span class="dot"></span>{statusWord}{#if detail.sinceS != null}
            · {fmt(m.since, { since: formatSince(detail.sinceS) })}{/if}
        </span>
      {/if}
    </div>
    <button class="icon" type="button" onclick={onClose} title={m.close} aria-label={m.close}>
      <X size={14} weight="bold" />
    </button>
  </header>

  {#if phase === "loading"}
    <p class="note">{m.loading}</p>
  {:else if phase === "gone"}
    <p class="note">{m.gone}</p>
  {:else if phase === "error"}
    <p class="note err">{fmt(m.load_failed, { message: loadError })}</p>
  {:else if detail}
    {#if detail.title || detail.label}<p class="title">{detail.title || detail.label}</p>{/if}
    {#if place}<p class="meta">{place}</p>{/if}
    {#if where}<p class="meta">{where}</p>{/if}
    {#if !detail.connected}<p class="note err">{m.offline}</p>{/if}

    {#if detail.prompt}
      <h3>{m.prompt}</h3>
      <!-- A scrollable region must be focusable to scroll from the keyboard. -->
      <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
      <pre class="prompt" tabindex="0" aria-label={m.prompt}>{detail.prompt}</pre>
    {:else if detail.promptPending}
      <p class="note">{m.prompt_reading}</p>
    {:else if detail.status !== "blocked"}
      <p class="note">{m.prompt_none}</p>
    {/if}

    {#if detail.options.length}
      <h3>{m.options}</h3>
      <div class="options">
        {#each detail.options as o (o.key)}
          <button
            type="button"
            class="opt"
            class:approve={o.id === "approve"}
            class:always={o.id === "approve_always"}
            class:deny={o.id === "deny"}
            class:armed={armed === `opt:${o.key}`}
            disabled={actionsOff}
            onclick={() => answer(o)}
          >{armed === `opt:${o.key}` ? m.confirm : optionText(o)}</button>
        {/each}
      </div>
    {/if}

    {#if detail.subagents.length}
      <h3>{m.subagents}</h3>
      <ul class="subagents">
        {#each detail.subagents as s (s.id)}
          <li class="sub sub-{s.status}" style:--indent={subagentIndent(s.depth)}>
            <span class="dot"></span>
            <span class="sub-type">{s.type || s.provider || m["sub.unnamed"]}</span>
            {#if s.description}<span class="sub-desc" title={s.description}>{s.description}</span>{/if}
            <span class="sub-state">{m[`sub.${s.status}`]}</span>
            <span class="sub-time" title={subTitle(s)}>{formatDuration(subDuration(s))}</span>
          </li>
        {/each}
      </ul>
    {/if}

    {#if detail.canText}
      <label class="reply">
        <span>{m.reply}</span>
        <textarea
          rows="2"
          bind:value={reply}
          placeholder={m.reply_placeholder}
          onkeydown={onReplyKey}
          disabled={!detail.connected}
        ></textarea>
      </label>
    {/if}

    <div class="actions">
      {#if detail.canText}
        <button type="button" class="primary" disabled={actionsOff || !reply.trim()} onclick={() => void send()}>{m.send}</button>
      {/if}
      {#if detail.canFocus}
        <button type="button" disabled={actionsOff} title={m.focus_title} onclick={() => void run("focus")}>{m.focus}</button>
      {/if}
      {#if detail.backend !== "t3"}
        <button
          type="button"
          class:on={showTerminal}
          aria-pressed={showTerminal}
          title={m.terminal_title}
          onclick={() => (showTerminal = !showTerminal)}
        >{m.terminal}</button>
      {/if}
      {#if detail.canStop}
        <button type="button" class="danger" class:armed={armed === "stop"} disabled={actionsOff} title={m.stop_title} onclick={stop}>
          {armed === "stop" ? m.confirm : m.stop}
        </button>
      {/if}
    </div>
  {/if}

  {#if showTerminal && detail && detail.backend !== "t3"}
    <!-- Unmounting it (toggle off, card closed) stops the observation. -->
    <AgentTerminal
      {transport}
      agent={{ serverId: detail.serverId, paneId: detail.paneId }}
      {...createTerminal ? { createTerminal } : {}}
    />
  {/if}

  {#if feedback}
    <p class="feedback" class:bad={!feedback.ok} role="status">{outcomeText(feedback)}</p>
  {/if}
</div>

<style>
  .agent-card {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
    padding: var(--s3);
    border: 1px solid var(--line-strong);
    border-radius: var(--r-panel);
    background: var(--panel-raised);
    color: var(--text);
    font: var(--t-body);
    outline: none;
  }
  header {
    display: flex;
    align-items: flex-start;
    gap: var(--s2);
  }
  .who {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--s1) var(--s2);
  }
  .agent { font: var(--t-h2); }
  .status {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    color: var(--text-dim);
    font: var(--t-label);
  }
  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--st-unknown);
  }
  .st-blocked .dot { background: var(--st-blocked); }
  .st-working .dot { background: var(--st-working); }
  .st-idle .dot { background: var(--st-idle); }
  .st-done .dot { background: var(--st-done); }
  .st-waiting .dot { background: var(--st-waiting); }
  .subagents {
    display: grid;
    gap: 2px;
    margin: 0;
    padding: 0;
    list-style: none;
    max-height: 200px;
    overflow: auto;
  }
  .sub {
    display: flex;
    align-items: baseline;
    gap: var(--s2);
    min-width: 0;
    padding-left: calc(var(--indent, 0) * 14px);
    font: var(--t-help);
  }
  .sub .dot { flex: none; align-self: center; }
  .sub-running .dot { background: var(--st-working); }
  .sub-done .dot { background: var(--st-done); }
  .sub-failed .dot { background: var(--st-red); }
  .sub-type { flex: none; font: var(--t-label); }
  .sub-desc {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    color: var(--text-dim);
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .sub-state { flex: none; color: var(--text-faint); }
  .sub-failed .sub-state { color: var(--st-offline-text); }
  .sub-time {
    flex: none;
    margin-left: auto;
    color: var(--text-dim);
    font-variant-numeric: tabular-nums;
  }
  .title { margin: 0; font: var(--t-label); overflow-wrap: anywhere; }
  .meta, .note { margin: 0; color: var(--text-dim); font: var(--t-help); overflow-wrap: anywhere; }
  .err { color: var(--st-offline-text); }
  h3 {
    margin: var(--s1) 0 0;
    color: var(--text-faint);
    font: var(--t-eyebrow);
    text-transform: uppercase;
    letter-spacing: .04em;
  }
  .prompt {
    margin: 0;
    max-height: 220px;
    overflow: auto;
    padding: var(--s2);
    border: 1px solid var(--line);
    border-radius: var(--r-control);
    background: var(--field);
    font: var(--t-mono);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .options, .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--s1);
  }
  button {
    padding: 5px 10px;
    border: 1px solid var(--line-strong);
    border-radius: var(--r-control);
    background: var(--key);
    color: var(--text);
    font: var(--t-label);
    cursor: pointer;
  }
  button:hover:not(:disabled) { border-color: var(--accent); }
  .opt { text-align: left; max-width: 100%; overflow-wrap: anywhere; }
  .opt.approve { border-color: var(--st-working); }
  .opt.always { border-color: var(--st-blocked); }
  .opt.deny, .danger { border-color: var(--st-red); }
  .armed { background: color-mix(in srgb, var(--st-blocked) 22%, var(--key)); }
  .primary { background: var(--accent-soft); border-color: var(--accent); }
  .on { border-color: var(--accent); background: var(--accent-soft); }
  .icon {
    display: grid;
    place-items: center;
    width: 24px;
    height: 24px;
    padding: 0;
    flex: none;
  }
  .reply { display: grid; gap: var(--s1); }
  .reply span { color: var(--text-faint); font: var(--t-eyebrow); text-transform: uppercase; letter-spacing: .04em; }
  textarea {
    resize: vertical;
    min-height: 40px;
    padding: var(--s2);
    border: 1px solid var(--line-strong);
    border-radius: var(--r-control);
    background: var(--field);
    color: var(--text);
  }
  .feedback {
    margin: 0;
    padding: var(--s1) var(--s2);
    border-left: 3px solid var(--st-working);
    font: var(--t-help);
  }
  .feedback.bad {
    border-left-color: var(--st-offline);
    color: var(--st-offline-text);
  }
</style>
