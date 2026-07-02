<script lang="ts">
  import type { Snippet } from "svelte";

  let { label, state, inheritedDisplay, onstate, children, help = "" }:
    {
      label: string;
      state: "inherit" | "override";
      inheritedDisplay: string;
      onstate: (s: "inherit" | "override") => void;
      children: Snippet;
      help?: string;
    } = $props();

  const SEGMENTS: { value: "inherit" | "override"; text: string }[] = [
    { value: "inherit", text: "Zdědit" },
    { value: "override", text: "Vlastní" },
  ];

  function pick(next: "inherit" | "override"): void {
    if (next !== state) onstate(next);
  }
</script>

<div class="override">
  <span class="label fieldlabel" class:hashelp={!!help} title={help || undefined}>{label}</span>
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
      <p class="hint">zděděno: {inheritedDisplay}</p>
    {/if}
  </div>
</div>

<style>
  .override { display: grid; grid-template-columns: var(--field-label-w, 120px) 1fr; align-items: start; gap: 8px; margin: 6px 0; }
  .label { color: #aaa; padding-top: 4px; }
  .fieldlabel.hashelp { text-decoration: underline dotted #5a5a62; text-underline-offset: 3px; cursor: help; }
  .body { display: flex; flex-direction: column; gap: 4px; }
  .seg { display: inline-flex; align-self: flex-start; border: 1px solid #2a2a30; border-radius: 4px; overflow: hidden; }
  .seg button { background: #141417; border: 0; border-right: 1px solid #2a2a30; color: #aaa; padding: 4px 10px; cursor: pointer; }
  .seg button:last-child { border-right: 0; }
  .seg button.on { background: #2a2a30; color: #e8e8ea; }
  .hint { color: #777; margin: 2px 0; font-style: italic; }
</style>
