<script lang="ts">
  import FieldCopy from "./FieldCopy.svelte";
  import { defineMessages, locale } from "../i18n.svelte";
  import { serializeNamedRows } from "../configClient";

  // Free-key `repo = "path"` map editor ([view.project_icons]). Edited rows stay
  // local so a half-typed row (a repo name without a path yet) is never written
  // to the config; only complete rows are serialized.
  type Row = { name: string; path: string };
  let { label, help = "", entries, inheritedHint = "", resetKey, onchange, onError }:
    {
      label: string;
      help?: string;
      entries: Record<string, string>;
      inheritedHint?: string;
      resetKey: string;
      onchange: (map: Record<string, string> | undefined) => void;
      onError: (msg: string) => void;
    } = $props();

  const LM = defineMessages({
    en: {
      repo: "Repo name",
      path: "Icon file path",
      path_placeholder: "~/icons/project.png",
      add: "+ add project icon",
      remove: "Remove project icon",
      empty: "No overrides: each repo's own favicon is used.",
      err_duplicate: "duplicate repo name in project icons. Rename it before saving",
    },
    cs: {
      repo: "Název repa",
      path: "Cesta k souboru ikony",
      path_placeholder: "~/icons/project.png",
      add: "+ přidat ikonu projektu",
      remove: "Odebrat ikonu projektu",
      empty: "Bez přepisů: použije se favicon z každého repa.",
      err_duplicate: "duplicitní název repa v ikonách projektů. Před uložením ho přejmenuj",
    },
  });
  const lm = $derived(LM[locale.lang]);

  const rowsOf = (map: Record<string, string>): Row[] =>
    Object.entries(map).map(([name, path]) => ({ name, path }));
  // `draft` holds the rows once the user edits; until then (and after every
  // resetKey change, e.g. a reload or a profile switch) the rows mirror `entries`.
  let draft = $state<Row[] | null>(null);
  let previousResetKey = "";
  let resetKeySeen = false;
  $effect(() => {
    const nextResetKey = resetKey;
    if (resetKeySeen && nextResetKey !== previousResetKey) draft = null;
    previousResetKey = nextResetKey;
    resetKeySeen = true;
  });
  const rows = $derived(draft ?? rowsOf(entries));

  function commit(next: Row[]): void {
    draft = next;
    const complete = next.filter((r) => r.path.trim() !== "");
    const { duplicate, section } = serializeNamedRows(complete, (r) => r.path.trim());
    if (duplicate) { onError(lm.err_duplicate); return; }
    onchange(section);
  }
  function edit(i: number, patch: Partial<Row>): void {
    commit(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  }
  const valueOf = (e: Event): string => (e.target as HTMLInputElement).value;
</script>

<div class="field project-icons">
  <FieldCopy {label} {help} />
  <div class="body">
    {#if inheritedHint}<p class="hint">{inheritedHint}</p>{/if}
    {#each rows as row, i (i)}
      <div class="row">
        <input aria-label={lm.repo} placeholder={lm.repo} value={row.name} oninput={(e) => edit(i, { name: valueOf(e) })} />
        <input aria-label={lm.path} placeholder={lm.path_placeholder} value={row.path} oninput={(e) => edit(i, { path: valueOf(e) })} />
        <button type="button" class="remove" title={lm.remove} aria-label={lm.remove} onclick={() => commit(rows.filter((_, j) => j !== i))}>×</button>
      </div>
    {/each}
    {#if rows.length === 0 && !inheritedHint}<p class="hint">{lm.empty}</p>{/if}
    <button type="button" class="add" onclick={() => (draft = [...rows, { name: "", path: "" }])}>{lm.add}</button>
  </div>
</div>

<style>
  .field {
    display: grid;
    grid-template-columns: var(--field-label-w) minmax(0, 1fr);
    gap: var(--s1) var(--s6);
    padding: var(--s3) 0;
    border-bottom: 1px solid var(--line);
  }
  .body { grid-column: 2; grid-row: 1 / span 2; display: grid; gap: var(--s2); max-width: var(--control-lg); }
  .row { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 2fr) auto; gap: var(--s2); }
  input {
    min-height: 32px;
    padding: 0 var(--s3);
    border: 1px solid var(--line-strong);
    border-radius: var(--r-control);
    background: var(--field);
    color: var(--text);
  }
  input:hover { border-color: var(--accent-ring); }
  button {
    min-height: 30px;
    padding: 0 var(--s3);
    border: 1px solid var(--line-strong);
    border-radius: var(--r-control);
    background: var(--field);
    color: var(--text-dim);
    cursor: pointer;
  }
  button:hover { color: var(--text); background: var(--panel-raised); }
  .add { justify-self: start; }
  .hint { margin: 0; color: var(--text-dim); font: var(--t-help); }
  @media (max-width: 760px) {
    .field { grid-template-columns: minmax(0, 1fr); }
    .body { grid-column: 1; grid-row: auto; max-width: none; }
  }
  @container settings-form (max-width: 600px) {
    .field { grid-template-columns: minmax(0, 1fr); }
    .body { grid-column: 1; grid-row: auto; max-width: none; }
  }
</style>
