# Telegram Interactive Notifications Design

## Goal

Make Telegram notifications actionable: when an agent enters `blocked`, Herdeck sends a rich Telegram alert with the current prompt, inline action buttons, and a reply path that sends user text back to that specific agent.

This is the first interactive version. It targets one Telegram chat and, optionally, one forum topic (`message_thread_id`) such as a Hermes topic. It does not create or manage one topic per agent.

## Current Foundation

Herdeck already has most of the agent-control plumbing:

- `src/herdeck/notify.py` can send Telegram `sendMessage` requests.
- `src/herdeck/app.py` detects `blocked` transitions and schedules notifications off the render loop.
- `src/herdeck/commands.py` already models `read`, `act_if_blocked`, `act_force`, and `send_text`.
- `src/herdeck/bridge.py` maps those commands to Herdr RPCs: `pane.read`, `pane.send_keys`, `agent.send`, and `agent.focus`.
- `src/herdeck/ctl.py` already has a one-shot control session that can approve, deny, stop, focus, and send text.

The missing pieces are Telegram inbound polling, per-alert correlation, richer outbound messages, and a request path that does not collide with deck prompt reads.

## Decision

Build an in-process `TelegramInteractor` owned by the main Herdeck runtime. It will replace the simple Telegram sink when `[notifications.telegram].interactive = true`.

Rejected alternatives:

- A separate `herdeck-telegram` daemon would keep the main app smaller, but it would duplicate connector state and lifecycle handling in V1.
- A Telegram webhook server is a better fit for public deployments, but local Herdeck does not have a stable public HTTPS endpoint. Long polling is simpler and matches the local Mac/Tailscale workflow.

## Configuration

Extend `TelegramConfig` with interaction-specific fields:

```toml
[notifications]
enabled = true
backends = ["telegram"]
on = ["blocked"]
sound = true

[notifications.telegram]
token_env = "HERDECK_TELEGRAM_TOKEN"
chat_id = "-1001234567890"
message_thread_id = 456
interactive = true
allowed_user_ids = [123456789]
prompt_max_chars = 1200
```

Field semantics:

- `token_env`: env/keychain name for the bot token. The token value is never stored in TOML.
- `chat_id`: target chat. Incoming updates from any other chat are ignored.
- `message_thread_id`: optional forum topic id. When present, outbound messages include it and inbound messages outside that topic are ignored.
- `interactive`: when false or omitted, the existing one-way Telegram sink remains the behavior.
- `allowed_user_ids`: required for `interactive = true`; only these Telegram users can trigger buttons or send replies.
- `prompt_max_chars`: optional limit for prompt text included in alerts; default 1200.

## Runtime Architecture

Introduce `src/herdeck/telegram.py` with four small units:

- `TelegramBotClient`: stdlib HTTP wrapper for Bot API methods used by V1: `sendMessage`, `getUpdates`, `answerCallbackQuery`, `editMessageText`, and optionally `editMessageReplyMarkup`.
- `TelegramAlertFormatter`: builds plain-text alert bodies and inline keyboard JSON.
- `TelegramAlertStore`: in-memory map from Telegram `message_id` and callback token to `AgentKey` plus metadata.
- `TelegramInteractor`: orchestrates outbound blocked alerts and inbound update processing.

Change the app notification boundary from "pre-rendered title/body" to a typed blocked-alert event:

```python
class BlockedAlertNotifier(Protocol):
    async def notify_blocked(self, agent: AgentState, *, body: str, sound: bool, multi_server: bool) -> None: ...
```

`body` is the current legacy-safe metadata string, so existing one-way sinks can be adapted without reimplementing formatting. `agent` gives interactive Telegram the `AgentKey`, repo, branch, label, and agent type it needs for prompt reads, callbacks, and reply correlation.

Keep `Notifier.notify(title, body, sound)` as the low-level sink wrapper for macOS and non-interactive Telegram. Add a small async adapter, for example `LegacyBlockedNotifier`, that converts `AgentState` plus metadata into the old `title/body/sound` call and runs blocking sinks off the event loop. `App._maybe_notify()` should schedule the typed blocked-alert notifier, not a raw sink, so interactive and legacy behavior share one transition detector.

When interactive Telegram is enabled alongside other backends, use `CompositeBlockedNotifier` to preserve the other configured backends. Avoid duplicate Telegram sends by excluding the one-way Telegram sink from the legacy notifier whenever the interactive Telegram notifier is active. Only suppress the one-way Telegram sink after interactive Telegram is fully usable: token exists, `chat_id` is set, `allowed_user_ids` is non-empty, and the interactor is installed. If interactive config is incomplete, keep the existing one-way Telegram alert path and surface the missing interactive readiness in diagnostics.

