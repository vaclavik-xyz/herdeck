<script lang="ts">
  let { label, value, onchange }:
    { label: string; value: string[]; onchange: (v: string[]) => void } = $props();

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

<div class="listfield">
  <span class="label">{label}</span>
  <div class="rows">
    {#each items as item, i (i)}
      <div class="row">
        <input value={item} oninput={(e) => setItem(i, (e.target as HTMLInputElement).value)} />
        <button type="button" onclick={() => remove(i)}>×</button>
      </div>
    {/each}
    <button type="button" class="add" onclick={add}>+ přidat</button>
  </div>
</div>

<style>
  .listfield { display: grid; grid-template-columns: 120px 1fr; align-items: start; gap: 8px; margin: 6px 0; }
  .label { color: #aaa; padding-top: 4px; }
  .rows { display: flex; flex-direction: column; gap: 4px; }
  .row { display: flex; gap: 6px; }
  input { flex: 1; background: #141417; border: 1px solid #2a2a30; color: inherit; padding: 4px 6px; border-radius: 4px; }
  button { background: #1b1b1f; border: 1px solid #2a2a30; color: inherit; border-radius: 4px; padding: 4px 8px; cursor: pointer; }
  .row button { color: #e05050; }
  .add { align-self: flex-start; }
</style>
