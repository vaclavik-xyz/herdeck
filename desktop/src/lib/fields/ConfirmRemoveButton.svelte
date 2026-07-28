<script lang="ts">
  import { onDestroy } from "svelte";
  import { t } from "../i18n.svelte";

  let { title, onconfirm, identity = "", resetKey = 0 }:
    { title: string; onconfirm: () => void; identity?: string; resetKey?: string | number } = $props();

  let armed = $state(false);
  let resetTimer: ReturnType<typeof setTimeout> | undefined;
  let previousIdentity: string | undefined;
  let previousResetKey: string | number | undefined;
  let resetSeen = false;

  $effect(() => {
    if (resetSeen && (identity !== previousIdentity || resetKey !== previousResetKey)) {
      if (resetTimer) clearTimeout(resetTimer);
      armed = false;
    }
    previousIdentity = identity;
    previousResetKey = resetKey;
    resetSeen = true;
  });

  function press(): void {
    if (armed) {
      if (resetTimer) clearTimeout(resetTimer);
      armed = false;
      onconfirm();
      return;
    }
    armed = true;
    resetTimer = setTimeout(() => (armed = false), 4000);
  }

  onDestroy(() => {
    if (resetTimer) clearTimeout(resetTimer);
  });
</script>

<button
  type="button"
  class="confirm-remove"
  class:armed
  {title}
  aria-label={armed ? t("widget.confirm_remove") : title}
  onclick={press}
>{armed ? t("widget.confirm_remove") : "×"}</button>

<style>
  /* Self-contained: this button lives inside a <legend> in six different
     sections, and Svelte's scoped styles never cross a component boundary — so
     the parent's `button {…}` rule cannot reach it. It previously declared only
     a colour (with !important, aimed at an ambient rule that does not exist),
     which left the UA's light-grey chrome button on a dark panel. */
  button {
    min-height: 24px;
    padding: 0 var(--s2);
    border: 1px solid var(--line-strong);
    border-radius: var(--r-control);
    background: var(--panel-raised);
    color: var(--st-offline);
    font: var(--t-label);
    line-height: 1;
    vertical-align: middle;
    cursor: pointer;
    transition: background var(--dur) var(--ease), border-color var(--dur) var(--ease);
  }
  button:hover { background: var(--key); }
  button.armed {
    border-color: var(--st-offline);
    background: color-mix(in srgb, var(--st-offline) 18%, var(--panel-raised));
    color: var(--text);
  }
  /* Coarse pointers need a real target; 24px is fine for a mouse, not a thumb. */
  @media (max-width: 760px) {
    button { min-height: 32px; padding: 0 var(--s3); }
  }
</style>
