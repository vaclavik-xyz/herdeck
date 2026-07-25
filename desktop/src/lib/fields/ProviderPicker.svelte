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
    border: 1px solid var(--border, #2b323d);
    border-radius: var(--radius-control, 7px);
    background: var(--field, #10141a);
    transition: border-color 120ms ease, background 120ms ease;
  }
  .provider.enabled { border-color: var(--border-strong, #3b4553); background: var(--surface, #151a21); }
  .mark {
    display: grid;
    place-items: center;
    width: 26px;
    height: 26px;
    border-radius: 6px;
    color: #f0f2f5;
    font-size: 12px;
    font-weight: 750;
    letter-spacing: -0.02em;
  }
  .mark.claude { background: #995b48; }
  .mark.codex { background: #276f65; }
  .identity { display: flex; flex-direction: column; min-width: 0; }
  .name { color: var(--text, #e8ebef); font-size: 12px; font-weight: 650; }
  .fieldlabel.hashelp { text-decoration: underline dotted #5a5a62; text-underline-offset: 3px; cursor: help; }
  .source { color: var(--muted, #939ca9); font-size: 10px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .state { color: var(--muted-low, #6f7885); font-size: 10px; }
  .enabled .state { color: #b2bac5; }
  .switch {
    position: relative;
    width: 34px;
    height: 20px;
    padding: 0;
    border: 1px solid var(--border-strong, #3b4553);
    border-radius: 999px;
    background: #272d36;
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
  .switch.on { border-color: #7897d5; background: #5879bd; }
  .switch.on span { transform: translateX(14px); background: #f2f4f7; }
  .switch:focus-visible { outline: 2px solid #8eabe7; outline-offset: 2px; }
  @media (prefers-reduced-motion: reduce) {
    .provider, .switch span { transition: none; }
  }
</style>
