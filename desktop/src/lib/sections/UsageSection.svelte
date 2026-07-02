<script lang="ts">
  import ListField from "../fields/ListField.svelte";
  import NumberField from "../fields/NumberField.svelte";
  import TextField from "../fields/TextField.svelte";
  import TriStateListField from "../fields/TriStateListField.svelte";
  import OverrideField from "../fields/OverrideField.svelte";
  import {
    getAt, setAt, removeAt, putList,
    inheritedFor, overrideState, overrideValue, setOverride, clearOverride,
    type ListFieldState, type ConfigPayload,
  } from "../configClient";
  import { defineMessages, fieldHelp, fmt, locale } from "../i18n.svelte";

  let { payload = $bindable(), onChange, editProfile = null }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void; editProfile?: string | null } = $props();

  const SEC = "usage";
  const overlay = $derived(editProfile != null && editProfile !== "default");
  const prof = $derived(editProfile ?? "");

  // Mirror of backend defaults (settings.py _usage_config) — keep in sync.
  const USAGE_DEFAULTS: Record<string, unknown> = { refresh_secs: 300, codexbar_path: "codexbar" };

  const HELP = $derived(fieldHelp("usage"));

  const LM = defineMessages({
    en: {
      title: "Usage limits",
      intro: "Provider usage limits (via the CodexBar CLI) shown on the deck's status panel. Empty providers = off.",
      none: "(none)",
      inherit: "Inherit",
      inherited_hint: "inherited: {value}",
    },
    cs: {
      title: "Limity využití",
      intro: "Limity poskytovatelů (přes CodexBar CLI) zobrazené na stavovém panelu decku. Prázdní provideři = vypnuto.",
      none: "(nic)",
      inherit: "Zdědit",
      inherited_hint: "zděděno: {value}",
    },
  });
  const lm = $derived(LM[locale.lang]);

  // --- base mode ---
  const providers = $derived((getAt(payload, "base", SEC, "providers") as string[]) ?? []);
  const refreshSecs = $derived((getAt(payload, "base", SEC, "refresh_secs") as number) ?? 300);
  const codexbarPath = $derived((getAt(payload, "base", SEC, "codexbar_path") as string) ?? "codexbar");
  function set(key: string, value: unknown): void { payload = setAt(payload, "base", SEC, key, value); onChange(); }
  // Empty text / cleared number returns the key to the backend default instead
  // of persisting "" (rejected by validation) or a hard-coded literal.
  function setOrRemove(key: string, value: unknown): void {
    payload = value == null || value === "" ? removeAt(payload, "base", SEC, key) : setAt(payload, "base", SEC, key, value);
    onChange();
  }
  function setBaseProviders(list: string[]): void { payload = putList(payload, "base", SEC, "providers", list); onChange(); }

  // --- overlay mode (same shape as SafetySection) ---
  function hint(key: string): string { const v = inheritedFor(payload, prof, SEC, key) ?? USAGE_DEFAULTS[key]; return Array.isArray(v) ? v.join(" · ") : v == null ? lm.none : String(v); }
  function scState(key: string): "inherit" | "override" { return overrideState(payload, prof, SEC, key) === "default" ? "inherit" : "override"; }
  function scValue(key: string): unknown { const v = overrideValue(payload, prof, SEC, key); return v === undefined ? (inheritedFor(payload, prof, SEC, key) ?? USAGE_DEFAULTS[key]) : v; }
  function setScState(key: string, s: "inherit" | "override"): void {
    payload = { ...payload, profiles: s === "inherit" ? clearOverride(payload.profiles, prof, SEC, key) : setOverride(payload.profiles, prof, SEC, key, inheritedFor(payload, prof, SEC, key) ?? USAGE_DEFAULTS[key]) };
    onChange();
  }
  function setSc(key: string, v: unknown): void { payload = { ...payload, profiles: setOverride(payload.profiles, prof, SEC, key, v) }; onChange(); }
  // A cleared/invalid overlay value returns the field to INHERIT (visible in
  // the toggle) instead of silently overriding with a hard-coded default.
  function setScOrInherit(key: string, v: unknown): void {
    if (v == null || v === "") {
      payload = { ...payload, profiles: clearOverride(payload.profiles, prof, SEC, key) };
      onChange();
      return;
    }
    setSc(key, v);
  }
  function ovProviders(): string[] { const v = overrideValue(payload, prof, SEC, "providers"); return Array.isArray(v) ? v as string[] : []; }
  function setOvProviders(state: ListFieldState, list: string[]): void {
    payload = { ...payload, profiles: state === "default" ? clearOverride(payload.profiles, prof, SEC, "providers") : setOverride(payload.profiles, prof, SEC, "providers", state === "empty" ? [] : list) };
    onChange();
  }
</script>

<h2>{lm.title}{#if overlay} · overlay: {editProfile}{/if}</h2>
<p class="hint">{lm.intro}</p>
{#if overlay}
  <TriStateListField label="providers" help={HELP.providers} state={overrideState(payload, prof, SEC, "providers")} list={ovProviders()} inheritLabel={lm.inherit} inheritHint={fmt(lm.inherited_hint, { value: hint("providers") })} onchange={setOvProviders} />
  <OverrideField label="refresh_secs" help={HELP.refresh_secs} state={scState("refresh_secs")} inheritedDisplay={hint("refresh_secs")} onstate={(s) => setScState("refresh_secs", s)}>
    <NumberField label="" int value={Number(scValue("refresh_secs"))} onchange={(v) => setScOrInherit("refresh_secs", v)} />
  </OverrideField>
  <OverrideField label="codexbar_path" help={HELP.codexbar_path} state={scState("codexbar_path")} inheritedDisplay={hint("codexbar_path")} onstate={(s) => setScState("codexbar_path", s)}>
    <TextField label="" value={String(scValue("codexbar_path") ?? "")} oninput={(v) => setScOrInherit("codexbar_path", v.trim() === "" ? "" : v)} />
  </OverrideField>
{:else}
  <ListField label="providers" help={HELP.providers} value={providers} onchange={setBaseProviders} />
  <NumberField label="refresh_secs" help={HELP.refresh_secs} int value={refreshSecs} onchange={(v) => setOrRemove("refresh_secs", v)} />
  <TextField label="codexbar_path" help={HELP.codexbar_path} value={codexbarPath} oninput={(v) => setOrRemove("codexbar_path", v.trim() === "" ? "" : v)} />
{/if}

<style>
  h2 { margin: 0 0 8px; }
  .hint { color: #8a8a92; font-size: 12px; margin: 0 0 10px; }
</style>
