<script lang="ts">
  import type { Snippet } from "svelte";

  import { t } from "../i18n.svelte";
  import FieldCopy from "./FieldCopy.svelte";

  let { label, state, inheritedDisplay, onstate, children, help = "" }:
    {
      label: string;
      state: "inherit" | "override";
      inheritedDisplay: string;
      onstate: (s: "inherit" | "override") => void;
      children: Snippet;
      help?: string;
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
  <FieldCopy {label} {help} />
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
  .override { display: grid; grid-template-columns: var(--field-label-w, 120px) minmax(0, 1fr); align-items: start; gap: 12px; margin: 6px 0; }
  .body { display: flex; grid-column: 2; grid-row: 1 / span 2; flex-direction: column; gap: 4px; }
  .seg { display: inline-flex; align-self: flex-start; border: 1px solid #303b49; border-radius: 7px; overflow: hidden; }
  .seg button { background: #141417; border: 0; border-right: 1px solid #2a2a30; color: #aaa; padding: 4px 10px; cursor: pointer; }
  .seg button:last-child { border-right: 0; }
  .seg button.on { background: #29384f; color: #eef3fa; }
  .hint { color: #7f8c9d; margin: 2px 0; font-size: 9px; }
</style>
