<script lang="ts">
  import ListField from "./ListField.svelte";
  import FieldCopy from "./FieldCopy.svelte";
  import { t } from "../i18n.svelte";
  import type { ListFieldState } from "../configClient";

  let { label, state: fieldState, list, customSeed, defaultHint, inheritLabel, inheritHint, resetKey = "", onchange, help = "" }:
    {
      label: string;
      state: ListFieldState;
      list: string[];
      customSeed?: string[];
      defaultHint?: string;
      inheritLabel?: string;
      inheritHint?: string;
      resetKey?: string;
      onchange: (state: ListFieldState, list: string[]) => void;
      help?: string;
    } = $props();

  const SEGMENTS = $derived<{ value: ListFieldState; text: string }[]>([
    { value: "default", text: inheritLabel ?? t("widget.default") },
    { value: "custom", text: t("widget.custom") },
    { value: "empty", text: t("widget.off") },
  ]);
  let draft = $state<string[] | null>(null);
  let previousResetKey = "";
  let resetKeySeen = false;
  $effect(() => {
    const nextResetKey = resetKey;
    if (resetKeySeen && nextResetKey !== previousResetKey) draft = null;
    previousResetKey = nextResetKey;
    resetKeySeen = true;
  });
  const visibleState = $derived(draft === null ? fieldState : "custom");
  const visibleList = $derived(draft === null ? list : draft);

  // Switching to "custom" carries the current list (user then edits it); if the list is
  // empty, seed one blank row so the write is non-empty ([] persists as "empty", not
  // "custom", because setListField writes [] for both — see configClient comment).
  // "default"/"empty" pass list through unchanged (write-time state drives the output).
  function pick(next: ListFieldState): void {
    if (next === visibleState) return;
    if (next === "custom") {
      const seed = list.length > 0 ? list : customSeed !== undefined ? customSeed : [""];
      if (seed.length === 0) {
        draft = [""];
        return;
      }
      onchange("custom", seed);
    } else {
      draft = null;
      onchange(next, list);
    }
  }
  function editCustom(next: string[]): void {
    if (draft !== null && !next.some((item) => item.trim() !== "")) {
      draft = next.length > 0 ? next : [""];
      return;
    }
    draft = null;
    onchange("custom", next);
  }
</script>

<div class="tristate">
  <FieldCopy {label} {help} />
  <div class="body">
    <div class="seg" role="group" aria-label={label}>
      {#each SEGMENTS as s}
        <button
          type="button"
          class:on={s.value === visibleState}
          aria-pressed={s.value === visibleState}
          onclick={() => pick(s.value)}
        >{s.text}</button>
      {/each}
    </div>
    {#if visibleState === "custom"}
      <ListField label="" value={visibleList} onchange={editCustom} />
    {:else if visibleState === "default"}
      <p class="hint">{inheritHint ?? (defaultHint ? `${t("widget.default_prefix")} ${defaultHint}` : t("widget.default_empty"))}</p>
    {:else}
      <p class="hint">{t("widget.empty_off")}</p>
    {/if}
  </div>
</div>

<style>
  .tristate {
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
    .tristate { grid-template-columns: minmax(0, 1fr); }
    .body { grid-column: 1; grid-row: auto; }
  }
</style>
