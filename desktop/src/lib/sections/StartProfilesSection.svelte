<script lang="ts">
  import TextField from "../fields/TextField.svelte";
  import ListField from "../fields/ListField.svelte";
  import {
    startProfileRows, serializeNamedRows, applyMapSection,
    type ConfigPayload, type StartProfileRow,
  } from "../configClient";

  let { payload = $bindable(), onChange, onError, reloadRev }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void; reloadRev: number } = $props();

  // Local editor rows are the source of truth WHILE editing — they may hold blank or
  // duplicate names that a map cannot, so a rename never collapses a sibling. Re-seed
  // ONLY when ConfigApp bumps `reloadRev` (an explicit load/discard/Apply-reload signal):
  // a same-content discard still resets local-only rows that never reached the payload.
  // Our own commits don't bump reloadRev, so they never re-seed (no loop, no lost edit).
  let rows = $state<StartProfileRow[]>(startProfileRows(payload));
  let seenRev = $state(reloadRev);

  $effect(() => {
    if (reloadRev !== seenRev) {
      seenRev = reloadRev;
      rows = startProfileRows(payload);
    }
  });

  // All map-serialization rules (skip blank, block duplicate, omit empty section, no-op
  // detection) live in the tested configClient helpers; this stays a thin orchestrator.
  function commit(next: StartProfileRow[]): void {
    rows = next; // local rows always reflect the edit, so the user sees + can fix a clash
    const { duplicate, section } = serializeNamedRows(next, (r) => r.argv);
    if (duplicate) {
      onError("duplicitní jméno start profilu — neuloží se, dokud nepřejmenuješ");
      return;
    }
    const updated = applyMapSection(payload, "start_profiles", section);
    if (updated === null) return; // unchanged serialized section → no dirty
    payload = updated;
    onChange();
  }

  function rename(i: number, name: string): void { commit(rows.map((r, j) => (j === i ? { ...r, name } : r))); }
  function setArgv(i: number, argv: string[]): void { commit(rows.map((r, j) => (j === i ? { ...r, argv } : r))); }
  function add(): void { commit([...rows, { name: "", argv: [] }]); }
  function remove(i: number): void { commit(rows.filter((_, j) => j !== i)); }
</script>

<h2>Start profiles</h2>
<p class="hint">Spouštěcí příkaz (argv) pro každý typ agenta startovaného z decku.</p>
{#each rows as e, i (i)}
  <fieldset>
    <legend>{e.name || "(nový profil)"} <button type="button" onclick={() => remove(i)}>×</button></legend>
    <TextField label="name" value={e.name} oninput={(v) => rename(i, v)} />
    {#if e.name.trim() !== ""}
      <ListField label="argv" value={e.argv} onchange={(v) => setArgv(i, v)} />
    {:else}
      <p class="hint">Zadej jméno profilu pro úpravu argv.</p>
    {/if}
  </fieldset>
{/each}
<button type="button" onclick={add}>+ přidat profil</button>

<style>
  h2 { margin: 0 0 8px; }
  .hint { color: #888; margin: 0 0 8px; }
  fieldset { border: 1px solid #2a2a30; border-radius: 6px; margin: 8px 0; padding: 8px 12px; }
  legend { color: #ccc; } legend button { color: #e05050; background: none; border: 0; cursor: pointer; }
  button { background: #1b1b1f; border: 1px solid #2a2a30; color: inherit; border-radius: 4px; padding: 4px 8px; cursor: pointer; }
</style>
