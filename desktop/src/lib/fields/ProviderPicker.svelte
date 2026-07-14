<script lang="ts">
  import { defineMessages, locale } from "../i18n.svelte";

  let { providers, onchange, help = "" }:
    { providers: string[]; onchange: (providers: string[]) => void; help?: string } = $props();

  const KNOWN = [
    { id: "claude", name: "Claude", mark: "A", tone: "claude" },
    { id: "codex", name: "Codex", mark: "O", tone: "codex" },
  ] as const;

  const LM = defineMessages({
    en: {
      claude: "Anthropic subscription limits from Claude Code",
      codex: "ChatGPT plan limits from the Codex account API",
      on: "Included",
      off: "Hidden",
    },
    cs: {
      claude: "Limity předplatného Anthropic z Claude Code",
      codex: "Limity tarifu ChatGPT z Codex account API",
      on: "Zobrazeno",
      off: "Skryto",
    },
  });
  const lm = $derived(LM[locale.lang]);

  function toggle(id: string): void {
    onchange(
      providers.includes(id)
        ? providers.filter((provider) => provider !== id)
        : [...providers, id],
    );
  }
</script>

<div class="providers">
  {#each KNOWN as provider}
    <div class="provider" class:enabled={providers.includes(provider.id)}>
      <span class="mark {provider.tone}" aria-hidden="true">{provider.mark}</span>
      <span class="identity">
        <span class="name fieldlabel" class:hashelp={!!help} title={help || undefined}>{provider.name}</span>
        <span class="source">{lm[provider.id]}</span>
      </span>
      <span class="state">{providers.includes(provider.id) ? lm.on : lm.off}</span>
      <button
        type="button"
        class="switch"
        class:on={providers.includes(provider.id)}
        role="switch"
        aria-label={provider.name}
        aria-checked={providers.includes(provider.id)}
        onclick={() => toggle(provider.id)}
      ><span></span></button>
    </div>
  {/each}
</div>

<style>
  .providers { display: grid; gap: 6px; margin: 2px 0 8px; }
  .provider {
    display: grid;
    grid-template-columns: 28px minmax(0, 1fr) auto 36px;
    align-items: center;
    gap: 9px;
    min-height: 46px;
    padding: 5px 9px;
    border: 1px solid #25272e;
    border-radius: 8px;
    background: #111217;
    transition: border-color 120ms ease, background 120ms ease;
  }
  .provider.enabled { border-color: #353944; background: #15171d; }
  .mark {
    display: grid;
    place-items: center;
    width: 26px;
    height: 26px;
    border-radius: 7px;
    color: #fff;
    font-size: 12px;
    font-weight: 750;
    letter-spacing: -0.02em;
  }
  .mark.claude { background: #b85f42; }
  .mark.codex { background: #147d69; }
  .identity { display: flex; flex-direction: column; min-width: 0; }
  .name { color: #eeeef2; font-size: 13px; font-weight: 650; }
  .fieldlabel.hashelp { text-decoration: underline dotted #5a5a62; text-underline-offset: 3px; cursor: help; }
  .source { color: #797c87; font-size: 10px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .state { color: #696c75; font-size: 10px; text-transform: uppercase; letter-spacing: .07em; }
  .enabled .state { color: #aeb2bd; }
  .switch {
    position: relative;
    width: 34px;
    height: 20px;
    padding: 0;
    border: 1px solid #383b44;
    border-radius: 999px;
    background: #24262d;
    cursor: pointer;
  }
  .switch span {
    position: absolute;
    top: 3px;
    left: 3px;
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: #8b8e97;
    transition: transform 120ms ease, background 120ms ease;
  }
  .switch.on { border-color: #287e70; background: #1b554c; }
  .switch.on span { transform: translateX(14px); background: #e7fff9; }
  .switch:focus-visible { outline: 2px solid #6bd8c2; outline-offset: 2px; }
  @media (prefers-reduced-motion: reduce) {
    .provider, .switch span { transition: none; }
  }
</style>
