# Telegram Interactive Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build interactive Telegram notifications for blocked agents: rich prompt alerts, inline approve/deny/stop/read buttons, and reply-to-agent text routing in one configured chat/topic.

**Architecture:** Keep Telegram interaction in the main Herdeck runtime, behind a typed blocked-alert notifier that receives `AgentState`. Legacy macOS and one-way Telegram notifications stay as low-level sinks adapted from the richer event. Interactive Telegram uses a small stdlib Bot API client, in-memory alert correlation, and a runtime command broker that sends existing `Command` messages without touching deck prompt-read state.

**Tech Stack:** Python 3.12+, stdlib `urllib`, existing `websockets`, existing Herdeck `Connector`/`Command` protocol, pytest, Ruff, Roborev.

---

## Preflight

Worktree:

```bash
cd /Users/admin/.herdr/worktrees/herdeck/worktree-telegram-interactive-notifications
git status --short --branch
```

Expected branch:

```text
## feat/telegram-interactive-notifications
```

The worktree does not currently contain `.venv`. Create one before implementing:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

Expected baseline after setup:

```text
574 passed
```

The baseline was already verified once from this worktree using the main checkout venv:

```text
574 passed in 14.99s
```

## File Structure

- Modify: `src/herdeck/config.py`
  - Extend `TelegramConfig`.
  - Keep legacy `parse_notifications()` behavior for flat config.
- Modify: `src/herdeck/settings.py`
  - Parse the same Telegram fields through the unified profile/overlay settings path.
  - Expose resolved notifications without requiring server token secrets.
- Modify: `src/herdeck/doctor.py`
  - Report interactive readiness and missing `allowed_user_ids` without leaking token values.
- Modify: `src/herdeck/notify.py`
  - Add typed blocked-alert notifier adapters.
  - Keep `Notifier`, `NoopNotifier`, `make_telegram_sink`, and `composite_sink` as low-level sink APIs.
- Modify: `src/herdeck/app.py`
  - Route blocked transitions through typed notifier.
  - Add a runtime command broker/control adapter for Telegram reads/actions/text.
  - Start and stop interactive Telegram polling in remote runtime.
- Create: `src/herdeck/telegram.py`
  - Telegram Bot API client.
  - Alert formatter.
  - Alert store.
  - Interactor for outbound alerts, callbacks, replies, and `/status`.
- Modify: `config.example.toml`
  - Document optional interactive topic fields.
- Modify: `README.md`
  - Document setup, allowed users, topic id, and security behavior.
- Test: `tests/test_config.py`
- Test: `tests/test_settings.py`
- Test: `tests/test_doctor.py`
- Test: `tests/test_notify.py`
- Test: `tests/test_app.py`
- Create: `tests/test_app_control.py`
- Create: `tests/test_telegram.py`

---

### Task 1: Config And Doctor Support

**Files:**
- Modify: `src/herdeck/config.py:35-39`
- Modify: `src/herdeck/config.py:176-193`
- Modify: `src/herdeck/settings.py:217-229`
- Modify: `src/herdeck/doctor.py:110-140`
- Test: `tests/test_config.py`
- Test: `tests/test_doctor.py`

- [ ] **Step 1: Write failing config tests**

Add these tests near the existing notification tests in `tests/test_config.py`:

```python
def test_notifications_parses_interactive_telegram_fields(tmp_path):
    cfg = load_config(
        _write(
            tmp_path,
            '[notifications]\n'
            'enabled=true\n'
            'backends=["telegram"]\n'
            '[notifications.telegram]\n'
            'token_env="HERDECK_TG"\n'
            'chat_id="-100123"\n'
            "message_thread_id=456\n"
            "interactive=true\n"
            "allowed_user_ids=[111, 222]\n"
            "prompt_max_chars=777\n",
        )
    )

    tg = cfg.notifications.telegram
    assert tg is not None
    assert tg.token_env == "HERDECK_TG"
    assert tg.chat_id == "-100123"
    assert tg.message_thread_id == 456
    assert tg.interactive is True
    assert tg.allowed_user_ids == [111, 222]
    assert tg.prompt_max_chars == 777


def test_notifications_interactive_defaults_are_safe(tmp_path):
    cfg = load_config(
        _write(
            tmp_path,
            '[notifications]\n'
            'enabled=true\n'
            'backends=["telegram"]\n'
            '[notifications.telegram]\n'
            'token_env="HERDECK_TG"\n'
            'chat_id="42"\n',
        )
    )

    tg = cfg.notifications.telegram
    assert tg is not None
    assert tg.message_thread_id is None
    assert tg.interactive is False
    assert tg.allowed_user_ids == []
    assert tg.prompt_max_chars == 1200


def test_profile_notifications_parse_interactive_telegram_fields(tmp_path):
    cfg = load_config(
        _write(
            tmp_path,
            'active_profile="work"\n'
            '[deck]\n'
            'grid="5x3"\n'
            '[profiles.work]\n'
            'servers=[]\n'
            '[profiles.work.notifications]\n'
            'enabled=true\n'
            'backends=["telegram"]\n'
            '[profiles.work.notifications.telegram]\n'
            'token_env="HERDECK_TG"\n'
            'chat_id="-100123"\n'
            "message_thread_id=456\n"
            "interactive=true\n"
            "allowed_user_ids=[111, 222]\n"
            "prompt_max_chars=777\n",
        )
    )

    tg = cfg.notifications.telegram
    assert cfg.meta.active_profile == "work"
    assert cfg.notifications.enabled is True
    assert tg is not None
    assert tg.message_thread_id == 456
    assert tg.interactive is True
    assert tg.allowed_user_ids == [111, 222]
    assert tg.prompt_max_chars == 777
```

- [ ] **Step 2: Write failing doctor tests**

Add these tests near `test_check_notifications_telegram_present_redacts` in `tests/test_doctor.py`:

```python
def test_check_notifications_interactive_requires_allowed_users():
    from herdeck.config import Notifications, TelegramConfig
    from herdeck.doctor import check_notifications

    n = Notifications(
        enabled=True,
        backends=["telegram"],
        telegram=TelegramConfig("HERDECK_TG", "42", interactive=True),
    )

    c = check_notifications(n, getenv=lambda k: "SECRET-TOKEN-VALUE")

    assert c.ok is False
    assert "interactive=missing allowed_user_ids" in c.detail
    assert "no usable backend" not in c.detail
    assert "SECRET-TOKEN-VALUE" not in c.detail


def test_check_notifications_interactive_ready_redacts():
    from herdeck.config import Notifications, TelegramConfig
    from herdeck.doctor import check_notifications

    n = Notifications(
        enabled=True,
        backends=["telegram"],
        telegram=TelegramConfig(
            "HERDECK_TG",
            "-100123",
            message_thread_id=456,
            interactive=True,
            allowed_user_ids=[111],
        ),
    )

    c = check_notifications(n, getenv=lambda k: "SECRET-TOKEN-VALUE")

    assert c.ok is True
    assert "interactive=ready" in c.detail
    assert "topic=present" in c.detail
    assert "SECRET-TOKEN-VALUE" not in c.detail


def test_collect_checks_resolves_active_profile_notifications(tmp_path, monkeypatch):
    from herdeck.doctor import collect_checks

    config = tmp_path / "config.toml"
    config.write_text(
        'active_profile="work"\n'
        '[deck]\n'
        'grid="5x3"\n'
        '[profiles.work]\n'
        'servers=[]\n'
        '[profiles.work.notifications]\n'
        'enabled=true\n'
        'backends=["telegram"]\n'
        '[profiles.work.notifications.telegram]\n'
        'token_env="HERDECK_TG"\n'
        'chat_id="-100123"\n'
        "message_thread_id=456\n"
        "interactive=true\n"
        "allowed_user_ids=[111]\n"
    )
    monkeypatch.setenv("HERDECK_CONFIG", str(config))
    monkeypatch.setenv("HERDECK_TG", "SECRET-TOKEN-VALUE")
    monkeypatch.setenv("HERDR_SOCKET", str(tmp_path / "missing.sock"))

    checks = {check.name: check for check in collect_checks()}

    assert checks["notifications"].ok is True
    assert "interactive=ready" in checks["notifications"].detail
    assert "topic=present" in checks["notifications"].detail
    assert "SECRET-TOKEN-VALUE" not in checks["notifications"].detail


def test_collect_checks_reports_notifications_when_server_token_missing(tmp_path, monkeypatch):
    from herdeck.doctor import collect_checks

    config = tmp_path / "config.toml"
    config.write_text(
        'active_profile="work"\n'
        "[[servers]]\n"
        'id="remote"\n'
        'url="wss://remote.example.test"\n'
        'token_env="MISSING_SERVER_TOKEN"\n'
        '[deck]\n'
        'grid="5x3"\n'
        '[profiles.work]\n'
        'servers=["remote"]\n'
        '[profiles.work.notifications]\n'
        'enabled=true\n'
        'backends=["telegram"]\n'
        '[profiles.work.notifications.telegram]\n'
        'token_env="HERDECK_TG"\n'
        'chat_id="-100123"\n'
        "interactive=true\n"
    )
    monkeypatch.setenv("HERDECK_CONFIG", str(config))
    monkeypatch.delenv("MISSING_SERVER_TOKEN", raising=False)
    monkeypatch.setenv("HERDECK_TG", "SECRET-TOKEN-VALUE")
    monkeypatch.setenv("HERDR_SOCKET", str(tmp_path / "missing.sock"))

    checks = {check.name: check for check in collect_checks()}

    assert checks["configuration"].ok is False
    assert "MISSING_SERVER_TOKEN=missing" in checks["configuration"].detail
    assert checks["notifications"].ok is False
    assert "interactive=missing allowed_user_ids" in checks["notifications"].detail
    assert "disabled" not in checks["notifications"].detail
    assert "SECRET-TOKEN-VALUE" not in checks["notifications"].detail
```

- [ ] **Step 3: Run red tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_config.py::test_notifications_parses_interactive_telegram_fields \
  tests/test_config.py::test_notifications_interactive_defaults_are_safe \
  tests/test_config.py::test_profile_notifications_parse_interactive_telegram_fields \
  tests/test_doctor.py::test_check_notifications_interactive_requires_allowed_users \
  tests/test_doctor.py::test_check_notifications_interactive_ready_redacts \
  tests/test_doctor.py::test_collect_checks_resolves_active_profile_notifications \
  tests/test_doctor.py::test_collect_checks_reports_notifications_when_server_token_missing \
  -q
```

Expected: FAIL because `TelegramConfig` has no `message_thread_id`, `interactive`, `allowed_user_ids`, or `prompt_max_chars` fields yet, and doctor does not yet resolve profile notifications independently of server token secrets.

- [ ] **Step 4: Implement config parsing**

In `src/herdeck/config.py`, change `TelegramConfig` to:

```python
@dataclass
class TelegramConfig:
    token_env: str  # env var holding the bot token (never the token itself)
    chat_id: str  # target chat (not secret)
    message_thread_id: int | None = None  # optional Telegram forum topic id
    interactive: bool = False
    allowed_user_ids: list[int] = field(default_factory=list)
    prompt_max_chars: int = 1200
```

Add this helper above `parse_notifications()` in `src/herdeck/config.py`:

```python
def _parse_telegram_config(tg_raw: dict) -> TelegramConfig | None:
    if "token_env" not in tg_raw or "chat_id" not in tg_raw:
        log.warning(
            "[notifications.telegram] needs both token_env and "
            "chat_id; ignoring telegram config"
        )
        return None
    thread = tg_raw.get("message_thread_id")
    return TelegramConfig(
        token_env=tg_raw["token_env"],
        chat_id=str(tg_raw["chat_id"]),
        message_thread_id=int(thread) if thread is not None else None,
        interactive=bool(tg_raw.get("interactive", False)),
        allowed_user_ids=[int(v) for v in tg_raw.get("allowed_user_ids", [])],
        prompt_max_chars=int(tg_raw.get("prompt_max_chars", 1200)),
    )
```

Replace the body of `parse_notifications()`'s Telegram parsing with:

```python
def parse_notifications(n: dict) -> Notifications:
    tg_raw = n.get("telegram")
    telegram = _parse_telegram_config(tg_raw) if isinstance(tg_raw, dict) else None
    return Notifications(
        enabled=n.get("enabled", False),
        on=list(n.get("on", ["blocked"])),
        sound=n.get("sound", True),
        backends=list(n.get("backends", ["macos"])),
        telegram=telegram,
    )
```

In `src/herdeck/settings.py`, import `_parse_telegram_config` from `.config` and update `_notifications_config()`:

```python
def _notifications_config(raw: dict | None) -> Notifications:
    raw = raw or {}
    telegram = None
    tg_raw = raw.get("telegram")
    if isinstance(tg_raw, dict):
        telegram = _parse_telegram_config(tg_raw)
    return Notifications(
        enabled=raw.get("enabled", False),
        on=list(raw.get("on", ["blocked"])),
        sound=raw.get("sound", True),
        backends=list(raw.get("backends", ["macos"])),
        telegram=telegram,
    )
```

Add this helper below `_notifications_config()` in `src/herdeck/settings.py`:

```python
def resolve_notifications(snapshot: SettingsSnapshot) -> Notifications:
    active = _active_profile_name(snapshot)
    merged, _selection = _merged_sections(snapshot.data, active)
    return _notifications_config(merged.get("notifications"))
```

- [ ] **Step 5: Implement doctor checks**

In `src/herdeck/doctor.py`, update `_read_notifications()` so doctor resolves the active profile's notification section without validating server token secrets:

```python
def _read_notifications(config_path: str | None) -> Notifications:
    if config_path is None:
        return Notifications()
    try:
        from .bootstrap import _discover_local_config_path
        from .settings import load_settings, resolve_notifications

        snapshot = load_settings(config_path, _discover_local_config_path(config_path))
        return resolve_notifications(snapshot)
    except Exception:
        return Notifications()
