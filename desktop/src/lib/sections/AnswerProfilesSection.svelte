<script lang="ts">
  import TextField from "../fields/TextField.svelte";
  import ListField from "../fields/ListField.svelte";
  import TriStateListField from "../fields/TriStateListField.svelte";
  import OverrideField from "../fields/OverrideField.svelte";
  import { defineMessages, fieldHelp, fmt, locale } from "../i18n.svelte";
  import {
    DEFAULT_ANSWER_PROFILES, answerProfileRows, serializeNamedRows, applyMapSection,
    inheritedAnswerProfiles, overrideValuePath, setOverridePath, clearOverridePath,
    type ConfigPayload, type AnswerProfileRow, type ListFieldState,
  } from "../configClient";

  let { payload = $bindable(), onChange, onError, reloadRev, editProfile = null }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void; reloadRev: number; editProfile?: string | null } = $props();

  const LIST_KEYS = ["approve", "deny", "stop"] as const;

  // Field tooltips in the current language — central catalog in help.ts,
  // required for each labelled field (enforced by sections.help.test.ts).
  const HELP = $derived(fieldHelp("answer_profiles"));

  const LM = defineMessages({
    en: {
      heading: "Answer profiles",
      overlay_hint:
        "Per-entry overlay: override an inherited answer profile or add a profile-only one. Inherited entries cannot be removed in an overlay.",
      remove_entry: "Remove profile entry",
      inherit: "Inherit",
      inherited_hint: "inherited: {value}",
      none: "(none)",
      entry_name_placeholder: "profile entry name",
      add_profile_only: "+ add (profile only)",
      base_hint: "Keys sent to the agent for approve / deny / stop by agent type.",
      new_profile: "(new profile)",
      remove_profile: "Remove answer profile",
      name_first_hint: "Enter a profile name to edit the keys.",
      add_profile: "+ add profile",
      err_duplicate: "duplicate answer profile name — it won't save until you rename it",
      err_exists: "entry '{name}' already exists",
      err_builtin_name: "'{name}' is a built-in answer profile and cannot replace a custom profile",
    },
    cs: {
      heading: "Profily odpovědí",
      overlay_hint:
        "Per-entry overlay: přepiš zděděný answer profil nebo přidej profilový. Zděděné položky nelze v overlay smazat.",
      remove_entry: "Odebrat profilovou položku",
      inherit: "Zdědit",
      inherited_hint: "zděděno: {value}",
      none: "(nic)",
      entry_name_placeholder: "jméno profilové položky",
      add_profile_only: "+ přidat (jen profil)",
      base_hint: "Klávesy posílané agentovi pro approve / deny / stop podle typu agenta.",
      new_profile: "(nový profil)",
      remove_profile: "Odebrat answer profil",
      name_first_hint: "Zadej jméno profilu pro úpravu kláves.",
      add_profile: "+ přidat profil",
      err_duplicate: "duplicitní jméno answer profilu — neuloží se, dokud nepřejmenuješ",
      err_exists: "položka '{name}' už existuje",
      err_builtin_name: "„{name}“ je vestavěný answer profil a nemůže nahradit vlastní profil",
    },
  });
  const lm = $derived(LM[locale.lang]);

  // Local editor rows (source of truth while editing); re-seeded only when ConfigApp bumps
  // `reloadRev` (load/discard/Apply-reload) — same pattern as StartProfilesSection.
  let rows = $state<AnswerProfileRow[]>(answerProfileRows(payload));
  let seenRev = $state(reloadRev);
  let rejectedRenameRev = $state(0);

  $effect(() => {
    if (reloadRev !== seenRev) {
      seenRev = reloadRev;
      rows = answerProfileRows(payload);
    }
  });

  // Serialization rules live in the tested configClient helpers; approve_always is omitted
  // when null (absent → backend falls back to approve), so an unrelated edit never writes [].
  function commit(next: AnswerProfileRow[]): void {
    rows = next; // local rows always reflect the edit, so the user sees + can fix a clash
    const { duplicate, section } = serializeNamedRows(next, (r) => {
      const prof: Record<string, string[]> = { approve: r.approve, deny: r.deny, stop: r.stop };
      if (r.approve_always !== null) prof.approve_always = r.approve_always;
      return prof;
    });
    if (duplicate) {
      onError(lm.err_duplicate);
      return;
    }
    const updated = applyMapSection(payload, "answer_profiles", section);
    if (updated === null) return; // unchanged serialized section → no dirty
    payload = updated;
    onChange();
  }

  function rename(i: number, name: string): void {
    const nextName = name.trim();
    if (!isBuiltIn(rows[i].name) && isBuiltIn(nextName)) {
      rejectedRenameRev += 1;
      onError(fmt(lm.err_builtin_name, { name: nextName }));
      return;
    }
    commit(rows.map((r, j) => (j === i ? { ...r, name } : r)));
  }
  function setList(i: number, key: (typeof LIST_KEYS)[number], v: string[]): void {
    commit(rows.map((r, j) => (j === i ? { ...r, [key]: v } : r)));
  }
  // approve_always carries a third "default" (null) state: absent → backend falls back to approve.
  function aaState(r: AnswerProfileRow): ListFieldState {
    if (r.approve_always === null) return "default";
    return r.approve_always.length === 0 ? "empty" : "custom";
  }
  function setApproveAlways(i: number, state: ListFieldState, list: string[]): void {
    const value: string[] | null = state === "default" ? null : state === "empty" ? [] : list;
    commit(rows.map((r, j) => (j === i ? { ...r, approve_always: value } : r)));
  }
  function add(): void {
    commit([...rows, { name: "", approve: [], deny: [], stop: [], approve_always: null }]);
  }
  function isBuiltIn(name: string): boolean { return Object.hasOwn(DEFAULT_ANSWER_PROFILES, name); }
  function remove(i: number): void { commit(rows.filter((_, j) => j !== i)); }

  // --- overlay mode: per-entry override (whole entry dict) ---
  const SEC = "answer_profiles";
  const overlay = $derived(editProfile != null && editProfile !== "default");
  const prof = $derived(editProfile ?? "");
  const argvOf = (v: unknown): string[] => (Array.isArray(v) ? v.map(String) : []);
  const dictOf = (v: unknown): Record<string, unknown> => (v != null && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {});

  // inhMap is default-aware: built-in answer profiles (claude/codex/default) are always
  // inherited (backend seeds DEFAULT_PROFILES), so overlay shows them as overridable entries.
  function inhMap(): Record<string, unknown> { return inheritedAnswerProfiles(payload, prof); }
  function ownMap(): Record<string, unknown> { const v = overrideValuePath(payload, prof, [SEC]); return dictOf(v); }
  function entryNames(): string[] { return Array.from(new Set([...Object.keys(inhMap()), ...Object.keys(ownMap())])); }
  function isInherited(name: string): boolean { return name in inhMap(); }
  function entryState(name: string): "inherit" | "override" { return name in ownMap() ? "override" : "inherit"; }
  function inhEntry(name: string): Record<string, unknown> { return dictOf(inhMap()[name]); }
  function ovEntry(name: string): Record<string, unknown> { return dictOf(ownMap()[name]); }
  function inhSummary(name: string): string {
    const e = inhEntry(name);
    return LIST_KEYS.map((k) => `${k}:${argvOf(e[k]).length}`).join(" · ");
  }
  // Effective per-subkey value for display: the backend merges answer_profile entries
  // RECURSIVELY per-subkey, so a partial overlay (e.g. only `approve`) inherits the omitted
  // fields from base. Show that inherited value rather than an empty list; the write path
  // (setEntryKey/setAAOv) only persists a field once the user changes it, so an omitted field
  // stays inherited until edited.
  function entryKeyValue(name: string, k: string): string[] {
    const own = ovEntry(name);
    return k in own ? argvOf(own[k]) : argvOf(inhEntry(name)[k]);
  }
  // Backend `_parse_profile` falls back `approve_always` → the entry's `approve` when the key
  // is absent from the merged entry. Mirror that so the overlay shows/seeds the real effective
  // value: own approve_always → inherited approve_always → effective approve.
  function aaListOv(name: string): string[] {
    const own = ovEntry(name);
    if ("approve_always" in own) return argvOf(own.approve_always);
    const inh = inhEntry(name);
    return "approve_always" in inh ? argvOf(inh.approve_always) : entryKeyValue(name, "approve");
  }
  function aaHint(name: string): string {
    const inh = inhEntry(name);
    // Same effective fallback as aaListOv: an absent approve_always resolves to the EFFECTIVE
    // approve (own override if present, else inherited) — mirrors backend _parse_profile.
    const v = "approve_always" in inh ? argvOf(inh.approve_always) : entryKeyValue(name, "approve");
    return v.join(" · ") || lm.none;
  }
  function writeEntry(name: string, entry: Record<string, unknown>): void { payload = { ...payload, profiles: setOverridePath(payload.profiles, prof, [SEC, name], entry) }; onChange(); }
  // Deep-copy an entry's list values so the override seed never aliases the DEFAULT_ANSWER_PROFILES
  // singleton (inhEntry returns a reference into it for built-in profiles); guards against any
  // future in-place edit corrupting the shared default process-wide.
  function cloneEntry(e: Record<string, unknown>): Record<string, unknown> {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(e)) out[k] = Array.isArray(v) ? v.map(String) : v;
    return out;
  }
  function setEntryState(name: string, s: "inherit" | "override"): void {
    payload = { ...payload, profiles: s === "inherit" ? clearOverridePath(payload.profiles, prof, [SEC, name]) : setOverridePath(payload.profiles, prof, [SEC, name], cloneEntry(inhEntry(name))) };
    onChange();
  }
  function setEntryKey(name: string, key: (typeof LIST_KEYS)[number], v: string[]): void { writeEntry(name, { ...ovEntry(name), [key]: v }); }
  function aaStateOv(name: string): ListFieldState { const e = ovEntry(name); if (!("approve_always" in e)) return "default"; const v = e.approve_always; return Array.isArray(v) && v.length === 0 ? "empty" : "custom"; }
  function setAAOv(name: string, state: ListFieldState, list: string[]): void {
    const e = { ...ovEntry(name) };
    if (state === "default") delete e.approve_always;
    else e.approve_always = state === "empty" ? [] : list;
    writeEntry(name, e);
  }
  let newName = $state("");
  function addEntry(): void {
    const n = newName.trim();
    if (n === "") return;
    if (entryNames().includes(n)) { onError(fmt(lm.err_exists, { name: n })); return; }
    payload = { ...payload, profiles: setOverridePath(payload.profiles, prof, [SEC, n], { approve: [], deny: [], stop: [] }) };
    newName = "";
    onChange();
  }
  function removeOwn(name: string): void { payload = { ...payload, profiles: clearOverridePath(payload.profiles, prof, [SEC, name]) }; onChange(); }
