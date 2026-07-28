<script lang="ts">
  import FieldCopy from "./FieldCopy.svelte";
  let { label, value, onchange, help = "" }:
    { label: string; value: boolean; onchange: (v: boolean) => void; help?: string } = $props();
</script>

<label class="field" class:unlabelled={!label}>
  {#if label}<FieldCopy {label} {help} />{/if}
  <input
    type="checkbox"
    checked={value}
    onchange={(e) => onchange((e.target as HTMLInputElement).checked)}
  />
</label>

<style>
  .field {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    gap: var(--s1) var(--s6);
    padding: var(--s3) 0;
    border-bottom: 1px solid var(--line);
    cursor: pointer;
  }
  .field.unlabelled { padding: 0; border-bottom: 0; }
  input {
    appearance: none;
    position: relative;
    grid-column: 2;
    grid-row: 1 / span 2;
    width: 36px;
    height: 21px;
    margin: 0;
    border: 1px solid var(--line-strong);
    border-radius: 999px;
    background: var(--key);
    cursor: pointer;
    transition: background var(--dur) var(--ease), border-color var(--dur) var(--ease);
  }
  input::after {
    content: "";
    position: absolute;
    top: 3px;
    left: 3px;
    width: 13px;
    height: 13px;
    border-radius: 50%;
    background: var(--text-dim);
    transition: transform var(--dur) var(--ease), background var(--dur) var(--ease);
  }
  input:checked { border-color: var(--accent-strong); background: var(--accent); }
  input:checked::after { transform: translateX(15px); background: var(--text); }
</style>
