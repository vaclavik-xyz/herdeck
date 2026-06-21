# Telegram Notification Sink Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Telegram as a notification backend that runs alongside macOS, so blocked-agent alerts reach the phone over the internet (not just the Mac's Notification Center).

**Architecture:** Keep the existing sink abstraction (`Callable[[title, body, sound], None]`). Add a Telegram sink built from a token + chat_id and a composite sink that fans out to multiple backends with per-sink error isolation. `[notifications].backends` selects which run; a backend that can't be built (missing token/chat_id) is skipped with a logged warning.

**Tech Stack:** Python 3.12, stdlib `urllib` (HTTP), `tomllib` (config), pytest. No new runtime dependency.

## Global Constraints

- **No new runtime dependency** — Telegram uses `urllib` from the Python stdlib only.
- **Privacy:** notification text carries only repo/label, branch, and (multi-server) server id — never prompt text, command output, or tokens. The bot token is read from an env var and never logged or placed in a message body.
- **Graceful skip:** a Telegram backend with a missing token/chat_id must NOT crash the app or fail config load — it is skipped with a `log.warning`.
- **Backward compatible:** `backends` defaults to `["macos"]` when omitted, preserving existing configs.
- **Conventions:** code & commit messages in English; conventional-commit format; after each commit check `roborev show <sha>` (review runs async via the global post-commit hook — wait for it to finish, then fix any findings before moving on).

Reference spec: `docs/superpowers/specs/2026-06-21-telegram-notifications-design.md`

---

### Task 1: Telegram sink + composite sink

**Files:**
- Modify: `src/herdeck/notify.py`
- Test: `tests/test_notify.py`

**Interfaces:**
- Consumes: nothing new (existing `log` logger in `notify.py`).
- Produces:
  - `make_telegram_sink(token: str, chat_id: str, *, post=_http_post) -> Callable[[str, str, bool], None]`
  - `_http_post(url: str, fields: dict[str, str]) -> None`
  - `composite_sink(sinks: list[Callable[[str, str, bool], None]]) -> Callable[[str, str, bool], None]`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_notify.py`:

```python
def test_telegram_sink_builds_url_and_payload():
    from herdeck.notify import make_telegram_sink
    sent = []
    sink = make_telegram_sink("TOK", "42", post=lambda url, fields: sent.append((url, fields)))
    sink("Blocked", "api · main", True)
    url, fields = sent[0]
    assert url == "https://api.telegram.org/botTOK/sendMessage"
    assert fields["chat_id"] == "42"
    assert fields["text"] == "Blocked\napi · main"
    assert fields["disable_notification"] == "false"   # sound=True -> not silent


def test_telegram_sink_silent_when_no_sound():
    from herdeck.notify import make_telegram_sink
    sent = []
    sink = make_telegram_sink("TOK", "42", post=lambda url, fields: sent.append(fields))
    sink("t", "b", False)
    assert sent[0]["disable_notification"] == "true"


def test_composite_sink_calls_all_even_if_one_raises():
    from herdeck.notify import composite_sink
    calls = []
    def boom(*a):
        raise RuntimeError("x")
    sink = composite_sink([
        lambda t, b, s: calls.append(("a", t)),
        boom,
        lambda t, b, s: calls.append(("c", t)),
    ])
    sink("title", "body", True)
    assert calls == [("a", "title"), ("c", "title")]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_notify.py -v`
Expected: FAIL — `ImportError: cannot import name 'make_telegram_sink'` (and `composite_sink`).

- [ ] **Step 3: Implement the sinks**

In `src/herdeck/notify.py`, add the stdlib imports near the top (after the existing `import subprocess`):

```python
import urllib.parse
import urllib.request
```

Then add, after `_macos_sink` and before `class Notifier`:

```python
def _http_post(url: str, fields: dict[str, str]) -> None:
    data = urllib.parse.urlencode(fields).encode()
    with urllib.request.urlopen(url, data=data, timeout=5):
        pass