</script>

<h2>{lm.heading}{#if overlay} · overlay: {editProfile}{/if}</h2>
{#if overlay}
  <p class="hint">{lm.overlay_hint}</p>
  {#each entryNames() as name (name)}
    <fieldset>
      <legend>{name}{#if !isInherited(name)} <button type="button" title={lm.remove_entry} onclick={() => removeOwn(name)}>×</button>{/if}</legend>
      <OverrideField label="keys" help={HELP.keys} state={entryState(name)} inheritedDisplay={inhSummary(name)} onstate={(s) => setEntryState(name, s)}>
        {#each LIST_KEYS as k}
          <ListField label={k} help={HELP[k]} value={entryKeyValue(name, k)} onchange={(v) => setEntryKey(name, k, v)} />
        {/each}
        <TriStateListField label="approve_always" help={HELP.approve_always} state={aaStateOv(name)} list={aaListOv(name)} inheritLabel={lm.inherit} inheritHint={fmt(lm.inherited_hint, { value: aaHint(name) })} onchange={(s, l) => setAAOv(name, s, l)} />
      </OverrideField>
    </fieldset>
  {/each}
  <div class="create">
    <input placeholder={lm.entry_name_placeholder} bind:value={newName} />
    <button type="button" onclick={addEntry}>{lm.add_profile_only}</button>
  </div>
{:else}
  <p class="hint">{lm.base_hint}</p>
  {#each rows as e, i (i)}
    <fieldset>
      <legend>{e.name || lm.new_profile}{#if !isBuiltIn(e.name)} <button type="button" title={lm.remove_profile} onclick={() => remove(i)}>×</button>{/if}</legend>
      {#if !isBuiltIn(e.name)}
        {#key `${i}:${rejectedRenameRev}`}
          <TextField label="name" help={HELP.name} value={e.name} oninput={(v) => rename(i, v)} />
        {/key}
      {/if}
      {#if e.name.trim() !== ""}
        {#each LIST_KEYS as k}
          <ListField label={k} help={HELP[k]} value={e[k] ?? []} onchange={(v) => setList(i, k, v)} />
        {/each}
        <TriStateListField label="approve_always" help={HELP.approve_always} state={aaState(e)} list={e.approve_always ?? []} onchange={(s, l) => setApproveAlways(i, s, l)} />
      {:else}
        <p class="hint">{lm.name_first_hint}</p>
      {/if}
    </fieldset>
  {/each}
  <button type="button" onclick={add}>{lm.add_profile}</button>
{/if}

<style>
  h2 { margin: 0 0 8px; }
  .hint { color: #888; margin: 0 0 8px; }
  .create { display: flex; gap: 6px; margin: 8px 0; }
  .create input { flex: 1; background: #141417; border: 1px solid #2a2a30; color: inherit; padding: 4px 6px; border-radius: 4px; }
  fieldset { border: 1px solid #2a2a30; border-radius: 6px; margin: 8px 0; padding: 8px 12px; }
  legend { color: #ccc; } legend button { color: #e05050; background: none; border: 0; cursor: pointer; }
  button { background: #1b1b1f; border: 1px solid #2a2a30; color: inherit; border-radius: 4px; padding: 4px 8px; cursor: pointer; }
</style>
