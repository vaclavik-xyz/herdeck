<script lang="ts">
  // The mini dock's card: one agent (title, repo · branch, elapsed; for a
  // blocked agent its prompt and one button per option, coloured like the
  // deck) or the folded idle agents. Every action goes through the guarded
  // /agent/* routes (agentCardClient.ts): an answer names the prompt revision
  // it was read under, and every outcome is shown — never assumed. Any agent
  // herdr card also carries a live output tail (DockPreview) and, when the
  // detail allows free text, a reply field (DockReply). A T3 thread has no
  // pane to open or observe: its card shows the thread's latest text and its
  // own actions (Mark seen, Snooze, …) in any status instead.
  import { onMount, untrack } from "svelte";
  import DockMark from "./DockMark.svelte";
  import DockPreview from "./DockPreview.svelte";
  import DockReply from "./DockReply.svelte";
  import {
    formatSince,
    type AgentDetail,
    type AgentOption,
    type AgentTransport,
  } from "./agentCardClient";
  import type { DockAgent } from "./dockClient";
  import { optionTone, PROMPT_REPOLL_MS } from "./dockModel";
  import { DOCK_MESSAGES, outcomeText, statusLabel } from "./dockMessages";
  import { fmt, locale } from "./i18n.svelte";

  let {
    agent = null,
    idle = [],
    heading = null,
    sinceOf,
    iconUrl,
    transport,
    onClose,
    onSent = () => {},
    onReplyFocus = () => {},
    keyboardHints = false,
  }: {
    agent?: DockAgent | null;
    idle?: DockAgent[];
    /** The fold list's heading (default: "Idle agents (n)"). */
    heading?: string | null;
    sinceOf: (a: DockAgent) => number;
    iconUrl: (a: DockAgent) => string | null;
    transport: AgentTransport | null;
    /** "opened" = Open in herdr succeeded (herdr now owns the focus). */
    onClose: (reason?: "opened") => void;
    onSent?: () => void;
    /** The reply field gained (true) or lost (false) keyboard focus. */
    onReplyFocus?: (focused: boolean) => void;
    /** Opened by the next-blocked hotkey: show the key legend. */
    keyboardHints?: boolean;
  } = $props();

  let replyField = $state<ReturnType<typeof DockReply> | undefined>(undefined);

  /** Put the caret in the reply field; false when this card has none. */
  export function focusReply(): boolean {
    return replyField?.focus() ?? false;
  }

  /** The reply field holds unsent text. */
  export function hasDraft(): boolean {
    return replyField?.hasDraft() ?? false;
  }

  /** Keys 1–9: press the numbered option, exactly like its button (armed
   *  confirm, revision check). False when the prompt offers no such option. */
  export function answerKey(digit: string): boolean {
    const o = detail?.options.find((x) => x.kind === "option" && x.key === digit);
    if (!o || !actionable || sent || busy) return false;
    void answer(o);
    return true;
  }

  /** Key O: the "Open in herdr" button. False when it is not offered. */
  export function openInHerdr(): boolean {
    const a = agent;
    if (!a || !detail?.canFocus) return false;
    void focusAgent(a);
    return true;
  }

  const m = $derived(DOCK_MESSAGES[locale.lang]);

  let detail = $state<AgentDetail | null>(null);
  let note = $state("");
  let busy = $state(false);
  let sent = $state(false);
  let armed = $state<string | null>(null);
  let rowNotes = $state<Record<string, string>>({});
  let repoll: ReturnType<typeof setTimeout> | undefined;
  let alive = true;

  const blocked = $derived(agent?.status === "blocked");
  const t3 = $derived(agent?.backend === "t3" || detail?.backend === "t3");
  // A T3 thread's options are its actions in any status (approve, Mark seen,
  // Snooze…); a herdr agent's exist only on a blocked prompt.
  const actionable = $derived(blocked || detail?.backend === "t3");
  const meta = $derived(agent ? [agent.repo, agent.branch].filter(Boolean).join(" · ") : "");
  const rowKey = (a: DockAgent) => `${a.serverId}/${a.paneId}`;

  // After an answer was sent while the agent stays blocked, a follow-up prompt
  // may replace the answered one without a status change: keep re-reading the
  // detail (never resending) until a new revision shows or the agent unblocks.
  function repollAfterSent(): void {
    clearTimeout(repoll);
    repoll = setTimeout(() => {
      if (!alive || !sent || untrack(() => agent?.status) !== "blocked") return;
      void load(true);
    }, PROMPT_REPOLL_MS);
  }

  async function load(refresh: boolean, keepNote = false): Promise<void> {
    const t = transport;
    const a = untrack(() => agent);
    if (!t || !a) return;
    clearTimeout(repoll);
    const res = await t.detail({ serverId: a.serverId, paneId: a.paneId }, refresh);
    if (!alive) return;
    if (res.kind === "ok") {
      // An armed confirm belongs to the prompt it was armed on: a freshly
      // loaded prompt must be armed again after the user has seen it.
      armed = null;
      if (res.detail.revision !== detail?.revision) sent = false;
      // A note about an answered/old prompt must not linger over a new one;
      // a pending note stays while the same prompt is still shown.
      if (!keepNote && !sent) note = "";
      detail = res.detail;
      if (res.detail.status === "blocked" && res.detail.prompt === null && res.detail.promptPending) {
        repoll = setTimeout(() => void load(false), PROMPT_REPOLL_MS);
      } else if (sent && res.detail.status === "blocked") {
        repollAfterSent();
      }
    } else if (res.kind === "gone") {
      note = m.detail_gone;
    } else {
      note = fmt(m.detail_error, { e: res.message });
    }
  }

  onMount(() => {
    const a = untrack(() => agent);
    if (a) void load(a.status === "blocked");
    return () => {
      alive = false;
      clearTimeout(repoll);
    };
  });

  // An agent that blocks while its card is open: read the new prompt.
  let lastStatus = untrack(() => agent?.status);
  $effect(() => {
    const status = agent?.status;
    if (status === lastStatus) return;
    lastStatus = status;
    if (status === "blocked") {
      untrack(() => {
        sent = false;
        void load(true);
      });
    }
  });

  async function answer(option: AgentOption): Promise<void> {
    const t = transport;
    const a = agent;
    const revision = detail?.revision;
    if (!t || !a || !revision || busy || sent) return;
    if (option.confirm && armed !== option.key) {
      armed = option.key;
      return;
    }
    armed = null;
    busy = true;
    note = "";
    const out = await t.act("answer", { serverId: a.serverId, paneId: a.paneId }, { key: option.key, revision });
    busy = false;
    if (!alive) return;
    if (out.code === "sent" || out.code === "pending") {
      sent = true;
      note = out.code === "pending" ? outcomeText(locale.lang, out) : "";
      onSent();
      repollAfterSent();
      return;
    }
    note = outcomeText(locale.lang, out);
    if (out.code === "stale") await load(true, true);
  }

  async function focusAgent(target: DockAgent): Promise<void> {
    const t = transport;
    if (!t) return;
    const out = await t.act("focus", { serverId: target.serverId, paneId: target.paneId });
    if (!alive) return;
    if (out.ok) {
      onClose("opened");
      return;
    }
    if (agent) note = outcomeText(locale.lang, out);
    else rowNotes = { ...rowNotes, [rowKey(target)]: outcomeText(locale.lang, out) };
  }

  // A text reply to a blocked agent answers it like an option would (the dock
  // closes the card once it moves on); to a working one it is just a nudge.
  function replySent(): void {
    if (untrack(() => agent?.status) === "blocked") onSent();
  }

  function optionText(o: AgentOption): string {
    if (armed === o.key) return m.confirm;
    return o.kind === "option" ? `${o.key}. ${o.label}` : o.label;
  }

  // The live question sits at the bottom of a captured prompt.
  function scrollEnd(node: HTMLElement, _text: string) {
    node.scrollTop = node.scrollHeight;
    return {
      update() {
        node.scrollTop = node.scrollHeight;
      },
    };
  }
