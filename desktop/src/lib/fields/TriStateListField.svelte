<script lang="ts">
  import ListField from "./ListField.svelte";
  import type { ListFieldState } from "../configClient";

  let { label, state, list, defaultHint, onchange }:
    {
      label: string;
      state: ListFieldState;
      list: string[];
      defaultHint?: string;
      onchange: (state: ListFieldState, list: string[]) => void;
    } = $props();

  const SEGMENTS: { value: ListFieldState; text: string }[] = [
    { value: "default", text: "Výchozí" },
    { value: "custom", text: "Vlastní" },
    { value: "empty", text: "Vypnuto" },
  ];

  // Switching to "custom" carries the current list (user then edits it); if the list is
  // empty, seed one blank row so the write is non-empty ([] persists as "empty", not
  // "custom", because setListField writes [] for both — see configClient comment).
  // "default"/"empty" pass list through unchanged (write-time state drives the output).
  function pick(next: ListFieldState): void {
    if (next === state) return;
    if (next === "custom") {
      onchange("custom", list.length > 0 ? list : [""]);
    } else {
      onchange(next, list);
    }
  }
</script>

<div class="tristate">
  <span class="label">{label}</span>
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
    {#if state === "custom"}
      <ListField label="" value={list} onchange={(v) => onchange("custom", v)} />
    {:else if state === "default"}
      <p class="hint">{defaultHint ? `výchozí: ${defaultHint}` : "(výchozí)"}</p>
    {:else}
      <p class="hint">prázdné — vypnuto</p>
    {/if}
  </div>
</div>

<style>
  .tristate { display: grid; grid-template-columns: 120px 1fr; align-items: start; gap: 8px; margin: 6px 0; }
  .label { color: #aaa; padding-top: 4px; }
  .body { display: flex; flex-direction: column; gap: 4px; }
  .seg { display: inline-flex; align-self: flex-start; border: 1px solid #2a2a30; border-radius: 4px; overflow: hidden; }
  .seg button { background: #141417; border: 0; border-right: 1px solid #2a2a30; color: #aaa; padding: 4px 10px; cursor: pointer; }
  .seg button:last-child { border-right: 0; }
  .seg button.on { background: #2a2a30; color: #e8e8ea; }
  .hint { color: #777; margin: 2px 0; font-style: italic; }
</style>
