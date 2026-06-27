<script lang="ts">
  import TextField from "../fields/TextField.svelte";
  import OverrideField from "../fields/OverrideField.svelte";
  import {
    macrosOf, addMacro, removeMacro, updateMacro, macroRecords, inheritedMacros,
    overrideValuePath, overrideStatePath, setOverridePath, clearOverridePath,
    type ConfigPayload, type MacroRecord,
  } from "../configClient";

  let { payload = $bindable(), onChange, editProfile = null }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void; editProfile?: string | null } = $props();

  const overlay = $derived(editProfile != null && editProfile !== "default");
  const prof = $derived(editProfile ?? "");

  // --- base mode (unchanged) ---
  const macros = $derived(macrosOf(payload));
  function set(i: number, field: keyof MacroRecord, v: string): void { payload = updateMacro(payload, i, field, v); onChange(); }
  function add(): void { payload = addMacro(payload); onChange(); }
  function remove(i: number): void { payload = removeMacro(payload, i); onChange(); }

  // --- overlay mode: whole-list override (macros replace wholesale in the backend merge) ---
  function ovMacros(): MacroRecord[] { return macroRecords(overrideValuePath(payload, prof, ["macros"])); }
  function inhMacros(): MacroRecord[] { return inheritedMacros(payload, prof); }
  function ovState(): "inherit" | "override" { return overrideStatePath(payload, prof, ["macros"]) === "default" ? "inherit" : "override"; }
  function writeOv(list: MacroRecord[]): void { payload = { ...payload, profiles: setOverridePath(payload.profiles, prof, ["macros"], list) }; onChange(); }
  function setOvState(s: "inherit" | "override"): void {
    payload = { ...payload, profiles: s === "inherit" ? clearOverridePath(payload.profiles, prof, ["macros"]) : setOverridePath(payload.profiles, prof, ["macros"], inhMacros()) };
    onChange();
  }
  function ovSet(i: number, field: keyof MacroRecord, v: string): void { writeOv(ovMacros().map((m, j) => (j === i ? { ...m, [field]: v } : m))); }
  function ovAdd(): void { writeOv([...ovMacros(), { label: "", text: "" }]); }
  function ovRemove(i: number): void { writeOv(ovMacros().filter((_, j) => j !== i)); }
</script>

<h2>Macros{#if overlay} · overlay: {editProfile}{/if}</h2>
{#if overlay}
  <OverrideField label="macros" state={ovState()} inheritedDisplay={`${inhMacros().length} maker`} onstate={setOvState}>
    {#each ovMacros() as m, i (i)}
      <fieldset>
        <legend>{m.label || "(nové makro)"} <button type="button" onclick={() => ovRemove(i)}>×</button></legend>
        <TextField label="label" value={m.label} oninput={(v) => ovSet(i, "label", v)} />
        <TextField label="text" value={m.text} oninput={(v) => ovSet(i, "text", v)} />
      </fieldset>
    {/each}
    <button type="button" onclick={ovAdd}>+ přidat makro</button>
  </OverrideField>
{:else}
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
{/if}

<style>
  h2 { margin: 0 0 8px; }
  fieldset { border: 1px solid #2a2a30; border-radius: 6px; margin: 8px 0; padding: 8px 12px; }
  legend { color: #ccc; } legend button { color: #e05050; background: none; border: 0; cursor: pointer; }
  button { background: #1b1b1f; border: 1px solid #2a2a30; color: inherit; border-radius: 4px; padding: 4px 8px; cursor: pointer; }
</style>
