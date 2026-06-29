<script lang="ts">
  import TextField from "../fields/TextField.svelte";
  import {
    DEFAULT_TOGGLE_DECK_HOTKEY,
    toggleDeckHotkey,
    setToggleDeckHotkey,
    type ConfigPayload,
  } from "../configClient";

  let { payload = $bindable(), onChange }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void } = $props();

  const value = $derived(toggleDeckHotkey(payload));
  function set(v: string): void {
    payload = setToggleDeckHotkey(payload, v);
    onChange();
  }
</script>

<h2>Desktop</h2>
<p class="hint">
  Globální hotkey pro zobrazení/schování plovoucího decku. Výchozí
  <code>{DEFAULT_TOGGLE_DECK_HOTKEY}</code>; prázdné pole = hotkey vypnutý.
  Změna se projeví po Apply.
</p>
<TextField label="toggle_deck" value={value} oninput={set} />

<style>
  h2 { margin: 0 0 8px; }
  .hint { color: #888; margin: 0 0 12px; }
  code { color: #aaa; }
</style>
