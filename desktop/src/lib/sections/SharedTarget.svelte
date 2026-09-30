<script lang="ts">
  // The target picker of the shared config sections (bridge shared settings,
  // spec S5-S7): which bridge's shared fields the section edits, or "This Mac
  // (fallback)". Also offers adoption of an unset bridge, "Apply to all
  // bridges", and shows the last Apply's per-bridge results. Saving drafts is
  // ConfigApp's job (one Apply for everything); adoption is an explicit,
  // immediate action and happens here.
  import FieldCopy from "../fields/FieldCopy.svelte";
  import {
    FALLBACK_TARGET, adoptedIds, extractShared, putBridgeSettings, targetIds, targetMode,
    type BridgePutFn, type BridgeShared, type PutOutcome,
  } from "../bridgeSettings";
  import { defineMessages, fieldHelp, fmt, locale } from "../i18n.svelte";

  let {
    bridges, target, onTarget, applyAll, onApplyAll, editingProfile, overlayIgnored, results,
    baseConfig, put, onAdopted, sharedKeys = null,
  }: {
    bridges: Record<string, BridgeShared>;
    target: string;
    onTarget: (id: string) => void;
    applyAll: boolean;
    onApplyAll: (v: boolean) => void;
    /** A profile overlay is being edited (its shared-field controls are hidden on a bridge). */
    editingProfile: boolean;
    /** Adopted bridges that ignore the active profile's overlays of shared keys. */
    overlayIgnored: string[];
    results: PutOutcome[];
    /** The editor's current config.toml base — adoption moves its shared part. */
    baseConfig: Record<string, unknown>;
    /** null = no runtime to talk to (browser preview): adoption is not offered. */
    put: BridgePutFn | null;
    onAdopted: (outcome: PutOutcome) => void;
    /** Mixed sections: the fields that follow the target (the rest stay local). */
    sharedKeys?: readonly string[] | null;
  } = $props();

  const HELP = $derived(fieldHelp("shared"));
  const LM = defineMessages({
    en: {
      target: "Edit settings on",
      fallback: "This Mac (fallback)",
      offline_suffix: "{id} (offline)",
      unset_suffix: "{id} (not adopted)",
      fallback_note: "Edits this Mac's config.toml. Bridges that have not adopted shared settings (and older bridges) use these values.",
      unset_note: "Bridge {id} still uses this Mac's values, so these fields edit config.toml. Move them to the bridge to share them with every Mac connected to it.",
      adopt: "Move these settings to bridge {id}",
      adopt_title: "Copy this Mac's shared settings to bridge {id}; config.toml keeps its copy as the fallback",
      adopting: "Moving…",
      adopted_note: "Editing the settings stored on bridge {id} (revision {rev}). This Mac's config.toml keeps its own copy as the fallback.",
      offline: "Bridge offline — showing last known settings",
      unset_offline: "Bridge {id} is offline; settings can be moved to it once it reconnects.",
      shared_keys: "Only these fields follow the selected target: {keys}. Everything else is stored on this Mac.",
      overlay_hidden: "Profile overrides of these fields apply only to This Mac (fallback); an adopted bridge ignores them, so they are hidden here.",
      overlay_ignored: "The active profile overrides some shared settings, but bridge {id} ignores those overrides.",
      apply_all: "Apply to all bridges",
      results: "Last save",
      r_saved: "{id}: saved",
      r_stale_revision: "{id}: changed elsewhere — reloaded",
      r_invalid: "{id}: rejected: {detail}",
      r_too_large: "{id}: rejected: the settings are too large",
      r_offline: "{id}: bridge not connected",
      r_timeout: "{id}: no answer from the bridge",
      r_bridge_error: "{id}: the bridge refused the change: {detail}",
      r_unknown_server: "{id}: unknown server",
      r_unreachable: "{id}: runtime unreachable: {detail}",
      r_http: "{id}: failed (HTTP {status})",
      adopt_failed: "Moving the settings failed. {detail}",
    },
    cs: {
      target: "Upravit nastavení na",
      fallback: "Tento Mac (záloha)",
      offline_suffix: "{id} (offline)",
      unset_suffix: "{id} (nepřevzato)",
      fallback_note: "Upravuje config.toml tohoto Macu. Bridge, které sdílené nastavení nepřevzaly (a starší bridge), používají tyto hodnoty.",
      unset_note: "Bridge {id} zatím používá hodnoty tohoto Macu, takže tato pole upravují config.toml. Přesuň je na bridge a budou společná pro všechny Macy, které jsou k němu připojené.",
      adopt: "Přesunout tato nastavení na bridge {id}",
      adopt_title: "Zkopíruje sdílené nastavení tohoto Macu na bridge {id}; config.toml si ponechá kopii jako zálohu",
      adopting: "Přesouvám…",
      adopted_note: "Upravuješ nastavení uložené na bridgi {id} (revize {rev}). Config.toml tohoto Macu si ponechává vlastní kopii jako zálohu.",
      offline: "Bridge je offline — zobrazuji poslední známé nastavení",
      unset_offline: "Bridge {id} je offline; nastavení na něj půjde přesunout, až se znovu připojí.",
      shared_keys: "Vybraný cíl se týká jen těchto polí: {keys}. Vše ostatní se ukládá na tomto Macu.",
      overlay_hidden: "Úpravy těchto polí v profilech platí jen pro Tento Mac (záloha); převzatý bridge je ignoruje, proto jsou zde skryté.",
      overlay_ignored: "Aktivní profil přepisuje některá sdílená nastavení, ale bridge {id} tato přepsání ignoruje.",
      apply_all: "Použít na všechny bridge",
      results: "Poslední uložení",
      r_saved: "{id}: uloženo",
      r_stale_revision: "{id}: změněno jinde — načteno znovu",
      r_invalid: "{id}: odmítnuto: {detail}",
      r_too_large: "{id}: odmítnuto: nastavení je příliš velké",
      r_offline: "{id}: bridge není připojený",
      r_timeout: "{id}: bridge neodpověděl",
      r_bridge_error: "{id}: bridge změnu odmítl: {detail}",
      r_unknown_server: "{id}: neznámý server",
      r_unreachable: "{id}: runtime je nedostupný: {detail}",
      r_http: "{id}: selhalo (HTTP {status})",
      adopt_failed: "Přesun nastavení selhal. {detail}",
    },
  });
  const lm = $derived(LM[locale.lang]);

  const ids = $derived(targetIds(bridges));
  const mode = $derived(targetMode(bridges, target));
  const current = $derived(target === FALLBACK_TARGET ? undefined : bridges[target]);
  const adoptedCount = $derived(adoptedIds(bridges).length);

  function optionLabel(id: string): string {
    const b = bridges[id];
    if (!b.connected) return fmt(lm.offline_suffix, { id });
    if (!b.set) return fmt(lm.unset_suffix, { id });
    return id;
  }

  /** One result row in the current language. */
  function resultText(r: PutOutcome): string {
    const vars = { id: r.serverId, status: r.status, detail: r.messages.join("; ") };
    if (r.ok) return fmt(lm.r_saved, vars);
    const key = `r_${r.error ?? "http"}` as keyof typeof lm;
    return fmt(lm[key] ?? lm.r_http, vars).replace(/: $/, "");
  }

  let adopting = $state(false);
  let adoptError = $state<string | null>(null);
  $effect(() => {
    void target;
    adoptError = null;
  });

  async function adopt(): Promise<void> {
    if (put == null || current == null) return;
    adopting = true;
    adoptError = null;
    try {
      const outcome = await putBridgeSettings(put, target, 0, extractShared(baseConfig));
      if (!outcome.ok) adoptError = fmt(lm.adopt_failed, { detail: resultText(outcome) });
      onAdopted(outcome);
    } finally {
      adopting = false;
    }
  }