```

Then extend the `telegram` branch inside `check_notifications()`:

```python
if token_present and chat_present:
    usable += 1
    if tg.interactive:
        if tg.allowed_user_ids:
            parts.append("interactive=ready")
            parts.append("topic=present" if tg.message_thread_id is not None else "topic=absent")
        else:
            parts.append("interactive=missing allowed_user_ids")
            ok = False
else:
    ok = False
```

Keep the existing `token_env=present/missing` and `chat_id=present/missing` details. Do not print user ids or token values.

- [ ] **Step 6: Run green tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_config.py::test_notifications_parses_interactive_telegram_fields \
  tests/test_config.py::test_notifications_interactive_defaults_are_safe \
  tests/test_config.py::test_profile_notifications_parse_interactive_telegram_fields \
  tests/test_config.py::test_notifications_parses_telegram_and_backends \
  tests/test_config.py::test_notifications_telegram_incomplete_is_skipped \
  tests/test_doctor.py::test_check_notifications_telegram_present_redacts \
  tests/test_doctor.py::test_check_notifications_interactive_requires_allowed_users \
  tests/test_doctor.py::test_check_notifications_interactive_ready_redacts \
  tests/test_doctor.py::test_collect_checks_resolves_active_profile_notifications \
  tests/test_doctor.py::test_collect_checks_reports_notifications_when_server_token_missing \
  -q
```

Expected: PASS.

- [ ] **Step 7: Commit and Roborev**

```bash
git add src/herdeck/config.py src/herdeck/settings.py src/herdeck/doctor.py tests/test_config.py tests/test_doctor.py
git commit -m "feat: parse interactive telegram notification config"
sha=$(git rev-parse --short HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

Expected: Roborev has no findings before continuing.

---

### Task 2: Typed Blocked-Alert Notifier Boundary

**Files:**
- Modify: `src/herdeck/notify.py`
- Modify: `src/herdeck/app.py:50-182`
- Test: `tests/test_notify.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Write failing notify tests**

Add to `tests/test_notify.py`:

```python
def test_legacy_blocked_notifier_uses_agent_type_title_and_body():
    import asyncio

    from herdeck.model import AgentKey, AgentState, Status
    from herdeck.notify import LegacyBlockedNotifier, Notifier

    calls = []
    notifier = LegacyBlockedNotifier(
        Notifier(sink=lambda title, body, sound: calls.append((title, body, sound)))
    )
    agent = AgentState(AgentKey("local", "p1"), "codex", "herdeck", Status.BLOCKED)

    asyncio.run(notifier.notify_blocked(agent, body="herdeck · main", sound=True, multi_server=False))

    assert calls == [("codex", "herdeck · main", True)]


def test_composite_blocked_notifier_calls_all_even_if_one_raises():
    import asyncio

    from herdeck.model import AgentKey, AgentState, Status
    from herdeck.notify import CompositeBlockedNotifier

    agent = AgentState(AgentKey("local", "p1"), "codex", "herdeck", Status.BLOCKED)
    calls = []

    class Boom:
        async def notify_blocked(self, agent, *, body, sound, multi_server):
            raise RuntimeError("x")

    class Rec:
        async def notify_blocked(self, agent, *, body, sound, multi_server):
            calls.append((agent.key.pane_id, body, sound, multi_server))

    asyncio.run(
        CompositeBlockedNotifier([Rec(), Boom(), Rec()]).notify_blocked(
            agent, body="body", sound=False, multi_server=True
        )
    )

    assert calls == [("p1", "body", False, True), ("p1", "body", False, True)]
```

- [ ] **Step 2: Write failing app typed-event test**

Add to `tests/test_app.py` near `test_app_notifies_on_block_transition`:

```python
def test_app_blocked_notifier_receives_agent_state_and_metadata():
    calls = []
    cfg = make_config()
    cfg.notifications.enabled = True

    class CaptureBlocked:
        async def notify_blocked(self, agent, *, body, sound, multi_server):
            calls.append((agent.key, agent.agent_type, body, sound, multi_server))

    app = App(
        cfg,
        FakeRenderer(13),
        send=lambda c: None,
        blocked_notifier=CaptureBlocked(),
    )

    app.handle_snapshot(
        "dev",
        [AgentState(AgentKey("dev", "p1"), "claude", "api", Status.BLOCKED)],
    )

    assert calls == [(AgentKey("dev", "p1"), "claude", "api", True, False)]


def test_apply_config_rebuilds_blocked_notification_runtime():
    import asyncio

    from herdeck.notify import BlockedNotificationRuntime

    calls = []
    pollers = []

    class CaptureBlocked:
        def __init__(self, label):
            self._label = label

        async def notify_blocked(self, agent, *, body, sound, multi_server):
            calls.append((self._label, agent.key, body, sound, multi_server))

    def runtime_factory(config):
        poller = object()
        pollers.append(poller)
        return BlockedNotificationRuntime(CaptureBlocked(config.meta.active_profile), poller)

    cfg = make_config()
    cfg.meta.active_profile = "default"
    app = App(
        cfg,
        FakeRenderer(13),
        send=lambda c: None,
        blocked_runtime_factory=runtime_factory,
    )
    first_poller = app.notification_poller

    new_cfg = make_config()
    new_cfg.meta.active_profile = "mobile"
    app._apply_config(new_cfg)

    assert app.notification_poller is pollers[-1]
    assert app.notification_poller is not first_poller
    agent = AgentState(AgentKey("dev", "p1"), "claude", "api", Status.BLOCKED)
    asyncio.run(
        app.blocked_notifier.notify_blocked(
            agent, body="api · main", sound=False, multi_server=False
        )
    )
    assert calls == [("mobile", AgentKey("dev", "p1"), "api · main", False, False)]
```

- [ ] **Step 3: Run red tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_notify.py::test_legacy_blocked_notifier_uses_agent_type_title_and_body \
  tests/test_notify.py::test_composite_blocked_notifier_calls_all_even_if_one_raises \
  tests/test_app.py::test_app_blocked_notifier_receives_agent_state_and_metadata \
  tests/test_app.py::test_apply_config_rebuilds_blocked_notification_runtime \
  -q
```

Expected: FAIL because the typed blocked-alert notifier classes and `App(blocked_notifier=...)` do not exist.

- [ ] **Step 4: Implement notifier classes**

In `src/herdeck/notify.py`, add imports:

```python
import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .model import AgentState
```

Add these classes after `NoopNotifier`:

```python
class BlockedAlertNotifier(Protocol):
    async def notify_blocked(
        self, agent: AgentState, *, body: str, sound: bool, multi_server: bool
    ) -> None: ...


class InboundNotificationPoller(Protocol):
    async def poll_once(
        self, *, timeout: int = 20, is_current: Callable[[], bool] | None = None
    ) -> None: ...


@dataclass(frozen=True)
class BlockedNotificationRuntime:
    notifier: BlockedAlertNotifier
    poller: InboundNotificationPoller | None = None


class NoopBlockedNotifier:
    async def notify_blocked(
        self, agent: AgentState, *, body: str, sound: bool, multi_server: bool
    ) -> None:
        return None


class LegacyBlockedNotifier:
    def __init__(self, notifier: Notifier):
        self._notifier = notifier

    async def notify_blocked(
        self, agent: AgentState, *, body: str, sound: bool, multi_server: bool
    ) -> None:
        await asyncio.to_thread(self._notifier.notify, agent.agent_type, body, sound)


class CompositeBlockedNotifier:
    def __init__(self, notifiers: list[BlockedAlertNotifier]):
        self._notifiers = notifiers

    async def notify_blocked(
        self, agent: AgentState, *, body: str, sound: bool, multi_server: bool
    ) -> None:
        for notifier in self._notifiers:
            try:
                await notifier.notify_blocked(
                    agent, body=body, sound=sound, multi_server=multi_server
                )
            except Exception:
                log.debug("blocked alert notifier failed", exc_info=True)
```

- [ ] **Step 5: Wire App to typed notifier**

In `src/herdeck/app.py`, import the new adapter classes:

```python
from collections.abc import Awaitable, Callable

from .notify import (
    BlockedAlertNotifier,
    BlockedNotificationRuntime,
    CompositeBlockedNotifier,
    LegacyBlockedNotifier,
    NoopNotifier,
    Notifier,
    _macos_sink,
    composite_sink,
    make_telegram_sink,
)
```

Update `App.__init__` signature:

```python
        notifier: Notifier | None = None,
        blocked_notifier: BlockedAlertNotifier | None = None,
        blocked_runtime_factory: Callable[[Config], BlockedNotificationRuntime] | None = None,
        notify_schedule: Callable[[Awaitable[None]], None] | None = None,
```

Initialize:

```python
        self.notifier = notifier or NoopNotifier()
        self._blocked_runtime_factory = blocked_runtime_factory
        self.notification_poller = None
        self._notification_generation = 0
        if blocked_notifier is not None:
            self._install_blocked_runtime(BlockedNotificationRuntime(blocked_notifier))
        elif blocked_runtime_factory is not None:
            self._install_blocked_runtime(blocked_runtime_factory(config))
        else:
            self._install_blocked_runtime(
                BlockedNotificationRuntime(LegacyBlockedNotifier(self.notifier))
            )
        self._notify_schedule = notify_schedule or (lambda coro: asyncio.run(coro))
```

Add a small installer and update `_apply_config()` so profile switches and reloads rebuild the typed blocked notifier instead of keeping the old runtime:

```python
    def _install_blocked_runtime(self, runtime: BlockedNotificationRuntime) -> None:
        self._notification_generation += 1
        self.blocked_notifier = runtime.notifier
        self.notification_poller = runtime.poller

    @property
    def notification_generation(self) -> int:
        return self._notification_generation

    def _rebuild_blocked_runtime(self, config: Config) -> None:
        if self._blocked_runtime_factory is not None:
            self._install_blocked_runtime(self._blocked_runtime_factory(config))
        else:
            self._install_blocked_runtime(
                BlockedNotificationRuntime(LegacyBlockedNotifier(self.notifier))
            )

    def set_blocked_runtime_factory(
        self, factory: Callable[[Config], BlockedNotificationRuntime]
    ) -> None:
        self._blocked_runtime_factory = factory
        self._rebuild_blocked_runtime(self.config)
```

In `_apply_config()`, immediately after `self.notifier = _build_notifier(new_config)`, call:

```python
        self._rebuild_blocked_runtime(new_config)
```

Update `_maybe_notify()` loop:

```python
        sound = self.config.notifications.sound
        for s in (x for x in states if x.key in to):
            label = s.repo or s.label
            parts = [p for p in (s.branch, s.key.server_id if multi else None) if p]
            body = f"{label}" + (f" · {' · '.join(parts)}" if parts else "")
            self._schedule_blocked_notify(s, body, sound, multi)
```

Replace `_schedule_notify()` with:

```python
    def _schedule_blocked_notify(
        self, agent: AgentState, body: str, sound: bool, multi_server: bool
    ) -> None:
        self._notify_schedule(
            self.blocked_notifier.notify_blocked(
                agent, body=body, sound=sound, multi_server=multi_server
            )
        )
```

Keep `self.notifier` for backwards tests and non-interactive low-level behavior.

In `_run()`, change the runtime schedule argument from executor scheduling to event-loop task scheduling:

```python
        notify_schedule=lambda coro: asyncio.create_task(coro),
```

- [ ] **Step 6: Run green and legacy tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_notify.py \
  tests/test_app.py::test_app_notifies_on_block_transition \
  tests/test_app.py::test_app_notify_keeps_other_servers_blocked_keys \
  tests/test_app.py::test_app_does_not_notify_when_blocked_not_in_on \
  tests/test_app.py::test_app_blocked_notifier_receives_agent_state_and_metadata \
  tests/test_app.py::test_apply_config_rebuilds_blocked_notification_runtime \
  -q
```

Expected: PASS.

- [ ] **Step 7: Commit and Roborev**

```bash
git add src/herdeck/notify.py src/herdeck/app.py tests/test_notify.py tests/test_app.py
git commit -m "refactor: route notifications through blocked alert events"
sha=$(git rev-parse --short HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

Expected: Roborev has no findings before continuing.

---

### Task 3: Telegram Bot Client, Formatter, And Alert Store

**Files:**
- Create: `src/herdeck/telegram.py`
- Test: `tests/test_telegram.py`

- [ ] **Step 1: Write failing Telegram client tests**

Create `tests/test_telegram.py` with:

```python
from herdeck.model import AgentKey, AgentState, Status


def test_bot_client_send_message_payload_includes_topic_and_markup():
    from herdeck.telegram import TelegramBotClient

    calls = []
    client = TelegramBotClient("TOK", request=lambda method, fields: calls.append((method, fields)) or {"message_id": 9})

    result = client.send_message(
        chat_id="-1001",
        text="body",
        sound=False,
        message_thread_id=456,
        reply_markup={"inline_keyboard": [[{"text": "Approve", "callback_data": "h:a:approve"}]]},
    )

    assert result == {"message_id": 9}
    assert calls == [
        (
            "sendMessage",
            {
                "chat_id": "-1001",
                "text": "body",
                "disable_notification": "true",
                "message_thread_id": "456",
                "reply_markup": '{"inline_keyboard":[[{"text":"Approve","callback_data":"h:a:approve"}]]}',
            },
        )
    ]


def test_bot_client_get_updates_uses_allowed_updates():
    from herdeck.telegram import TelegramBotClient

    calls = []
    client = TelegramBotClient("TOK", request=lambda method, fields: calls.append((method, fields)) or [])

    client.get_updates(offset=12, timeout=20)

    assert calls == [
        (
            "getUpdates",
            {
                "offset": "12",
                "timeout": "20",
                "allowed_updates": '["message","callback_query"]',
            },
        )
    ]


