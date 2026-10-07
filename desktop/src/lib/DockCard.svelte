<script lang="ts">
  // The mini dock's card: one agent (title, repo · branch, elapsed; for a
  // blocked agent its prompt and one button per option, coloured like the
  // deck) or the folded idle agents. Every action goes through the guarded
  // /agent/* routes (agentCardClient.ts): an answer names the prompt revision
  // it was read under, and every outcome is shown — never assumed.
  import { onMount, untrack } from "svelte";
  import DockMark from "./DockMark.svelte";
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
    sinceOf,
    iconUrl,
    transport,
    onClose,
    onSent = () => {},
  }: {
    agent?: DockAgent | null;
    idle?: DockAgent[];
    sinceOf: (a: DockAgent) => number;
    iconUrl: (a: DockAgent) => string | null;
    transport: AgentTransport | null;
    onClose: () => void;
    onSent?: () => void;
  } = $props();

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
  const meta = $derived(agent ? [agent.repo, agent.branch].filter(Boolean).join(" · ") : "");
  const rowKey = (a: DockAgent) => `${a.serverId}/${a.paneId}`;

  async function load(refresh: boolean): Promise<void> {
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
      detail = res.detail;
      if (res.detail.status === "blocked" && res.detail.prompt === null && res.detail.promptPending) {
        repoll = setTimeout(() => void load(false), PROMPT_REPOLL_MS);
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
      return;
    }
    note = outcomeText(locale.lang, out);
    if (out.code === "stale") await load(true);
  }

  async function focusAgent(target: DockAgent): Promise<void> {
    const t = transport;
    if (!t) return;
    const out = await t.act("focus", { serverId: target.serverId, paneId: target.paneId });
    if (!alive) return;
    if (out.ok) {
      onClose();
      return;
    }
    if (agent) note = outcomeText(locale.lang, out);
    else rowNotes = { ...rowNotes, [rowKey(target)]: outcomeText(locale.lang, out) };
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
      <button type="button" class="close" title={m.close_title} aria-label={m.close_title} onclick={onClose}>×</button>
    </header>
    <p class="status st-{agent.status}">
      <span class="pill">{statusLabel(locale.lang, agent.status, agent.offline)}</span>
      <span>{fmt(m.since, { t: formatSince(sinceOf(agent)) })}</span>
      {#if agent.waitingOn}<span>{fmt(m.waiting_on, { what: agent.waitingOn })}</span>{/if}
      {#if agent.subagents > 0}<span>{fmt(m.subagents, { n: agent.subagents })}</span>{/if}
    </p>
    {#if blocked}
      {#if detail?.prompt}
        <pre class="prompt" use:scrollEnd={detail.prompt}>{detail.prompt}</pre>
      {:else if !note && (detail === null || detail.promptPending)}
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
    {#if detail?.canFocus}
      <button type="button" class="focus" onclick={() => agent && void focusAgent(agent)}>{m.open_herdr}</button>
    {/if}
  {:else}
    <h2 class="title">{fmt(m.idle_heading, { n: idle.length })}</h2>
    <ul class="idle">
      {#each idle as a (rowKey(a))}
        <li>
          <button type="button" class="row" title={m.open_herdr} onclick={() => void focusAgent(a)}>
            <DockMark agent={a} src={iconUrl(a)} size={20} />
            <span class="row-title">{a.title}</span>
            <span class="row-since">{formatSince(sinceOf(a))}</span>
          </button>
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
    max-height: 380px;
    overflow: auto;
    padding: var(--s3);
    display: grid;
    gap: var(--s2);
    background: var(--panel-raised);
    border: 1px solid var(--line-strong);
    border-radius: var(--r-panel);
    box-shadow: 0 10px 28px color-mix(in srgb, var(--canvas) 70%, transparent);
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
  .row-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .row-since { color: var(--text-faint); font: var(--t-help); }
</style>