def make_telegram_sink(token, chat_id, *, post=_http_post):
    """Sink that posts the alert to a Telegram chat via the Bot API."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    def sink(title: str, body: str, sound: bool) -> None:
        post(url, {
            "chat_id": str(chat_id),
            "text": f"{title}\n{body}",
            "disable_notification": "false" if sound else "true",
        })

    return sink


def composite_sink(sinks):
    """Fan out to multiple sinks; one failing sink never stops the others."""
    def sink(title: str, body: str, sound: bool) -> None:
        for s in sinks:
            try:
                s(title, body, sound)
            except Exception:
                log.debug("notify sink failed", exc_info=True)

    return sink
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_notify.py -v`
Expected: PASS (all tests, including the pre-existing ones).

- [ ] **Step 5: Lint + commit**

```bash
ruff check src/herdeck/notify.py tests/test_notify.py
git add src/herdeck/notify.py tests/test_notify.py
git commit -m "feat(notify): telegram sink and composite multi-sink"
```

After the commit, wait for roborev (`roborev list` until the job shows `done`, then `roborev show <id>`), and fix any findings before continuing.

---

### Task 2: Config schema — `backends` + `[notifications.telegram]`

**Files:**
- Modify: `src/herdeck/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: existing `Notifications`, `load_config`; a new module logger `log`.
- Produces:
  - `TelegramConfig(token_env: str, chat_id: str)` dataclass.
  - `Notifications` gains `backends: list[str]` (default `["macos"]`) and `telegram: TelegramConfig | None` (default `None`).
  - `parse_notifications(n: dict) -> Notifications` — parses the `[notifications]` table; reused by `doctor`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py`:

```python
def test_notifications_backends_default_macos(tmp_path):
    cfg = load_config(_write(tmp_path, "[deck]\ngrid=\"5x3\"\n"))
    assert cfg.notifications.backends == ["macos"]
    assert cfg.notifications.telegram is None


def test_notifications_parses_telegram_and_backends(tmp_path):
    cfg = load_config(_write(tmp_path,
        "[notifications]\nenabled=true\nbackends=[\"macos\",\"telegram\"]\n"
        "[notifications.telegram]\ntoken_env=\"HERDECK_TG\"\nchat_id=123\n"))
    assert cfg.notifications.backends == ["macos", "telegram"]
    assert cfg.notifications.telegram.token_env == "HERDECK_TG"
    assert cfg.notifications.telegram.chat_id == "123"   # coerced to str


def test_notifications_telegram_incomplete_is_skipped(tmp_path):
    # Incomplete telegram table never fails config load (graceful skip);
    # _build_notifier / doctor surface it later.
    cfg = load_config(_write(tmp_path,
        "[notifications]\nenabled=true\nbackends=[\"telegram\"]\n"
        "[notifications.telegram]\nchat_id=123\n"))   # no token_env
    assert cfg.notifications.telegram is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_config.py -k notifications -v`
Expected: FAIL — `AttributeError: 'Notifications' object has no attribute 'backends'` (the new attribute does not exist yet).

- [ ] **Step 3: Implement the schema + parser**

In `src/herdeck/config.py`, add the dataclass after `Macro` (before `Notifications`):

```python
@dataclass
class TelegramConfig:
    token_env: str     # env var holding the bot token (never the token itself)
    chat_id: str       # target chat (not secret)
```

Extend `Notifications`:

```python
@dataclass
class Notifications:
    enabled: bool = False
    on: list[str] = field(default_factory=lambda: ["blocked"])
    sound: bool = True
    backends: list[str] = field(default_factory=lambda: ["macos"])
    telegram: TelegramConfig | None = None
```

Add a module logger near the top of `src/herdeck/config.py` (after the existing
imports, before `class ConfigError`):

```python
import logging

log = logging.getLogger("herdeck.config")
```

Add the parser (place it just above `def load_config`). An incomplete telegram
table is **ignored with a warning** (graceful skip) — config load never fails
over a notification setting:

```python
def parse_notifications(n: dict) -> Notifications:
    tg_raw = n.get("telegram")
    telegram = None
    if isinstance(tg_raw, dict):
        if "token_env" in tg_raw and "chat_id" in tg_raw:
            telegram = TelegramConfig(token_env=tg_raw["token_env"],
                                      chat_id=str(tg_raw["chat_id"]))
        else:
            log.warning("[notifications.telegram] needs both token_env and "
                        "chat_id; ignoring telegram config")
    return Notifications(
        enabled=n.get("enabled", False),
        on=list(n.get("on", ["blocked"])),
        sound=n.get("sound", True),
        backends=list(n.get("backends", ["macos"])),
        telegram=telegram,
    )
```

In `load_config`, replace the inline notifications block:

```python
    n = data.get("notifications", {})
    notifications = Notifications(
        enabled=n.get("enabled", False),
        on=list(n.get("on", ["blocked"])),
        sound=n.get("sound", True),
    )
```

with:

```python
    notifications = parse_notifications(data.get("notifications", {}))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS (new tests + all pre-existing config tests, including `test_notifications_disabled_by_default` / sound parsing).

- [ ] **Step 5: Lint + commit**

```bash
ruff check src/herdeck/config.py tests/test_config.py
git add src/herdeck/config.py tests/test_config.py
git commit -m "feat(config): notifications backends list and telegram section"
```

After the commit, wait for roborev to finish and fix any findings before continuing.

---

### Task 3: `_build_notifier` assembles backends with graceful skip

**Files:**
- Modify: `src/herdeck/app.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `make_telegram_sink`, `composite_sink`, `_macos_sink` (Task 1); `Notifications`/`TelegramConfig` (Task 2).
- Produces: `_build_notifier(config, *, getenv=os.environ.get, macos_sink=_macos_sink, telegram_factory=make_telegram_sink) -> Notifier` — keeps taking the full `Config` (existing call site & tests pass `config`), gains injectable builders.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_app.py` (the existing `test_build_notifier_respects_config` stays as-is):

```python
def test_build_notifier_fires_both_backends():
    from herdeck.app import _build_notifier
    from herdeck.config import TelegramConfig
    calls = []
    cfg = make_config()
    cfg.notifications.enabled = True
    cfg.notifications.backends = ["macos", "telegram"]
    cfg.notifications.telegram = TelegramConfig("HERDECK_TG", "42")
    rec_macos = lambda t, b, s: calls.append(("macos", t))
    rec_tg = lambda t, b, s: calls.append(("telegram", t))
    n = _build_notifier(cfg, getenv=lambda k: "TOK",
                        macos_sink=rec_macos,
                        telegram_factory=lambda tok, cid: rec_tg)
    n.notify("title", "body", False)
    assert ("macos", "title") in calls and ("telegram", "title") in calls


def test_build_notifier_skips_telegram_without_token():
    from herdeck.app import _build_notifier
    from herdeck.config import TelegramConfig
    from herdeck.notify import NoopNotifier
    cfg = make_config()
    cfg.notifications.enabled = True
    cfg.notifications.backends = ["telegram"]
    cfg.notifications.telegram = TelegramConfig("HERDECK_TG", "42")
    n = _build_notifier(cfg, getenv=lambda k: None)   # token env unset
    assert isinstance(n, NoopNotifier)


def test_build_notifier_skips_telegram_without_config():
    from herdeck.app import _build_notifier
    from herdeck.notify import NoopNotifier
    cfg = make_config()
    cfg.notifications.enabled = True
    cfg.notifications.backends = ["telegram"]   # telegram is None
    n = _build_notifier(cfg, getenv=lambda k: "TOK")
    assert isinstance(n, NoopNotifier)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_app.py -k build_notifier -v`
Expected: FAIL — `TypeError: _build_notifier() got an unexpected keyword argument 'getenv'`.

- [ ] **Step 3: Implement**

In `src/herdeck/app.py`, add `import os` to the imports at the top (after `import logging`).

Update the notify import line:

```python
from .notify import (
    NoopNotifier, Notifier, _macos_sink, composite_sink, make_telegram_sink,
)
```

Replace `_build_notifier`:

```python
def _build_notifier(config: Config, *, getenv=os.environ.get,
                    macos_sink=_macos_sink,
                    telegram_factory=make_telegram_sink) -> Notifier:
    """Assemble a notifier from the configured backends (graceful skip)."""
    n = config.notifications
    if not n.enabled:
        return NoopNotifier()
    sinks = []
    for backend in n.backends:
        if backend == "macos":
            sinks.append(macos_sink)
        elif backend == "telegram":
            tg = n.telegram
            token = getenv(tg.token_env) if tg else None
            if tg and token and tg.chat_id:
                sinks.append(telegram_factory(token, tg.chat_id))
            else:
                log.warning("telegram notifications enabled but token/chat_id "
                            "missing; skipping telegram backend")
        else:
            log.warning("unknown notification backend %r; skipping", backend)
    if not sinks:
        return NoopNotifier()
    return Notifier(sink=composite_sink(sinks))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_app.py -v`
Expected: PASS (new tests + existing `test_build_notifier_respects_config` and the block-transition tests).

- [ ] **Step 5: Lint + commit**

```bash
ruff check src/herdeck/app.py tests/test_app.py
git add src/herdeck/app.py tests/test_app.py
git commit -m "feat(app): assemble notifier from backends with graceful skip"
```

After the commit, wait for roborev to finish and fix any findings before continuing.

---

### Task 4: `herdeck-doctor` notifications check

**Files:**
- Modify: `src/herdeck/doctor.py`
- Test: `tests/test_doctor.py`

**Interfaces:**
- Consumes: `Notifications`/`TelegramConfig` and `parse_notifications` (Task 2); existing `Check` dataclass and `collect_checks`.
- Produces: `check_notifications(notifications, getenv=os.environ.get) -> Check`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_doctor.py`:

```python
def test_check_notifications_disabled():
    from herdeck.config import Notifications
    from herdeck.doctor import check_notifications
    c = check_notifications(Notifications(enabled=False))
    assert c.ok is True and "disabled" in c.detail.lower()


def test_check_notifications_telegram_present_redacts():
    from herdeck.config import Notifications, TelegramConfig
    from herdeck.doctor import check_notifications
    n = Notifications(enabled=True, backends=["macos", "telegram"],
                      telegram=TelegramConfig("HERDECK_TG", "42"))
    c = check_notifications(n, getenv=lambda k: "SECRET-TOKEN-VALUE")
    assert c.ok is True
    assert "token_env=present" in c.detail and "chat_id=present" in c.detail
    assert "SECRET-TOKEN-VALUE" not in c.detail   # never leak the value


def test_check_notifications_telegram_missing_token_fails():
    from herdeck.config import Notifications, TelegramConfig
    from herdeck.doctor import check_notifications
    n = Notifications(enabled=True, backends=["telegram"],
                      telegram=TelegramConfig("HERDECK_TG", "42"))
    c = check_notifications(n, getenv=lambda k: None)
    assert c.ok is False and "token_env=missing" in c.detail
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_doctor.py -k notifications -v`
Expected: FAIL — `ImportError: cannot import name 'check_notifications'`.

- [ ] **Step 3: Implement the check + wire into `collect_checks`**

In `src/herdeck/doctor.py`, add after `check_deck`:

```python
def check_notifications(notifications, getenv=os.environ.get) -> Check:
    if not notifications.enabled:
        return Check("notifications", True, "disabled")
    parts = [f"backends={','.join(notifications.backends)}"]
    ok = True
    if "telegram" in notifications.backends:
        tg = notifications.telegram
        if tg is None:
            parts.append("telegram=no usable [notifications.telegram] "
                         "(need token_env + chat_id)")
            ok = False
        else:
            token_present = bool(getenv(tg.token_env))
            chat_present = bool(tg.chat_id)
            parts.append(f"token_env={'present' if token_present else 'missing'}")
            parts.append(f"chat_id={'present' if chat_present else 'missing'}")
            if not (token_present and chat_present):
                ok = False
    return Check("notifications", ok, "; ".join(parts))


def _read_notifications(config_path):
    from .config import Notifications, parse_notifications
    if config_path is None:
        return Notifications()
    try:
        data = tomllib.loads(Path(config_path).read_text())
        return parse_notifications(data.get("notifications", {}))
    except Exception:
        return Notifications()
```

In `collect_checks`, append the notifications check to the `checks` list (after `check_deck(_module_available)`):

```python
        check_deck(_module_available),
        check_notifications(_read_notifications(config_path)),
    ]
    return checks
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_doctor.py -v`
Expected: PASS (new tests + all pre-existing doctor tests).

- [ ] **Step 5: Lint + commit**

```bash
ruff check src/herdeck/doctor.py tests/test_doctor.py
git add src/herdeck/doctor.py tests/test_doctor.py
git commit -m "feat(doctor): notifications check with redacted telegram status"
```

After the commit, wait for roborev to finish and fix any findings before continuing.

---

### Task 5: Docs — config example + README

**Files:**
- Modify: `config.example.toml`
- Modify: `README.md`
- Test: `tests/test_config.py` (one assertion guarding the example)

**Interfaces:**
- Consumes: `load_config` (the example must still parse).
- Produces: documentation only.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_example_notifications_backends_default(monkeypatch):
    monkeypatch.setenv("HERDECK_WORKBOX_TOKEN", "secret123")
    path = Path(__file__).resolve().parents[1] / "config.example.toml"
    cfg = load_config(path)
    assert "macos" in cfg.notifications.backends
```

- [ ] **Step 2: Run test to verify it passes already, then make it meaningful**

Run: `python -m pytest tests/test_config.py::test_example_notifications_backends_default -v`
Expected: PASS (default `["macos"]` already holds). This test guards that editing the example never breaks parsing or drops macOS; keep it.

- [ ] **Step 3: Update `config.example.toml`**

Replace the existing `[notifications]` block with:

```toml
# Out-of-band alerts so you don't have to watch the deck. Omit this section to
# disable entirely. macOS uses osascript notifications; telegram delivers to your
# phone via a bot (token from an env var, chat_id below). Run both at once.
[notifications]
enabled = false                    # set true to be notified when an agent needs you
backends = ["macos"]               # any of: "macos", "telegram" (e.g. both)
on = ["blocked"]                   # transitions to alert on (currently only "blocked")
sound = true                       # macOS plays a sound; telegram sends non-silent

# Telegram backend (only used when "telegram" is in backends above):
#   1. Create a bot with @BotFather, copy its token.
#   2. export HERDECK_TELEGRAM_TOKEN=<token>   (never put the token in this file)
#   3. Put your numeric chat_id below.
# A missing token/chat_id makes herdeck skip telegram with a warning (macOS still fires).
# [notifications.telegram]
# token_env = "HERDECK_TELEGRAM_TOKEN"
# chat_id = "123456789"
```

- [ ] **Step 4: Update `README.md` Notifications section**

Replace the body of the `## Notifications` section with (outer fence is four
backticks so the nested TOML block renders correctly):

````markdown
Get notified when an agent enters the **blocked** state, so you don't have to
watch the deck. Off by default; enable in your config and pick one or more
backends:
```toml
[notifications]
enabled = true
backends = ["macos", "telegram"]   # run both, or just one
on = ["blocked"]
sound = true

# Only needed when "telegram" is a backend:
[notifications.telegram]
token_env = "HERDECK_TELEGRAM_TOKEN"   # bot token read from this env var
chat_id = "123456789"
```
- **macOS** posts to Notification Center (osascript). **Telegram** delivers to
  your phone via the Bot API over HTTPS (stdlib only, no extra dependency) —
  useful when you drive herdeck from the phone over Tailscale.
- Telegram setup: create a bot with @BotFather, `export HERDECK_TELEGRAM_TOKEN=<token>`
  (never commit the token), and set your numeric `chat_id`. A missing token or
  chat_id makes herdeck skip telegram with a warning — other backends still fire.
- Notifications contain only the repo/label, branch, and (multi-server) server id
  — never prompt text, command output, or tokens. They fire once per blocked
  episode (re-arming after the agent leaves `blocked`) and never block the UI loop.
````

- [ ] **Step 5: Run the full suite + commit**

```bash
python -m pytest -q
ruff check src tests
git add config.example.toml README.md tests/test_config.py
git commit -m "docs: document telegram notification backend"
```

After the commit, wait for roborev to finish and fix any findings before continuing.

---

## Verification (after all tasks)

- [ ] `python -m pytest -q` — all tests pass.
- [ ] `ruff check src tests` — clean.
- [ ] `git log --oneline` shows the five feature/docs commits.
- [ ] Each commit's roborev review is `done` with no open findings.
