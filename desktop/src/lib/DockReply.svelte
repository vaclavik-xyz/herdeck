<script lang="ts">
  // The dock card's free-text reply: Enter sends through the guarded
  // /agent/text route (herdeck-ctl `send` — typed and submitted), Shift+Enter
  // adds a line. Sending / sent (field clears) / error (text kept) are shown
  // inline; Esc leaves the field. The parent can focus the field (`focus()`)
  // and hears when it gains or loses focus, so keyboard shortcuts never fire
  // while typing here.
  import { onMount, tick } from "svelte";
  import { TEXT_MAX_CHARS, type AgentRef, type AgentTransport } from "./agentCardClient";
  import { DOCK_MESSAGES, outcomeText } from "./dockMessages";
  import { fmt, locale } from "./i18n.svelte";

  let {
    transport,
    agent,
    disabled = false,
    onSent = () => {},
    onFocusChange = () => {},
  }: {
    transport: AgentTransport;
    agent: AgentRef;
    disabled?: boolean;
    /** The text was handed to the agent ("sent", or "pending" = not yet confirmed). */
    onSent?: () => void;
    onFocusChange?: (focused: boolean) => void;
  } = $props();

  /** How long "Sent ✓" stays before the hint line empties again. */
  const SENT_SHOW_MS = 2500;
  /** One line (18 px) + 2 × 5 px padding; matches the CSS min-height. */
  const MIN_FIELD_PX = 28;
  /** Tallest the field grows before it scrolls (4 lines). */
  const MAX_FIELD_PX = 82;

  const m = $derived(DOCK_MESSAGES[locale.lang]);

  let text = $state("");
  let phase = $state<"idle" | "sending" | "sent" | "error">("idle");
  let message = $state("");
  let field = $state<HTMLTextAreaElement | undefined>(undefined);
  let sentTimer: ReturnType<typeof setTimeout> | undefined;
  let alive = true;

  const ready = $derived(text.trim() !== "" && phase !== "sending" && !disabled);

  /** Focus the field (Task-5 shortcuts: T or /). False when it cannot take focus. */
  export function focus(): boolean {
    if (!field || disabled) return false;
    field.focus();
    return true;
  }

  // Grow with the text up to MAX_FIELD_PX. The field only becomes scrollable
  // once the text is taller than that: a scrollable textarea gets classic
  // scrollbars on macOS (mouse attached), and under the dock's CSS zoom a
  // sub-pixel overflow alone was enough to show both of them on an empty
  // one-line field, covering the placeholder.
  function fit(): void {
    const el = field;
    if (!el) return;
    el.style.height = "auto";
    const full = el.scrollHeight;
    el.style.height = `${Math.max(MIN_FIELD_PX, Math.min(full, MAX_FIELD_PX))}px`;
    el.style.overflowY = full > MAX_FIELD_PX + 1 ? "auto" : "hidden";
  }

  async function send(): Promise<void> {
    const body = text;
    if (!body.trim() || phase === "sending" || disabled) return;
    if (body.length > TEXT_MAX_CHARS) {
      phase = "error";
      message = fmt(m.reply_too_long, { n: TEXT_MAX_CHARS });
      return;
    }
    clearTimeout(sentTimer);
    phase = "sending";
    message = "";
    const out = await transport.act("text", agent, { text: body });
    if (!alive) return;
    if (out.code === "sent" || out.code === "pending") {
      // Handed over: clearing the field keeps a retry from typing it twice.
      if (text === body) text = "";
      phase = "sent";
      message = out.code === "pending" ? outcomeText(locale.lang, out) : "";
      void tick().then(fit);
      sentTimer = setTimeout(() => {
        if (phase === "sent") {
          phase = "idle";
          message = "";
        }
      }, SENT_SHOW_MS);
      onSent();
      return;
    }
    phase = "error";
    message = outcomeText(locale.lang, out);
  }

  function onKey(e: KeyboardEvent): void {
    // Esc only leaves the field (the card stays; the dock's keys work again).
    if (e.key === "Escape" && !e.isComposing) {
      e.preventDefault();
      field?.blur();
      return;
    }
    // Never submit mid IME composition; Shift+Enter is a plain newline.
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      void send();
    }
  }

  function onInput(): void {
    if (phase === "error" || phase === "sent") {
      phase = "idle";
      message = "";
    }
    fit();
  }

  onMount(() => {
    fit();
    return () => {
      alive = false;
      clearTimeout(sentTimer);
    };
  });
