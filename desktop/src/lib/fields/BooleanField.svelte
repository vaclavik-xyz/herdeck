<script lang="ts">
  import FieldCopy from "./FieldCopy.svelte";
  let { label, value, onchange, help = "", configKey = "" }:
    { label: string; value: boolean; onchange: (v: boolean) => void; help?: string; configKey?: string } = $props();
</script>

<label class="field" class:unlabelled={!label}>
  {#if label}<FieldCopy {label} {help} {configKey} />{/if}
  <input
    type="checkbox"
    checked={value}
    onchange={(e) => onchange((e.target as HTMLInputElement).checked)}
  />
</label>

<style>
  /* Same label column as every other field, so a toggle lines up with the
     selects and text inputs of its group instead of floating at the far edge. */
  .field {
    display: grid;
    grid-template-columns: var(--field-label-w) minmax(0, 1fr);
    align-items: center;
    gap: var(--s1) var(--s6);
    padding: var(--s3) 0;
    border-bottom: 1px solid var(--line);
    cursor: pointer;
  }
  .field.unlabelled { grid-template-columns: minmax(0, 1fr); padding: 0; border-bottom: 0; }
  input {
    appearance: none;
    position: relative;
    grid-column: 2;
    grid-row: 1 / span 2;
    justify-self: start;
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
  .field.unlabelled input { grid-column: 1; grid-row: auto; }
  @media (max-width: 760px) {
    .field { grid-template-columns: minmax(0, 1fr); }
    input { grid-column: 1; grid-row: auto; }
  }
  @container settings-form (max-width: 600px) {
    .field { grid-template-columns: minmax(0, 1fr); }
    input { grid-column: 1; grid-row: auto; }
  }
</style>
