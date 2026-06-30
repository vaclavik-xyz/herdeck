<script lang="ts">
  import TextField from "../fields/TextField.svelte";
  import SelectField from "../fields/SelectField.svelte";
  import {
    DEFAULT_TOGGLE_DECK_HOTKEY,
    toggleDeckHotkey,
    setToggleDeckHotkey,
    WINDOW_MODES,
    windowMode,
    setWindowMode,
    type ConfigPayload,
    type WindowMode,
  } from "../configClient";

  let { payload = $bindable(), onChange }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void } = $props();

  const hotkey = $derived(toggleDeckHotkey(payload));
  const mode = $derived(windowMode(payload));
  function setHotkey(v: string): void {
    payload = setToggleDeckHotkey(payload, v);
    onChange();
  }
  function setMode(v: string): void {
    payload = setWindowMode(payload, v as WindowMode);
    onChange();
  }
</script>

<h2>Desktop</h2>
<p class="hint">
  Režim plovoucího okna decku: <code>normal</code> = běžné okno s rámečkem,
  <code>floating</code> = bez rámečku, <code>always_on_top</code> = vždy navrchu.
  Apply tuto volbu jen uloží — projeví se po restartu aplikace. Pro okamžité
  přepnutí použij tray menu „Window mode".
</p>
<SelectField label="window_mode" value={mode} options={[...WINDOW_MODES]} onchange={setMode} />
<p class="hint">
  Globální hotkey pro zobrazení/schování decku. Výchozí
  <code>{DEFAULT_TOGGLE_DECK_HOTKEY}</code>; prázdné pole = hotkey vypnutý.
  Změna se projeví po Apply.
</p>
<TextField label="toggle_deck" value={hotkey} oninput={setHotkey} />

<style>
  h2 { margin: 0 0 8px; }
  .hint { color: #888; margin: 0 0 12px; }
  code { color: #aaa; }
</style>
