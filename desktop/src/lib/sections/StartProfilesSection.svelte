<script lang="ts">
  import TextField from "../fields/TextField.svelte";
  import ListField from "../fields/ListField.svelte";
  import OverrideField from "../fields/OverrideField.svelte";
  import {
    startProfileRows, serializeNamedRows, applyMapSection,
    mapSectionState, setMapSectionState, inheritedStartProfiles,
    overrideValuePath, setOverridePath, clearOverridePath,
    type ConfigPayload, type StartProfileRow, type ListFieldState,
  } from "../configClient";

  let { payload = $bindable(), onChange, onError, reloadRev, editProfile = null }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void; reloadRev: number; editProfile?: string | null } = $props();

  const SEC = "start_profiles";
  const overlay = $derived(editProfile != null && editProfile !== "default");
  const prof = $derived(editProfile ?? "");
  const argvOf = (v: unknown): string[] => (Array.isArray(v) ? v.map(String) : []);

  // Czech tooltips for every field — required for each labelled field
  // (enforced by sections.help.test.ts).
  const HELP: Record<string, string> = {
    name: "Jméno typu agenta — zobrazí se na dlaždici v menu „+ New“ a určuje ikonu i profil odpovědí.",
    argv: "Příkaz a jeho argumenty, kterým se agent spustí v novém panelu (každá položka = jedno slovo).",
  };

  // --- base mode: local rows (re-seed only on reloadRev) + explicit-empty mode ---
  let rows = $state<StartProfileRow[]>(startProfileRows(payload));
  let seenRev = $state(reloadRev);
  let mode = $state<ListFieldState>(mapSectionState(payload, SEC));

  $effect(() => {
    if (reloadRev !== seenRev) {
      seenRev = reloadRev;
      rows = startProfileRows(payload);
      mode = mapSectionState(payload, SEC);
    }
  });

  function commit(next: StartProfileRow[]): void {
    rows = next;
    const { duplicate, section } = serializeNamedRows(next, (r) => r.argv);
    if (duplicate) { onError("duplicitní jméno start profilu — neuloží se, dokud nepřejmenuješ"); return; }
    const updated = applyMapSection(payload, SEC, section);
    if (updated === null) return;
    payload = updated;
    onChange();
  }
  function setMode(m: ListFieldState): void {
    mode = m;
    if (m === "custom") { commit(rows); return; } // reveal editor; rows drive the map
    payload = setMapSectionState(payload, SEC, m);
    onChange();
  }
  function rename(i: number, name: string): void { commit(rows.map((r, j) => (j === i ? { ...r, name } : r))); }
  function setArgv(i: number, argv: string[]): void { commit(rows.map((r, j) => (j === i ? { ...r, argv } : r))); }
  function add(): void { commit([...rows, { name: "", argv: [] }]); }
  function remove(i: number): void { commit(rows.filter((_, j) => j !== i)); }

  // --- overlay mode: per-entry override (read live, no local rows) ---
  // inhMap is default-aware: when base omits start_profiles the inherited map is
  // DEFAULT_START_PROFILES (5 launchers), so overlay shows them as overridable entries.
  function inhMap(): Record<string, unknown> { return inheritedStartProfiles(payload, prof); }
  function ownMap(): Record<string, unknown> { const v = overrideValuePath(payload, prof, [SEC]); return v != null && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {}; }
  function entryNames(): string[] { return Array.from(new Set([...Object.keys(inhMap()), ...Object.keys(ownMap())])); }
  function isInherited(name: string): boolean { return name in inhMap(); }
  function entryState(name: string): "inherit" | "override" { return name in ownMap() ? "override" : "inherit"; }
  function inhArgv(name: string): string[] { return argvOf(inhMap()[name]); }
  function ovArgv(name: string): string[] { return argvOf(ownMap()[name]); }
  function setEntryState(name: string, s: "inherit" | "override"): void {
    payload = { ...payload, profiles: s === "inherit" ? clearOverridePath(payload.profiles, prof, [SEC, name]) : setOverridePath(payload.profiles, prof, [SEC, name], inhArgv(name)) };
    onChange();
  }
  function setEntryArgv(name: string, argv: string[]): void { payload = { ...payload, profiles: setOverridePath(payload.profiles, prof, [SEC, name], argv) }; onChange(); }
  let newName = $state("");
  function addEntry(): void {
    const n = newName.trim();
    if (n === "") return;
    if (entryNames().includes(n)) { onError(`položka '${n}' už existuje`); return; }
    payload = { ...payload, profiles: setOverridePath(payload.profiles, prof, [SEC, n], []) };
    newName = "";
    onChange();
  }
  function removeOwn(name: string): void { payload = { ...payload, profiles: clearOverridePath(payload.profiles, prof, [SEC, name]) }; onChange(); }
