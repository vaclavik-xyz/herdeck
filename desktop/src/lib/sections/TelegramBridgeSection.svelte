<script lang="ts">
  // Telegram alerts sent BY a bridge (spec T9). Its own target picker ("This
  // Mac" = the local [notifications.telegram] fields of the Notifications
  // section above | a bridge that offers `telegram_config`). The bridge's
  // Telegram document is separate from the shared settings, so it is saved
  // immediately with its own button (base revision -> 409 = changed elsewhere,
  // reload), not with the editor's Apply. The bot token is write-only: only its
  // status is ever shown.
  import { untrack } from "svelte";
  import BooleanField from "../fields/BooleanField.svelte";
  import NumberField from "../fields/NumberField.svelte";
  import SelectField from "../fields/SelectField.svelte";
  import TextField from "../fields/TextField.svelte";
  import FieldCopy from "../fields/FieldCopy.svelte";
  import FieldGroup from "./FieldGroup.svelte";
  import type { BridgeShared } from "../bridgeSettings";
  import {
    TELEGRAM_LANGUAGES, callTelegram, docFrom, docToWire, localToBridgeSettings, parseAllowedUsers, telegramIds,
    tokenErrorKind, type RecentChat, type TelegramCallFn, type TelegramDoc, type TelegramReply, type TokenErrorKind,
  } from "../bridgeTelegram";
  import { defineMessages, fieldHelp, fmt, locale } from "../i18n.svelte";

  let {
    bridges, localTelegram, call, onReload, initialTarget = "",
  }: {
    bridges: Record<string, BridgeShared>;
    /** This Mac's saved `[notifications.telegram]` (null = none): source of "Move". */
    localTelegram: Record<string, unknown> | null;
    /** null = no runtime to talk to (browser preview). */
    call: TelegramCallFn | null;
    /** Re-read `GET /config` (after a 409 or a change). */
    onReload: () => Promise<void>;
    initialTarget?: string;
  } = $props();

  const HELP = $derived(fieldHelp("telegram_bridge"));
  const LM = defineMessages({
    en: {
      title: "Telegram on the bridge",
      description: "A bridge can send the Telegram alerts itself, so they still arrive when no Mac is running. Settings are stored on the bridge and saved separately from Apply.",
      target: "Configure Telegram on",
      this_mac: "This Mac",
      this_mac_note: "This Mac sends its own Telegram alerts, configured in the Telegram bot group above. Pick a bridge to let the bridge send them instead.",
      save: "Save Telegram settings",
      saving: "Saving…",
      saved: "Saved (revision {rev}).",
      stale: "Changed elsewhere, reloaded. Review the values and save again.",
      invalid: "The bridge rejected the settings: {detail}",
      offline: "Bridge not connected.",
      timeout: "The bridge did not answer in time.",
      unreachable: "The runtime is unreachable.",
      http: "Failed (HTTP {status}).",
      bad_users: "allowed_user_ids must be a comma-separated list of positive numbers.",
      token: "token",
      token_file: "set (file)",
      token_env: "set (env)",
      token_unset: "not set",
      token_set: "Set",
      token_clear: "Clear",
      token_input: "Bot token from BotFather",
      token_save: "Save token",
      token_cancel: "Cancel",
      token_env_title: "The token comes from HERDECK_TELEGRAM_TOKEN in the bridge's environment; change it there.",
      token_clear_none: "There is no token to clear.",
      token_saved: "Token saved on the bridge.",
      token_cleared: "Token cleared on the bridge.",
      token_invalid: "That is not a valid bot token.",
      token_env_locked: "The token comes from the environment (HERDECK_TELEGRAM_TOKEN) on the bridge and cannot be changed here.",
      token_io_error: "The bridge could not write the token file.",
      token_no_local_token: "This Mac has no Telegram token to copy — enter the token on the bridge (Set).",
      token_failed: "The bridge refused the token change.",
      status: "Status",
      active: "active",
      inactive: "inactive",
      inbound: "inbound",
      inbound_off: "off",
      inbound_ok: "ok",
      inbound_disabled: "disabled",
      last_sent: "last sent",
      never: "never",
      last_error: "last error",
      chats: "Recent chats",
      chat_pick: "Use this chat (and topic) as chat_id / message_thread_id",
      chats_empty: "No chats yet. Write to the bot (or add it to a group) and reload.",
      chat_topic: "topic {name}",
      test: "Send test message",
      testing: "Sending…",
      test_ok: "Test message sent.",
      test_fail: "Test failed: {detail}",
      move: "Move Telegram from this Mac to the bridge",
      move_title: "Copy this Mac's Telegram fields (and its token, read by the runtime) to bridge {id}",
      moving: "Moving…",
      moved: "Telegram moved to bridge {id}. This Mac's own Telegram can now be turned off — the runtime already stops sending Telegram for agents on that bridge.",
      move_no_token: "Settings moved to bridge {id}. {detail}",
      no_runtime: "Not available in the browser preview.",
    },
    cs: {
      title: "Telegram na bridgi",
      description: "Bridge umí Telegram alerty posílat sám, takže dorazí i když neběží žádný Mac. Nastavení je uložené na bridgi a ukládá se odděleně od Použít.",
      target: "Nastavit Telegram na",
      this_mac: "Tento Mac",
      this_mac_note: "Tento Mac posílá vlastní Telegram alerty, nastavené ve skupině Telegram bot výše. Vyber bridge, aby je posílal on.",
      save: "Uložit nastavení Telegramu",
      saving: "Ukládám…",
      saved: "Uloženo (revize {rev}).",
      stale: "Změněno jinde, načteno znovu. Zkontroluj hodnoty a ulož znovu.",
      invalid: "Bridge nastavení odmítl: {detail}",
      offline: "Bridge není připojený.",
      timeout: "Bridge neodpověděl včas.",
      unreachable: "Runtime je nedostupný.",
      http: "Selhalo (HTTP {status}).",
      bad_users: "allowed_user_ids musí být čísla větší než 0 oddělená čárkou.",
      token: "token",
      token_file: "nastaven (file)",
      token_env: "nastaven (env)",
      token_unset: "nenastaven",
      token_set: "Nastavit",
      token_clear: "Smazat",
      token_input: "Token bota od BotFather",
      token_save: "Uložit token",
      token_cancel: "Zrušit",
      token_env_title: "Token pochází z HERDECK_TELEGRAM_TOKEN v prostředí bridge; změň ho tam.",
      token_clear_none: "Není co smazat, token není nastaven.",
      token_saved: "Token uložen na bridgi.",
      token_cleared: "Token na bridgi smazán.",
      token_invalid: "To není platný token bota.",
      token_env_locked: "Token pochází z prostředí (HERDECK_TELEGRAM_TOKEN) bridge a tady ho nejde změnit.",
      token_io_error: "Bridge nemohl zapsat soubor s tokenem.",
      token_no_local_token: "Tento Mac nemá token Telegramu ke zkopírování — zadej token na bridgi (Nastavit).",
      token_failed: "Bridge změnu tokenu odmítl.",
      status: "Stav",
      active: "aktivní",
      inactive: "neaktivní",
      inbound: "příjem",
      inbound_off: "vypnutý",
      inbound_ok: "ok",
      inbound_disabled: "zakázaný",
      last_sent: "naposledy odesláno",
      never: "nikdy",
      last_error: "poslední chyba",
      chats: "Nedávné chaty",
      chat_pick: "Použít tento chat (a téma) jako chat_id / message_thread_id",
      chats_empty: "Zatím žádné chaty. Napiš botovi (nebo ho přidej do skupiny) a načti znovu.",
      chat_topic: "téma {name}",
      test: "Poslat zkušební zprávu",
      testing: "Odesílám…",
      test_ok: "Zkušební zpráva odeslána.",
      test_fail: "Test selhal: {detail}",
      move: "Přesunout Telegram z tohoto Macu na bridge",
      move_title: "Zkopíruje pole Telegramu tohoto Macu (a jeho token, který přečte runtime) na bridge {id}",
      moving: "Přesouvám…",
      moved: "Telegram přesunut na bridge {id}. Vlastní Telegram tohoto Macu už můžeš vypnout — runtime pro agenty na tomto bridgi Telegram už sám neposílá.",
      move_no_token: "Nastavení přesunuto na bridge {id}. {detail}",
      no_runtime: "V náhledu v prohlížeči není k dispozici.",
    },
  });
  const lm = $derived(LM[locale.lang]);

  let pick = $state(untrack(() => initialTarget));
  const ids = $derived(telegramIds(bridges));
  const target = $derived(ids.includes(pick) ? pick : "");
  const bt = $derived(target === "" ? undefined : bridges[target]?.telegram);

  // What this editor saved, until the bridge's broadcast reaches `GET /config`.
  let known = $state<Record<string, { revision: number; settings: Record<string, unknown> }>>({});
  const baseRev = $derived(Math.max(bt?.revision ?? 0, known[target]?.revision ?? 0));
  let seedTick = $state(0);

  function currentSettings(): Record<string, unknown> | null {
    const k = known[target];
    if (k != null && k.revision > (bt?.revision ?? 0)) return k.settings;
    return bt?.settings ?? null;
  }

  let doc = $state<TelegramDoc>(docFrom(null));
  let usersText = $state("");
  let message = $state<{ bad: boolean; text: string } | null>(null);
  let testResult = $state<{ bad: boolean; text: string } | null>(null);
  let movedTo = $state<string | null>(null);
  let busy = $state(false);
  let tokenEntering = $state(false);
  let tokenValue = $state("");
  let tokenAssumed = $state<{ id: string; value: "file" | null } | null>(null);

  const seedKey = $derived(`${target}|${bt?.revision ?? -1}|${seedTick}`);
  $effect(() => {
    void seedKey;
    untrack(() => {
      doc = docFrom(currentSettings());
      usersText = doc.allowed_user_ids.join(", ");
    });
  });
  $effect(() => {
    void target;
    untrack(() => {
      message = null;
      testResult = null;
      movedTo = null;
      tokenEntering = false;
      tokenValue = "";
    });
  });

  const tokenState = $derived.by((): "file" | "env" | null => {
    const s = bt?.status.token ?? null;
    if (s === "env") return "env";
    if (tokenAssumed != null && tokenAssumed.id === target) return tokenAssumed.value;
    return s;
  });
  $effect(() => {
    if (tokenAssumed != null && tokenAssumed.id === target && (bt?.status.token ?? null) === tokenAssumed.value) tokenAssumed = null;
  });

  const canCall = $derived(call != null && !busy);

  function reply(r: TelegramReply): string {
    const detail = Array.isArray(r.body.messages) ? r.body.messages.filter((m): m is string => typeof m === "string").join("; ") : "";
    if (r.status === 409) return lm.stale;
    if (r.status === 422) return fmt(lm.invalid, { detail: detail || String(r.body.error ?? "") });
    if (r.status === 503) return lm.offline;
    if (r.status === 504) return lm.timeout;
    if (r.status === 0) return lm.unreachable;
    return fmt(lm.http, { status: r.status });
  }

  async function reloadAfterStale(): Promise<void> {
    delete known[target];
    await onReload();
    seedTick += 1;
  }

  async function save(): Promise<void> {
    if (call == null) return;
    const users = parseAllowedUsers(usersText);
    if (users == null) {
      message = { bad: true, text: lm.bad_users };
      return;
    }
    const settings = docToWire({ ...doc, allowed_user_ids: users });
    busy = true;
    message = { bad: false, text: lm.saving };
    try {
      const r = await callTelegram(call, target, "", { base_revision: baseRev, settings });
      if (r.status === 200 && r.body.ok === true) {
        const rev = typeof r.body.revision === "number" ? r.body.revision : baseRev + 1;
        known[target] = { revision: rev, settings };
        message = { bad: false, text: fmt(lm.saved, { rev }) };
        void onReload();
      } else {
        message = { bad: true, text: reply(r) };
        if (r.status === 409) await reloadAfterStale();
      }
    } finally {
      busy = false;
    }
  }

  function tokenMessage(kind: TokenErrorKind): string {
    switch (kind) {
      case "invalid": return lm.token_invalid;
      case "env_locked": return lm.token_env_locked;
      case "io_error": return lm.token_io_error;
      case "no_local_token": return lm.token_no_local_token;
      case "offline": return lm.offline;
      case "timeout": return lm.timeout;
      case "unreachable": return lm.unreachable;
      default: return lm.token_failed;
    }
  }

  async function tokenCall(body: Record<string, unknown>, done: string, assumed: "file" | null): Promise<void> {
    if (call == null) return;
    busy = true;
    try {
      const r = await callTelegram(call, target, "token", body);
      const kind = tokenErrorKind(r);
      if (kind == null) {
        message = { bad: false, text: done };
        tokenAssumed = { id: target, value: assumed };
        await onReload();
        setTimeout(() => void onReload(), 1500); // the bridge's status frame can trail its answer
      } else {
        message = { bad: true, text: tokenMessage(kind) };
      }
    } finally {
      busy = false;
    }
  }

  async function saveToken(): Promise<void> {
    const token = tokenValue;
    tokenValue = "";
    tokenEntering = false;
    if (token === "") return;
    await tokenCall({ action: "set", token }, lm.token_saved, "file");
  }

  async function runTest(): Promise<void> {
    if (call == null) return;
    busy = true;
    testResult = null;
    try {
      const r = await callTelegram(call, target, "test", {});
      if (r.status === 200 && r.body.ok === true) testResult = { bad: false, text: lm.test_ok };
      else if (r.status === 200) testResult = { bad: true, text: fmt(lm.test_fail, { detail: typeof r.body.error === "string" ? r.body.error : "?" }) };
      else testResult = { bad: true, text: r.status === 503 ? lm.offline : r.status === 504 ? lm.timeout : r.status === 0 ? lm.unreachable : fmt(lm.http, { status: r.status }) };
    } finally {
      busy = false;
    }
  }

  function useChat(c: RecentChat): void {
    doc.chat_id = c.chat_id;
    doc.message_thread_id = c.message_thread_id;
  }

  const canMove = $derived(localTelegram != null && Object.keys(localTelegram).length > 0);

  async function move(): Promise<void> {
    if (call == null || localTelegram == null) return;
    busy = true;
    message = { bad: false, text: lm.moving };
    movedTo = null;
    try {
      const settings = localToBridgeSettings(localTelegram, docFrom(currentSettings()));
      const put = await callTelegram(call, target, "", { base_revision: baseRev, settings });
      if (!(put.status === 200 && put.body.ok === true)) {
        message = { bad: true, text: reply(put) };
        if (put.status === 409) await reloadAfterStale();
        return;
      }
      const rev = typeof put.body.revision === "number" ? put.body.revision : baseRev + 1;
      known[target] = { revision: rev, settings };
      // The runtime reads this Mac's token itself; the browser never sees it.
      const tok = await callTelegram(call, target, "token", { action: "set", from_local: true });
      const kind = tokenErrorKind(tok);
      if (kind == null) {
        message = null;
        movedTo = target;
        tokenAssumed = { id: target, value: "file" };
      } else {
        message = { bad: true, text: fmt(lm.move_no_token, { id: target, detail: tokenMessage(kind) }) };
      }
      seedTick += 1;
      await onReload();
    } finally {
      busy = false;
    }
  }

  function when(ms: number | null): string {
    return ms == null ? lm.never : new Date(ms).toLocaleString(locale.lang);
  }