def test_bot_client_answer_callback_and_edit_message_text_payloads():
    from herdeck.telegram import TelegramBotClient

    calls = []
    client = TelegramBotClient("TOK", request=lambda method, fields: calls.append((method, fields)) or True)

    client.answer_callback_query("cb1", text="sent")
    client.edit_message_text(chat_id="-1001", message_id=9, text="updated", message_thread_id=456)

    assert calls == [
        ("answerCallbackQuery", {"callback_query_id": "cb1", "text": "sent"}),
        (
            "editMessageText",
            {
                "chat_id": "-1001",
                "message_id": "9",
                "text": "updated",
                "message_thread_id": "456",
            },
        ),
    ]
```

- [ ] **Step 2: Write failing formatter and store tests**

Append to `tests/test_telegram.py`:

```python
def test_alert_formatter_truncates_prompt_and_builds_keyboard():
    from herdeck.telegram import TelegramAlertFormatter

    agent = AgentState(AgentKey("local", "p1"), "codex", "herdeck", Status.BLOCKED)
    agent.repo = "herdeck"
    agent.branch = "feat/tg"

    formatter = TelegramAlertFormatter(prompt_max_chars=12)
    text, markup = formatter.blocked_alert(agent, metadata_body="herdeck · feat/tg", prompt="0123456789abcdef", token="tok1")

    assert "codex blocked" in text
    assert "local:p1" in text
    assert "0123456789ab..." in text
    assert markup["inline_keyboard"][0][0] == {"text": "Approve", "callback_data": "h:tok1:approve"}
    assert markup["inline_keyboard"][1][0] == {"text": "Read again", "callback_data": "h:tok1:read"}


def test_alert_store_maps_message_and_token_to_agent_and_expires():
    from herdeck.telegram import TelegramAlertStore

    key = AgentKey("local", "p1")
    store = TelegramAlertStore(now=lambda: 100.0, ttl_seconds=10.0, token_factory=lambda: "tok1")
    record = store.create(key, chat_id="-1001", message_id=9)

    assert record.token == "tok1"
    assert store.by_token("tok1").key == key
    assert store.by_message("-1001", 9).key == key

    store.prune(now=111.0, live_blocked_keys={key})

    assert store.by_token("tok1") is None
    assert store.by_message("-1001", 9) is None


def test_alert_store_replaces_prior_alert_for_same_agent_on_reblock():
    from herdeck.telegram import TelegramAlertStore

    tokens = iter(["tok1", "tok2"])
    key = AgentKey("local", "p1")
    store = TelegramAlertStore(now=lambda: 100.0, token_factory=lambda: next(tokens))

    first = store.create(key, chat_id="-1001", message_id=9)
    second = store.create(key, chat_id="-1001", message_id=10)

    assert first.token == "tok1"
    assert second.token == "tok2"
    assert store.by_token("tok1") is None
    assert store.by_message("-1001", 9) is None
    assert store.by_token("tok2").message_id == 10
    assert store.by_message("-1001", 10).token == "tok2"
```

- [ ] **Step 3: Run red tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_telegram.py -q
```

Expected: FAIL because `src/herdeck/telegram.py` does not exist.

- [ ] **Step 4: Implement `src/herdeck/telegram.py` core types**

Create `src/herdeck/telegram.py` with:

```python
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from .model import AgentKey, AgentState, Status

log = logging.getLogger("herdeck.telegram")


class TelegramApiError(RuntimeError):
    def __init__(self, error_code: int, description: str):
        super().__init__(description)
        self.error_code = error_code
        self.description = description


def _request_json(token: str, method: str, fields: dict[str, str]):
    data = urllib.parse.urlencode(fields).encode()
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        with urllib.request.urlopen(url, data=data, timeout=25) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read().decode() or "{}")
        raise TelegramApiError(
            int(payload.get("error_code", exc.code)),
            str(payload.get("description", exc)),
        ) from exc
    if not payload.get("ok"):
        raise TelegramApiError(
            int(payload.get("error_code", 0)),
            str(payload.get("description", f"Telegram {method} failed")),
        )
    return payload.get("result")


class TelegramBotClient:
    def __init__(self, token: str, *, request: Callable[[str, dict[str, str]], object] | None = None):
        self._token = token
        self._request = request or (lambda method, fields: _request_json(token, method, fields))

    def send_message(
        self,
        *,
        chat_id: str,
        text: str,
        sound: bool,
        message_thread_id: int | None = None,
        reply_markup: dict | None = None,
        reply_to_message_id: int | None = None,
    ):
        fields = {
            "chat_id": str(chat_id),
            "text": text,
            "disable_notification": "false" if sound else "true",
        }
        if message_thread_id is not None:
            fields["message_thread_id"] = str(message_thread_id)
        if reply_markup is not None:
            fields["reply_markup"] = json.dumps(reply_markup, separators=(",", ":"))
        if reply_to_message_id is not None:
            fields["reply_to_message_id"] = str(reply_to_message_id)
        return self._request("sendMessage", fields)

    def get_updates(self, *, offset: int | None, timeout: int = 20):
        fields = {
            "timeout": str(timeout),
            "allowed_updates": json.dumps(["message", "callback_query"], separators=(",", ":")),
        }
        if offset is not None:
            fields["offset"] = str(offset)
        return self._request("getUpdates", fields)

    def answer_callback_query(self, callback_query_id: str, *, text: str = ""):
        fields = {"callback_query_id": callback_query_id}
        if text:
            fields["text"] = text
        return self._request("answerCallbackQuery", fields)

    def edit_message_text(
        self,
        *,
        chat_id: str,
        message_id: int,
        text: str,
        message_thread_id: int | None = None,
        reply_markup: dict | None = None,
    ):
        fields = {"chat_id": str(chat_id), "message_id": str(message_id), "text": text}
        if message_thread_id is not None:
            fields["message_thread_id"] = str(message_thread_id)
        if reply_markup is not None:
            fields["reply_markup"] = json.dumps(reply_markup, separators=(",", ":"))
        return self._request("editMessageText", fields)
```

Continue in the same file:

```python
class TelegramAlertFormatter:
    def __init__(self, *, prompt_max_chars: int = 1200):
        self._prompt_max_chars = prompt_max_chars

    def blocked_alert(
        self, agent: AgentState, *, metadata_body: str, prompt: str, token: str
    ) -> tuple[str, dict]:
        prompt = self._truncate(prompt.strip()) if prompt.strip() else "Prompt unavailable; use Read again."
        text = (
            f"{agent.agent_type} blocked\n"
            f"{metadata_body} · {agent.key.server_id}:{agent.key.pane_id}\n\n"
            f"Waiting for:\n{prompt}\n\n"
            "Reply to this message to send text to the agent."
        )
        markup = {
            "inline_keyboard": [
                [
                    {"text": "Approve", "callback_data": f"h:{token}:approve"},
                    {"text": "Deny", "callback_data": f"h:{token}:deny"},
                    {"text": "Stop", "callback_data": f"h:{token}:stop"},
                ],
                [{"text": "Read again", "callback_data": f"h:{token}:read"}],
            ]
        }
        return text, markup

    def _truncate(self, text: str) -> str:
        if len(text) <= self._prompt_max_chars:
            return text
        return text[: self._prompt_max_chars] + "..."
```

Continue in the same file:

```python
@dataclass
class TelegramAlertRecord:
    token: str
    key: AgentKey
    chat_id: str
    message_id: int
    created_at: float


class TelegramAlertStore:
    def __init__(
        self,
        *,
        now: Callable[[], float] = time.time,
        ttl_seconds: float = 24 * 60 * 60,
        token_factory: Callable[[], str] | None = None,
    ):
        self._now = now
        self._ttl_seconds = ttl_seconds
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(8))
        self._by_token: dict[str, TelegramAlertRecord] = {}
        self._by_message: dict[tuple[str, int], TelegramAlertRecord] = {}

    def create(self, key: AgentKey, *, chat_id: str, message_id: int) -> TelegramAlertRecord:
        for old in [record for record in self._by_token.values() if record.key == key]:
            self.discard(old.token)
        token = self._token_factory()
        record = TelegramAlertRecord(token, key, str(chat_id), int(message_id), self._now())
        self._by_token[token] = record
        self._by_message[(record.chat_id, record.message_id)] = record
        return record

    def by_token(self, token: str) -> TelegramAlertRecord | None:
        return self._by_token.get(token)

    def by_message(self, chat_id: str, message_id: int) -> TelegramAlertRecord | None:
        return self._by_message.get((str(chat_id), int(message_id)))

    def discard(self, token: str) -> None:
        record = self._by_token.pop(token, None)
        if record is not None:
            self._by_message.pop((record.chat_id, record.message_id), None)

    def prune(self, *, now: float | None = None, live_blocked_keys: set[AgentKey] | None = None) -> None:
        current = self._now() if now is None else now
        live = live_blocked_keys
        for token, record in list(self._by_token.items()):
            expired = current - record.created_at > self._ttl_seconds
            not_live = live is not None and record.key not in live
            if expired or not_live:
                self.discard(token)

    def records(self) -> list[TelegramAlertRecord]:
        return list(self._by_token.values())
```

- [ ] **Step 5: Run green tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_telegram.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit and Roborev**

```bash
git add src/herdeck/telegram.py tests/test_telegram.py
git commit -m "feat: add telegram bot client and alert store"
sha=$(git rev-parse --short HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

Expected: Roborev has no findings before continuing.

---

### Task 4: Runtime Agent Control Broker

**Files:**
- Create: `src/herdeck/app_control.py`
- Modify: `src/herdeck/app.py`
- Test: `tests/test_app_control.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Write failing broker tests**

Create `tests/test_app_control.py`:

```python
import asyncio

import pytest

from herdeck.commands import Command
from herdeck.model import AgentKey, AgentState, Status


class FakeSender:
    def __init__(self):
        self.sent = []

    async def send(self, cmd, req):
        self.sent.append((cmd, req))


@pytest.mark.asyncio
async def test_runtime_agent_control_read_prompt_uses_own_request_ids():
    from herdeck.app_control import RuntimeAgentControl
    from herdeck.config import AnswerProfile, Config

    sender = FakeSender()
    key = AgentKey("local", "p1")
    cfg = Config(
        servers=[],
        profiles={
            "default": AnswerProfile(["y"], ["n"], ["ctrl+c"], ["y"]),
            "codex": AnswerProfile(["y", "enter"], ["n", "enter"], ["ctrl+c"], ["y", "enter"]),
        },
        overview_order=["local"],
        grid=(5, 3),
    )
    agent = AgentState(key, "codex", "herdeck", Status.BLOCKED)
    control = RuntimeAgentControl(cfg, send=sender.send, current_agent=lambda k: agent if k == key else None)

    task = asyncio.create_task(control.read_prompt(key, timeout=1))
    await asyncio.sleep(0)

    cmd, req = sender.sent[0]
    assert cmd == Command("read", "local", "p1", source="detection")
    assert req.startswith("tg")

    assert control.handle_result(req, {"text": "Approve?", "pane_id": "p1"}) == cmd
    assert await task == "Approve?"


@pytest.mark.asyncio
async def test_runtime_agent_control_approve_uses_profile_and_guard():
    from herdeck.app_control import RuntimeAgentControl
    from herdeck.config import AnswerProfile, Config

    sender = FakeSender()
    key = AgentKey("local", "p1")
    cfg = Config(
        servers=[],
        profiles={
            "default": AnswerProfile(["y"], ["n"], ["ctrl+c"], ["y"]),
            "codex": AnswerProfile(["y", "enter"], ["n", "enter"], ["ctrl+c"], ["y", "enter"]),
        },
        overview_order=["local"],
        grid=(5, 3),
    )
    agent = AgentState(key, "codex", "herdeck", Status.BLOCKED)
    control = RuntimeAgentControl(cfg, send=sender.send, current_agent=lambda k: agent if k == key else None)

    task = asyncio.create_task(control.approve(key, timeout=1))
    await asyncio.sleep(0)

    cmd, req = sender.sent[0]
    assert cmd == Command("act_if_blocked", "local", "p1", keys=["y", "enter"])

    assert control.handle_result(req, {"sent": True}) == cmd
    result = await task
    assert result.sent is True
    assert result.skipped is False


@pytest.mark.asyncio
async def test_runtime_agent_control_action_result_preserves_connector_failure_message():
    from herdeck.app_control import RuntimeAgentControl
    from herdeck.config import AnswerProfile, Config

    sender = FakeSender()
    key = AgentKey("local", "p1")
    cfg = Config(
        servers=[],
        profiles={
            "default": AnswerProfile(["y"], ["n"], ["ctrl+c"], ["y"]),
            "codex": AnswerProfile(["y", "enter"], ["n", "enter"], ["ctrl+c"], ["y", "enter"]),
        },
        overview_order=["local"],
        grid=(5, 3),
    )
    agent = AgentState(key, "codex", "herdeck", Status.BLOCKED)
    control = RuntimeAgentControl(cfg, send=sender.send, current_agent=lambda k: agent if k == key else None)

    task = asyncio.create_task(control.approve(key, timeout=1))
    await asyncio.sleep(0)

    cmd, req = sender.sent[0]
    assert cmd == Command("act_if_blocked", "local", "p1", keys=["y", "enter"])

    assert control.handle_result(req, {"sent": False, "message": "connection lost"}) == cmd
    result = await task
    assert result.sent is False
    assert result.skipped is False
    assert result.message == "connection lost"


@pytest.mark.asyncio
async def test_runtime_agent_control_update_config_changes_action_profile():
    from herdeck.app_control import RuntimeAgentControl
    from herdeck.config import AnswerProfile, Config

    sender = FakeSender()
    key = AgentKey("local", "p1")
    cfg = Config(
        servers=[],
        profiles={
            "default": AnswerProfile(["y"], ["n"], ["ctrl+c"], ["y"]),
            "codex": AnswerProfile(["y", "enter"], ["n", "enter"], ["ctrl+c"], ["y", "enter"]),
        },
        overview_order=["local"],
        grid=(5, 3),
    )
    new_cfg = Config(
        servers=[],
        profiles={
            "default": AnswerProfile(["ok"], ["no"], ["ctrl+c"], ["ok"]),
            "codex": AnswerProfile(["ok", "enter"], ["no", "enter"], ["ctrl+c"], ["ok", "enter"]),
        },
        overview_order=["local"],
        grid=(5, 3),
    )
    agent = AgentState(key, "codex", "herdeck", Status.BLOCKED)
    control = RuntimeAgentControl(cfg, send=sender.send, current_agent=lambda k: agent if k == key else None)

    control.update_config(new_cfg)
    task = asyncio.create_task(control.approve(key, timeout=1))
    await asyncio.sleep(0)

    cmd, req = sender.sent[0]
    assert cmd == Command("act_if_blocked", "local", "p1", keys=["ok", "enter"])
    assert control.handle_result(req, {"sent": True}) == cmd
    assert (await task).sent is True


@pytest.mark.asyncio
async def test_runtime_agent_control_send_text_returns_missing_agent():
    from herdeck.app_control import RuntimeAgentControl
    from herdeck.config import Config

    sender = FakeSender()
    key = AgentKey("local", "p1")
    cfg = Config(servers=[], profiles={}, overview_order=[], grid=(5, 3))
    control = RuntimeAgentControl(cfg, send=sender.send, current_agent=lambda k: None)

    result = await control.send_text(key, "hello", timeout=1)

    assert result.sent is False
    assert result.message == "agent is no longer available"
    assert sender.sent == []
```

