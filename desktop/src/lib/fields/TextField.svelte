<script lang="ts">
  import FieldCopy from "./FieldCopy.svelte";
  // `help` is the Czech tooltip explaining what the setting does. Every field
  // with a visible label must pass one — enforced by sections.help.test.ts.
  let { label, value, oninput, help = "", owner = null }:
    { label: string; value: string; oninput: (v: string) => void; help?: string; owner?: string | null } = $props();
</script>

<label class="field" class:unlabelled={!label}>
  {#if label}<FieldCopy {label} {help} {owner} />{/if}
  <input value={value} oninput={(e) => oninput((e.target as HTMLInputElement).value)} />
</label>

<style>
  .field { display: grid; grid-template-columns: var(--field-label-w, 120px) minmax(0, 1fr); align-items: center; gap: 12px; margin: 4px 0; }
  .field.unlabelled { grid-template-columns: minmax(0, 1fr); }
  input { grid-column: 2; grid-row: 1 / span 2; width: 100%; background: #141417; border: 1px solid #2a2a30; color: inherit; padding: 4px 6px; border-radius: 4px; }
  .field.unlabelled input { grid-column: 1; grid-row: auto; }
</style>
