<script lang="ts">
  import TextField from "../fields/TextField.svelte";
  import ListField from "../fields/ListField.svelte";
  import OverrideField from "../fields/OverrideField.svelte";
  import ConfirmRemoveButton from "../fields/ConfirmRemoveButton.svelte";
  import { defineMessages, fieldHelp, fmt, locale } from "../i18n.svelte";
  import {
    DEFAULT_START_PROFILES, startProfileRows, serializeNamedRows, applyMapSection,
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

  // Field tooltips in the current language — central catalog in help.ts,
  // required for each labelled field (enforced by sections.help.test.ts).
  const HELP = $derived(fieldHelp("start_profiles"));

  const LM = defineMessages({
    en: {
      overlay_hint:
        "Per-entry overlay: override an inherited entry or add a profile-only one. Inherited entries cannot be removed in an overlay (the backend merge is additive).",
      remove_entry: "Remove profile entry",
      empty_value: "(empty)",
      entry_name_placeholder: "profile entry name",
      add_profile_only: "+ add (profile only)",
      base_hint: "Launch command (argv) for each agent type started from the deck.",
      mode_default: "Default",
      mode_custom: "Custom",
      mode_off: "Off",
      empty_hint: "No launchers (explicit empty map).",
      new_profile: "(new profile)",
      remove_launcher: "Remove launcher",
      name_first_hint: "Enter a profile name to edit argv.",
      add_profile: "+ add profile",
      default_hint: 'Default launchers (DEFAULT_START_PROFILES). Switch to "Custom" to edit.',
      err_duplicate: "duplicate start profile name. Rename it before saving",
      err_exists: "entry '{name}' already exists",
    },
    cs: {
      overlay_hint:
        "Per-entry overlay: přepiš zděděnou položku nebo přidej profilovou. Zděděné položky nelze v overlay smazat (backend merge je aditivní).",
      remove_entry: "Odebrat profilovou položku",
      empty_value: "(prázdné)",
      entry_name_placeholder: "jméno profilové položky",
      add_profile_only: "+ přidat (jen profil)",
      base_hint: "Spouštěcí příkaz (argv) pro každý typ agenta startovaného z decku.",
      mode_default: "Výchozí",
      mode_custom: "Vlastní",
      mode_off: "Vypnuto",
      empty_hint: "Žádné launchery (explicitní prázdná mapa).",
      new_profile: "(nový profil)",
      remove_launcher: "Odebrat spouštěč",
      name_first_hint: "Zadej jméno profilu pro úpravu argv.",
      add_profile: "+ přidat profil",
      default_hint: 'Výchozí launchery (DEFAULT_START_PROFILES). Přepni na „Vlastní" pro úpravu.',
      err_duplicate: "duplicitní jméno start profilu. Před uložením ho přejmenuj",
      err_exists: "položka '{name}' už existuje",
    },
  });
  const lm = $derived(LM[locale.lang]);

  // --- base mode: local rows (re-seed only on reloadRev) + explicit-empty mode ---
  let rows = $state<StartProfileRow[]>(startProfileRows(payload));
  let removalRevision = $state(0);
  let seenRev = $state<number | null>(null);
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
    if (duplicate) { onError(lm.err_duplicate); return; }
    const updated = applyMapSection(payload, SEC, section);
    if (updated === null) return;
    payload = updated;
    onChange();
  }
  function setMode(m: ListFieldState): void {
    mode = m;
    if (m === "custom") {
      const seeded = rows.length > 0
        ? rows
        : Object.entries(DEFAULT_START_PROFILES).map(([name, argv]) => ({ name, argv: [...argv] }));
      commit(seeded);
      return;
    }
    payload = setMapSectionState(payload, SEC, m);
    onChange();
  }
  function rename(i: number, name: string): void { commit(rows.map((r, j) => (j === i ? { ...r, name } : r))); }
  function setArgv(i: number, argv: string[]): void { commit(rows.map((r, j) => (j === i ? { ...r, argv } : r))); }
  function add(): void { commit([...rows, { name: "", argv: [] }]); }
  function remove(i: number): void {
    const next = rows.filter((_, j) => j !== i);
    removalRevision += 1;
    if (next.length === 0) {
      rows = [];
      mode = "empty";
      payload = setMapSectionState(payload, SEC, "empty");
      onChange();
      return;
    }
    commit(next);
  }

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
    if (entryNames().includes(n)) { onError(fmt(lm.err_exists, { name: n })); return; }
    payload = { ...payload, profiles: setOverridePath(payload.profiles, prof, [SEC, n], []) };
    newName = "";
    onChange();
  }
  function removeOwn(name: string): void { payload = { ...payload, profiles: clearOverridePath(payload.profiles, prof, [SEC, name]) }; onChange(); }
