<script lang="ts">
  import TextField from "../fields/TextField.svelte";
  import NumberField from "../fields/NumberField.svelte";
  import TriStateListField from "../fields/TriStateListField.svelte";
  import OverrideField from "../fields/OverrideField.svelte";
  import { defineMessages, fieldHelp, fmt, locale } from "../i18n.svelte";
  import {
    getAt, setAt, removeAt, listFieldState, setListField, serversOf,
    inheritedFor, overrideState, overrideValue, setOverride, clearOverride,
    type ListFieldState, type ConfigPayload,
  } from "../configClient";

  let { payload = $bindable(), onChange, reloadRev = 0, editProfile = null }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void; reloadRev?: number; editProfile?: string | null } = $props();

  const SEC = "deck";
  const DEFAULT_GRID = "5x3";
  const HARDWARE_DEFAULTS = {
    brightness: 80,
    debounce: 0.25,
    keep_alive_interval: 5.0,
    tick_interval: 0.4,
  } as const;

  // Field tooltips in the current language — required for each labelled field
  // (enforced by sections.help.test.ts); texts live in help.ts under "deck".
  const HELP = $derived(fieldHelp("deck"));

  const LM = defineMessages({
    en: {
      heading: "Deck",
      none: "(none)",
      inherit: "Inherit",
      inherited_hint: "inherited: {value}",
      hw_legend: "Hardware (this machine — local.toml)",
      hw_hint: "Applies only to this computer; never carried into profiles or the base config (not even in overlay mode).",
    },
    cs: {
      heading: "Deck",
      none: "(nic)",
      inherit: "Zdědit",
      inherited_hint: "zděděno: {value}",
      hw_legend: "Hardware (tento stroj — local.toml)",
      hw_hint: "Platí jen pro tento počítač; nikdy se nepřenáší do profilů ani base configu (ani v overlay módu).",
    },
  });
  const lm = $derived(LM[locale.lang]);

  const overlay = $derived(editProfile != null && editProfile !== "default");
  const prof = $derived(editProfile ?? "");
  const serverHint = $derived(serversOf(payload).map((s) => s.id).filter((id) => id !== "").join(" · "));

  // --- base mode (grid + overview_order) ---
  const grid = $derived((getAt(payload, "base", SEC, "grid") as string) ?? DEFAULT_GRID);
  const overviewState = $derived(listFieldState(payload, "base", SEC, "overview_order"));
  const overviewOrder = $derived((getAt(payload, "base", SEC, "overview_order") as string[]) ?? []);
  function setBase(key: string, value: unknown): void { payload = setAt(payload, "base", SEC, key, value); onChange(); }
  function setBaseOverview(state: ListFieldState, list: string[]): void { payload = setListField(payload, "base", SEC, "overview_order", state, list); onChange(); }

  // --- overlay helpers (grid + overview_order) ---
  function inheritedScalar(key: string): unknown {
    const v = inheritedFor(payload, prof, SEC, key);
    return key === "grid" && v === undefined ? DEFAULT_GRID : v;
  }
  function hint(key: string): string { const v = inheritedScalar(key); return Array.isArray(v) ? v.join(" · ") : v == null ? lm.none : String(v); }
  function scState(key: string): "inherit" | "override" { return overrideState(payload, prof, SEC, key) === "default" ? "inherit" : "override"; }
  function scValue(key: string): unknown { const v = overrideValue(payload, prof, SEC, key); return v === undefined ? inheritedScalar(key) : v; }
  function setScState(key: string, s: "inherit" | "override"): void {
    payload = { ...payload, profiles: s === "inherit" ? clearOverride(payload.profiles, prof, SEC, key) : setOverride(payload.profiles, prof, SEC, key, inheritedScalar(key)) };
    onChange();
  }
  function setSc(key: string, v: unknown): void { payload = { ...payload, profiles: setOverride(payload.profiles, prof, SEC, key, v) }; onChange(); }
  function ovOverviewList(): string[] { const v = overrideValue(payload, prof, SEC, "overview_order"); return Array.isArray(v) ? v as string[] : []; }
  function setOvOverview(state: ListFieldState, list: string[]): void {
    payload = { ...payload, profiles: state === "default" ? clearOverride(payload.profiles, prof, SEC, "overview_order") : setOverride(payload.profiles, prof, SEC, "overview_order", state === "empty" ? [] : list) };
    onChange();
  }

  // Hardware (local.toml) — never overlaid, always local.
  const hwDeck = $derived((getAt(payload, "local", "local", "deck") as string) ?? "");
  const hwSocket = $derived((getAt(payload, "local", "local", "herdr_socket") as string) ?? "");
  const hwBind = $derived((getAt(payload, "local", "local", "web_bind") as string) ?? "");
  const hwIcons = $derived((getAt(payload, "local", "local", "icons_dir") as string) ?? "");
  const hwPort = $derived((getAt(payload, "local", "local", "web_port") as number | null) ?? null);
  const brightness = $derived((getAt(payload, "local", "hardware", "brightness") as number | null) ?? HARDWARE_DEFAULTS.brightness);
  const debounce = $derived((getAt(payload, "local", "hardware", "debounce") as number | null) ?? HARDWARE_DEFAULTS.debounce);
  const keepAlive = $derived((getAt(payload, "local", "hardware", "keep_alive_interval") as number | null) ?? HARDWARE_DEFAULTS.keep_alive_interval);
  const tick = $derived((getAt(payload, "local", "hardware", "tick_interval") as number | null) ?? HARDWARE_DEFAULTS.tick_interval);
  function setLocalStr(table: string, key: string, v: string): void {
    payload = v.trim() === "" ? removeAt(payload, "local", table, key) : setAt(payload, "local", table, key, v);
    onChange();
  }
  function setLocalNum(table: string, key: string, v: number | null): void {
    payload = v === null ? removeAt(payload, "local", table, key) : setAt(payload, "local", table, key, v);
    onChange();
  }
