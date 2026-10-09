<script lang="ts">
  // The dock card's live output: the last few lines of the agent's pane as
  // plain text, like a tiny terminal tail. The session (dockPreview.ts) runs
  // only while the box is expanded and this is mounted — the card unmounts on
  // close and on an agent switch, which closes it. Unsupported / T3 / error →
  // the box is simply absent. The DOM only changes when the visible text does.
  import { onMount, untrack } from "svelte";
  import type { AgentRef, AgentTransport } from "./agentCardClient";
  import { DOCK_MESSAGES } from "./dockMessages";
  import {
    markPreviewUnsupported,
    previewUnsupported,
    readPreviewCollapsed,
    startPreview,
    viewerStorage,
    writePreviewCollapsed,
    type PreviewHandle,
  } from "./dockPreview";
  import { locale } from "./i18n.svelte";

  let {
    transport,
    agent,
    plausible,
  }: {
    transport: AgentTransport;
    agent: AgentRef;
    /** The detail says a preview could work (herdr backend, server up): a
     *  collapsed box is offered, an expanded one reserves its space early. */
    plausible: boolean;
  } = $props();

  const m = $derived(DOCK_MESSAGES[locale.lang]);
  const storage = viewerStorage();

  let collapsed = $state(readPreviewCollapsed(storage));
  let phase = $state<"idle" | "connecting" | "live" | "ended" | "absent">("idle");
  let lines = $state<string[]>([]);
  let handle: PreviewHandle | null = null;

  function begin(): void {
    const ref = untrack(() => ({ serverId: agent.serverId, paneId: agent.paneId }));
    phase = "connecting";
    lines = [];
    handle = startPreview(transport, ref, {
      onOpen: () => {
        phase = "live";
      },
      onLines: (next) => {
        lines = next;
        phase = "live";
      },
      onEnd: (reason) => {
        handle = null;
        if (reason === "unsupported") markPreviewUnsupported(ref.serverId);
        // An open that never showed a frame leaves no box; a stream that
        // ended keeps its last lines, marked as ended.
        phase = lines.length > 0 ? "ended" : "absent";
      },
    });
  }

  function end(): void {
    handle?.stop();
    handle = null;
  }

  function toggle(): void {
    collapsed = !collapsed;
    writePreviewCollapsed(storage, collapsed);
    if (collapsed) {
      end();
      phase = "idle";
      lines = [];
    } else {
      begin();
    }
  }

  onMount(() => {
    if (!collapsed) begin();
    return end;
  });

  const shown = $derived(
    phase === "live" ||
      phase === "ended" ||
      (phase === "connecting" && plausible) ||
      (phase === "idle" && collapsed && plausible && !previewUnsupported(agent.serverId)),
  );
</script>

{#if shown}
  <section class="preview" class:collapsed aria-label={m.preview_label}>
    <button
      type="button"
      class="bar"
      aria-expanded={!collapsed}
      title={m.preview_toggle}
      onclick={toggle}
    >
      <svg class="chev" viewBox="0 0 10 10" width="8" height="8" aria-hidden="true">
        <path d="M3 2l3 3-3 3" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" />
      </svg>
      <span class="label">{m.preview_label}</span>
      {#if !collapsed}
        <span class="state" class:live={phase === "live"}>
          {#if phase === "live"}<span class="dot" aria-hidden="true"></span>{/if}
          {phase === "live" ? m.preview_live : phase === "ended" ? m.preview_ended : m.preview_connecting}
        </span>
      {/if}
    </button>
    {#if !collapsed}
      <div class="screen" class:ended={phase === "ended"}>
        {#if lines.length === 0 && phase === "live"}
          <div class="ln empty">{m.preview_empty}</div>
        {/if}
        {#each lines as line, i (i)}
          <div class="ln">{line}</div>
        {/each}
      </div>
    {/if}
  </section>
{/if}

<style>
  .preview { display: grid; gap: 3px; min-width: 0; }
  .bar {
    display: flex;
    align-items: center;
    gap: 5px;
    padding: 0;
    border: 0;
    background: transparent;
    color: var(--text-faint);
    font: var(--t-eyebrow);
    text-transform: uppercase;
    letter-spacing: .05em;
    cursor: pointer;
    text-align: left;
  }
  .bar:hover { color: var(--text-dim); }
  .bar:focus-visible { outline: 2px solid var(--accent-ring); outline-offset: 2px; border-radius: 3px; }
  .chev {
    transform: rotate(90deg);
    transition: transform var(--dur) var(--ease);
  }
  .collapsed .chev { transform: none; }
  .label { flex: none; }
  .state {
    margin-left: auto;
    display: inline-flex;
    align-items: center;
    gap: 4px;
    text-transform: none;
    letter-spacing: 0;
    font: var(--t-help);
    font-size: 10.5px;
  }
  .state.live { color: var(--st-working); }
  .dot {
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: currentColor;
  }
  .screen {
    box-sizing: border-box;
    /* Eight lines tall whatever arrives, the tail anchored at the bottom like
       a terminal: the card doesn't jump as output fills in. */
    height: calc(8 * 14px + 2 * 6px);
    padding: 6px var(--s2);
    display: flex;
    flex-direction: column;
    justify-content: flex-end;
    overflow: hidden;
    border-radius: var(--r-control);
    background: var(--field);
    border: 1px solid var(--line);
    color: var(--text-dim);
    font: 10.5px/14px var(--font-mono);
    font-variant-ligatures: none;
    user-select: text;
  }
  .screen.ended { color: var(--text-faint); }
  .ln {
    flex: none;
    white-space: pre;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .ln.empty { color: var(--text-faint); font-style: italic; }
  @media (prefers-reduced-motion: reduce) {
    .chev { transition: none; }
  }
  :global(.mini-dock.reduced) .chev { transition: none; }
</style>
