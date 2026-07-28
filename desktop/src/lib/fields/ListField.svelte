<script lang="ts">
  import { t } from "../i18n.svelte";
  import FieldCopy from "./FieldCopy.svelte";

  let { label, value, onchange, help = "", owner = null }:
    { label: string; value: string[]; onchange: (v: string[]) => void; help?: string; owner?: string | null } = $props();

  const items = $derived(Array.isArray(value) ? value : []);

  function setItem(i: number, v: string): void {
    onchange(items.map((x, j) => (j === i ? v : x)));
  }
  function add(): void {
    onchange([...items, ""]);
  }
  function remove(i: number): void {
    onchange(items.filter((_, j) => j !== i));
  }
</script>

<div class="listfield" class:unlabelled={!label}>
  {#if label}<FieldCopy {label} {help} {owner} />{/if}
  <div class="rows">
    {#each items as item, i (i)}
      <div class="row">
        <input value={item} oninput={(e) => setItem(i, (e.target as HTMLInputElement).value)} />
        <button type="button" title={t("widget.remove_row")} onclick={() => remove(i)}>×</button>
      </div>
    {/each}
    <button type="button" class="add" onclick={add}>{t("widget.add")}</button>
  </div>
</div>

<style>
  .listfield {
    display: grid;
    grid-template-columns: var(--field-label-w) minmax(0, 1fr);
    align-items: start;
    gap: var(--s1) var(--s6);
    padding: var(--s3) 0;
    border-bottom: 1px solid var(--line);
  }
  .listfield.unlabelled { grid-template-columns: minmax(0, 1fr); padding: 0; border-bottom: 0; }
  .rows {
    display: flex;
    grid-column: 2;
    grid-row: 1 / span 2;
    flex-direction: column;
    gap: var(--s1);
    max-width: var(--control-lg);
  }
  .listfield.unlabelled .rows { grid-column: 1; grid-row: auto; max-width: none; }
  .row { display: flex; gap: var(--s2); }
  input {
    flex: 1;
    min-height: 32px;
    padding: 0 var(--s3);
    border: 1px solid var(--line-strong);
    border-radius: var(--r-control);
    background: var(--field);
    color: var(--text);
  }
  button {
    min-height: 30px;
    padding: 0 var(--s3);
    border: 1px solid var(--line-strong);
    border-radius: var(--r-control);
    background: var(--panel-raised);
    color: var(--text);
    cursor: pointer;
  }
  button:hover { background: var(--key); }
  .row button { color: var(--st-offline); }
  .add { align-self: flex-start; }
  @media (max-width: 760px) {
    .listfield { grid-template-columns: minmax(0, 1fr); }
    .rows { grid-column: 1; grid-row: auto; max-width: none; }
  }
</style>
