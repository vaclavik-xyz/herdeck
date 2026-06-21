# Telegram notification sink — design

- **Date:** 2026-06-21
- **Status:** Approved (brainstorming)

## Goal

Add Telegram as a notification backend alongside the existing macOS one, with
**both enabled at once**. macOS notifications land in the Mac's Notification
Center; herdeck is driven from the phone (web simulator over Tailscale), so a
Telegram message is what actually reaches the user wherever they are.

## Scope

The notification path only: `notify.py` (sinks), `config.py` (schema),
`app._build_notifier` (assembly), `doctor.py` (diagnostics), plus
`config.example.toml` and `README.md`. The blocked-transition detection
(`app.newly_blocked` / `_maybe_notify`) and notification *content* (already
redacted to repo/label/branch/server) are unchanged. No new runtime dependency —
Telegram uses `urllib` from the stdlib.

## Approach

Keep the existing sink abstraction (`Callable[[title, body, sound], None]`). Add
a Telegram sink built from a token + chat_id, and a **composite sink** that
fans out to multiple backends and isolates each backend's failures. Backends are
selected by a `backends` list in config. When a backend can't be built (e.g.
Telegram token/chat_id missing) it is **skipped with a logged warning** —
notifications are non-critical and must never crash the app or block the UI loop.

## Config schema

```toml
[notifications]
enabled = true
backends = ["macos", "telegram"]   # run both; default ["macos"] if omitted
on = ["blocked"]
sound = true

[notifications.telegram]
token_env = "HERDECK_TELEGRAM_TOKEN"   # bot token read from THIS env var
chat_id = "123456789"                  # target chat (not a secret)
```

- `backends` defaults to `["macos"]` when omitted, preserving current behaviour
  for existing configs.
- The bot **token is never stored in the file** — only the env var *name*
  (`token_env`) is. `chat_id` is not secret and lives in the file.
- `[notifications.telegram]` is optional; absent → `telegram` config is `None`
  (and a `telegram` backend, if listed, is skipped with a warning).

## Components

### `notify.py`

- `make_telegram_sink(token: str, chat_id: str, *, post=_http_post) -> Callable[[str, str, bool], None]`
  — returns a sink closure. `post` is injectable for tests.
- `_http_post(url: str, fields: dict[str, str]) -> None` — `urllib.request`
  POST, 5 s timeout. Network/HTTP errors propagate to the caller (the composite
  isolates them).
- The Telegram sink POSTs to
  `https://api.telegram.org/bot{token}/sendMessage` with fields
  `chat_id`, `text = f"{title}\n{body}"`, and
  `disable_notification = "true" if not sound else "false"` (sound maps to a
  *non-silent* Telegram message).
- `composite_sink(sinks: list[Callable[[str, str, bool], None]]) -> Callable[[str, str, bool], None]`
  — calls each sink inside its own `try/except`; a failing sink logs at debug
  and does not stop the others.
- `_macos_sink`, `escape_applescript`, `Notifier`, `NoopNotifier` unchanged.

### `config.py`

- New dataclass:
  ```python
  @dataclass
  class TelegramConfig:
      token_env: str
      chat_id: str
  ```
- `Notifications` gains:
  ```python
  backends: list[str] = field(default_factory=lambda: ["macos"])
  telegram: TelegramConfig | None = None
  ```
- `load_config` parses `backends` (default `["macos"]`) and, when present, a
  `[notifications.telegram]` table into `TelegramConfig`. If the table is present
  but missing `token_env` or `chat_id`, it is **ignored with a logged warning**
  (`telegram = None`) — config load never fails over a notification setting
  (graceful skip). The token value itself is **not** resolved at load time — it
  is resolved lazily when the notifier is built, so a missing env var also
  degrades gracefully.

### `app._build_notifier`

Signature gains injectable builders (matching the codebase's DI style —
`make_deck`, `resolve_mode`), so tests verify composition without touching
subprocess/network:

```python
def _build_notifier(notifications, *, getenv=os.environ.get,
                    macos_sink=_macos_sink,
                    telegram_factory=make_telegram_sink) -> Notifier: ...
```

