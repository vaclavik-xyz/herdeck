<script lang="ts">
  import BooleanField from "../fields/BooleanField.svelte";
  import NumberField from "../fields/NumberField.svelte";
  import TextField from "../fields/TextField.svelte";
  import ProviderPicker from "../fields/ProviderPicker.svelte";
  import OverrideField from "../fields/OverrideField.svelte";
  import {
    getAt, setAt, removeAt,
    inheritedFor, overrideState, overrideValue, setOverride, clearOverride,
    type ConfigPayload,
  } from "../configClient";
  import { defineMessages, fieldHelp, locale } from "../i18n.svelte";

  let { payload = $bindable(), onChange, editProfile = null }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void; editProfile?: string | null } = $props();

  const SEC = "usage";
  const overlay = $derived(editProfile != null && editProfile !== "default");
  const prof = $derived(editProfile ?? "");

  // Mirror of backend defaults (settings.py _usage_config) — keep in sync.
  const USAGE_DEFAULTS: Record<string, unknown> = {
    providers: [], paid_only: false, refresh_secs: 300, codexbar_path: "codexbar",
  };

  const HELP = $derived(fieldHelp("usage"));

  const LM = defineMessages({
    en: {
      title: "Usage limits",
      intro: "Choose which subscriptions belong on the deck. Native account data confirms paid plans; CodexBar remains a compatibility fallback.",
      active_only: "Active subscriptions only",
      enabled: "Enabled",
      none: "(none)",
    },
    cs: {
      title: "Limity využití",
      intro: "Vyberte předplatná pro deck. Placený tarif potvrzují nativní data účtu; CodexBar zůstává jen jako záloha.",
      active_only: "Jen aktivní předplatná",
      enabled: "Zapnuto",
      none: "(nic)",
    },
  });
  const lm = $derived(LM[locale.lang]);

  // --- base mode ---
  const providers = $derived((getAt(payload, "base", SEC, "providers") as string[]) ?? []);
  const paidOnly = $derived((getAt(payload, "base", SEC, "paid_only") as boolean) ?? false);
  const refreshSecs = $derived((getAt(payload, "base", SEC, "refresh_secs") as number) ?? 300);
  const codexbarPath = $derived((getAt(payload, "base", SEC, "codexbar_path") as string) ?? "codexbar");
  function set(key: string, value: unknown): void { payload = setAt(payload, "base", SEC, key, value); onChange(); }
  // Empty text / cleared number returns the key to the backend default instead
  // of persisting "" (rejected by validation) or a hard-coded literal.
  function setOrRemove(key: string, value: unknown): void {
    payload = value == null || value === "" ? removeAt(payload, "base", SEC, key) : setAt(payload, "base", SEC, key, value);
    onChange();
  }
  function setBaseProviders(list: string[]): void { set("providers", list); }

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
  function ovProviders(): string[] { const v = scValue("providers"); return Array.isArray(v) ? v as string[] : []; }
  function setOvProviders(list: string[]): void { setSc("providers", list); }
</script>

<h2>{lm.title}{#if overlay} · overlay: {editProfile}{/if}</h2>
<p class="hint">{lm.intro}</p>
{#if overlay}
  <OverrideField label="providers" help={HELP.providers} state={scState("providers")} inheritedDisplay={hint("providers")} onstate={(s) => setScState("providers", s)}>
    <ProviderPicker providers={ovProviders()} help={HELP.providers} onchange={setOvProviders} />
  </OverrideField>
  <OverrideField label="paid_only" help={HELP.paid_only} state={scState("paid_only")} inheritedDisplay={hint("paid_only")} onstate={(s) => setScState("paid_only", s)}>
    <BooleanField label={lm.enabled} help={HELP.paid_only} value={Boolean(scValue("paid_only"))} onchange={(v) => setSc("paid_only", v)} />
  </OverrideField>
  <OverrideField label="refresh_secs" help={HELP.refresh_secs} state={scState("refresh_secs")} inheritedDisplay={hint("refresh_secs")} onstate={(s) => setScState("refresh_secs", s)}>
    <NumberField label="" int value={Number(scValue("refresh_secs"))} onchange={(v) => setScOrInherit("refresh_secs", v)} />
  </OverrideField>
  <OverrideField label="codexbar_path" help={HELP.codexbar_path} state={scState("codexbar_path")} inheritedDisplay={hint("codexbar_path")} onstate={(s) => setScState("codexbar_path", s)}>
    <TextField label="" value={String(scValue("codexbar_path") ?? "")} oninput={(v) => setScOrInherit("codexbar_path", v.trim() === "" ? "" : v)} />
  </OverrideField>
{:else}
  <ProviderPicker providers={providers} help={HELP.providers} onchange={setBaseProviders} />
  <BooleanField label={lm.active_only} help={HELP.paid_only} value={paidOnly} onchange={(v) => set("paid_only", v)} />
  <NumberField label="refresh_secs" help={HELP.refresh_secs} int value={refreshSecs} onchange={(v) => setOrRemove("refresh_secs", v)} />
  <TextField label="codexbar_path" help={HELP.codexbar_path} value={codexbarPath} oninput={(v) => setOrRemove("codexbar_path", v.trim() === "" ? "" : v)} />
{/if}

<style>
  h2 { margin: 0 0 8px; }
  .hint { color: #8a8a92; font-size: 12px; margin: 0 0 10px; }
</style>