</script>

{#if overlay}
  <p class="hint">{lm.overlay_hint}</p>
  {#each entryNames() as name (name)}
    <fieldset>
      <legend>{name}{#if !isInherited(name)} <ConfirmRemoveButton title={lm.remove_entry} onconfirm={() => removeOwn(name)} />{/if}</legend>
      <OverrideField label="argv" help={HELP.argv} state={entryState(name)} inheritedDisplay={inhArgv(name).join(" · ") || lm.empty_value} onstate={(s) => setEntryState(name, s)}>
        <ListField label="" value={ovArgv(name)} onchange={(v) => setEntryArgv(name, v)} />
      </OverrideField>
    </fieldset>
  {/each}
  <div class="create">
    <input placeholder={lm.entry_name_placeholder} bind:value={newName} />
    <button type="button" disabled={!newName.trim()} onclick={addEntry}>{lm.add_profile_only}</button>
  </div>
{:else}
  <p class="hint">{lm.base_hint}</p>
  <div class="seg" role="group" aria-label={lm.base_hint}>
    <button type="button" class:on={mode === "default"} aria-pressed={mode === "default"} onclick={() => setMode("default")}>{lm.mode_default}</button>
    <button type="button" class:on={mode === "custom"} aria-pressed={mode === "custom"} onclick={() => setMode("custom")}>{lm.mode_custom}</button>
    <button type="button" class:on={mode === "empty"} aria-pressed={mode === "empty"} onclick={() => setMode("empty")}>{lm.mode_off}</button>
  </div>
  {#if mode === "empty"}
    <p class="hint">{lm.empty_hint}</p>
  {:else if mode === "custom"}
    {#each rows as e, i (i)}
      <fieldset>
        <legend>{e.name || lm.new_profile} <ConfirmRemoveButton title={lm.remove_launcher} identity={`${e.name}\u0000${e.argv.join("\u0000")}`} resetKey={removalRevision} onconfirm={() => remove(i)} /></legend>
        <TextField label="name" help={HELP.name} value={e.name} oninput={(v) => rename(i, v)} />
        {#if e.name.trim() !== ""}
          <ListField label="argv" help={HELP.argv} value={e.argv} onchange={(v) => setArgv(i, v)} />
        {:else}
          <p class="hint">{lm.name_first_hint}</p>
        {/if}
      </fieldset>
    {/each}
    <button type="button" onclick={add}>{lm.add_profile}</button>
  {:else}
    <p class="hint">{lm.default_hint}</p>
    <div class="launchers">
      {#each Object.entries(DEFAULT_START_PROFILES) as [name, argv] (name)}
        <div class="launcher">
          <strong>{name}</strong>
          <code>{argv.join(" ")}</code>
        </div>
      {/each}
    </div>
  {/if}
{/if}

<style>
  .hint { margin: 0 0 var(--s3); color: var(--text-dim); font: var(--t-help); }
  .seg {
    display: inline-flex;
    margin: 0 0 var(--s3);
    border: 1px solid var(--line-strong);
    border-radius: var(--r-control);
    overflow: hidden;
  }
  .seg button {
    min-height: 30px;
    padding: 0 var(--s3);
    border: 0;
    border-right: 1px solid var(--line);
    background: var(--field);
    color: var(--text-dim);
    cursor: pointer;
    transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
  }
  .seg button:last-child { border-right: 0; }
  .seg button:hover { color: var(--text); background: var(--panel-raised); }
  .seg button.on {
    background: var(--accent-soft);
    color: var(--text);
    box-shadow: inset 0 0 0 1px var(--accent-ring);
  }
  .create { display: flex; gap: var(--s2); margin: var(--s3) 0; }
  .create input {
    flex: 1;
    min-height: 30px;
    padding: 0 var(--s3);
    border: 1px solid var(--line-strong);
    border-radius: var(--r-control);
    background: var(--field);
    color: var(--text);
  }
  fieldset { border: 1px solid var(--line); border-radius: var(--r-panel); background: var(--panel); padding: var(--s4) var(--s5); margin: var(--s2) 0; }
  legend { color: var(--text); }
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
  .launchers { display: grid; gap: var(--s2); margin-top: var(--s3); }
  .launcher {
    display: grid;
    grid-template-columns: minmax(0, 160px) minmax(0, 1fr);
    align-items: center;
    gap: var(--s4);
    padding: var(--s3) var(--s4);
    border: 1px solid var(--line);
    border-radius: var(--r-panel);
    background: var(--panel);
  }
  .launcher strong { font: var(--t-label); }
  .launcher code { color: var(--text-dim); font: var(--t-mono); overflow-wrap: anywhere; }
</style>