`message_thread_id` applies to both one-way Telegram notifications and interactive Telegram messages. Non-interactive alerts and incomplete-interactive fallback alerts must still be posted into the configured forum topic.

Keep polling ownership separate from the composite notifier. The builder should return an explicit runtime bundle containing the `BlockedAlertNotifier` plus an optional inbound poller reference. The app stores that poller directly and must not infer it with `isinstance(app.blocked_notifier, TelegramInteractor)`, because interactive Telegram is normally wrapped inside `CompositeBlockedNotifier`.

Config reloads and profile switches must rebuild the blocked notification runtime. That rebuild updates the legacy notifier, interactive Telegram interactor, poller reference, chat/topic/user filters, and runtime control config together, so old Telegram settings do not survive after `_apply_config()`.

The interactor depends on an app-facing control adapter rather than directly knowing deck internals:

```python
class AgentControl(Protocol):
    async def read_prompt(self, key: AgentKey) -> str: ...
    async def approve(self, key: AgentKey) -> ActionResult: ...
    async def deny(self, key: AgentKey) -> ActionResult: ...
    async def stop(self, key: AgentKey) -> ActionResult: ...
    async def send_text(self, key: AgentKey, text: str) -> ActionResult: ...
    def current_agent(self, key: AgentKey) -> AgentState | None: ...
```

For the main app, this adapter should be backed by existing `Connector.send()` and `Command` messages. It must use its own request ids and pending-result map, not `App._active_read_req`, so Telegram reads cannot invalidate deck UI reads.

## Outbound Flow

1. A snapshot or event marks an agent as newly `blocked`.
2. `App._maybe_notify()` builds the legacy-safe metadata body and calls `blocked_notifier.notify_blocked(agent, body=..., sound=..., multi_server=...)`.
3. If the notifier is a `LegacyBlockedNotifier`, behavior stays as today through the existing macOS/non-interactive Telegram sinks.
4. If the notifier is or includes `TelegramInteractor`, it schedules `notify_blocked(agent)` while preserving the legacy metadata in the outbound alert.
5. The interactor calls `read_prompt(agent.key)` with a short timeout.
6. It formats an alert:

```text
codex blocked
herdeck · feat/telegram-interactive-notifications · local:p1

Waiting for:
Allow this edit?
1. Yes
2. Yes, and don't ask again
3. No

Reply to this message to send text to the agent.
```

7. It calls `sendMessage` with `chat_id`, optional `message_thread_id`, plain text, `disable_notification`, and inline buttons.
8. It stores the returned Telegram `message_id -> AgentKey` mapping.

If `read_prompt()` fails or times out, the alert is still sent with metadata and the prompt block is replaced by `Prompt unavailable; use Read again`.

## Inline Actions

Each alert has this keyboard:

- `Approve`
- `Deny`
- `Stop`
- `Read again`

Callback data should be compact and opaque, for example `h:<token>:approve`. The token maps to an in-memory alert record. Do not encode server ids, pane ids, or secret values directly into callback data.

Button handling:

- `Approve` calls guarded approve (`act_if_blocked`) using the configured agent answer profile.
- `Deny` calls guarded deny (`act_if_blocked`).
- `Stop` calls unconditional stop (`act_force`).
- `Read again` re-runs `read_prompt()` and posts a reply to the original alert or edits the alert text if the Bot API call succeeds.

After every callback, Herdeck calls `answerCallbackQuery` so Telegram clients stop showing a spinner. This includes timeout and failure responses when the underlying agent command, prompt read, or message edit raises. For action results, report `sent` only when `ActionResult.sent` is true, `skipped` only when `ActionResult.skipped` is true, otherwise report `ActionResult.message` or `failed`.

When a Telegram action or reply result is consumed by the runtime control broker, Herdeck must still request a fresh `list` from that server for non-read commands so the deck UI refresh path matches local approve/deny/stop/send actions.

## Reply-To-Agent Flow

The interactor accepts a message as agent input only when all checks pass:

- it is from an allowed user id,
- it is in the configured chat,
- if configured, it is in the configured `message_thread_id`,
- it is a text message,
- it is a reply to a Telegram message id known in `TelegramAlertStore`,
- the mapped agent still exists.

The interactor then sends the message text through existing `send_text`, which uses `agent.send` plus Enter. It replies in Telegram with a short delivery status:

- `sent to local:p1`
- `agent is no longer available`
- `connection lost`
- `not authorized`

Plain, non-reply messages are ignored except `/status`, which can return a short list of currently tracked blocked alerts. `/status` must pass the same allowed-user, chat, and topic filters before producing any response; otherwise it is ignored or answered with `not authorized` without exposing agent metadata.

