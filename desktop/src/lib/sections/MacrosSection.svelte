<script lang="ts">
  import TextField from "../fields/TextField.svelte";
  import {
    macrosOf, addMacro, removeMacro, updateMacro,
    type ConfigPayload, type MacroRecord,
  } from "../configClient";

  let { payload = $bindable(), onChange }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void } = $props();

  const macros = $derived(macrosOf(payload));

  function set(i: number, field: keyof MacroRecord, v: string): void {
    payload = updateMacro(payload, i, field, v);
    onChange();
  }
  function add(): void {
    payload = addMacro(payload);
    onChange();
  }
  function remove(i: number): void {
    payload = removeMacro(payload, i);
    onChange();
  }
</script>

<h2>Macros</h2>
<!-- Index keying: append/remove list, no reordering, no per-row transient state. Same
     rationale as ServersSection — a stable-id apparatus would add needless complexity. -->
{#each macros as m, i (i)}
  <fieldset>
    <legend>{m.label || "(nové makro)"} <button type="button" onclick={() => remove(i)}>×</button></legend>
    <TextField label="label" value={m.label} oninput={(v) => set(i, "label", v)} />
    <TextField label="text" value={m.text} oninput={(v) => set(i, "text", v)} />
  </fieldset>
{/each}
<button type="button" onclick={add}>+ přidat makro</button>

<style>
  h2 { margin: 0 0 8px; }
  fieldset { border: 1px solid #2a2a30; border-radius: 6px; margin: 8px 0; padding: 8px 12px; }
  legend { color: #ccc; } legend button { color: #e05050; background: none; border: 0; cursor: pointer; }
  button { background: #1b1b1f; border: 1px solid #2a2a30; color: inherit; border-radius: 4px; padding: 4px 8px; cursor: pointer; }
</style>