</script>

<div class="shared-target" data-shared-target>
  <label class="field">
    <FieldCopy label={lm.target} help={HELP.target} />
    <select data-shared-target-select value={target} onchange={(e) => onTarget((e.target as HTMLSelectElement).value)}>
      <option value={FALLBACK_TARGET}>{lm.fallback}</option>
      {#each ids as id (id)}<option value={id}>{optionLabel(id)}</option>{/each}
    </select>
  </label>

  {#if mode === "fallback"}
    <p class="note">{lm.fallback_note}</p>
  {:else if mode === "unset"}
    <p class="note">{fmt(lm.unset_note, { id: target })}</p>
    {#if current?.connected && current.offered && put != null}
      <button type="button" class="adopt" data-action="adopt" title={fmt(lm.adopt_title, { id: target })} disabled={adopting} onclick={() => void adopt()}>
        {adopting ? lm.adopting : fmt(lm.adopt, { id: target })}
      </button>
    {:else if !current?.connected}
      <p class="note warn" data-shared-offline>{fmt(lm.unset_offline, { id: target })}</p>
    {/if}
    {#if adoptError}<p class="note bad" role="alert" data-adopt-error>{adoptError}</p>{/if}
  {:else if mode === "offline"}
    <p class="note warn" role="status" data-shared-offline>{lm.offline}</p>
  {:else}
    <p class="note">{fmt(lm.adopted_note, { id: target, rev: current?.revision ?? 0 })}</p>
  {/if}

  {#if sharedKeys && sharedKeys.length > 0}
    <p class="note" data-shared-keys>{fmt(lm.shared_keys, { keys: sharedKeys.join(", ") })}</p>
  {/if}
  {#if (mode === "adopted" || mode === "offline") && editingProfile}
    <p class="note" data-overlay-hidden>{lm.overlay_hidden}</p>
  {/if}
  {#if mode !== "fallback" && overlayIgnored.includes(target)}
    <p class="note warn" data-overlay-ignored>{fmt(lm.overlay_ignored, { id: target })}</p>
  {/if}

  {#if mode === "adopted" && adoptedCount >= 2}
    <label class="apply-all" title={HELP.apply_all}>
      <input type="checkbox" data-action="apply-all" checked={applyAll} onchange={(e) => onApplyAll((e.target as HTMLInputElement).checked)} />
      <span>{lm.apply_all}</span>
    </label>
  {/if}

  {#if results.length > 0}
    <div class="results" data-shared-results>
      <span>{lm.results}</span>
      <ul>
        {#each results as r (r.serverId)}<li class:bad={!r.ok}>{resultText(r)}</li>{/each}
      </ul>
    </div>
  {/if}
</div>

<style>
  .shared-target {
    margin: 0 0 var(--s5);
    padding: var(--s2) var(--s4) var(--s3);
    border: 1px solid var(--line);
    border-radius: var(--r-panel);
    background: var(--panel);
  }
  .field {
    display: grid;
    grid-template-columns: var(--field-label-w) minmax(0, 1fr);
    gap: var(--s1) var(--s6);
    padding: var(--s2) 0;
  }
  select {
    grid-column: 2;
    grid-row: 1 / span 2;
    align-self: center;
    width: 100%;
    max-width: var(--control-md);
    min-height: 32px;
    padding: 0 var(--s6) 0 var(--s3);
    border: 1px solid var(--line-strong);
    border-radius: var(--r-control);
    background: var(--field);
    color: var(--text);
  }
  .note { margin: var(--s2) 0 0; max-width: 72ch; color: var(--text-dim); font: var(--t-help); }
  .note.warn, .note.bad {
    padding: var(--s2) var(--s3);
    border: 1px solid color-mix(in srgb, var(--st-blocked) 45%, var(--line));
    border-radius: var(--r-control);
    background: color-mix(in srgb, var(--st-blocked) 12%, var(--canvas));
    color: var(--text);
  }
  .adopt, .apply-all { margin-top: var(--s2); }
  .adopt {
    min-height: 30px;
    padding: 0 var(--s3);
    border: 1px solid var(--line-strong);
    border-radius: var(--r-control);
    background: var(--panel-raised);
    color: var(--text);
    cursor: pointer;
  }
  .adopt:disabled { cursor: progress; opacity: .7; }
  .apply-all { display: flex; align-items: center; gap: var(--s2); color: var(--text); font: var(--t-label); cursor: pointer; }
  .results { margin-top: var(--s3); color: var(--text-dim); font: var(--t-help); }
  .results ul { margin: var(--s1) 0 0; padding-left: var(--s5); }
  .results li.bad { color: var(--st-offline-text); }
  @media (max-width: 760px) {
    .field { grid-template-columns: minmax(0, 1fr); }
    select { grid-column: 1; grid-row: auto; max-width: none; }
  }
</style>