- If `not notifications.enabled` → `NoopNotifier`.
- For each name in `notifications.backends`, build a sink:
  - `"macos"` → `_macos_sink`.
  - `"telegram"` → resolve `token = getenv(notifications.telegram.token_env)`
    when `notifications.telegram` is set; if both `token` and `chat_id` are
    present → `telegram_factory(token, chat_id)`; otherwise **log a warning and
    skip** this backend.
  - unknown name → log a warning and skip.
- If no sinks remain → `NoopNotifier`. Otherwise
  `Notifier(sink=composite_sink(sinks))`.

### `doctor.py`

- `check_notifications(notifications, getenv=os.environ.get) -> Check`:
  - disabled → `ok`, detail `"disabled"`.
  - enabled → detail lists `backends`; for a `telegram` backend report
    `token_env=present|missing` (redacted — never the value) and
    `chat_id=present|missing`. `ok` is `False` if `telegram` is listed but its
    token env or chat_id is missing (the backend would be skipped at runtime);
    otherwise `True`.
- Wired into `collect_checks`: load the parsed `Notifications` from the config
  (when a config path exists and parses) and append the check. If config is
  absent/invalid, append a check with detail `"no config; notifications off"`
  (`ok=True`).

## Data flow

```
blocked transition → app._maybe_notify → Notifier.notify(title, body, sound)
  → composite_sink → [ _macos_sink (osascript),
                       telegram sink (HTTPS POST → phone) ]   (each isolated)
```

`Notifier.notify` already runs via `notify_schedule` (executor), so the 5 s
Telegram POST never blocks the asyncio UI loop.

## Error handling

- Per-sink `try/except` inside `composite_sink`: one backend failing (network
  down, osascript missing) never stops the others.
- `Notifier.notify` swallows all exceptions as a second layer.
- `_http_post` uses a 5 s timeout.
- Missing/empty Telegram token or chat_id at build time → warning + skip.

## Privacy

The Telegram message text is the **same already-redacted content** as macOS:
repo/label, branch, and (multi-server) server id. It never contains prompt text,
command output, or tokens. The bot token is read from an env var and never
logged or sent in message bodies.

## Testing (TDD)

- `tests/test_notify.py`:
  - `make_telegram_sink` with an injected `post` → URL is
    `https://api.telegram.org/bot<token>/sendMessage`; fields carry `chat_id`,
    `text == f"{title}\n{body}"`, and `disable_notification == "true"` when
    `sound=False`, `"false"` when `sound=True`.
  - `composite_sink([a, raises, b])` calls `a` and `b` even though the middle
    sink raises (record calls via a list).
- `tests/test_config.py`:
  - `backends` defaults to `["macos"]` when the section omits it.
  - explicit `backends = ["macos", "telegram"]` parses through.
  - `[notifications.telegram]` parses into `TelegramConfig(token_env, chat_id)`;
    absent table → `telegram is None`.
  - `[notifications.telegram]` missing `token_env` or `chat_id` → `telegram is
    None` (warned, not raised — graceful skip).
- `tests/test_app.py` (`_build_notifier`):
  - `enabled=False` → `NoopNotifier`.
  - `backends=["macos","telegram"]`, telegram config set, fake `getenv` returns
    a token, inject recording `macos_sink` + `telegram_factory` → calling
    `notifier.notify("t","b",False)` records the call in **both** sinks.
  - `backends=["telegram"]`, telegram config set but fake `getenv` returns
    `None` → `NoopNotifier` (telegram skipped).
  - `backends=["telegram"]`, `telegram is None` → `NoopNotifier` (skipped).
- `tests/test_doctor.py`:
  - disabled → `ok`, detail contains `disabled`.
  - telegram listed, token env present + chat_id present → `ok=True`, detail
    shows `token_env=present`, `chat_id=present`, never the token value.
  - telegram listed, token env missing → `ok=False`, detail `token_env=missing`.

## Out of scope

- Other backends (Slack, email, ntfy) — the composite design leaves room, but
  only macOS + Telegram ship now.
- A setup helper to discover `chat_id` — user already has token + chat_id.
- Per-backend `on`/`sound` overrides — a single `on`/`sound` applies to all
  backends.
- Telegram message formatting (Markdown/HTML), inline buttons, or two-way
  control (approve/deny from Telegram) — notifications are one-way only.
