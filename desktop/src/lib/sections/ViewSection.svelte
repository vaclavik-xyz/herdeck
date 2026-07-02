<script lang="ts">
  import TextField from "../fields/TextField.svelte";
  import SelectField from "../fields/SelectField.svelte";
  import BooleanField from "../fields/BooleanField.svelte";
  import TriStateListField from "../fields/TriStateListField.svelte";
  import OverrideField from "../fields/OverrideField.svelte";
  import { defineMessages, fieldHelp, fmt, locale } from "../i18n.svelte";
  import {
    getAt, setAt, listFieldState, setListField,
    inheritedFor, overrideState, overrideValue, setOverride, clearOverride,
    type ListFieldState, type ConfigPayload,
  } from "../configClient";

  let { payload = $bindable(), onChange, editProfile = null }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void; editProfile?: string | null } = $props();

  const SEC = "view";
  const MANAGEMENT = ["launcher_menu", "bottom_row"];
  const WORKING_ANIMATIONS = ["spin", "comet", "pulse", "sweep", "none"];
  const UI_LANGUAGES = ["en", "cs"];
  const TILE_FILLS = ["none", "tint", "solid"];
  const LIST_KEYS = ["bottom_row", "tile_fields", "tile_primary", "tile_secondary"] as const;
  const overlay = $derived(editProfile != null && editProfile !== "default");
  const prof = $derived(editProfile ?? "");

  // Mirror of backend defaults (config.py ViewConfig) — keep in sync.
  const VIEW_DEFAULTS: Record<string, unknown> = { management: "launcher_menu", agent_slots: "max", show_profile_on_panel: false, working_animation: "spin", tile_fill: "none" };

  // Field tooltips in the current language — required for each labelled field
  // (enforced by sections.help.test.ts); texts live in help.ts under "view".
  const HELP = $derived(fieldHelp("view"));

  const LM = defineMessages({
    en: {
      heading: "View",
      none: "(none)",
      inherit: "Inherit",
      inherited_hint: "inherited: {value}",
    },
    cs: {
      heading: "Zobrazení",
      none: "(nic)",
      inherit: "Zdědit",
      inherited_hint: "zděděno: {value}",
    },
  });
  const lm = $derived(LM[locale.lang]);

  // --- base mode (unchanged from α) ---
  const management = $derived((getAt(payload, "base", SEC, "management") as string) ?? "launcher_menu");
  const agentSlots = $derived((getAt(payload, "base", SEC, "agent_slots") as string) ?? "");
  const showProfile = $derived((getAt(payload, "base", SEC, "show_profile_on_panel") as boolean) ?? false);
  const workingAnimation = $derived((getAt(payload, "base", SEC, "working_animation") as string) ?? "spin");
  const tileFill = $derived((getAt(payload, "base", SEC, "tile_fill") as string) ?? "none");
  const uiLanguage = $derived((getAt(payload, "base", SEC, "language") as string) ?? "en");
  function set(key: string, value: unknown): void { payload = setAt(payload, "base", SEC, key, value); onChange(); }
  function setBaseTri(key: string, state: ListFieldState, list: string[]): void { payload = setListField(payload, "base", SEC, key, state, list); onChange(); }

  // --- overlay mode helpers ---
  function hint(key: string): string { const v = inheritedFor(payload, prof, SEC, key) ?? VIEW_DEFAULTS[key]; return Array.isArray(v) ? v.join(" · ") : v == null ? lm.none : String(v); }
  function scState(key: string): "inherit" | "override" { return overrideState(payload, prof, SEC, key) === "default" ? "inherit" : "override"; }
  function scValue(key: string): unknown { const v = overrideValue(payload, prof, SEC, key); return v === undefined ? (inheritedFor(payload, prof, SEC, key) ?? VIEW_DEFAULTS[key]) : v; }
  function setScState(key: string, s: "inherit" | "override"): void {
    payload = { ...payload, profiles: s === "inherit" ? clearOverride(payload.profiles, prof, SEC, key) : setOverride(payload.profiles, prof, SEC, key, inheritedFor(payload, prof, SEC, key) ?? VIEW_DEFAULTS[key]) };
    onChange();
  }
  function setSc(key: string, v: unknown): void { payload = { ...payload, profiles: setOverride(payload.profiles, prof, SEC, key, v) }; onChange(); }
  function ovListValue(key: string): string[] { const v = overrideValue(payload, prof, SEC, key); return Array.isArray(v) ? v as string[] : []; }
  function setOvList(key: string, state: ListFieldState, list: string[]): void {
    payload = { ...payload, profiles: state === "default" ? clearOverride(payload.profiles, prof, SEC, key) : setOverride(payload.profiles, prof, SEC, key, state === "empty" ? [] : list) };
    onChange();
  }