</script>

{#if ids.length > 0}
  <div data-tg-section>
    <FieldGroup title={lm.title} description={lm.description}>
      <label class="field">
        <FieldCopy label="target" help={HELP.target} />
        <select data-tg-target value={target} onchange={(e) => (pick = (e.target as HTMLSelectElement).value)}>
          <option value="">{lm.this_mac}</option>
          {#each ids as id (id)}<option value={id}>{id}</option>{/each}
        </select>
      </label>

      {#if target === ""}
        <p class="note" data-tg-this-mac>{lm.this_mac_note}</p>
      {:else}
        <div data-tg-form>
          <BooleanField label="enabled" help={HELP.enabled} value={doc.enabled} onchange={(v) => (doc.enabled = v)} />
          <TextField label="chat_id" help={HELP.chat_id} value={doc.chat_id} oninput={(v) => (doc.chat_id = v)} />
          <NumberField label="message_thread_id" help={HELP.message_thread_id} int min={1} value={doc.message_thread_id} onchange={(v) => (doc.message_thread_id = v)} />
          <BooleanField label="interactive" help={HELP.interactive} value={doc.interactive} onchange={(v) => (doc.interactive = v)} />
          <TextField label="allowed_user_ids" help={HELP.allowed_user_ids} value={usersText} oninput={(v) => (usersText = v)} />
          <NumberField label="prompt_max_chars" help={HELP.prompt_max_chars} int min={200} max={4000} value={doc.prompt_max_chars} onchange={(v) => (doc.prompt_max_chars = v ?? 1200)} />
          <NumberField label="only_when_away" help={HELP.only_when_away} int min={0} max={1440} value={doc.only_when_away} onchange={(v) => (doc.only_when_away = v ?? 0)} />
          <SelectField label="language" help={HELP.language} value={doc.language} options={[...TELEGRAM_LANGUAGES]} onchange={(v) => (doc.language = v)} />
          <BooleanField label="sound" help={HELP.sound} value={doc.sound} onchange={(v) => (doc.sound = v)} />

          <div class="field" data-tg-token>
            <FieldCopy label="token" help={HELP.token} />
            <div class="row">
              <span class="state">{tokenState === "file" ? lm.token_file : tokenState === "env" ? lm.token_env : lm.token_unset}</span>
              {#if tokenEntering}
                <input type="password" data-tg-token-input autocomplete="off" placeholder={lm.token_input} aria-label={lm.token_input} bind:value={tokenValue} />
                <button type="button" data-action="tg-token-save" disabled={!canCall || tokenValue === ""} onclick={() => void saveToken()}>{lm.token_save}</button>
                <button type="button" data-action="tg-token-cancel" onclick={() => { tokenEntering = false; tokenValue = ""; }}>{lm.token_cancel}</button>
              {:else}
                <button type="button" data-action="tg-token-set" disabled={!canCall || tokenState === "env"} title={tokenState === "env" ? lm.token_env_title : undefined} onclick={() => (tokenEntering = true)}>{lm.token_set}</button>
                <button type="button" data-action="tg-token-clear" disabled={!canCall || tokenState !== "file"} title={tokenState === "env" ? lm.token_env_title : tokenState === null ? lm.token_clear_none : undefined} onclick={() => void tokenCall({ action: "clear" }, lm.token_cleared, null)}>{lm.token_clear}</button>
              {/if}
            </div>
          </div>
        </div>

        <p class="status" data-tg-status>
          <span>{lm.status}: <b>{bt?.status.active ? lm.active : lm.inactive}</b></span>
          <span>{lm.inbound}: {bt?.status.inbound === "ok" ? lm.inbound_ok : bt?.status.inbound === "disabled" ? lm.inbound_disabled : lm.inbound_off}</span>
          <span>{lm.last_sent}: <span data-tg-last-sent>{when(bt?.status.last_sent_at_ms ?? null)}</span></span>
          {#if bt?.status.last_error}<span class="bad" data-tg-last-error>{lm.last_error}: {bt.status.last_error}</span>{/if}
        </p>

        <div class="field chats">
          <FieldCopy label="recent_chats" help={HELP.recent_chats} />
          <div>
            {#if (bt?.status.recent_chats.length ?? 0) === 0}
              <p class="note" data-tg-chats-empty>{lm.chats_empty}</p>
            {:else}
              <ul>
                {#each bt?.status.recent_chats ?? [] as c (`${c.chat_id}/${c.message_thread_id ?? ""}`)}
                  <li>
                    <button type="button" data-tg-chat title={lm.chat_pick} onclick={() => useChat(c)}>
                      {c.title || c.chat_id}
                      <code>{c.chat_id}{c.message_thread_id != null ? `/${c.message_thread_id}` : ""}</code>
                      {#if c.topic_name}<em>{fmt(lm.chat_topic, { name: c.topic_name })}</em>{/if}
                    </button>
                  </li>
                {/each}
              </ul>
            {/if}
          </div>
        </div>

        <div class="actions">
          <button type="button" class="primary" data-action="tg-save" disabled={!canCall} title={call == null ? lm.no_runtime : undefined} onclick={() => void save()}>{lm.save}</button>
          <button type="button" data-action="tg-test" disabled={!canCall} title={call == null ? lm.no_runtime : undefined} onclick={() => void runTest()}>{lm.test}</button>
          {#if canMove}
            <button type="button" data-action="tg-move" disabled={!canCall} title={fmt(lm.move_title, { id: target })} onclick={() => void move()}>{lm.move}</button>
          {/if}
        </div>
        {#if message}<p class="note" class:bad={message.bad} role="status" data-tg-message>{message.text}</p>{/if}
        {#if testResult}<p class="note" class:bad={testResult.bad} role="status" data-tg-test-result>{testResult.text}</p>{/if}
        {#if movedTo}<p class="note" role="status" data-tg-moved>{fmt(lm.moved, { id: movedTo })}</p>{/if}
      {/if}
    </FieldGroup>
  </div>
{/if}

<style>
  [data-tg-section] { margin-top: var(--s6); }
  .field {
    display: grid;
    grid-template-columns: var(--field-label-w) minmax(0, 1fr);
    gap: var(--s1) var(--s6);
    padding: var(--s3) 0;
    border-bottom: 1px solid var(--line);
  }
  select, input[type="password"] {
    align-self: center;
    width: 100%;
    max-width: var(--control-md);
    min-height: 32px;
    padding: 0 var(--s3);
    border: 1px solid var(--line-strong);
    border-radius: var(--r-control);
    background: var(--field);
    color: var(--text);
  }
  .row { display: flex; flex-wrap: wrap; align-items: center; gap: var(--s2); grid-column: 2; grid-row: 1 / span 2; }
  .state { color: var(--text-dim); font: var(--t-label); }
  button {
    min-height: 30px;
    padding: 0 var(--s3);
    border: 1px solid var(--line-strong);
    border-radius: var(--r-control);
    background: var(--panel-raised);
    color: var(--text);
    cursor: pointer;
  }
  button:disabled { cursor: not-allowed; opacity: .6; }
  .actions { display: flex; flex-wrap: wrap; gap: var(--s2); margin-top: var(--s4); }
  .status { display: flex; flex-wrap: wrap; gap: var(--s2) var(--s5); margin: var(--s3) 0; color: var(--text-dim); font: var(--t-help); }
  .note { margin: var(--s2) 0 0; max-width: 72ch; color: var(--text-dim); font: var(--t-help); }
  .note.bad, .bad { color: var(--st-offline-text); }
  ul { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: var(--s1); }
  li button { display: flex; flex-wrap: wrap; align-items: baseline; gap: var(--s2); text-align: left; }
  li code { color: var(--text-faint); font: var(--t-mono); }
  li em { color: var(--text-dim); font-style: normal; }
  @media (max-width: 760px) {
    .field { grid-template-columns: minmax(0, 1fr); }
    .row { grid-column: 1; grid-row: auto; }
  }
</style>
