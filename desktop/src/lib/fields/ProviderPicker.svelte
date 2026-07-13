<script lang="ts">
  import { untrack } from "svelte";
  import { defineMessages, locale, t } from "../i18n.svelte";

  let { providers, onchange, help = "" }:
    { providers: string[]; onchange: (providers: string[]) => void; help?: string } = $props();

  const KNOWN = [
    { id: "claude", name: "Claude", mark: "A", tone: "claude" },
    { id: "codex", name: "Codex", mark: "O", tone: "codex" },
  ] as const;
  const KNOWN_IDS = new Set<string>(KNOWN.map((provider) => provider.id));

  const LM = defineMessages({
    en: {
      claude: "Anthropic subscription limits from Claude Code",
      codex: "ChatGPT plan limits from the Codex account API",
      on: "Included",
      off: "Hidden",
      other: "Other provider ids",
    },
    cs: {
      claude: "Limity předplatného Anthropic z Claude Code",
      codex: "Limity tarifu ChatGPT z Codex account API",
      on: "Zobrazeno",
      off: "Skryto",
      other: "Další ID poskytovatelů",
    },
  });
  const lm = $derived(LM[locale.lang]);
  let drafts = $state<string[]>(
    untrack(() => providers.filter((id) => !KNOWN_IDS.has(id))),
  );
  let syncedOthers = $state(
    untrack(() => JSON.stringify(providers.filter((id) => !KNOWN_IDS.has(id)))),
  );

  // Do not publish every keystroke. A draft such as "claude-enterprise"
  // passes through the exact text "claude" while being typed; publishing that
  // prefix would make it disappear into the built-in Claude toggle.
  $effect(() => {
    const next = providers.filter((id) => !KNOWN_IDS.has(id));
    const serialized = JSON.stringify(next);
    if (serialized !== syncedOthers) {
      drafts = next;
      syncedOthers = serialized;
    }
  });

  function toggle(id: string): void {
    onchange(
      providers.includes(id)
        ? providers.filter((provider) => provider !== id)
        : [...providers, id],
    );
  }

  function setOther(next: string[]): void {
    // Replace custom slots in place so editing ["zai", "claude"] never
    // silently changes the panel order to ["claude", "zai"].
    let customIndex = 0;
    const merged: string[] = [];
    for (const provider of providers) {
      if (KNOWN_IDS.has(provider)) {
        merged.push(provider);
      } else if (customIndex < next.length) {
        merged.push(next[customIndex++]);
      }
    }
    merged.push(...next.slice(customIndex));
    syncedOthers = JSON.stringify(next);
    onchange(merged);
  }

  function setDraft(index: number, value: string): void {
    drafts = drafts.map((draft, i) => (i === index ? value : draft));
  }

  function addDraft(): void {
    drafts = [...drafts, ""];
  }

  function removeDraft(index: number): void {
    drafts = drafts.filter((_, i) => i !== index);
    setOther(drafts);
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

<div class="other">
  <span class="other-label fieldlabel" class:hashelp={!!help} title={help || undefined}>{lm.other}</span>
  <div class="other-rows">
    {#each drafts as draft, index (index)}
      <div class="other-row">
        <input
          value={draft}
          oninput={(event) => setDraft(index, (event.target as HTMLInputElement).value)}
          onchange={() => setOther(drafts)}
        />
        <button type="button" class="remove" title={t("widget.remove_row")} onclick={() => removeDraft(index)}>×</button>
      </div>
    {/each}
    <button type="button" class="add" onclick={addDraft}>{t("widget.add")}</button>
  </div>
</div>

<style>
  .providers {
    display: grid;
    gap: 6px;
    margin: 2px 0 8px;
  }
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
  .provider.enabled {
    border-color: #353944;
    background: #15171d;
  }
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
  .name { color: #eeeeF2; font-size: 13px; font-weight: 650; }
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
  .other {
    display: grid;
    grid-template-columns: var(--field-label-w, 120px) 1fr;
    align-items: start;
    gap: 8px;
    margin: 6px 0;
  }
  .other-label { color: #aaa; padding-top: 4px; }
  .other-rows { display: flex; flex-direction: column; gap: 4px; }
  .other-row { display: flex; gap: 6px; }
  .other-row input {
    flex: 1;
    min-width: 0;
    background: #141417;
    border: 1px solid #2a2a30;
    color: inherit;
    padding: 4px 6px;
    border-radius: 4px;
  }
  .other button {
    background: #1b1b1f;
    border: 1px solid #2a2a30;
    color: inherit;
    border-radius: 4px;
    padding: 4px 8px;
    cursor: pointer;
  }
  .other .remove { color: #e05050; }
  .other .add { align-self: flex-start; }
  @media (prefers-reduced-motion: reduce) {
    .provider, .switch span { transition: none; }
  }
</style>
