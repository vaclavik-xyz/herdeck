<script lang="ts">
  import TextField from "../fields/TextField.svelte";
  import SelectField from "../fields/SelectField.svelte";
  import { defineMessages, fieldHelp, locale } from "../i18n.svelte";
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

  // Field tooltips in the current language — required for each labelled field
  // (enforced by sections.help.test.ts); texts live in help.ts under "desktop".
  const HELP = $derived(fieldHelp("desktop"));

  const LM = defineMessages({
    en: {
      heading: "Window",
      mode_intro: "Floating deck window mode:",
      mode_normal: "a regular framed window",
      mode_floating: "frameless",
      mode_top: "always on top",
      mode_restart: "The Apply button only saves this choice — it takes effect after the app restarts. For an immediate switch use the “Window mode” tray menu.",
      hotkey_intro: "Global hotkey to show or hide the deck. Default",
      hotkey_rest: "; an empty field disables the hotkey. Takes effect after saving with Apply.",
    },
    cs: {
      heading: "Okno",
      mode_intro: "Režim plovoucího okna decku:",
      mode_normal: "běžné okno s rámečkem",
      mode_floating: "bez rámečku",
      mode_top: "vždy navrchu",
      mode_restart: "Tlačítko Použít tuto volbu jen uloží — projeví se po restartu aplikace. Pro okamžité přepnutí použij menu v liště „Režim okna\".",
      hotkey_intro: "Globální hotkey pro zobrazení/schování decku. Výchozí",
      hotkey_rest: "; prázdné pole = hotkey vypnutý. Změna se projeví po uložení tlačítkem Použít.",
    },
  });
  const lm = $derived(LM[locale.lang]);

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

<h2>{lm.heading}</h2>
<p class="hint">
  {lm.mode_intro}
  <code>normal</code> = {lm.mode_normal},
  <code>floating</code> = {lm.mode_floating},
  <code>always_on_top</code> = {lm.mode_top}.
  {lm.mode_restart}
</p>
<SelectField label="window_mode" help={HELP.window_mode} value={mode} options={[...WINDOW_MODES]} onchange={setMode} />
<p class="hint">
  {lm.hotkey_intro}
  <code>{DEFAULT_TOGGLE_DECK_HOTKEY}</code>{lm.hotkey_rest}
</p>
<TextField label="toggle_deck" help={HELP.toggle_deck} value={hotkey} oninput={setHotkey} />

<style>
  h2 { margin: 0 0 8px; }
  .hint { color: #888; margin: 0 0 12px; }
  code { color: #aaa; }
</style>
