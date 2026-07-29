<script lang="ts">
  import TextField from "../fields/TextField.svelte";
  import BooleanField from "../fields/BooleanField.svelte";
  import { defineMessages, fieldHelp, locale } from "../i18n.svelte";
  import {
    DEFAULT_TOGGLE_DECK_HOTKEY,
    toggleDeckHotkey,
    setToggleDeckHotkey,
    deckAlwaysOnTop,
    setDeckAlwaysOnTop,
    type ConfigPayload,
  } from "../configClient";

  let { payload = $bindable(), onChange }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void } = $props();

  // Field tooltips in the current language — required for each labelled field
  // (enforced by sections.help.test.ts); texts live in help.ts under "desktop".
  const HELP = $derived(fieldHelp("desktop"));

  const LM = defineMessages({
    en: {
      hotkey_intro: "Global hotkey to show or hide the deck. Default",
      hotkey_rest: "; an empty field disables the hotkey. Takes effect after saving with Apply.",
    },
    cs: {
      hotkey_intro: "Globální hotkey pro zobrazení/schování decku. Výchozí",
      hotkey_rest: "; prázdné pole = hotkey vypnutý. Změna se projeví po uložení tlačítkem Použít.",
    },
  });
  const lm = $derived(LM[locale.lang]);

  const hotkey = $derived(toggleDeckHotkey(payload));
  const alwaysOnTop = $derived(deckAlwaysOnTop(payload));
  function setHotkey(v: string): void {
    payload = setToggleDeckHotkey(payload, v);
    onChange();
  }
  function setAlwaysOnTop(v: boolean): void {
    payload = setDeckAlwaysOnTop(payload, v);
    onChange();
  }
</script>

<BooleanField label="deck_always_on_top" help={HELP.deck_always_on_top} value={alwaysOnTop} onchange={setAlwaysOnTop} />
<p class="hint">
  {lm.hotkey_intro}
  <code>{DEFAULT_TOGGLE_DECK_HOTKEY}</code>{lm.hotkey_rest}
</p>
<TextField label="toggle_deck" help={HELP.toggle_deck} value={hotkey} oninput={setHotkey} />

<style>
  .hint { margin: 0 0 var(--s3); color: var(--text-dim); font: var(--t-help); }
  /* The hint introduces the field BELOW it. It follows the always-on-top field,
     so without this it'd sit flush against that field's bottom rule and read
     as its footnote instead. */
  .hint:not(:first-child) { margin-top: var(--s5); }
  code { color: var(--text-dim); font: var(--t-mono); }
</style>