</script>

<h2>{lm.heading}{#if overlay} · overlay: {editProfile}{/if}</h2>
{#if overlay}
  <OverrideField label="management" help={HELP.management} state={scState("management")} inheritedDisplay={hint("management")} onstate={(s) => setScState("management", s)}>
    <SelectField label="" value={String(scValue("management") ?? "")} options={MANAGEMENT} onchange={(v) => setSc("management", v)} />
  </OverrideField>
  <OverrideField label="agent_slots" help={HELP.agent_slots} state={scState("agent_slots")} inheritedDisplay={hint("agent_slots")} onstate={(s) => setScState("agent_slots", s)}>
    <TextField label="" value={String(scValue("agent_slots") ?? "")} oninput={(v) => setSc("agent_slots", v)} />
  </OverrideField>
  <OverrideField label="show_profile_on_panel" help={HELP.show_profile_on_panel} state={scState("show_profile_on_panel")} inheritedDisplay={hint("show_profile_on_panel")} onstate={(s) => setScState("show_profile_on_panel", s)}>
    <BooleanField label="" value={Boolean(scValue("show_profile_on_panel"))} onchange={(v) => setSc("show_profile_on_panel", v)} />
  </OverrideField>
  <OverrideField label="working_animation" help={HELP.working_animation} state={scState("working_animation")} inheritedDisplay={hint("working_animation")} onstate={(s) => setScState("working_animation", s)}>
    <SelectField label="" value={String(scValue("working_animation") ?? "spin")} options={WORKING_ANIMATIONS} onchange={(v) => setSc("working_animation", v)} />
  </OverrideField>
  <OverrideField label="tile_fill" help={HELP.tile_fill} state={scState("tile_fill")} inheritedDisplay={hint("tile_fill")} onstate={(s) => setScState("tile_fill", s)}>
    <SelectField label="" value={String(scValue("tile_fill") ?? "none")} options={TILE_FILLS} onchange={(v) => setSc("tile_fill", v)} />
  </OverrideField>
  {#each LIST_KEYS as key}
    <TriStateListField label={key} help={HELP[key]} state={overrideState(payload, prof, SEC, key)} list={ovListValue(key)} inheritLabel={lm.inherit} inheritHint={fmt(lm.inherited_hint, { value: hint(key) })} onchange={(s, l) => setOvList(key, s, l)} />
  {/each}
{:else}
  <SelectField label="management" help={HELP.management} value={management} options={MANAGEMENT} onchange={(v) => set("management", v)} />
  <TextField label="agent_slots" help={HELP.agent_slots} value={agentSlots} oninput={(v) => set("agent_slots", v)} />
  <BooleanField label="show_profile_on_panel" help={HELP.show_profile_on_panel} value={showProfile} onchange={(v) => set("show_profile_on_panel", v)} />
  <SelectField label="working_animation" help={HELP.working_animation} value={workingAnimation} options={WORKING_ANIMATIONS} onchange={(v) => set("working_animation", v)} />
  <SelectField label="tile_fill" help={HELP.tile_fill} value={tileFill} options={TILE_FILLS} onchange={(v) => set("tile_fill", v)} />
  <SelectField label="language" help={HELP.language} value={uiLanguage} options={UI_LANGUAGES} onchange={(v) => set("language", v)} />
  {#each LIST_KEYS as key}
    <TriStateListField label={key} help={HELP[key]} state={listFieldState(payload, "base", SEC, key)} list={(getAt(payload, "base", SEC, key) as string[]) ?? []} onchange={(s, l) => setBaseTri(key, s, l)} />
  {/each}
{/if}

<style>
  h2 { margin: 0 0 8px; }
</style>
