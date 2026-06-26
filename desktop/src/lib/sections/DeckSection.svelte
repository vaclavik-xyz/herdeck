<script lang="ts">
  import TextField from "../fields/TextField.svelte";
  import NumberField from "../fields/NumberField.svelte";
  import TriStateListField from "../fields/TriStateListField.svelte";
  import { getAt, setAt, removeAt, listFieldState, setListField, serversOf, type ListFieldState, type ConfigPayload } from "../configClient";

  let { payload = $bindable(), onChange }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void } = $props();

  const grid = $derived((getAt(payload, "base", "deck", "grid") as string) ?? "");
  const overviewOrder = $derived((getAt(payload, "base", "deck", "overview_order") as string[]) ?? []);
  const overviewState = $derived(listFieldState(payload, "base", "deck", "overview_order"));
  const overviewHint = $derived(serversOf(payload).map((s) => s.id).filter((id) => id !== "").join(" · "));

  // Hardware (local.toml). [local] = deck kind / sockets / web bind; [hardware] = numeric tuning.
  const hwDeck = $derived((getAt(payload, "local", "local", "deck") as string) ?? "");
  const hwSocket = $derived((getAt(payload, "local", "local", "herdr_socket") as string) ?? "");
  const hwBind = $derived((getAt(payload, "local", "local", "web_bind") as string) ?? "");
  const hwIcons = $derived((getAt(payload, "local", "local", "icons_dir") as string) ?? "");
  const hwPort = $derived((getAt(payload, "local", "local", "web_port") as number | null) ?? null);
  const brightness = $derived((getAt(payload, "local", "hardware", "brightness") as number | null) ?? null);
  const debounce = $derived((getAt(payload, "local", "hardware", "debounce") as number | null) ?? null);
  const keepAlive = $derived((getAt(payload, "local", "hardware", "keep_alive_interval") as number | null) ?? null);
  const tick = $derived((getAt(payload, "local", "hardware", "tick_interval") as number | null) ?? null);

  function setBase(key: string, value: unknown): void {
    payload = setAt(payload, "base", "deck", key, value);
    onChange();
  }
  // overview_order tri-state: absent → all servers (default), [] → empty overview, custom → list.
  function setOverviewOrder(state: ListFieldState, list: string[]): void {
    payload = setListField(payload, "base", "deck", "overview_order", state, list);
    onChange();
  }
  // For optional local strings: blank clears the key (so we never write empty hardware paths).
  function setLocalStr(table: string, key: string, v: string): void {
    payload = v.trim() === "" ? removeAt(payload, "local", table, key) : setAt(payload, "local", table, key, v);
    onChange();
  }
  function setLocalNum(table: string, key: string, v: number | null): void {
    payload = v === null ? removeAt(payload, "local", table, key) : setAt(payload, "local", table, key, v);
    onChange();
  }
</script>

<h2>Deck</h2>
<TextField label="grid" value={grid} oninput={(v) => setBase("grid", v)} />
<TriStateListField label="overview_order" state={overviewState} list={overviewOrder} defaultHint={overviewHint} onchange={setOverviewOrder} />

<fieldset class="hw">
  <legend>Hardware (tento stroj — local.toml)</legend>
  <p class="hint">Platí jen pro tento počítač; nikdy se nepřenáší do profilů ani base configu.</p>
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
