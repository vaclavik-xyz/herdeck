<script lang="ts">
  import TextField from "../fields/TextField.svelte";
  import OverrideField from "../fields/OverrideField.svelte";
  import ConfirmRemoveButton from "../fields/ConfirmRemoveButton.svelte";
  import { defineMessages, fieldHelp, fmt, locale } from "../i18n.svelte";
  import {
    macrosOf, addMacro, removeMacro, updateMacro, macroRecords, inheritedMacros,
    overrideValuePath, overrideStatePath, setOverridePath, clearOverridePath,
    type ConfigPayload, type MacroRecord,
  } from "../configClient";

  let { payload = $bindable(), onChange, editProfile = null }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void; editProfile?: string | null } = $props();

  const overlay = $derived(editProfile != null && editProfile !== "default");
  const prof = $derived(editProfile ?? "");

  // Tooltips for every field come from the central catalog in the current
  // language — required for each labelled field (enforced by sections.help.test.ts).
  const HELP = $derived(fieldHelp("macros"));

  const LM = defineMessages({
    en: {
      new_macro: "(new macro)",
      remove_macro: "Remove macro",
      add_macro: "+ add macro",
      empty: "No macros yet. Add one to send a prepared message from the deck.",
      n_macros: "{n} macros",
    },
    cs: {
      new_macro: "(nové makro)",
      remove_macro: "Odebrat makro",
      add_macro: "+ přidat makro",
      empty: "Zatím žádné makro. Přidej připravenou zprávu, kterou odešleš z decku.",
      n_macros: "{n} maker",
    },
  });
  const lm = $derived(LM[locale.lang]);

  // --- base mode (unchanged) ---
  const macros = $derived(macrosOf(payload));
  let removalRevision = $state(0);
  function set(i: number, field: keyof MacroRecord, v: string): void { payload = updateMacro(payload, i, field, v); onChange(); }
  function add(): void { payload = addMacro(payload); onChange(); }
  function remove(i: number): void { payload = removeMacro(payload, i); removalRevision += 1; onChange(); }

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
  function ovRemove(i: number): void { removalRevision += 1; writeOv(ovMacros().filter((_, j) => j !== i)); }
</script>

{#if overlay}
  <OverrideField label="macros" help={HELP.macros} state={ovState()} inheritedDisplay={fmt(lm.n_macros, { n: inhMacros().length })} onstate={setOvState}>
    {#each ovMacros() as m, i (i)}
      <fieldset>
        <legend>{m.label || lm.new_macro} <ConfirmRemoveButton title={lm.remove_macro} identity={`${m.label}\u0000${m.text}`} resetKey={removalRevision} onconfirm={() => ovRemove(i)} /></legend>
        <TextField label="label" help={HELP.label} value={m.label} oninput={(v) => ovSet(i, "label", v)} />
        <TextField label="text" help={HELP.text} value={m.text} oninput={(v) => ovSet(i, "text", v)} />
      </fieldset>
    {/each}
    {#if ovMacros().length === 0}<p class="hint">{lm.empty}</p>{/if}
    <button type="button" onclick={ovAdd}>{lm.add_macro}</button>
  </OverrideField>
{:else}
  <!-- Index keying: append/remove list, no reordering, no per-row transient state. Same
       rationale as ServersSection — a stable-id apparatus would add needless complexity. -->
  {#each macros as m, i (i)}
    <fieldset>
      <legend>{m.label || lm.new_macro} <ConfirmRemoveButton title={lm.remove_macro} identity={`${m.label}\u0000${m.text}`} resetKey={removalRevision} onconfirm={() => remove(i)} /></legend>
      <TextField label="label" help={HELP.label} value={m.label} oninput={(v) => set(i, "label", v)} />
      <TextField label="text" help={HELP.text} value={m.text} oninput={(v) => set(i, "text", v)} />
    </fieldset>
  {/each}
  {#if macros.length === 0}<p class="hint">{lm.empty}</p>{/if}
  <button type="button" onclick={add}>{lm.add_macro}</button>
{/if}

<style>
  fieldset {
    margin: var(--s2) 0;
    padding: var(--s4) var(--s5);
    border: 1px solid var(--line);
    border-radius: var(--r-panel);
    background: var(--panel);
  }
  legend { color: var(--text); font: var(--t-label); }
  button {
    min-height: 30px;
    padding: 0 var(--s3);
    border: 1px solid var(--line-strong);
    border-radius: var(--r-control);
    background: var(--field);
    color: var(--text-dim);
    cursor: pointer;
    transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
  }
  button:hover { color: var(--text); background: var(--panel-raised); }
  .hint { margin: 0 0 var(--s3); color: var(--text-dim); font: var(--t-help); }
</style>