</script>

<section class="card">
  {#if agent}
    <header>
      <DockMark {agent} src={iconUrl(agent)} size={28} />
      <div class="head">
        <h2 class="title">{agent.title}</h2>
        {#if meta}<p class="meta">{meta}</p>{/if}
      </div>
      <button type="button" class="close" title={m.close_title} aria-label={m.close_title} onclick={() => onClose()}>×</button>
    </header>
    <p class="status st-{agent.status}">
      <span class="pill">{statusLabel(locale.lang, agent.status, agent.offline)}</span>
      <span>{fmt(m.since, { t: formatSince(sinceOf(agent)) })}</span>
      {#if agent.waitingOn}<span>{fmt(m.waiting_on, { what: agent.waitingOn })}</span>{/if}
      {#if agent.subagents > 0}<span>{fmt(m.subagents, { n: agent.subagents })}</span>{/if}
    </p>
    {#if transport && !t3}
      <DockPreview
        {transport}
        agent={{ serverId: agent.serverId, paneId: agent.paneId }}
        plausible={detail !== null && detail.backend !== "t3" && detail.connected}
      />
    {/if}
    {#if actionable}
      {#if detail?.prompt}
        <pre class="prompt" use:scrollEnd={detail.prompt}>{detail.prompt}</pre>
      {:else if blocked && !note && (detail === null || detail.promptPending)}
        <p class="reading">{m.reading}</p>
      {/if}
      {#if detail && detail.options.length > 0 && !sent}
        <div class="options">
          {#each detail.options as o (o.key)}
            <button
              type="button"
              class="opt tone-{optionTone(o, agent.agentType)}"
              class:armed={armed === o.key}
              disabled={busy}
              onclick={() => void answer(o)}
            >{optionText(o)}</button>
          {/each}
        </div>
      {/if}
      {#if sent}<p class="sent">{m.sent}</p>{/if}
    {/if}
    {#if note}<p class="note" role="status">{note}</p>{/if}
    {#if transport && detail?.canText}
      <DockReply
        bind:this={replyField}
        {transport}
        agent={{ serverId: agent.serverId, paneId: agent.paneId }}
        disabled={!detail.connected}
        onSent={replySent}
        onFocusChange={onReplyFocus}
      />
    {/if}
    {#if detail?.canFocus}
      <button type="button" class="focus" onclick={() => agent && void focusAgent(agent)}>{m.open_herdr}</button>
    {/if}
    {#if keyboardHints}<p class="keys">{m.key_legend}</p>{/if}
  {:else}
    <h2 class="title">{heading ?? fmt(m.idle_heading, { n: idle.length })}</h2>
    <ul class="idle">
      {#each idle as a (rowKey(a))}
        <li>
          {#if a.backend === "t3"}
            <!-- A T3 thread has no herdr pane to open: a plain row. -->
            <div class="row static">
              <DockMark agent={a} src={iconUrl(a)} size={20} />
              <span class="row-title">{a.title}</span>
              <span class="row-since">{formatSince(sinceOf(a))}</span>
            </div>
          {:else}
            <button type="button" class="row" aria-label={`${a.title} · ${m.open_herdr}`} onclick={() => void focusAgent(a)}>
              <DockMark agent={a} src={iconUrl(a)} size={20} />
              <span class="row-title">{a.title}</span>
              <span class="row-since">{formatSince(sinceOf(a))}</span>
            </button>
          {/if}
          {#if rowNotes[rowKey(a)]}<p class="note">{rowNotes[rowKey(a)]}</p>{/if}
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  .card {
    box-sizing: border-box;
    width: 300px;
    max-height: 520px; /* dockModel CARD_MAX_HEIGHT */
    overflow: auto;
    padding: var(--s3);
    display: grid;
    gap: var(--s2);
    background: var(--panel-raised);
    border: 1px solid var(--line-strong);
    border-radius: var(--r-panel);
    /* No CSS shadow: macOS draws the window's shadow from the drawn pixels'
       alpha, and a soft CSS halo made it trace a hard grey outline ~10 px
       outside the card. The window shadow alone shades the card. */
    color: var(--text);
  }
  header {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    gap: var(--s2);
    align-items: start;
  }
  .head { min-width: 0; }
  .title {
    margin: 0;
    font: var(--t-h2);
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .meta {
    margin: 0;
    color: var(--text-dim);
    font: var(--t-help);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .close {
    border: 0;
    background: transparent;
    color: var(--text-dim);
    font-size: 16px;
    line-height: 1;
    cursor: pointer;
    padding: 2px 4px;
  }
  .close:hover { color: var(--text); }
  .status {
    margin: 0;
    display: flex;
    flex-wrap: wrap;
    gap: var(--s2);
    align-items: center;
    color: var(--text-dim);
    font: var(--t-help);
  }
  .pill {
    padding: 1px var(--s2);
    border-radius: 999px;
    font: var(--t-eyebrow);
    color: var(--canvas);
    background: var(--st-unknown);
  }
  .st-blocked .pill { background: var(--st-blocked); }
  .st-waiting .pill { background: var(--st-waiting); }
  .st-done .pill { background: var(--st-done); }
  .st-working .pill { background: var(--st-working); }
  .st-idle .pill { background: var(--st-idle); }
  .prompt {
    margin: 0;
    max-height: calc(6 * 1.4em);
    overflow: auto;
    padding: var(--s2);
    background: var(--field);
    border-radius: var(--r-control);
    font: var(--t-mono);
    white-space: pre-wrap;
    word-break: break-word;
  }
  .reading, .sent { margin: 0; color: var(--text-dim); font: var(--t-help); }
  .options { display: grid; gap: var(--s1); }
  .opt {
    text-align: left;
    padding: var(--s1) var(--s2);
    border-radius: var(--r-control);
    border: 1px solid var(--tone);
    background: color-mix(in srgb, var(--tone) 16%, var(--key));
    color: var(--text);
    font: var(--t-label);
    cursor: pointer;
    transition: background var(--dur) var(--ease);
  }
  @media (prefers-reduced-motion: reduce) {
    .opt { transition: none; }
  }
  .opt:hover, .opt.armed { background: color-mix(in srgb, var(--tone) 40%, var(--key)); }
  .opt:disabled { opacity: .6; cursor: default; }
  .tone-approve { --tone: var(--st-green); }
  .tone-approve_always { --tone: var(--st-amber); }
  .tone-deny { --tone: var(--st-red); }
  .tone-other { --tone: var(--st-blue); }
  .note { margin: 0; color: var(--sev-warning-text); font: var(--t-help); }
  .focus {
    justify-self: start;
    padding: var(--s1) var(--s3);
    border-radius: var(--r-control);
    border: 1px solid var(--accent-ring);
    background: var(--accent-soft);
    color: var(--accent-strong);
    font: var(--t-label);
    cursor: pointer;
  }
  .keys {
    margin: 0;
    padding-top: var(--s1);
    border-top: 1px solid var(--line);
    color: var(--text-faint);
    font: var(--t-help);
  }
  .idle { list-style: none; margin: 0; padding: 0; display: grid; gap: 2px; }
  .row {
    width: 100%;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    gap: var(--s2);
    align-items: center;
    padding: var(--s1);
    border: 0;
    border-radius: var(--r-control);
    background: transparent;
    color: var(--text);
    font: var(--t-body);
    text-align: left;
    cursor: pointer;
  }
  .row:hover { background: var(--key); }
  .row.static { cursor: default; }
  .row.static:hover { background: transparent; }
  .row-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .row-since { color: var(--text-faint); font: var(--t-help); }
</style>