</script>

<div class="reply" class:busy={phase === "sending"}>
  <div class="field">
    <textarea
      bind:this={field}
      bind:value={text}
      rows="1"
      wrap="soft"
      maxlength={TEXT_MAX_CHARS}
      placeholder={m.reply_placeholder}
      aria-label={m.reply_label}
      readonly={phase === "sending"}
      {disabled}
      spellcheck="false"
      onkeydown={onKey}
      oninput={onInput}
      onfocus={() => onFocusChange(true)}
      onblur={() => onFocusChange(false)}
    ></textarea>
    <button
      type="button"
      class="send"
      class:on={ready}
      title={m.reply_send}
      aria-label={m.reply_send}
      disabled={!ready}
      onmousedown={(e) => e.preventDefault()}
      onclick={() => void send()}
    >
      <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
        <path d="M8 13V3.5M3.5 8 8 3.5 12.5 8" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" />
      </svg>
    </button>
  </div>
  {#if phase === "sending"}
    <p class="hint" role="status">{m.reply_sending}</p>
  {:else if phase === "sent"}
    <p class="hint ok" role="status">{message || m.reply_sent}</p>
  {:else if phase === "error"}
    <p class="hint bad" role="status">{message}</p>
  {/if}
</div>

<style>
  .reply { display: grid; gap: var(--s1); }
  .field {
    position: relative;
    display: flex;
    /* A grown field keeps the send button on its last line. */
    align-items: flex-end;
    border: 1px solid var(--line);
    border-radius: var(--r-control);
    background: var(--field);
  }
  .field:focus-within {
    border-color: var(--accent-ring);
    box-shadow: 0 0 0 2px var(--accent-soft);
  }
  textarea {
    flex: 1;
    min-width: 0;
    box-sizing: border-box;
    display: block;
    /* MIN_FIELD_PX / MAX_FIELD_PX: one line of 18 px + 2 × 5 px, up to 4 lines. */
    height: 28px;
    min-height: 28px;
    max-height: 82px;
    margin: 0;
    padding: 5px var(--s1) 5px var(--s2);
    border: 0;
    outline: none;
    resize: none;
    background: transparent;
    color: var(--text);
    font: var(--t-body);
    line-height: 18px;
    /* Wrap, never scroll sideways; fit() turns vertical scrolling on only
       past four lines, and even then no scrollbar is drawn. */
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    overflow: hidden;
    scrollbar-width: none;
  }
  textarea::-webkit-scrollbar { display: none; }
  textarea::placeholder {
    color: var(--text-faint);
    line-height: 18px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  textarea:disabled { opacity: .5; }
  .busy textarea { color: var(--text-dim); }
  .send {
    flex: none;
    display: grid;
    place-items: center;
    width: 20px;
    height: 20px;
    margin: 4px; /* 20 + 2 × 4 = the one-line field's 28 px: centred on it */
    padding: 0;
    border: 0;
    border-radius: 5px;
    background: var(--key);
    color: var(--text-faint);
    cursor: default;
  }
  .send.on {
    background: var(--accent);
    color: var(--canvas);
    cursor: pointer;
  }
  .hint { margin: 0; color: var(--text-dim); font: var(--t-help); }
  .hint.ok { color: var(--st-working); }
  .hint.bad { color: var(--sev-warning-text); }
</style>
