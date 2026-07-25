<script lang="ts">
  import FieldCopy from "./FieldCopy.svelte";
  let { label, value, onchange, help = "" }:
    { label: string; value: boolean; onchange: (v: boolean) => void; help?: string } = $props();
</script>

<label class="field">
  <FieldCopy {label} {help} />
  <input
    type="checkbox"
    checked={value}
    onchange={(e) => onchange((e.target as HTMLInputElement).checked)}
  />
</label>

<style>
  .field { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 12px; margin: 6px 0; cursor: pointer; }
  input { appearance: none; position: relative; grid-column: 2; grid-row: 1 / span 2; width: 36px; height: 21px; margin: 0; border: 1px solid var(--border-strong, #3b4553); border-radius: 999px; background: #272d36; cursor: pointer; transition: background 120ms ease, border-color 120ms ease; }
  input::after { content: ""; position: absolute; top: 3px; left: 3px; width: 13px; height: 13px; border-radius: 50%; background: #a1a9b4; transition: transform 120ms ease, background 120ms ease; }
  input:checked { border-color: #7897d5; background: #5879bd; }
  input:checked::after { transform: translateX(15px); background: #f2f4f7; }
  input:focus-visible { outline: 2px solid #8eabe7; outline-offset: 2px; }
  @media (prefers-reduced-motion: reduce) { input, input::after { transition: none; } }
</style>