- [ ] **Step 2: Write failing app integration test**

Add to `tests/test_app.py` near read correlation tests:

```python
def test_app_routes_runtime_request_results_before_deck_read_state():
    cfg = make_config()
    app = App(cfg, FakeRenderer(13), send=lambda c: None)

    class Runtime:
        def __init__(self):
            self.results = []

        def handle_result(self, req, data):
            self.results.append((req, data))
            return Command("read", "dev", "p1") if req == "tg1" else None

    runtime = Runtime()
    app.set_runtime_control(runtime)
    app._active_read_req = "r1"
    app.handle_result("dev", "tg1", {"text": "telegram prompt", "pane_id": "p1"})

    assert runtime.results == [("tg1", {"text": "telegram prompt", "pane_id": "p1"})]
    assert app._active_read_req == "r1"


def test_app_re_lists_after_runtime_action_result():
    cfg = make_config()
    sent = []
    app = App(cfg, FakeRenderer(13), send=lambda c: sent.append(c))

    class Runtime:
        def handle_result(self, req, data):
            return Command("send_text", "dev", "p1") if req == "tg1" else None

    app.set_runtime_control(Runtime())
    app.handle_result("dev", "tg1", {"sent": True})

    assert sent == [Command("list", "dev")]
```

- [ ] **Step 3: Run red tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_app_control.py \
  tests/test_app.py::test_app_routes_runtime_request_results_before_deck_read_state \
  tests/test_app.py::test_app_re_lists_after_runtime_action_result \
  -q
```

Expected: FAIL because `app_control.py` and `App.set_runtime_control()` do not exist.

- [ ] **Step 4: Implement `src/herdeck/app_control.py`**

Create `src/herdeck/app_control.py`:

```python
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .commands import Command, build_action_command, profile_for
from .config import Config
from .model import AgentKey, AgentState


@dataclass
class ActionResult:
    sent: bool
    skipped: bool = False
    message: str = ""


class RuntimeAgentControl:
    def __init__(
        self,
        config: Config,
        *,
        send: Callable[[Command, str], Awaitable[None]],
        current_agent: Callable[[AgentKey], AgentState | None],
    ):
        self._config = config
        self._send = send
        self._current_agent = current_agent
        self._pending: dict[str, tuple[asyncio.Future, Command]] = {}
        self._req = 0

    def handle_result(self, req: str, data: dict) -> Command | None:
        pending = self._pending.pop(req, None)
        if pending is None:
            return None
        fut, cmd = pending
        if not fut.done():
            fut.set_result(data)
        return cmd

    async def read_prompt(self, key: AgentKey, *, timeout: float = 3.0) -> str:
        data = await self._request(Command("read", key.server_id, key.pane_id, source="detection"), timeout=timeout)
        return str(data.get("text", ""))

    async def approve(self, key: AgentKey, *, timeout: float = 3.0) -> ActionResult:
        return await self._act("approve", key, force=False, always=False, timeout=timeout)

    async def deny(self, key: AgentKey, *, timeout: float = 3.0) -> ActionResult:
        return await self._act("deny", key, force=False, always=False, timeout=timeout)

    async def stop(self, key: AgentKey, *, timeout: float = 3.0) -> ActionResult:
        return await self._act("stop", key, force=True, always=False, timeout=timeout)

    async def send_text(self, key: AgentKey, text: str, *, timeout: float = 3.0) -> ActionResult:
        agent = self.current_agent(key)
        if agent is None:
            return ActionResult(False, message="agent is no longer available")
        data = await self._request(Command("send_text", key.server_id, key.pane_id, text=text), timeout=timeout)
        return ActionResult(
            sent=bool(data.get("sent")),
            skipped=bool(data.get("skipped")),
            message=str(data.get("message", "")),
        )

    def current_agent(self, key: AgentKey) -> AgentState | None:
        return self._current_agent(key)

    def update_config(self, config: Config) -> None:
        self._config = config

    async def _act(self, action: str, key: AgentKey, *, force: bool, always: bool, timeout: float) -> ActionResult:
        agent = self.current_agent(key)
        if agent is None:
            return ActionResult(False, message="agent is no longer available")
        profile = profile_for(self._config, agent.agent_type)
        cmd = build_action_command(action, agent, profile, force=force, always=always)
        data = await self._request(cmd, timeout=timeout)
        return ActionResult(
            sent=bool(data.get("sent")),
            skipped=bool(data.get("skipped")),
            message=str(data.get("message", "")),
        )

    async def _request(self, cmd: Command, *, timeout: float) -> dict:
        self._req += 1
        req = f"tg{self._req}"
        fut = asyncio.get_running_loop().create_future()
        self._pending[req] = (fut, cmd)
        await self._send(cmd, req)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError:
            self._pending.pop(req, None)
            raise
```

- [ ] **Step 5: Wire App result routing**

In `src/herdeck/app.py`, import:

```python
from .app_control import RuntimeAgentControl
```

Add constructor field:

```python
        runtime_control: RuntimeAgentControl | None = None,
```

Initialize:

```python
        self._runtime_control = runtime_control
```

Add method:

```python
    def set_runtime_control(self, runtime_control: RuntimeAgentControl | None) -> None:
        self._runtime_control = runtime_control
```

At the top of `handle_result()` after `_server_allowed()`:

```python
        if self._runtime_control is not None:
            handled = self._runtime_control.handle_result(req, data)
            if handled is not None:
                if handled.kind != "read":
                    self._send(Command("list", handled.server_id))
                return
```

- [ ] **Step 6: Run green tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_app_control.py \
  tests/test_app.py::test_app_routes_runtime_request_results_before_deck_read_state \
  tests/test_app.py::test_app_re_lists_after_runtime_action_result \
  tests/test_app.py::test_read_result_shows_detection_in_panel \
  tests/test_app.py::test_stale_read_result_with_old_req_is_ignored \
  -q
```

Expected: PASS.

- [ ] **Step 7: Commit and Roborev**

```bash
git add src/herdeck/app_control.py src/herdeck/app.py tests/test_app_control.py tests/test_app.py
git commit -m "feat: add runtime agent control broker"
sha=$(git rev-parse --short HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

Expected: Roborev has no findings before continuing.

---

### Task 5: Outbound Rich Telegram Alerts

**Files:**
- Modify: `src/herdeck/notify.py`
- Modify: `src/herdeck/telegram.py`
- Modify: `src/herdeck/app.py`
- Test: `tests/test_notify.py`
- Test: `tests/test_telegram.py`
- Test: `tests/test_app.py`
- Test: `tests/test_secrets.py`

- [ ] **Step 1: Write failing interactor outbound tests**

Add `import pytest` to the top import block of `tests/test_telegram.py` if it is not already present. Then append:

```python
class FakeControl:
    def __init__(self, prompt="Allow edit?"):
        self.prompt = prompt
        self.read_keys = []

    async def read_prompt(self, key, *, timeout=3.0):
        self.read_keys.append(key)
        return self.prompt

    def current_agent(self, key):
        return AgentState(key, "codex", "herdeck", Status.BLOCKED)


@pytest.mark.asyncio
async def test_interactor_notify_blocked_sends_prompt_alert_and_stores_mapping():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    calls = []
    client = TelegramBotClient("TOK", request=lambda method, fields: calls.append((method, fields)) or {"message_id": 9})
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    control = FakeControl(prompt="Allow edit?\n1. Yes\n2. No")
    interactor = TelegramInteractor(
        client,
        control,
        chat_id="-1001",
        message_thread_id=456,
        allowed_user_ids=[111],
        store=store,
        prompt_max_chars=1200,
    )
    agent = AgentState(AgentKey("local", "p1"), "codex", "herdeck", Status.BLOCKED)

    await interactor.notify_blocked(agent, body="herdeck · main", sound=True, multi_server=False)

    assert control.read_keys == [AgentKey("local", "p1")]
    method, fields = calls[0]
    assert method == "sendMessage"
    assert fields["chat_id"] == "-1001"
    assert fields["message_thread_id"] == "456"
    assert "Allow edit?" in fields["text"]
    assert "h:tok1:approve" in fields["reply_markup"]
    assert store.by_message("-1001", 9).key == AgentKey("local", "p1")


@pytest.mark.asyncio
async def test_interactor_notify_blocked_discards_reserved_alert_when_send_fails():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    def request(method, fields):
        raise RuntimeError("telegram down")

    client = TelegramBotClient("TOK", request=request)
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    control = FakeControl(prompt="Allow edit?")
    interactor = TelegramInteractor(
        client,
        control,
        chat_id="-1001",
        message_thread_id=None,
        allowed_user_ids=[111],
        store=store,
        prompt_max_chars=1200,
    )
    agent = AgentState(AgentKey("local", "p1"), "codex", "herdeck", Status.BLOCKED)

    await interactor.notify_blocked(agent, body="herdeck · main", sound=True, multi_server=False)

    assert store.by_token("tok1") is None


@pytest.mark.asyncio
async def test_interactor_notify_blocked_discards_reserved_alert_without_message_id():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    client = TelegramBotClient("TOK", request=lambda method, fields: {})
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    control = FakeControl(prompt="Allow edit?")
    interactor = TelegramInteractor(
        client,
        control,
        chat_id="-1001",
        message_thread_id=None,
        allowed_user_ids=[111],
        store=store,
        prompt_max_chars=1200,
    )
    agent = AgentState(AgentKey("local", "p1"), "codex", "herdeck", Status.BLOCKED)

    await interactor.notify_blocked(agent, body="herdeck · main", sound=True, multi_server=False)

    assert store.by_token("tok1") is None
```

- [ ] **Step 2: Write failing one-way Telegram topic test**

Add to `tests/test_notify.py`:

```python
def test_telegram_sink_includes_message_thread_id():
    from herdeck.notify import make_telegram_sink

    sent = []
    sink = make_telegram_sink(
        "TOK",
        "42",
        message_thread_id=456,
        post=lambda url, fields: sent.append((url, fields)),
    )

    sink("title", "body", True)

    assert sent == [
        (
            "https://api.telegram.org/botTOK/sendMessage",
            {
                "chat_id": "42",
                "text": "title\nbody",
                "disable_notification": "false",
                "message_thread_id": "456",
            },
        )
    ]
```

- [ ] **Step 3: Write failing builder test**

Add to `tests/test_app.py`:

```python
def test_build_blocked_notifier_preserves_macos_with_interactive_telegram():
    import asyncio

    from herdeck.app import _build_blocked_notification_runtime
    from herdeck.config import TelegramConfig
    from herdeck.model import AgentKey, AgentState, Status

    cfg = make_config()
    cfg.notifications.enabled = True
    cfg.notifications.backends = ["macos", "telegram"]
    cfg.notifications.telegram = TelegramConfig(
        "HERDECK_TG",
        "-1001",
        interactive=True,
        allowed_user_ids=[111],
    )
    calls = []

    class Interactive:
        async def notify_blocked(self, agent, *, body, sound, multi_server):
            calls.append(("interactive", agent.key, body, sound, multi_server))

        async def poll_once(self, *, timeout=20, is_current=None):
            calls.append(("poll", timeout))

    def one_way_telegram_should_not_run(token, chat_id, message_thread_id):
        raise AssertionError("interactive Telegram must replace the one-way Telegram sink")

    runtime = _build_blocked_notification_runtime(
        cfg,
        getenv=lambda name: "TOK",
        macos_sink=lambda title, body, sound: calls.append(("macos", title, body, sound)),
        telegram_factory=one_way_telegram_should_not_run,
        telegram_interactor_factory=lambda token, tg: calls.append(("factory", token, tg.chat_id)) or Interactive(),
    )

    agent = AgentState(AgentKey("local", "p1"), "codex", "herdeck", Status.BLOCKED)
    asyncio.run(runtime.notifier.notify_blocked(agent, body="herdeck · main", sound=True, multi_server=False))
    assert runtime.poller is not None
    asyncio.run(runtime.poller.poll_once(timeout=5))

    assert calls == [
        ("factory", "TOK", "-1001"),
        ("macos", "codex", "herdeck · main", True),
        ("interactive", AgentKey("local", "p1"), "herdeck · main", True, False),
        ("poll", 5),
    ]


