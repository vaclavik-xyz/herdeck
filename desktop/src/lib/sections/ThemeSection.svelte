<script lang="ts">
  import ColorSelectField from "../fields/ColorSelectField.svelte";
  import ListField from "../fields/ListField.svelte";
  import TriStateListField from "../fields/TriStateListField.svelte";
  import OverrideField from "../fields/OverrideField.svelte";
  import {
    getAt, setAt, putList,
    inheritedFor, inheritedForPath, overrideValue, overrideValuePath, overrideState,
    setOverride, clearOverride, setOverridePath, clearOverridePath,
    type ListFieldState, type ConfigPayload,
  } from "../configClient";
  import { DEFAULT_STATUS_COLORS } from "../statusColors";
  import { defineMessages, fieldHelp, fmt, locale } from "../i18n.svelte";

  let { payload = $bindable(), onChange, editProfile = null }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void; editProfile?: string | null } = $props();

  const SEC = "theme";
  const STATUS = ["working", "idle", "blocked", "done", "waiting", "unknown", "offline"];

  // Current-language tooltips for every field — required for each labelled
  // field (enforced by sections.help.test.ts); texts live in help.ts.
  const HELP = $derived(fieldHelp("theme"));

  const LM = defineMessages({
    en: {
      title: "Colors",
      none: "(none)",
      inherit: "Inherit",
      inherited_hint: "inherited: {value}",
    },
    cs: {
      title: "Barvy",
      none: "(nic)",
      inherit: "Zdědit",
      inherited_hint: "zděděno: {value}",
    },
  });
  const lm = $derived(LM[locale.lang]);
  const overlay = $derived(editProfile != null && editProfile !== "default");
  const prof = $derived(editProfile ?? "");

  // --- base mode: UNCHANGED from today (colors via setAt, server_accents via ListField + putList) ---
  function baseColorOf(key: string): string {
    return (getAt(payload, "base", SEC, "colors") as Record<string, unknown> | undefined)?.[key] as string ?? "";
  }
  const accents = $derived((getAt(payload, "base", SEC, "server_accents") as string[]) ?? []);
  function setBaseColor(key: string, v: string): void {
    const cur = getAt(payload, "base", SEC, "colors");
    const colors: Record<string, unknown> = cur != null && typeof cur === "object" && !Array.isArray(cur) ? { ...(cur as Record<string, unknown>) } : {};
    if (v.trim() === "") delete colors[key]; else colors[key] = v;
    payload = setAt(payload, "base", SEC, "colors", colors);
    onChange();
  }
  function setBaseAccents(list: string[]): void { payload = putList(payload, "base", SEC, "server_accents", list); onChange(); }

  // --- overlay: per-status colors via path helpers (profiles[X].theme.colors.<status>) ---
  function colorPath(status: string): string[] { return [SEC, "colors", status]; }
  function colorInheritedHint(status: string): string { const v = inheritedForPath(payload, prof, colorPath(status)) ?? DEFAULT_STATUS_COLORS[status]; return v == null ? lm.none : String(v); }
  function colorState(status: string): "inherit" | "override" { return overrideValuePath(payload, prof, colorPath(status)) === undefined ? "inherit" : "override"; }
  function colorValue(status: string): string { const v = overrideValuePath(payload, prof, colorPath(status)); return v === undefined ? String(inheritedForPath(payload, prof, colorPath(status)) ?? DEFAULT_STATUS_COLORS[status] ?? "") : String(v); }
  function setColorState(status: string, s: "inherit" | "override"): void {
    payload = { ...payload, profiles: s === "inherit" ? clearOverridePath(payload.profiles, prof, colorPath(status)) : setOverridePath(payload.profiles, prof, colorPath(status), inheritedForPath(payload, prof, colorPath(status)) ?? DEFAULT_STATUS_COLORS[status] ?? "") };
    onChange();
  }
  function setColor(status: string, v: string): void { payload = { ...payload, profiles: setOverridePath(payload.profiles, prof, colorPath(status), v) }; onChange(); }

  // --- overlay: server_accents (regular 2-level list) ---
  function accentHint(): string { const v = inheritedFor(payload, prof, SEC, "server_accents"); return Array.isArray(v) ? v.join(" · ") : v == null ? lm.none : String(v); }
  function ovAccents(): string[] { const v = overrideValue(payload, prof, SEC, "server_accents"); return Array.isArray(v) ? v as string[] : []; }
  function setOvAccents(state: ListFieldState, list: string[]): void {
    payload = { ...payload, profiles: state === "default" ? clearOverride(payload.profiles, prof, SEC, "server_accents") : setOverride(payload.profiles, prof, SEC, "server_accents", state === "empty" ? [] : list) };
    onChange();
  }
</script>

<h2>{lm.title}{#if overlay} · overlay: {editProfile}{/if}</h2>
<fieldset class="colors">
  <legend>colors</legend>
  {#if overlay}
    {#each STATUS as key (key)}
      <OverrideField label={key} help={HELP[key]} state={colorState(key)} inheritedDisplay={colorInheritedHint(key)} onstate={(s) => setColorState(key, s)}>
        <ColorSelectField label="" value={colorValue(key)} allowEmpty={false} onchange={(v) => setColor(key, v)} />
      </OverrideField>
    {/each}
  {:else}
    {#each STATUS as key (key)}
      <ColorSelectField label={key} help={HELP[key]} value={baseColorOf(key)} onchange={(v) => setBaseColor(key, v)} />
    {/each}
  {/if}
</fieldset>
{#if overlay}
  <TriStateListField label="server_accents" help={HELP.server_accents} state={overrideState(payload, prof, SEC, "server_accents")} list={ovAccents()} inheritLabel={lm.inherit} inheritHint={fmt(lm.inherited_hint, { value: accentHint() })} resetKey={`${prof}:${payload.revision ?? ""}:theme:server_accents`} onchange={setOvAccents} />
{:else}
  <ListField label="server_accents" help={HELP.server_accents} value={accents} onchange={setBaseAccents} />
{/if}

<style>
  h2 { margin: 0 0 8px; }
  .colors { border: 1px solid #2a2a30; border-radius: 6px; margin: 8px 0; padding: 8px 12px; }
  .colors legend { color: #ccc; }
</style>
