<script lang="ts">
  import TextField from "../fields/TextField.svelte";
  import NumberField from "../fields/NumberField.svelte";
  import TriStateListField from "../fields/TriStateListField.svelte";
  import OverrideField from "../fields/OverrideField.svelte";
  import {
    getAt, setAt, removeAt, listFieldState, setListField, serversOf,
    inheritedFor, overrideState, overrideValue, setOverride, clearOverride,
    type ListFieldState, type ConfigPayload,
  } from "../configClient";

  let { payload = $bindable(), onChange, editProfile = null }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void; editProfile?: string | null } = $props();

  const SEC = "deck";
  const overlay = $derived(editProfile != null && editProfile !== "default");
  const prof = $derived(editProfile ?? "");
  const serverHint = $derived(serversOf(payload).map((s) => s.id).filter((id) => id !== "").join(" · "));

  // --- base mode (grid + overview_order) ---
  const grid = $derived((getAt(payload, "base", SEC, "grid") as string) ?? "");
  const overviewState = $derived(listFieldState(payload, "base", SEC, "overview_order"));
  const overviewOrder = $derived((getAt(payload, "base", SEC, "overview_order") as string[]) ?? []);
  function setBase(key: string, value: unknown): void { payload = setAt(payload, "base", SEC, key, value); onChange(); }
  function setBaseOverview(state: ListFieldState, list: string[]): void { payload = setListField(payload, "base", SEC, "overview_order", state, list); onChange(); }

  // --- overlay helpers (grid + overview_order) ---
  function hint(key: string): string { const v = inheritedFor(payload, prof, SEC, key); return Array.isArray(v) ? v.join(" · ") : v == null ? "(nic)" : String(v); }
  function scState(key: string): "inherit" | "override" { return overrideState(payload, prof, SEC, key) === "default" ? "inherit" : "override"; }
  function scValue(key: string): unknown { const v = overrideValue(payload, prof, SEC, key); return v === undefined ? inheritedFor(payload, prof, SEC, key) : v; }
  function setScState(key: string, s: "inherit" | "override"): void {
    payload = { ...payload, profiles: s === "inherit" ? clearOverride(payload.profiles, prof, SEC, key) : setOverride(payload.profiles, prof, SEC, key, inheritedFor(payload, prof, SEC, key)) };
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
  const brightness = $derived((getAt(payload, "local", "hardware", "brightness") as number | null) ?? null);
  const debounce = $derived((getAt(payload, "local", "hardware", "debounce") as number | null) ?? null);
  const keepAlive = $derived((getAt(payload, "local", "hardware", "keep_alive_interval") as number | null) ?? null);
  const tick = $derived((getAt(payload, "local", "hardware", "tick_interval") as number | null) ?? null);
  function setLocalStr(table: string, key: string, v: string): void {
    payload = v.trim() === "" ? removeAt(payload, "local", table, key) : setAt(payload, "local", table, key, v);
    onChange();
  }
  function setLocalNum(table: string, key: string, v: number | null): void {
    payload = v === null ? removeAt(payload, "local", table, key) : setAt(payload, "local", table, key, v);
    onChange();
  }
</script>

<h2>Deck{#if overlay} · overlay: {editProfile}{/if}</h2>
{#if overlay}
  <OverrideField label="grid" state={scState("grid")} inheritedDisplay={hint("grid")} onstate={(s) => setScState("grid", s)}>
    <TextField label="" value={String(scValue("grid") ?? "")} oninput={(v) => setSc("grid", v)} />
  </OverrideField>
  <TriStateListField label="overview_order" state={overrideState(payload, prof, SEC, "overview_order")} list={ovOverviewList()} inheritLabel="Zdědit" inheritHint={`zděděno: ${hint("overview_order")}`} onchange={setOvOverview} />
{:else}
  <TextField label="grid" value={grid} oninput={(v) => setBase("grid", v)} />
  <TriStateListField label="overview_order" state={overviewState} list={overviewOrder} defaultHint={serverHint} onchange={setBaseOverview} />
{/if}

<fieldset class="hw">
  <legend>Hardware (tento stroj — local.toml)</legend>
  <p class="hint">Platí jen pro tento počítač; nikdy se nepřenáší do profilů ani base configu (ani v overlay módu).</p>
  <TextField label="deck" value={hwDeck} oninput={(v) => setLocalStr("local", "deck", v)} />
  <TextField label="herdr_socket" value={hwSocket} oninput={(v) => setLocalStr("local", "herdr_socket", v)} />
  <TextField label="web_bind" value={hwBind} oninput={(v) => setLocalStr("local", "web_bind", v)} />
  <NumberField label="web_port" value={hwPort} int onchange={(v) => setLocalNum("local", "web_port", v)} />
  <TextField label="icons_dir" value={hwIcons} oninput={(v) => setLocalStr("local", "icons_dir", v)} />
  <NumberField label="brightness" value={brightness} int onchange={(v) => setLocalNum("hardware", "brightness", v)} />
  <NumberField label="debounce" value={debounce} step={0.05} onchange={(v) => setLocalNum("hardware", "debounce", v)} />
  <NumberField label="keep_alive_interval" value={keepAlive} step={0.5} onchange={(v) => setLocalNum("hardware", "keep_alive_interval", v)} />
  <NumberField label="tick_interval" value={tick} step={0.05} onchange={(v) => setLocalNum("hardware", "tick_interval", v)} />
</fieldset>

<style>
  h2 { margin: 0 0 8px; }
  .hw { border: 1px solid #2a2a30; border-radius: 6px; margin: 12px 0; padding: 8px 12px; }
  .hw legend { color: #ccc; }
  .hint { color: #888; margin: 0 0 8px; }
</style>