## Update Polling

Use long polling with `getUpdates`:

- `allowed_updates = ["message", "callback_query"]`
- maintain `offset = last_update_id + 1`
- advance the offset in a `finally` path for each received `update_id`, so Telegram acknowledgement failures cannot replay approve/deny/stop/text actions
- guard each long-poll result with the current notification runtime generation, so a poller replaced during config reload cannot process updates with stale chat/topic/user filters
- use a moderate timeout, for example 20 seconds
- retry transient HTTP/network errors with capped backoff
- prune expired and no-longer-blocked alert records before processing updates and before rendering `/status`

Herdeck should not set a webhook in V1. If Telegram reports a 409 webhook conflict, the interactor logs a clear warning, marks inbound polling disabled for that interactor, and keeps outbound alerts working.

## State And Expiry

`TelegramAlertStore` is in-memory for V1.

Records expire when:

- the agent leaves `blocked`,
- the agent disappears,
- the app reloads config and Telegram target changes,
- the record is older than a bounded TTL, default 24 hours.

After expiry, callback/reply attempts get a short Telegram response saying the alert is stale.

In-memory state is intentional for V1. It avoids storing operational agent mappings on disk and matches the fact that blocked episodes are short-lived.

## Security

The feature must preserve these invariants:

- Never store bot token values in config or logs.
- Never include prompt text in logs at info level.
- Never accept actions from an unlisted user id.
- Never accept messages outside the configured chat/topic.
- Never treat arbitrary topic messages as agent input; only replies to known alert messages route to agents.
- Keep existing guarded approve/deny behavior.
- Keep notification IO off the render loop.
- Keep blocking Telegram Bot API HTTP calls off the asyncio event loop by using `asyncio.to_thread()` or the existing executor path.
- Swallow or log network failures without crashing Herdeck.

The prompt may contain sensitive command context. This is acceptable only because the user explicitly configures a target chat/topic. The default remains one-way disabled notifications.

## Failure Handling

- Missing token or chat id: skip Telegram backend as today.
- Missing `allowed_user_ids` with `interactive = true`: interactive mode is disabled and doctor reports a failing notification check.
- Telegram send failure: log debug/warning and keep the app running.
- Telegram alert send failure or missing `message_id`: discard the reserved alert token so `/status` cannot show phantom alerts.
- Prompt read timeout: send alert without prompt and keep `Read again` available.
- Bridge connection loss: reply with failure status, do not retry indefinitely.
- Reply `send_text` timeout or exception: reply in Telegram with a short delivery failure status.
- Unknown/stale callback: answer callback with a short stale message.

## Testing Strategy

Unit tests:

- config parsing for `message_thread_id`, `interactive`, `allowed_user_ids`, and `prompt_max_chars`;
- Telegram client request payloads for `sendMessage`, `getUpdates`, `answerCallbackQuery`, and `editMessageText`;
- formatter output and prompt truncation;
- alert store token/message lookup and expiry;
- inbound authorization and chat/topic filtering;
- callback action mapping to approve/deny/stop/read;
- reply-to-alert mapping to `send_text`;
- failure paths for stale alert, missing agent, missing allowed users, and read timeout.

Integration-style tests:

- fake `AgentControl` plus fake Telegram HTTP transport;
- blocked transition sends one rich alert and does not duplicate while the same agent remains blocked;
- reply text reaches only the mapped agent;
- callback approve remains guarded and is skipped if the agent is no longer blocked.

Docs and diagnostics:

- README documents interactive Telegram setup, topic id, allowed user ids, and security expectations.
- `herdeck-doctor` reports interactive readiness without showing secrets.

## Out Of Scope For V1

- One Telegram topic per agent.
- Persistent alert mapping across process restarts.
- Webhook mode.
- Full transcript mirroring from agent output back into Telegram.
- Multiple independent Telegram targets at once.
- Agent-initiated Telegram messages without first going through Herdeck policy.

## Implementation Shape

The implementation should be sliced so each commit is independently testable:

1. Config and doctor support for interactive Telegram fields.
2. Typed blocked-alert notifier boundary plus legacy sink adapter.
3. Telegram Bot API client, formatter, and alert store.
4. App-side request broker/control adapter for `read`, actions, and `send_text`.
5. Outbound rich alerts on blocked transition.
6. Inbound long-poll loop with button callbacks.
7. Reply-to-agent routing.
8. README and config example updates.

Each slice should use red-green-refactor, commit separately, then run `roborev show <sha>` with `roborev review <sha> --wait` as the fallback if no review is found yet.