def test_build_blocked_runtime_keeps_one_way_telegram_when_interactive_incomplete():
    import asyncio

    from herdeck.app import _build_blocked_notification_runtime
    from herdeck.config import TelegramConfig
    from herdeck.model import AgentKey, AgentState, Status

    cfg = make_config()
    cfg.notifications.enabled = True
    cfg.notifications.backends = ["telegram"]
    cfg.notifications.telegram = TelegramConfig(
        "HERDECK_TG",
        "-1001",
        message_thread_id=456,
        interactive=True,
        allowed_user_ids=[],
    )
    calls = []

    def telegram_sink(token, chat_id, message_thread_id):
        return lambda title, body, sound: calls.append(
            ("telegram", token, chat_id, message_thread_id, title, body, sound)
        )

    def interactor_should_not_run(token, tg):
        raise AssertionError("interactive Telegram requires allowed_user_ids")

    runtime = _build_blocked_notification_runtime(
        cfg,
        getenv=lambda name: "TOK",
        telegram_factory=telegram_sink,
        telegram_interactor_factory=interactor_should_not_run,
    )

    agent = AgentState(AgentKey("local", "p1"), "codex", "herdeck", Status.BLOCKED)
    asyncio.run(runtime.notifier.notify_blocked(agent, body="herdeck · main", sound=True, multi_server=False))

    assert runtime.poller is None
    assert calls == [("telegram", "TOK", "-1001", 456, "codex", "herdeck · main", True)]
```

- [ ] **Step 4: Run red tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_notify.py::test_telegram_sink_includes_message_thread_id \
  tests/test_telegram.py::test_interactor_notify_blocked_sends_prompt_alert_and_stores_mapping \
  tests/test_telegram.py::test_interactor_notify_blocked_discards_reserved_alert_when_send_fails \
  tests/test_telegram.py::test_interactor_notify_blocked_discards_reserved_alert_without_message_id \
  tests/test_app.py::test_build_blocked_notifier_preserves_macos_with_interactive_telegram \
  tests/test_app.py::test_build_blocked_runtime_keeps_one_way_telegram_when_interactive_incomplete \
  -q
```

Expected: FAIL because `TelegramInteractor`, one-way topic forwarding, and `_build_blocked_notification_runtime()` do not exist.

- [ ] **Step 5: Implement `TelegramInteractor.notify_blocked()`**

In `src/herdeck/notify.py`, extend the one-way sink to carry Telegram forum topics:

```python
def make_telegram_sink(
    token: str,
    chat_id: str,
    message_thread_id: int | None = None,
    *,
    post: Callable[[str, dict[str, str]], None] = _http_post,
) -> Callable[[str, str, bool], None]:
    """Sink that posts the alert to a Telegram chat via the Bot API."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    def sink(title: str, body: str, sound: bool) -> None:
        fields = {
            "chat_id": str(chat_id),
            "text": f"{title}\n{body}",
            "disable_notification": "false" if sound else "true",
        }
        if message_thread_id is not None:
            fields["message_thread_id"] = str(message_thread_id)
        post(url, fields)

    return sink
```

In `src/herdeck/telegram.py`, add:

```python
class TelegramInteractor:
    def __init__(
        self,
        client: TelegramBotClient,
        control,
        *,
        chat_id: str,
        message_thread_id: int | None,
        allowed_user_ids: list[int],
        store: TelegramAlertStore | None = None,
        prompt_max_chars: int = 1200,
    ):
        self._client = client
        self._control = control
        self._chat_id = str(chat_id)
        self._message_thread_id = message_thread_id
        self._allowed_user_ids = set(int(v) for v in allowed_user_ids)
        self._store = store or TelegramAlertStore()
        self._formatter = TelegramAlertFormatter(prompt_max_chars=prompt_max_chars)
        self._inbound_disabled = False

    async def notify_blocked(
        self, agent: AgentState, *, body: str, sound: bool, multi_server: bool
    ) -> None:
        record = self._store.reserve(agent.key)
        try:
            prompt = await self._control.read_prompt(agent.key, timeout=3.0)
        except Exception:
            prompt = ""
        text, markup = self._formatter.blocked_alert(
            agent, metadata_body=body, prompt=prompt, token=record.token
        )
        try:
            result = await asyncio.to_thread(
                self._client.send_message,
                chat_id=self._chat_id,
                text=text,
                sound=sound,
                message_thread_id=self._message_thread_id,
                reply_markup=markup,
            )
        except Exception:
            self._store.discard(record.token)
            log.debug("telegram blocked alert send failed", exc_info=True)
            return
        message_id = int((result or {}).get("message_id", -1))
        if message_id == -1:
            self._store.discard(record.token)
            return
        self._store.attach_message(record.token, chat_id=self._chat_id, message_id=message_id)
```

Add `reserve()` and `attach_message()` to `TelegramAlertStore` so the callback token placed in the keyboard is the same token stored for the final Telegram message id. `discard()` already exists from Task 3 and is reused here:

```python
def reserve(self, key: AgentKey) -> TelegramAlertRecord:
    for old in [record for record in self._by_token.values() if record.key == key]:
        self.discard(old.token)
    token = self._token_factory()
    record = TelegramAlertRecord(token, key, "", -1, self._now())
    self._by_token[token] = record
    return record


def attach_message(self, token: str, *, chat_id: str, message_id: int) -> TelegramAlertRecord:
    record = self._by_token[token]
    record.chat_id = str(chat_id)
    record.message_id = int(message_id)
    self._by_message[(record.chat_id, record.message_id)] = record
    return record
```

- [ ] **Step 6: Implement blocked notification runtime builder**

In `src/herdeck/app.py`, add `_build_blocked_notification_runtime()` near `_build_notifier()`. Keep the optional `_build_blocked_notifier()` wrapper only for existing tests or internal callers that only need the outbound notifier:

```python
def _build_notifier(
    config: Config,
    *,
    getenv=get_secret,
    macos_sink=_macos_sink,
    telegram_factory=make_telegram_sink,
    skip_telegram: bool = False,
) -> Notifier:
    """Assemble low-level legacy sinks from configured backends."""
    n = config.notifications
    if not n.enabled:
        return NoopNotifier()
    sinks = []
    for backend in n.backends:
        if backend == "macos":
            sinks.append(macos_sink)
        elif backend == "telegram":
            if skip_telegram:
                continue
            tg = n.telegram
            token = getenv(tg.token_env) if tg else None
            if tg and token and tg.chat_id:
                sinks.append(telegram_factory(token, tg.chat_id, tg.message_thread_id))
            else:
                log.warning(
                    "telegram notifications enabled but token/chat_id "
                    "missing; skipping telegram backend"
                )
        else:
            log.warning("unknown notification backend %r; skipping", backend)
    if not sinks:
        return NoopNotifier()
    return Notifier(sink=composite_sink(sinks))


def _build_blocked_notification_runtime(
    config: Config,
    *,
    getenv=get_secret,
    macos_sink=_macos_sink,
    telegram_factory=make_telegram_sink,
    telegram_interactor_factory=None,
) -> BlockedNotificationRuntime:
    n = config.notifications
    tg = n.telegram
    interactive_requested = (
        n.enabled
        and tg is not None
        and tg.interactive
        and "telegram" in n.backends
        and telegram_interactor_factory is not None
    )
    interactive_token = getenv(tg.token_env) if interactive_requested else None
    interactive_enabled = bool(
        interactive_requested and interactive_token and tg.chat_id and tg.allowed_user_ids
    )
    legacy = _build_notifier(
        config,
        getenv=getenv,
        macos_sink=macos_sink,
        telegram_factory=telegram_factory,
        skip_telegram=interactive_enabled,
    )
    notifiers: list[BlockedAlertNotifier] = [LegacyBlockedNotifier(legacy)]
    poller = None
    if interactive_enabled:
        assert tg is not None
        assert interactive_token is not None
        interactor = telegram_interactor_factory(interactive_token, tg)
        poller = interactor
        notifiers.append(interactor)
    notifier = notifiers[0] if len(notifiers) == 1 else CompositeBlockedNotifier(notifiers)
    return BlockedNotificationRuntime(notifier, poller)


def _build_blocked_notifier(*args, **kwargs) -> BlockedAlertNotifier:
    return _build_blocked_notification_runtime(*args, **kwargs).notifier
```

Update existing `_build_notifier` tests so fake `telegram_factory` callables accept `(token, chat_id, message_thread_id)` and assert that non-topic configs pass `None`. Also update `tests/test_secrets.py::test_build_notifier_resolves_telegram_token_via_secrets` so its fake factory accepts the third `message_thread_id` argument.

The full runtime wiring for `telegram_interactor_factory` happens in Task 8 after the control broker is attached to `_run()`.

- [ ] **Step 7: Run green tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_notify.py \
  tests/test_telegram.py::test_interactor_notify_blocked_sends_prompt_alert_and_stores_mapping \
  tests/test_telegram.py::test_interactor_notify_blocked_discards_reserved_alert_when_send_fails \
  tests/test_telegram.py::test_interactor_notify_blocked_discards_reserved_alert_without_message_id \
  tests/test_app.py::test_build_notifier_fires_both_backends \
  tests/test_app.py::test_build_blocked_notifier_preserves_macos_with_interactive_telegram \
  tests/test_app.py::test_build_blocked_runtime_keeps_one_way_telegram_when_interactive_incomplete \
  tests/test_app.py::test_app_notifies_on_block_transition \
  tests/test_secrets.py::test_build_notifier_resolves_telegram_token_via_secrets \
  -q
