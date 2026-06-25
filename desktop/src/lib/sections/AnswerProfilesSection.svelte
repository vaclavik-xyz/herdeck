<script lang="ts">
  import TextField from "../fields/TextField.svelte";
  import ListField from "../fields/ListField.svelte";
  import {
    answerProfileRows, serializeNamedRows, applyMapSection,
    type ConfigPayload, type AnswerProfileRow,
  } from "../configClient";

  let { payload = $bindable(), onChange, onError, reloadRev }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void; reloadRev: number } = $props();

  const KEYS = ["approve", "deny", "stop", "approve_always"] as const;

  // Local editor rows (source of truth while editing); re-seeded only when ConfigApp bumps
  // `reloadRev` (load/discard/Apply-reload) — same pattern as StartProfilesSection.
  let rows = $state<AnswerProfileRow[]>(answerProfileRows(payload));
  let seenRev = $state(reloadRev);

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
      onError("duplicitní jméno answer profilu — neuloží se, dokud nepřejmenuješ");
      return;
    }
    const updated = applyMapSection(payload, "answer_profiles", section);
    if (updated === null) return; // unchanged serialized section → no dirty
    payload = updated;
    onChange();
  }

  function rename(i: number, name: string): void { commit(rows.map((r, j) => (j === i ? { ...r, name } : r))); }
  function setList(i: number, key: (typeof KEYS)[number], v: string[]): void {
    commit(rows.map((r, j) => (j === i ? { ...r, [key]: v } : r)));
  }
  function add(): void {
    commit([...rows, { name: "", approve: [], deny: [], stop: [], approve_always: null }]);
  }
  function remove(i: number): void { commit(rows.filter((_, j) => j !== i)); }
</script>

<h2>Answer profiles</h2>
<p class="hint">Klávesy posílané agentovi pro approve / deny / stop podle typu agenta.</p>
{#each rows as e, i (i)}
  <fieldset>
    <legend>{e.name || "(nový profil)"} <button type="button" onclick={() => remove(i)}>×</button></legend>
    <TextField label="name" value={e.name} oninput={(v) => rename(i, v)} />
    {#if e.name.trim() !== ""}
      {#each KEYS as k}
        <ListField label={k} value={e[k] ?? []} onchange={(v) => setList(i, k, v)} />
      {/each}
    {:else}
      <p class="hint">Zadej jméno profilu pro úpravu kláves.</p>
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
