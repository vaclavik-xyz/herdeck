<script lang="ts">
  import type { Snippet } from "svelte";

  import { t } from "../i18n.svelte";
  import FieldCopy from "./FieldCopy.svelte";

  let { label, state, inheritedDisplay, onstate, children, help = "", owner = null }:
    {
      label: string;
      state: "inherit" | "override";
      inheritedDisplay: string;
      onstate: (s: "inherit" | "override") => void;
      children: Snippet;
      help?: string;
      owner?: string | null;
    } = $props();

  const SEGMENTS = $derived<{ value: "inherit" | "override"; text: string }[]>([
    { value: "inherit", text: t("widget.inherit") },
    { value: "override", text: t("widget.custom") },
  ]);

  function pick(next: "inherit" | "override"): void {
    if (next !== state) onstate(next);
  }
</script>

<div class="override">
  <FieldCopy {label} {help} {owner} />
  <div class="body">
    <div class="seg" role="group" aria-label={label}>
      {#each SEGMENTS as s}
        <button
          type="button"
          class:on={s.value === state}
          aria-pressed={s.value === state}
          onclick={() => pick(s.value)}
        >{s.text}</button>
      {/each}
    </div>
    {#if state === "override"}
      {@render children()}
    {:else}
      <p class="hint">{t("widget.inherited")} {inheritedDisplay}</p>
    {/if}
  </div>
</div>

<style>
  .override {
    display: grid;
    grid-template-columns: var(--field-label-w) minmax(0, 1fr);
    align-items: start;
    gap: var(--s1) var(--s6);
    padding: var(--s3) 0;
    border-bottom: 1px solid var(--line);
  }
  .body {
    display: flex;
    grid-column: 2;
    grid-row: 1 / span 2;
    flex-direction: column;
    gap: var(--s2);
    min-width: 0;
  }
  .seg {
    display: inline-flex;
    align-self: flex-start;
    border: 1px solid var(--line-strong);
    border-radius: var(--r-control);
    overflow: hidden;
  }
  .seg button {
    min-height: 30px;
    padding: 0 var(--s3);
    border: 0;
    border-right: 1px solid var(--line);
    background: var(--field);
    color: var(--text-dim);
    cursor: pointer;
    transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
  }
  .seg button:last-child { border-right: 0; }
  .seg button:hover { color: var(--text); background: var(--panel-raised); }
  .seg button.on {
    background: var(--accent-soft);
    color: var(--text);
    box-shadow: inset 0 0 0 1px var(--accent-ring);
  }
  .hint { margin: 0; color: var(--text-dim); font: var(--t-help); }
  @media (max-width: 760px) {
    .override { grid-template-columns: minmax(0, 1fr); }
    .body { grid-column: 1; grid-row: auto; }
  }
</style>