```

Expected: PASS.

- [ ] **Step 8: Commit and Roborev**

```bash
git add src/herdeck/notify.py src/herdeck/telegram.py src/herdeck/app.py tests/test_notify.py tests/test_telegram.py tests/test_app.py tests/test_secrets.py
git commit -m "feat: send rich telegram blocked alerts"
sha=$(git rev-parse --short HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

Expected: Roborev has no findings before continuing.

---

### Task 6: Inbound Polling And Callback Buttons

**Files:**
- Modify: `src/herdeck/telegram.py`
- Test: `tests/test_telegram.py`

- [ ] **Step 1: Write failing callback tests**

Append to `tests/test_telegram.py`:

```python
class ActionControl(FakeControl):
    def __init__(self):
        super().__init__()
        self.actions = []

    async def approve(self, key, *, timeout=3.0):
        self.actions.append(("approve", key))
        from herdeck.app_control import ActionResult

        return ActionResult(True)

    async def deny(self, key, *, timeout=3.0):
        self.actions.append(("deny", key))
        from herdeck.app_control import ActionResult

        return ActionResult(True)

    async def stop(self, key, *, timeout=3.0):
        self.actions.append(("stop", key))
        from herdeck.app_control import ActionResult

        return ActionResult(True)


@pytest.mark.asyncio
async def test_callback_approve_requires_allowed_user_chat_and_topic():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    calls = []
    client = TelegramBotClient("TOK", request=lambda method, fields: calls.append((method, fields)) or True)
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    record = store.create(AgentKey("local", "p1"), chat_id="-1001", message_id=9)
    control = ActionControl()
    interactor = TelegramInteractor(
        client,
        control,
        chat_id="-1001",
        message_thread_id=456,
        allowed_user_ids=[111],
        store=store,
    )

    ok = await interactor.process_update(
        {
            "update_id": 1,
            "callback_query": {
                "id": "cb1",
                "from": {"id": 111},
                "message": {"chat": {"id": -1001}, "message_id": 9, "message_thread_id": 456},
                "data": f"h:{record.token}:approve",
            },
        }
    )

    assert ok is True
    assert control.actions == [("approve", AgentKey("local", "p1"))]
    assert ("answerCallbackQuery", {"callback_query_id": "cb1", "text": "sent"}) in calls


@pytest.mark.asyncio
async def test_callback_from_wrong_user_does_not_run_action():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    calls = []
    client = TelegramBotClient("TOK", request=lambda method, fields: calls.append((method, fields)) or True)
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    record = store.create(AgentKey("local", "p1"), chat_id="-1001", message_id=9)
    control = ActionControl()
    interactor = TelegramInteractor(
        client,
        control,
        chat_id="-1001",
        message_thread_id=456,
        allowed_user_ids=[111],
        store=store,
    )

    ok = await interactor.process_update(
        {
            "update_id": 1,
            "callback_query": {
                "id": "cb1",
                "from": {"id": 999},
                "message": {"chat": {"id": -1001}, "message_id": 9, "message_thread_id": 456},
                "data": f"h:{record.token}:approve",
            },
        }
    )

    assert ok is False
    assert control.actions == []
    assert ("answerCallbackQuery", {"callback_query_id": "cb1", "text": "not authorized"}) in calls


@pytest.mark.asyncio
async def test_callback_from_wrong_chat_does_not_run_action():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    calls = []
    client = TelegramBotClient("TOK", request=lambda method, fields: calls.append((method, fields)) or True)
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    record = store.create(AgentKey("local", "p1"), chat_id="-1001", message_id=9)
    control = ActionControl()
    interactor = TelegramInteractor(
        client,
        control,
        chat_id="-1001",
        message_thread_id=456,
        allowed_user_ids=[111],
        store=store,
    )

    ok = await interactor.process_update(
        {
            "update_id": 1,
            "callback_query": {
                "id": "cb1",
                "from": {"id": 111},
                "message": {"chat": {"id": -9999}, "message_id": 9, "message_thread_id": 456},
                "data": f"h:{record.token}:approve",
            },
        }
    )

    assert ok is False
    assert control.actions == []
    assert ("answerCallbackQuery", {"callback_query_id": "cb1", "text": "not authorized"}) in calls


@pytest.mark.asyncio
async def test_callback_from_wrong_topic_does_not_run_action():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    calls = []
    client = TelegramBotClient("TOK", request=lambda method, fields: calls.append((method, fields)) or True)
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    record = store.create(AgentKey("local", "p1"), chat_id="-1001", message_id=9)
    control = ActionControl()
    interactor = TelegramInteractor(
        client,
        control,
        chat_id="-1001",
        message_thread_id=456,
        allowed_user_ids=[111],
        store=store,
    )

    ok = await interactor.process_update(
        {
            "update_id": 1,
            "callback_query": {
                "id": "cb1",
                "from": {"id": 111},
                "message": {"chat": {"id": -1001}, "message_id": 9, "message_thread_id": 999},
                "data": f"h:{record.token}:approve",
            },
        }
    )

    assert ok is False
    assert control.actions == []
    assert ("answerCallbackQuery", {"callback_query_id": "cb1", "text": "not authorized"}) in calls


@pytest.mark.asyncio
async def test_callback_old_token_is_stale_after_reblock_alert():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    calls = []
    tokens = iter(["tok1", "tok2"])
    client = TelegramBotClient("TOK", request=lambda method, fields: calls.append((method, fields)) or True)
    store = TelegramAlertStore(token_factory=lambda: next(tokens))
    key = AgentKey("local", "p1")
    old = store.create(key, chat_id="-1001", message_id=9)
    store.create(key, chat_id="-1001", message_id=10)
    control = ActionControl()
    interactor = TelegramInteractor(
        client,
        control,
        chat_id="-1001",
        message_thread_id=456,
        allowed_user_ids=[111],
        store=store,
    )

    ok = await interactor.process_update(
        {
            "update_id": 1,
            "callback_query": {
                "id": "cb1",
                "from": {"id": 111},
                "message": {"chat": {"id": -1001}, "message_id": 9, "message_thread_id": 456},
                "data": f"h:{old.token}:approve",
            },
        }
    )

    assert ok is False
    assert control.actions == []
    assert ("answerCallbackQuery", {"callback_query_id": "cb1", "text": "stale"}) in calls


@pytest.mark.asyncio
async def test_callback_action_error_is_answered_without_raising():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    calls = []
    client = TelegramBotClient("TOK", request=lambda method, fields: calls.append((method, fields)) or True)
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    record = store.create(AgentKey("local", "p1"), chat_id="-1001", message_id=9)

    class TimeoutControl(ActionControl):
        async def approve(self, key, *, timeout=3.0):
            self.actions.append(("approve", key))
            raise TimeoutError("agent did not answer")

    control = TimeoutControl()
    interactor = TelegramInteractor(
        client,
        control,
        chat_id="-1001",
        message_thread_id=456,
        allowed_user_ids=[111],
        store=store,
    )

    ok = await interactor.process_update(
        {
            "update_id": 1,
            "callback_query": {
                "id": "cb1",
                "from": {"id": 111},
                "message": {"chat": {"id": -1001}, "message_id": 9, "message_thread_id": 456},
                "data": f"h:{record.token}:approve",
            },
        }
    )

    assert ok is False
    assert control.actions == [("approve", AgentKey("local", "p1"))]
    assert ("answerCallbackQuery", {"callback_query_id": "cb1", "text": "timed out"}) in calls


@pytest.mark.asyncio
async def test_callback_failed_action_result_uses_failure_message():
    from herdeck.app_control import ActionResult
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    calls = []
    client = TelegramBotClient("TOK", request=lambda method, fields: calls.append((method, fields)) or True)
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    record = store.create(AgentKey("local", "p1"), chat_id="-1001", message_id=9)

    class FailedControl(ActionControl):
        async def approve(self, key, *, timeout=3.0):
            self.actions.append(("approve", key))
            return ActionResult(False, message="connection lost")

    control = FailedControl()
    interactor = TelegramInteractor(
        client,
        control,
        chat_id="-1001",
        message_thread_id=456,
        allowed_user_ids=[111],
        store=store,
    )

    ok = await interactor.process_update(
        {
            "update_id": 1,
            "callback_query": {
                "id": "cb1",
                "from": {"id": 111},
                "message": {"chat": {"id": -1001}, "message_id": 9, "message_thread_id": 456},
                "data": f"h:{record.token}:approve",
            },
        }
    )

    assert ok is False
    assert control.actions == [("approve", AgentKey("local", "p1"))]
    assert ("answerCallbackQuery", {"callback_query_id": "cb1", "text": "connection lost"}) in calls
```

- [ ] **Step 2: Write failing poll loop test**

Append to `tests/test_telegram.py`:

```python
@pytest.mark.asyncio
async def test_poll_once_advances_offset_after_processing_update():
    from herdeck.telegram import TelegramBotClient, TelegramInteractor

    calls = []

    def request(method, fields):
        calls.append((method, fields))
        if method == "getUpdates":
            return [
                {
                    "update_id": 41,
                    "callback_query": {
                        "id": "cb1",
                        "from": {"id": 111},
                        "message": {"chat": {"id": -1001}, "message_id": 9},
                        "data": "h:missing:approve",
                    },
                }
            ]
        return {"message_id": 99}

    client = TelegramBotClient("TOK", request=request)
    interactor = TelegramInteractor(
        client,
        FakeControl(),
        chat_id="-1001",
        message_thread_id=None,
        allowed_user_ids=[111],
    )

    await interactor.poll_once(timeout=20)

    assert interactor.offset == 42
    assert calls[0] == (
        "getUpdates",
        {"timeout": "20", "allowed_updates": '["message","callback_query"]'},
    )


@pytest.mark.asyncio
async def test_poll_once_advances_offset_when_callback_ack_fails_after_action():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    calls = []
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    store.create(AgentKey("local", "p1"), chat_id="-1001", message_id=9)

    def request(method, fields):
        calls.append((method, fields))
        if method == "getUpdates":
            return [
                {
                    "update_id": 41,
                    "callback_query": {
                        "id": "cb1",
                        "from": {"id": 111},
                        "message": {"chat": {"id": -1001}, "message_id": 9},
                        "data": "h:tok1:approve",
                    },
                }
            ]
        if method == "answerCallbackQuery":
            raise RuntimeError("telegram ack failed")
        return True

    control = ActionControl()
    interactor = TelegramInteractor(
        TelegramBotClient("TOK", request=request),
        control,
        chat_id="-1001",
        message_thread_id=None,
        allowed_user_ids=[111],
        store=store,
    )

    with pytest.raises(RuntimeError, match="telegram ack failed"):
        await interactor.poll_once(timeout=20)

    assert interactor.offset == 42
    assert control.actions == [("approve", AgentKey("local", "p1"))]


@pytest.mark.asyncio
async def test_poll_once_prunes_expired_alert_before_callback():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    calls = []
    now = [100.0]
    store = TelegramAlertStore(
        now=lambda: now[0],
        ttl_seconds=10.0,
        token_factory=lambda: "tok1",
    )
    store.create(AgentKey("local", "p1"), chat_id="-1001", message_id=9)
    now[0] = 111.0

    def request(method, fields):
        calls.append((method, fields))
        if method == "getUpdates":
            return [
                {
                    "update_id": 41,
                    "callback_query": {
                        "id": "cb1",
                        "from": {"id": 111},
                        "message": {"chat": {"id": -1001}, "message_id": 9},
                        "data": "h:tok1:approve",
                    },
                }
            ]
        return True

    control = ActionControl()
    interactor = TelegramInteractor(
        TelegramBotClient("TOK", request=request),
        control,
        chat_id="-1001",
        message_thread_id=None,
        allowed_user_ids=[111],
        store=store,
    )

    await interactor.poll_once(timeout=20)

    assert store.by_token("tok1") is None
    assert control.actions == []
    assert ("answerCallbackQuery", {"callback_query_id": "cb1", "text": "stale"}) in calls


@pytest.mark.asyncio
async def test_poll_once_skips_updates_when_generation_guard_is_stale():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    calls = []
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    store.create(AgentKey("local", "p1"), chat_id="-1001", message_id=9)

    def request(method, fields):
        calls.append((method, fields))
        if method == "getUpdates":
            return [
                {
                    "update_id": 41,
                    "callback_query": {
                        "id": "cb1",
                        "from": {"id": 111},
                        "message": {"chat": {"id": -1001}, "message_id": 9},
                        "data": "h:tok1:approve",
                    },
                }
            ]
        return True

    control = ActionControl()
    interactor = TelegramInteractor(
        TelegramBotClient("TOK", request=request),
        control,
        chat_id="-1001",
        message_thread_id=None,
        allowed_user_ids=[111],
        store=store,
    )

    await interactor.poll_once(timeout=20, is_current=lambda: False)

    assert interactor.offset is None
    assert control.actions == []
    assert [method for method, _ in calls] == ["getUpdates"]


@pytest.mark.asyncio
async def test_poll_once_disables_inbound_on_webhook_conflict_but_keeps_outbound_alerts():
    from herdeck.telegram import TelegramAlertStore, TelegramApiError, TelegramBotClient, TelegramInteractor

    calls = []

    def request(method, fields):
        calls.append((method, fields))
        if method == "getUpdates":
            raise TelegramApiError(
                409,
                "Conflict: can't use getUpdates method while webhook is active",
            )
        return {"message_id": 9}

    interactor = TelegramInteractor(
        TelegramBotClient("TOK", request=request),
        FakeControl(prompt="Allow edit?"),
        chat_id="-1001",
        message_thread_id=None,
        allowed_user_ids=[111],
        store=TelegramAlertStore(token_factory=lambda: "tok1"),
    )

    await interactor.poll_once(timeout=20)
    await interactor.poll_once(timeout=20)
    await interactor.notify_blocked(
        AgentState(AgentKey("local", "p1"), "codex", "herdeck", Status.BLOCKED),
        body="herdeck · main",
        sound=True,
        multi_server=False,
    )

    assert interactor.inbound_disabled is True
    assert [method for method, _ in calls] == ["getUpdates", "sendMessage"]
```

- [ ] **Step 3: Run red tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_telegram.py::test_callback_approve_requires_allowed_user_chat_and_topic \
  tests/test_telegram.py::test_callback_from_wrong_user_does_not_run_action \
  tests/test_telegram.py::test_callback_from_wrong_chat_does_not_run_action \
  tests/test_telegram.py::test_callback_from_wrong_topic_does_not_run_action \
  tests/test_telegram.py::test_callback_old_token_is_stale_after_reblock_alert \
  tests/test_telegram.py::test_callback_action_error_is_answered_without_raising \
  tests/test_telegram.py::test_callback_failed_action_result_uses_failure_message \
  tests/test_telegram.py::test_poll_once_advances_offset_after_processing_update \
  tests/test_telegram.py::test_poll_once_advances_offset_when_callback_ack_fails_after_action \
  tests/test_telegram.py::test_poll_once_prunes_expired_alert_before_callback \
  tests/test_telegram.py::test_poll_once_skips_updates_when_generation_guard_is_stale \
  tests/test_telegram.py::test_poll_once_disables_inbound_on_webhook_conflict_but_keeps_outbound_alerts \
  -q
```

Expected: FAIL because `process_update()`, `poll_once()`, and `offset` do not exist.

- [ ] **Step 4: Implement callback processing**

In `TelegramInteractor`, add:

```python
    @property
    def offset(self) -> int | None:
        return getattr(self, "_offset", None)

    @property
    def inbound_disabled(self) -> bool:
        return self._inbound_disabled

    def _is_webhook_conflict(self, exc: TelegramApiError) -> bool:
        return exc.error_code == 409 and "webhook" in exc.description.lower()

    def _live_blocked_keys(self) -> set[AgentKey]:
        live = set()
        for record in self._store.records():
            try:
                agent = self._control.current_agent(record.key)
            except Exception:
                continue
            if agent is not None and agent.status is Status.BLOCKED:
                live.add(record.key)
        return live

    def _prune_alerts(self) -> None:
        self._store.prune(live_blocked_keys=self._live_blocked_keys())

    async def process_update(self, update: dict) -> bool:
        self._prune_alerts()
        if "callback_query" in update:
            return await self._process_callback(update["callback_query"])
        if "message" in update:
            return await self._process_message(update["message"])
        return False

    async def _process_callback(self, query: dict) -> bool:
        cb_id = str(query.get("id", ""))
        if not self._authorized(query.get("from", {}), query.get("message", {})):
            await asyncio.to_thread(self._client.answer_callback_query, cb_id, text="not authorized")
            return False
        data = str(query.get("data", ""))
        parts = data.split(":")
        if len(parts) != 3 or parts[0] != "h":
            await asyncio.to_thread(self._client.answer_callback_query, cb_id, text="stale")
            return False
        record = self._store.by_token(parts[1])
        if record is None:
            await asyncio.to_thread(self._client.answer_callback_query, cb_id, text="stale")
            return False
        action = parts[2]
        try:
            if action == "approve":
                result = await self._control.approve(record.key, timeout=3.0)
                status, ok = self._action_status(result)
            elif action == "deny":
                result = await self._control.deny(record.key, timeout=3.0)
                status, ok = self._action_status(result)
            elif action == "stop":
                result = await self._control.stop(record.key, timeout=3.0)
                status, ok = self._action_status(result)
            elif action == "read":
                await self._refresh_prompt(record)
                status = "refreshed"
                ok = True
            else:
                await asyncio.to_thread(self._client.answer_callback_query, cb_id, text="stale")
                return False
        except TimeoutError:
            await asyncio.to_thread(self._client.answer_callback_query, cb_id, text="timed out")
            return False
        except Exception:
            await asyncio.to_thread(self._client.answer_callback_query, cb_id, text="failed")
            return False
        await asyncio.to_thread(self._client.answer_callback_query, cb_id, text=status)
        return ok

    def _authorized(self, user: dict, message: dict) -> bool:
        if int(user.get("id", 0)) not in self._allowed_user_ids:
            return False
        chat = message.get("chat", {})
        if str(chat.get("id")) != self._chat_id:
            return False
        if self._message_thread_id is not None:
            return int(message.get("message_thread_id", 0)) == self._message_thread_id
        return True

    def _action_status(self, result) -> tuple[str, bool]:
        if result.sent:
            return "sent", True
        if result.skipped:
            return "skipped", True
        return result.message or "failed", False
```

Add `_refresh_prompt()` with `editMessageText`:

```python
    async def _refresh_prompt(self, record: TelegramAlertRecord) -> None:
        agent = self._control.current_agent(record.key)
        if agent is None:
            text = "agent is no longer available"
            markup = None
        else:
            try:
                prompt = await self._control.read_prompt(record.key, timeout=3.0)
            except Exception:
                prompt = ""
            text, markup = self._formatter.blocked_alert(
                agent,
                metadata_body=f"{agent.repo or agent.label}",
                prompt=prompt,
                token=record.token,
            )
        await asyncio.to_thread(
            self._client.edit_message_text,
            chat_id=record.chat_id,
            message_id=record.message_id,
            text=text,
            message_thread_id=self._message_thread_id,
            reply_markup=markup,
        )
```

- [ ] **Step 5: Implement poll once**

Add to `TelegramInteractor`:

```python
    async def poll_once(
        self, *, timeout: int = 20, is_current: Callable[[], bool] | None = None
    ) -> None:
        if self._inbound_disabled:
            return
        try:
            updates = await asyncio.to_thread(
                self._client.get_updates, offset=self.offset, timeout=timeout
            )
        except TelegramApiError as exc:
            if self._is_webhook_conflict(exc):
                self._inbound_disabled = True
                log.warning("telegram inbound polling disabled: %s", exc.description)
                return
            raise
        updates = updates or []
        if is_current is not None and not is_current():
            return
        for update in updates:
            if is_current is not None and not is_current():
                return
            update_id = update.get("update_id")
            try:
                await self.process_update(update)
            finally:
                if isinstance(update_id, int):
                    self._offset = update_id + 1
```

- [ ] **Step 6: Run green tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_telegram.py::test_callback_approve_requires_allowed_user_chat_and_topic \
  tests/test_telegram.py::test_callback_from_wrong_user_does_not_run_action \
  tests/test_telegram.py::test_callback_from_wrong_chat_does_not_run_action \
  tests/test_telegram.py::test_callback_from_wrong_topic_does_not_run_action \
  tests/test_telegram.py::test_callback_old_token_is_stale_after_reblock_alert \
  tests/test_telegram.py::test_callback_action_error_is_answered_without_raising \
  tests/test_telegram.py::test_callback_failed_action_result_uses_failure_message \
  tests/test_telegram.py::test_poll_once_advances_offset_after_processing_update \
  tests/test_telegram.py::test_poll_once_advances_offset_when_callback_ack_fails_after_action \
  tests/test_telegram.py::test_poll_once_prunes_expired_alert_before_callback \
  tests/test_telegram.py::test_poll_once_skips_updates_when_generation_guard_is_stale \
  tests/test_telegram.py::test_poll_once_disables_inbound_on_webhook_conflict_but_keeps_outbound_alerts \
  -q
```

Expected: PASS.

- [ ] **Step 7: Commit and Roborev**

```bash
git add src/herdeck/telegram.py tests/test_telegram.py
git commit -m "feat: process telegram callback actions"
sha=$(git rev-parse --short HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

Expected: Roborev has no findings before continuing.

---

### Task 7: Reply-To-Agent Routing And Status

**Files:**
- Modify: `src/herdeck/telegram.py`
- Test: `tests/test_telegram.py`

- [ ] **Step 1: Write failing reply tests**

Append to `tests/test_telegram.py`:

```python
class ReplyControl(ActionControl):
    async def send_text(self, key, text, *, timeout=3.0):
        self.actions.append(("send_text", key, text))
        from herdeck.app_control import ActionResult

        return ActionResult(True)


@pytest.mark.asyncio
async def test_reply_to_known_alert_sends_text_to_agent():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    calls = []
    client = TelegramBotClient("TOK", request=lambda method, fields: calls.append((method, fields)) or {"message_id": 10})
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    store.create(AgentKey("local", "p1"), chat_id="-1001", message_id=9)
    control = ReplyControl()
    interactor = TelegramInteractor(
        client,
        control,
        chat_id="-1001",
        message_thread_id=456,
        allowed_user_ids=[111],
        store=store,
    )

    ok = await interactor.process_update(
        {
            "update_id": 2,
            "message": {
                "chat": {"id": -1001},
                "from": {"id": 111},
                "message_thread_id": 456,
                "reply_to_message": {"message_id": 9},
                "text": "please continue",
            },
        }
    )

    assert ok is True
    assert control.actions == [("send_text", AgentKey("local", "p1"), "please continue")]
    assert calls[-1][0] == "sendMessage"
    assert "sent to local:p1" in calls[-1][1]["text"]


@pytest.mark.asyncio
async def test_reply_send_text_timeout_reports_failure_status():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    calls = []
    client = TelegramBotClient("TOK", request=lambda method, fields: calls.append((method, fields)) or {"message_id": 10})
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    store.create(AgentKey("local", "p1"), chat_id="-1001", message_id=9)

    class TimeoutReplyControl(ReplyControl):
        async def send_text(self, key, text, *, timeout=3.0):
            self.actions.append(("send_text", key, text))
            raise TimeoutError("agent did not answer")

    control = TimeoutReplyControl()
    interactor = TelegramInteractor(
        client,
        control,
        chat_id="-1001",
        message_thread_id=456,
        allowed_user_ids=[111],
        store=store,
    )

    ok = await interactor.process_update(
        {
            "update_id": 2,
            "message": {
                "chat": {"id": -1001},
                "from": {"id": 111},
                "message_thread_id": 456,
                "reply_to_message": {"message_id": 9},
                "message_id": 30,
                "text": "please continue",
            },
        }
    )

    assert ok is False
    assert control.actions == [("send_text", AgentKey("local", "p1"), "please continue")]
    assert calls[-1] == (
        "sendMessage",
        {
            "chat_id": "-1001",
            "text": "delivery timed out",
            "disable_notification": "true",
            "message_thread_id": "456",
            "reply_to_message_id": "30",
        },
    )


@pytest.mark.asyncio
async def test_reply_from_wrong_chat_does_not_send_text():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    calls = []
    client = TelegramBotClient("TOK", request=lambda method, fields: calls.append((method, fields)) or {"message_id": 10})
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    store.create(AgentKey("local", "p1"), chat_id="-1001", message_id=9)
    control = ReplyControl()
    interactor = TelegramInteractor(
        client,
        control,
        chat_id="-1001",
        message_thread_id=456,
        allowed_user_ids=[111],
        store=store,
    )

    ok = await interactor.process_update(
        {
            "update_id": 2,
            "message": {
                "chat": {"id": -9999},
                "from": {"id": 111},
                "message_thread_id": 456,
                "reply_to_message": {"message_id": 9},
                "message_id": 30,
                "text": "please continue",
            },
        }
    )

    assert ok is False
    assert control.actions == []
    assert calls == []


@pytest.mark.asyncio
async def test_reply_from_wrong_topic_does_not_send_text():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    calls = []
    client = TelegramBotClient("TOK", request=lambda method, fields: calls.append((method, fields)) or {"message_id": 10})
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    store.create(AgentKey("local", "p1"), chat_id="-1001", message_id=9)
    control = ReplyControl()
    interactor = TelegramInteractor(
        client,
        control,
        chat_id="-1001",
        message_thread_id=456,
        allowed_user_ids=[111],
        store=store,
    )

    ok = await interactor.process_update(
        {
            "update_id": 2,
            "message": {
                "chat": {"id": -1001},
                "from": {"id": 111},
                "message_thread_id": 999,
                "reply_to_message": {"message_id": 9},
                "message_id": 30,
                "text": "please continue",
            },
        }
    )

    assert ok is False
    assert control.actions == []
    assert calls == []


@pytest.mark.asyncio
async def test_status_requires_authorization_before_listing_alerts():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    calls = []
    client = TelegramBotClient("TOK", request=lambda method, fields: calls.append((method, fields)) or {"message_id": 10})
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    store.create(AgentKey("local", "p1"), chat_id="-1001", message_id=9)
    interactor = TelegramInteractor(
        client,
        ReplyControl(),
        chat_id="-1001",
        message_thread_id=456,
        allowed_user_ids=[111],
        store=store,
    )

    ok = await interactor.process_update(
        {
            "update_id": 3,
            "message": {
                "chat": {"id": -1001},
                "from": {"id": 999},
                "message_thread_id": 456,
                "text": "/status",
            },
        }
    )

    assert ok is False
    assert calls == []


@pytest.mark.asyncio
async def test_status_from_wrong_chat_does_not_list_alerts():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    calls = []
    client = TelegramBotClient("TOK", request=lambda method, fields: calls.append((method, fields)) or {"message_id": 10})
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    store.create(AgentKey("local", "p1"), chat_id="-1001", message_id=9)
    interactor = TelegramInteractor(
        client,
        ReplyControl(),
        chat_id="-1001",
        message_thread_id=456,
        allowed_user_ids=[111],
        store=store,
    )

    ok = await interactor.process_update(
        {
            "update_id": 3,
            "message": {
                "chat": {"id": -9999},
                "from": {"id": 111},
                "message_thread_id": 456,
                "text": "/status",
            },
        }
    )

    assert ok is False
    assert calls == []


@pytest.mark.asyncio
async def test_status_from_wrong_topic_does_not_list_alerts():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    calls = []
    client = TelegramBotClient("TOK", request=lambda method, fields: calls.append((method, fields)) or {"message_id": 10})
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    store.create(AgentKey("local", "p1"), chat_id="-1001", message_id=9)
    interactor = TelegramInteractor(
        client,
        ReplyControl(),
        chat_id="-1001",
        message_thread_id=456,
        allowed_user_ids=[111],
        store=store,
    )

    ok = await interactor.process_update(
        {
            "update_id": 3,
            "message": {
                "chat": {"id": -1001},
                "from": {"id": 111},
                "message_thread_id": 999,
                "text": "/status",
            },
        }
    )

    assert ok is False
    assert calls == []


@pytest.mark.asyncio
async def test_status_prunes_no_longer_blocked_alerts_before_listing():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    calls = []
    client = TelegramBotClient("TOK", request=lambda method, fields: calls.append((method, fields)) or {"message_id": 10})
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    key = AgentKey("local", "p1")
    store.create(key, chat_id="-1001", message_id=9)

    class DoneControl(ReplyControl):
        def current_agent(self, key):
            return AgentState(key, "codex", "herdeck", Status.DONE)

    interactor = TelegramInteractor(
        client,
        DoneControl(),
        chat_id="-1001",
        message_thread_id=456,
        allowed_user_ids=[111],
        store=store,
    )

    ok = await interactor.process_update(
        {
            "update_id": 3,
            "message": {
                "chat": {"id": -1001},
                "from": {"id": 111},
                "message_thread_id": 456,
                "text": "/status",
                "message_id": 30,
            },
        }
    )

    assert ok is True
    assert store.by_token("tok1") is None
    assert (
        "sendMessage",
        {
            "chat_id": "-1001",
            "text": "no tracked blocked alerts",
            "disable_notification": "true",
            "message_thread_id": "456",
            "reply_to_message_id": "30",
        },
    ) in calls
```

- [ ] **Step 2: Run red tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_telegram.py::test_reply_to_known_alert_sends_text_to_agent \
  tests/test_telegram.py::test_reply_send_text_timeout_reports_failure_status \
  tests/test_telegram.py::test_reply_from_wrong_chat_does_not_send_text \
  tests/test_telegram.py::test_reply_from_wrong_topic_does_not_send_text \
  tests/test_telegram.py::test_status_requires_authorization_before_listing_alerts \
  tests/test_telegram.py::test_status_from_wrong_chat_does_not_list_alerts \
  tests/test_telegram.py::test_status_from_wrong_topic_does_not_list_alerts \
  tests/test_telegram.py::test_status_prunes_no_longer_blocked_alerts_before_listing \
  -q
```

Expected: FAIL because `_process_message()` does not yet implement reply routing or `/status`.

- [ ] **Step 3: Implement message routing**

In `TelegramInteractor`, add:

```python
    async def _process_message(self, message: dict) -> bool:
        user = message.get("from", {})
        if not self._authorized(user, message):
            return False
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            return False
        if text.strip() == "/status":
            await self._send_status(message)
            return True
        reply = message.get("reply_to_message") or {}
        reply_id = reply.get("message_id")
        if reply_id is None:
            return False
        record = self._store.by_message(self._chat_id, int(reply_id))
        if record is None:
            await asyncio.to_thread(
                self._client.send_message,
                chat_id=self._chat_id,
                text="alert is stale",
                sound=False,
                message_thread_id=self._message_thread_id,
                reply_to_message_id=message.get("message_id"),
            )
            return False
        try:
            result = await self._control.send_text(record.key, text, timeout=3.0)
            status = f"sent to {record.key.server_id}:{record.key.pane_id}" if result.sent else result.message
            ok = result.sent
        except TimeoutError:
            status = "delivery timed out"
            ok = False
        except Exception:
            status = "delivery failed"
            ok = False
        await asyncio.to_thread(
            self._client.send_message,
            chat_id=self._chat_id,
            text=status,
            sound=False,
            message_thread_id=self._message_thread_id,
            reply_to_message_id=message.get("message_id"),
        )
        return ok
```

Add status helper:

```python
    async def _send_status(self, message: dict) -> None:
        self._prune_alerts()
        records = self._store.records()
        if records:
            lines = ["tracked blocked alerts:"]
            lines.extend(f"- {r.key.server_id}:{r.key.pane_id}" for r in records)
        else:
            lines = ["no tracked blocked alerts"]
        await asyncio.to_thread(
            self._client.send_message,
            chat_id=self._chat_id,
            text="\n".join(lines),
            sound=False,
            message_thread_id=self._message_thread_id,
            reply_to_message_id=message.get("message_id"),
        )
```

`TelegramAlertStore.records()` was added in Task 3 so `_send_status()` and pruning can share the same tracked-alert view.

- [ ] **Step 4: Run green tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_telegram.py::test_reply_to_known_alert_sends_text_to_agent \
  tests/test_telegram.py::test_reply_send_text_timeout_reports_failure_status \
  tests/test_telegram.py::test_reply_from_wrong_chat_does_not_send_text \
  tests/test_telegram.py::test_reply_from_wrong_topic_does_not_send_text \
  tests/test_telegram.py::test_status_requires_authorization_before_listing_alerts \
  tests/test_telegram.py::test_status_from_wrong_chat_does_not_list_alerts \
  tests/test_telegram.py::test_status_from_wrong_topic_does_not_list_alerts \
  tests/test_telegram.py::test_status_prunes_no_longer_blocked_alerts_before_listing \
  tests/test_telegram.py::test_callback_from_wrong_user_does_not_run_action \
  -q
```

Expected: PASS.

- [ ] **Step 5: Commit and Roborev**

```bash
git add src/herdeck/telegram.py tests/test_telegram.py
git commit -m "feat: route telegram replies to agents"
sha=$(git rev-parse --short HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

Expected: Roborev has no findings before continuing.

---

### Task 8: Runtime Wiring, Docs, And Final Verification

**Files:**
- Modify: `src/herdeck/app.py`
- Modify: `README.md`
- Modify: `config.example.toml`
- Test: `tests/test_app.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write failing runtime wiring test**

Add to `tests/test_app.py`:

```python
def test_run_builds_interactive_telegram_notifier_factory_contract():
    from herdeck.app import _build_blocked_notification_runtime
    from herdeck.config import TelegramConfig

    cfg = make_config()
    cfg.notifications.enabled = True
    cfg.notifications.backends = ["telegram"]
    cfg.notifications.telegram = TelegramConfig(
        "HERDECK_TG",
        "-1001",
        message_thread_id=456,
        interactive=True,
        allowed_user_ids=[111],
    )
    made = []

    class Interactive:
        async def notify_blocked(self, agent, *, body, sound, multi_server):
            pass

        async def poll_once(self, *, timeout=20, is_current=None):
            pass

    runtime = _build_blocked_notification_runtime(
        cfg,
        getenv=lambda name: "TOK",
        telegram_interactor_factory=lambda token, tg: made.append(
            (token, tg.chat_id, tg.message_thread_id, tg.allowed_user_ids)
        )
        or Interactive(),
    )

    assert made == [("TOK", "-1001", 456, [111])]
    assert isinstance(runtime.poller, Interactive)


def test_install_telegram_runtime_sets_factory_poller_and_updates_control():
    from herdeck.app import App, _install_telegram_runtime
    from herdeck.config import TelegramConfig

    cfg = make_config()
    cfg.notifications.enabled = True
    cfg.notifications.backends = ["telegram"]
    cfg.notifications.telegram = TelegramConfig(
        "HERDECK_TG",
        "-1001",
        message_thread_id=456,
        interactive=True,
        allowed_user_ids=[111],
    )
    updates = []
    made = []

    class Control:
        def update_config(self, config):
            updates.append(config)

    class Poller:
        async def notify_blocked(self, agent, *, body, sound, multi_server):
            pass

        async def poll_once(self, *, timeout=20, is_current=None):
            pass

    app = App(cfg, FakeRenderer(13), send=lambda c: None)

    _install_telegram_runtime(
        app,
        cfg,
        Control(),
        getenv=lambda name: "TOK",
        bot_client_factory=lambda token: ("client", token),
        interactor_factory=lambda client, control, **kwargs: made.append((client, kwargs)) or Poller(),
    )

    assert updates == [cfg]
    assert made == [
        (
            ("client", "TOK"),
            {
                "chat_id": "-1001",
                "message_thread_id": 456,
                "allowed_user_ids": [111],
                "prompt_max_chars": 1200,
            },
        )
    ]
    assert app.notification_poller is not None


def test_start_telegram_poll_loop_schedules_and_uses_current_poller():
    import asyncio

    from herdeck.app import App, _poll_telegram_once_from_app, _start_telegram_poll_loop
    from herdeck.notify import BlockedNotificationRuntime

    cfg = make_config()
    app = App(cfg, FakeRenderer(13), send=lambda c: None)
    calls = []
    guards = []

    class Poller:
        async def poll_once(self, *, timeout=20, is_current=None):
            guards.append(is_current)
            calls.append(("poll", timeout, is_current()))

    async def fail_sleep(delay):
        raise AssertionError("poller is present; idle sleep should not run")

    app.notification_poller = Poller()
    asyncio.run(_poll_telegram_once_from_app(app, timeout=7, idle_sleep=fail_sleep))

    created = []
    task = _start_telegram_poll_loop(app, create_task=lambda coro: created.append(coro) or object())

    assert calls == [("poll", 7, True)]
    assert guards[0]() is True
    app._install_blocked_runtime(BlockedNotificationRuntime(app.blocked_notifier))
    assert guards[0]() is False
    assert task is not None
    assert created
    created[0].close()


def test_poll_telegram_once_sleeps_after_poller_disables_inbound():
    import asyncio

    from herdeck.app import App, _poll_telegram_once_from_app

    cfg = make_config()
    app = App(cfg, FakeRenderer(13), send=lambda c: None)
    calls = []
    sleeps = []

    class Poller:
        inbound_disabled = False

        async def poll_once(self, *, timeout=20, is_current=None):
            calls.append(("poll", timeout, is_current()))
            self.inbound_disabled = True

    async def record_sleep(delay):
        sleeps.append(delay)

    app.notification_poller = Poller()
    asyncio.run(_poll_telegram_once_from_app(app, timeout=7, idle_sleep=record_sleep))

    assert calls == [("poll", 7, True)]
    assert sleeps == [60]
```

- [ ] **Step 2: Write failing docs/config example tests**

Add to `tests/test_config.py`:

```python
def test_readme_documents_interactive_telegram_security():
    path = Path(__file__).resolve().parents[1] / "README.md"
    section = path.read_text().split("## Notifications", 1)[1].split("\n## ", 1)[0]

    assert "interactive = true" in section
    assert "allowed_user_ids" in section
    assert "message_thread_id" in section
    assert "Reply to this message" in section
    assert "Non-interactive notifications contain only" in section
    assert "Notifications contain only the repo/label" not in section


def test_example_config_includes_interactive_telegram_fields(monkeypatch):
    monkeypatch.setenv("HERDECK_WORKBOX_TOKEN", "secret123")
    path = Path(__file__).resolve().parents[1] / "config.example.toml"
    text = path.read_text()

    assert "message_thread_id" in text
    assert "interactive = false" in text
    assert "allowed_user_ids" in text
```

- [ ] **Step 3: Run red tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_app.py::test_run_builds_interactive_telegram_notifier_factory_contract \
  tests/test_app.py::test_install_telegram_runtime_sets_factory_poller_and_updates_control \
  tests/test_app.py::test_start_telegram_poll_loop_schedules_and_uses_current_poller \
  tests/test_app.py::test_poll_telegram_once_sleeps_after_poller_disables_inbound \
  tests/test_config.py::test_readme_documents_interactive_telegram_security \
  tests/test_config.py::test_example_config_includes_interactive_telegram_fields \
  -q
```

Expected: FAIL for missing runtime helper functions, missing disabled-poller backoff, and docs/config fields. The builder test may already pass from Task 5; keep it as a regression test for the explicit polling reference.

- [ ] **Step 4: Wire interactive runtime in `_run()`**

In `src/herdeck/app.py`, import:

```python
from .notify import BlockedNotificationRuntime
from .telegram import TelegramBotClient, TelegramInteractor
```

Add testable helpers near `_run()`:

```python
def _install_telegram_runtime(
    app: App,
    config: Config,
    runtime_control: RuntimeAgentControl,
    *,
    getenv=get_secret,
    bot_client_factory=TelegramBotClient,
    interactor_factory=TelegramInteractor,
) -> None:
    def build_blocked_runtime(runtime_config: Config) -> BlockedNotificationRuntime:
        runtime_control.update_config(runtime_config)

        def make_interactor(token: str, tg):
            return interactor_factory(
                bot_client_factory(token),
                runtime_control,
                chat_id=tg.chat_id,
                message_thread_id=tg.message_thread_id,
                allowed_user_ids=tg.allowed_user_ids,
                prompt_max_chars=tg.prompt_max_chars,
            )

        return _build_blocked_notification_runtime(
            runtime_config,
            getenv=getenv,
            telegram_interactor_factory=make_interactor,
        )

    app.set_blocked_runtime_factory(build_blocked_runtime)


async def _poll_telegram_once_from_app(
    app: App,
    *,
    timeout: int = 20,
    idle_sleep=asyncio.sleep,
) -> None:
    generation = app.notification_generation
    poller = app.notification_poller
    if poller is None:
        await idle_sleep(1)
        return
    if getattr(poller, "inbound_disabled", False):
        await idle_sleep(60)
        return
    await poller.poll_once(
        timeout=timeout,
        is_current=lambda: app.notification_generation == generation,
    )
    if getattr(poller, "inbound_disabled", False):
        await idle_sleep(60)


def _start_telegram_poll_loop(app: App, *, create_task=asyncio.create_task):
    async def poll_telegram():
        while True:
            try:
                await _poll_telegram_once_from_app(app)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("telegram poll failed", exc_info=True)
                await asyncio.sleep(2)

    return create_task(poll_telegram())
```

Inside `_run()`, create a runtime control after `manager` exists and before the final app is fully used. The implementation should avoid circular references by constructing the app first with legacy notifier, then installing the helper-backed runtime factory after `runtime_control` exists:

```python
    app = App(
        config,
        deck,
        send,
        schedule=lambda fn: loop.call_soon_threadsafe(fn),
        notifier=_build_notifier(config),
        notify_schedule=lambda coro: asyncio.create_task(coro),
        switch_profile=switch_profile,
        update_connectors=lambda cfg: manager.update(cfg.servers),
        config_reloader=config_reloader,
    )

    async def runtime_send(cmd: Command, req: str) -> None:
        conn = manager.get(cmd.server_id)
        if conn is not None:
            await conn.send(command_to_msg(cmd, req))

    runtime_control = RuntimeAgentControl(
        config,
        send=runtime_send,
        current_agent=app.orch.get_agent,
    )
    app.set_runtime_control(runtime_control)
    _install_telegram_runtime(app, config, runtime_control)
    telegram_tasks = [_start_telegram_poll_loop(app)]
```

Include `telegram_tasks` in the `tasks` list gathered by `_run()`. Keep the same lifecycle: polling starts once, is cancellable, uses the latest poller after reload/profile switch, sleeps when interactive Telegram is disabled, and network exceptions do not crash the app.

- [ ] **Step 5: Update README**

In the `## Notifications` section of `README.md`, extend the Telegram example:

```toml
[notifications.telegram]
token_env = "HERDECK_TELEGRAM_TOKEN"
chat_id = "-1001234567890"

# Optional: route alerts into a Telegram forum topic, e.g. a Hermes topic.
message_thread_id = 456

# Optional: enable buttons + reply-to-agent.
interactive = true
allowed_user_ids = [123456789]
prompt_max_chars = 1200
```

Replace the existing security bullet that currently says all notifications never include prompt text with:

```markdown
- Non-interactive notifications contain only the repo/label, branch, and
  (multi-server) server id; they never include prompt text, command output, or
  tokens. When `interactive = true`, Telegram alerts include the current
  blocked prompt, Approve/Deny/Stop/Read again buttons, and reply routing.
  Reply to the bot's alert message to send text to that specific agent.
  Herdeck accepts inbound actions only from `allowed_user_ids`, only in the
  configured `chat_id`, and only in `message_thread_id` when one is configured.
```

- [ ] **Step 6: Update config example**

In `config.example.toml`, under `[profiles.mobile.notifications.telegram]`, add:

```toml
# Optional Hermes/forum topic and interactive controls.
# message_thread_id = 456
interactive = false
allowed_user_ids = []
prompt_max_chars = 1200
```

- [ ] **Step 7: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_app.py::test_run_builds_interactive_telegram_notifier_factory_contract \
  tests/test_app.py::test_install_telegram_runtime_sets_factory_poller_and_updates_control \
  tests/test_app.py::test_start_telegram_poll_loop_schedules_and_uses_current_poller \
  tests/test_app.py::test_poll_telegram_once_sleeps_after_poller_disables_inbound \
  tests/test_config.py::test_readme_documents_interactive_telegram_security \
  tests/test_config.py::test_example_config_includes_interactive_telegram_fields \
  tests/test_notify.py \
  tests/test_secrets.py \
  tests/test_telegram.py \
  tests/test_app_control.py \
  -q
```

Expected: PASS.

- [ ] **Step 8: Run full gates**

Run:

```bash
.venv/bin/ruff check src tests
.venv/bin/python -m pytest -q
```

Expected:

```text
All checks passed!
all tests passed
```

The pytest count will be higher than the 574-test baseline because this plan adds tests. Treat every failure as a blocker.

- [ ] **Step 9: Commit and Roborev**

```bash
git add src/herdeck/app.py README.md config.example.toml tests/test_app.py tests/test_config.py
git commit -m "docs: document interactive telegram setup"
sha=$(git rev-parse --short HEAD)
roborev show "$sha" || roborev review "$sha" --wait
roborev show "$sha"
```

Expected: Roborev has no findings.

- [ ] **Step 10: Final status**

Run:

```bash
git status --short --branch
git log --oneline --max-count=10
```

Expected:

```text
## feat/telegram-interactive-notifications
```

with no uncommitted changes. Report the final commit list, full gate results, and Roborev status.

---

## Self-Review Checklist

- Spec coverage:
  - Config fields: Task 1.
  - Typed notifier boundary: Task 2.
  - Bot API client, formatter, store: Task 3.
  - Request broker/control adapter: Task 4.
  - Rich outbound alerts: Task 5.
  - Long-poll callback buttons: Task 6.
  - Reply-to-agent and `/status` auth: Task 7.
  - Docs/example/runtime wiring: Task 8.
- Security coverage:
  - Token values are only read through `get_secret`.
  - `allowed_user_ids`, `chat_id`, and `message_thread_id` are tested before callback actions.
  - Reply routing and `/status` reject wrong `chat_id` and wrong `message_thread_id` before sending any response.
  - Old alert tokens are discarded when a pane blocks again, so stale buttons cannot act on a new prompt.
  - Approve/deny still use guarded `act_if_blocked`.
  - Connector action results with `sent=false` are surfaced as failures, not reported as sent.
  - Reply text only routes when it is a reply to a known alert.
- Verification:
  - Each task has a red test command, green command, conventional commit, and Roborev gate.
  - Final task runs Ruff and full pytest.