</script>

<h2>{lm.heading}{#if overlay} · overlay: {editProfile}{/if}</h2>
{#if overlay}
  <OverrideField label="grid" help={HELP.grid} state={scState("grid")} inheritedDisplay={hint("grid")} onstate={(s) => setScState("grid", s)}>
    <TextField label="" value={String(scValue("grid") ?? "")} oninput={(v) => setSc("grid", v)} />
  </OverrideField>
  <TriStateListField label="overview_order" help={HELP.overview_order} state={overrideState(payload, prof, SEC, "overview_order")} list={ovOverviewList()} inheritLabel={lm.inherit} inheritHint={fmt(lm.inherited_hint, { value: hint("overview_order") })} resetKey={`${prof}:${reloadRev}:deck:overview_order`} onchange={setOvOverview} />
{:else}
  <TextField label="grid" help={HELP.grid} value={grid} oninput={(v) => setBase("grid", v)} />
  <TriStateListField label="overview_order" help={HELP.overview_order} state={overviewState} list={overviewOrder} defaultHint={serverHint} resetKey={`base:${reloadRev}:deck:overview_order`} onchange={setBaseOverview} />
{/if}

<fieldset class="hw">
  <legend>{lm.hw_legend}</legend>
  <p class="hint">{lm.hw_hint}</p>
  <TextField label="deck" help={HELP.deck} value={hwDeck} oninput={(v) => setLocalStr("local", "deck", v)} />
  <TextField label="herdr_socket" help={HELP.herdr_socket} value={hwSocket} oninput={(v) => setLocalStr("local", "herdr_socket", v)} />
  <TextField label="web_bind" help={HELP.web_bind} value={hwBind} oninput={(v) => setLocalStr("local", "web_bind", v)} />
  <NumberField label="web_port" help={HELP.web_port} value={hwPort} int min={0} max={65535} onchange={(v) => setLocalNum("local", "web_port", v)} />
  <TextField label="icons_dir" help={HELP.icons_dir} value={hwIcons} oninput={(v) => setLocalStr("local", "icons_dir", v)} />
  <NumberField label="brightness" help={HELP.brightness} value={brightness} int min={0} max={100} onchange={(v) => setLocalNum("hardware", "brightness", v)} />
  <NumberField label="debounce" help={HELP.debounce} value={debounce} step="any" min={Number.MIN_VALUE} max={60} onchange={(v) => setLocalNum("hardware", "debounce", v)} />
  <NumberField label="keep_alive_interval" help={HELP.keep_alive_interval} value={keepAlive} step="any" min={Number.MIN_VALUE} max={86400} onchange={(v) => setLocalNum("hardware", "keep_alive_interval", v)} />
  <NumberField label="tick_interval" help={HELP.tick_interval} value={tick} step="any" min={Number.MIN_VALUE} max={60} onchange={(v) => setLocalNum("hardware", "tick_interval", v)} />
</fieldset>

<style>
  h2 { margin: 0 0 8px; }
  .hw { border: 1px solid #2a2a30; border-radius: 6px; margin: 12px 0; padding: 8px 12px; }
  .hw legend { color: #ccc; }
  .hint { color: #888; margin: 0 0 8px; }
</style>