</script>

<h2>Spouštěče agentů{#if overlay} · overlay: {editProfile}{/if}</h2>
{#if overlay}
  <p class="hint">Per-entry overlay: přepiš zděděnou položku nebo přidej profilovou. Zděděné položky nelze v overlay smazat (backend merge je aditivní).</p>
  {#each entryNames() as name (name)}
    <fieldset>
      <legend>{name}{#if !isInherited(name)} <button type="button" title="Odebrat profilovou položku" onclick={() => removeOwn(name)}>×</button>{/if}</legend>
      <OverrideField label="argv" help={HELP.argv} state={entryState(name)} inheritedDisplay={inhArgv(name).join(" · ") || "(prázdné)"} onstate={(s) => setEntryState(name, s)}>
        <ListField label="" value={ovArgv(name)} onchange={(v) => setEntryArgv(name, v)} />
      </OverrideField>
    </fieldset>
  {/each}
  <div class="create">
    <input placeholder="jméno profilové položky" bind:value={newName} />
    <button type="button" onclick={addEntry}>+ přidat (jen profil)</button>
  </div>
{:else}
  <p class="hint">Spouštěcí příkaz (argv) pro každý typ agenta startovaného z decku.</p>
  <div class="modes">
    <button type="button" class:active={mode === "default"} onclick={() => setMode("default")}>Výchozí</button>
    <button type="button" class:active={mode === "custom"} onclick={() => setMode("custom")}>Vlastní</button>
    <button type="button" class:active={mode === "empty"} onclick={() => setMode("empty")}>Vypnuto</button>
  </div>
  {#if mode === "empty"}
    <p class="hint">Žádné launchery (explicitní prázdná mapa).</p>
  {:else if mode === "custom"}
    {#each rows as e, i (i)}
      <fieldset>
        <legend>{e.name || "(nový profil)"} <button type="button" title="Odebrat spouštěč" onclick={() => remove(i)}>×</button></legend>
        <TextField label="name" help={HELP.name} value={e.name} oninput={(v) => rename(i, v)} />
        {#if e.name.trim() !== ""}
          <ListField label="argv" help={HELP.argv} value={e.argv} onchange={(v) => setArgv(i, v)} />
        {:else}
          <p class="hint">Zadej jméno profilu pro úpravu argv.</p>
        {/if}
      </fieldset>
    {/each}
    <button type="button" onclick={add}>+ přidat profil</button>
  {:else}
    <p class="hint">Výchozí launchery (DEFAULT_START_PROFILES). Přepni na „Vlastní" pro úpravu.</p>
  {/if}
{/if}

<style>
  h2 { margin: 0 0 8px; }
  .hint { color: #888; margin: 0 0 8px; }
  .modes { display: flex; gap: 4px; margin: 8px 0; }
  .modes button { background: #1b1b1f; border: 1px solid #2a2a30; color: inherit; border-radius: 4px; padding: 4px 10px; cursor: pointer; }
  .modes button.active { background: #2d3550; border-color: #4a5a80; }
  .create { display: flex; gap: 6px; margin: 8px 0; }
  .create input { flex: 1; background: #141417; border: 1px solid #2a2a30; color: inherit; padding: 4px 6px; border-radius: 4px; }
  fieldset { border: 1px solid #2a2a30; border-radius: 6px; margin: 8px 0; padding: 8px 12px; }
  legend { color: #ccc; } legend button { color: #e05050; background: none; border: 0; cursor: pointer; }
  button { background: #1b1b1f; border: 1px solid #2a2a30; color: inherit; border-radius: 4px; padding: 4px 8px; cursor: pointer; }
</style>
