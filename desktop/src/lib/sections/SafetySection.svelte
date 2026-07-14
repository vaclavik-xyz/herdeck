<script lang="ts">
  import BooleanField from "../fields/BooleanField.svelte";
  import TriStateListField from "../fields/TriStateListField.svelte";
  import OverrideField from "../fields/OverrideField.svelte";
  import {
    getAt, setAt, listFieldState, setListField,
    inheritedFor, overrideState, overrideValue, setOverride, clearOverride,
    type ListFieldState, type ConfigPayload,
  } from "../configClient";
  import { defineMessages, fieldHelp, fmt, locale } from "../i18n.svelte";
  import defaults from "../configDefaults.json";

  let { payload = $bindable(), onChange, reloadRev = 0, editProfile = null }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void; reloadRev?: number; editProfile?: string | null } = $props();

  const SEC = "safety";
  const overlay = $derived(editProfile != null && editProfile !== "default");
  const prof = $derived(editProfile ?? "");

  const SAFETY_DEFAULTS: Record<string, unknown> = defaults.safety;

  // Tooltips for every field come from the central catalog (help.ts) in the
  // current language — required for each labelled field
  // (enforced by sections.help.test.ts).
  const HELP = $derived(fieldHelp("safety"));

  const LM = defineMessages({
    en: {
      title: "Safety",
      none: "(none)",
      inherit: "Inherit",
      inherited_hint: "inherited: {value}",
    },
    cs: {
      title: "Bezpečnost",
      none: "(nic)",
      inherit: "Zdědit",
      inherited_hint: "zděděno: {value}",
    },
  });
  const lm = $derived(LM[locale.lang]);

  const approveAlways = $derived((getAt(payload, "base", SEC, "approve_always") as boolean) ?? defaults.safety.approve_always);
  const requireConfirmFor = $derived((getAt(payload, "base", SEC, "require_confirm_for") as string[]) ?? SAFETY_DEFAULTS.require_confirm_for as string[]);
  function set(key: string, value: unknown): void { payload = setAt(payload, "base", SEC, key, value); onChange(); }
  function setBaseRcf(state: ListFieldState, list: string[]): void { payload = setListField(payload, "base", SEC, "require_confirm_for", state, list); onChange(); }

  function hint(key: string): string { const v = inheritedFor(payload, prof, SEC, key) ?? SAFETY_DEFAULTS[key]; return Array.isArray(v) ? v.join(" · ") : v == null ? lm.none : String(v); }
  function scState(key: string): "inherit" | "override" { return overrideState(payload, prof, SEC, key) === "default" ? "inherit" : "override"; }
  function scValue(key: string): unknown { const v = overrideValue(payload, prof, SEC, key); return v === undefined ? (inheritedFor(payload, prof, SEC, key) ?? SAFETY_DEFAULTS[key]) : v; }
  function setScState(key: string, s: "inherit" | "override"): void {
    payload = { ...payload, profiles: s === "inherit" ? clearOverride(payload.profiles, prof, SEC, key) : setOverride(payload.profiles, prof, SEC, key, inheritedFor(payload, prof, SEC, key) ?? SAFETY_DEFAULTS[key]) };
    onChange();
  }
  function setSc(key: string, v: unknown): void { payload = { ...payload, profiles: setOverride(payload.profiles, prof, SEC, key, v) }; onChange(); }
  function ovRcfList(): string[] { const v = overrideValue(payload, prof, SEC, "require_confirm_for"); return Array.isArray(v) ? v as string[] : []; }
  function inheritedRcf(): string[] { const v = inheritedFor(payload, prof, SEC, "require_confirm_for") ?? SAFETY_DEFAULTS.require_confirm_for; return Array.isArray(v) ? v as string[] : []; }
  function setOvRcf(state: ListFieldState, list: string[]): void {
    payload = { ...payload, profiles: state === "default" ? clearOverride(payload.profiles, prof, SEC, "require_confirm_for") : setOverride(payload.profiles, prof, SEC, "require_confirm_for", state === "empty" ? [] : list) };
    onChange();
  }
</script>

<h2>{lm.title}{#if overlay} · overlay: {editProfile}{/if}</h2>
{#if overlay}
  <OverrideField label="approve_always" help={HELP.approve_always} state={scState("approve_always")} inheritedDisplay={hint("approve_always")} onstate={(s) => setScState("approve_always", s)}>
    <BooleanField label="" value={Boolean(scValue("approve_always"))} onchange={(v) => setSc("approve_always", v)} />
  </OverrideField>
  <TriStateListField label="require_confirm_for" help={HELP.require_confirm_for} state={overrideState(payload, prof, SEC, "require_confirm_for")} list={ovRcfList()} customSeed={inheritedRcf()} inheritLabel={lm.inherit} inheritHint={fmt(lm.inherited_hint, { value: hint("require_confirm_for") })} resetKey={`${prof}:${reloadRev}:safety:require_confirm_for`} onchange={setOvRcf} />
{:else}
  <BooleanField label="approve_always" help={HELP.approve_always} value={approveAlways} onchange={(v) => set("approve_always", v)} />
  <TriStateListField label="require_confirm_for" help={HELP.require_confirm_for} state={listFieldState(payload, "base", SEC, "require_confirm_for")} list={requireConfirmFor} customSeed={SAFETY_DEFAULTS.require_confirm_for as string[]} defaultHint={(SAFETY_DEFAULTS.require_confirm_for as string[]).join(" · ")} resetKey={`base:${reloadRev}:safety:require_confirm_for`} onchange={setBaseRcf} />
{/if}

<style>
  h2 { margin: 0 0 8px; }
</style>
