<script lang="ts">
  // The usage card beside the strip (hover/focus on a footer ring): the
  // provider and its plan, then one row per window, shortest first — a bar of
  // what is used, "62% used · resets in 1 h 20 min" and, when the burn rate
  // fills the window early, "runs out ~40 min before reset".
  import UsageMark from "./UsageMark.svelte";
  import {
    fullEarlyText,
    leftFraction,
    providerColor,
    providerName,
    usageTone,
    windowLine,
    windowsByLength,
    type ProviderUsage,
  } from "./dockUsage";
  import { DOCK_MESSAGES } from "./dockMessages";
  import { locale } from "./i18n.svelte";

  let {
    usage,
    markSrc,
    now,
    onClose,
  }: {
    usage: ProviderUsage;
    markSrc: string | null;
    now: number;
    onClose: () => void;
  } = $props();

  const m = $derived(DOCK_MESSAGES[locale.lang]);
  const color = $derived(providerColor(usage.provider));
  const rows = $derived(
    windowsByLength(usage.windows).map((w) => ({
      w,
      tone: usageTone(leftFraction(w.usedPercent)),
      line: windowLine(locale.lang, w, now),
      early: fullEarlyText(locale.lang, w.fullEarlyS),
    })),
  );
</script>

<section class="card usage-card" style:--c={color}>
  <header>
    <span class="logo"><UsageMark provider={usage.provider} src={markSrc} {color} size={18} /></span>
    <div class="head">
      <h2 class="title">{providerName(usage.provider)}</h2>
      {#if usage.plan}<p class="meta">{usage.plan}</p>{/if}
    </div>
    <button type="button" class="close" title={m.close_title} aria-label={m.close_title} onclick={onClose}>×</button>
  </header>
  <ul class="windows">
    {#each rows as row, i (i)}
      <li class="win tone-{row.tone}">
        <div class="win-head">
          <span class="win-label">{row.w.label}</span>
          <span class="win-line">{row.line}</span>
        </div>
        <div class="bar" aria-hidden="true">
          <span class="fill" style:transform="scaleX({Math.min(100, Math.max(0, row.w.usedPercent)) / 100})"></span>
        </div>
        {#if row.early}<p class="early">{row.early}</p>{/if}
      </li>
    {/each}
  </ul>
</section>

<style>
  /* Same surface as DockCard: raised panel, strong hairline, no CSS shadow
     (macOS traces a hard outline around a soft CSS halo). */
  .card {
    box-sizing: border-box;
    width: 264px;
    max-height: 260px;
    overflow: auto;
    padding: var(--s3);
    display: grid;
    gap: var(--s3);
    background: var(--panel-raised);
    border: 1px solid var(--line-strong);
    border-radius: var(--r-panel);
    color: var(--text);
  }
  header {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    gap: var(--s2);
    align-items: center;
  }
  .logo {
    width: 28px;
    height: 28px;
    display: grid;
    place-items: center;
    border-radius: 50%;
    background: color-mix(in srgb, var(--c) 14%, var(--key));
  }
  .head { min-width: 0; display: flex; align-items: baseline; gap: var(--s2); }
  .title { margin: 0; font: var(--t-h2); }
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
  .windows { list-style: none; margin: 0; padding: 0; display: grid; gap: var(--s3); }
  .win { display: grid; gap: 6px; --tone: var(--c); }
  .win.tone-low { --tone: var(--st-blocked); }
  .win.tone-critical { --tone: var(--st-offline); }
  .win-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--s2);
    min-width: 0;
  }
  .win-label {
    font: var(--t-label);
    font-variant-numeric: tabular-nums;
    color: var(--text);
  }
  .win-line {
    min-width: 0;
    color: var(--text-dim);
    font: var(--t-help);
    font-variant-numeric: tabular-nums;
    text-align: right;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .tone-low .win-line, .tone-critical .win-line { color: var(--text); }
  .bar {
    height: 6px;
    border-radius: 3px;
    overflow: hidden;
    background: color-mix(in srgb, var(--tone) 16%, var(--field));
  }
  .fill {
    display: block;
    height: 100%;
    background: var(--tone); /* square end: a scaled radius would squash */
    transform-origin: left center;
    /* transform only: the compositor eases a changed value. */
    transition: transform 600ms var(--ease);
  }
  .early { margin: 0; color: var(--sev-warning-text); font: var(--t-help); }
  :global(.mini-dock.reduced) .fill { transition: none; }
  @media (prefers-reduced-motion: reduce) {
    .fill { transition: none; }
  }
</style>
