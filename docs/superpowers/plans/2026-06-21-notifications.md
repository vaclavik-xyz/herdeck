# F1 + F2 — Block notifications + answer-profile docs — Implementation Plan

> **For agentic workers:** strict TDD (failing test → minimal code → commit). Conventional commits (English, no Co-Authored-By). After each commit run `roborev show <sha>` and fix findings.

**Goal:** Notify the user out-of-band when an agent becomes blocked (so they don't
have to watch the deck), and document/clean up answer-profile config.

**Files:**
- Create: `src/herdeck/notify.py`
- Modify: `src/herdeck/config.py` (notification config + answer-profile docs)
- Modify: `src/herdeck/app.py` (detect block transitions, fire notifier)
- Modify: `config.example.toml` (`[notifications]` + answer-profile verification note)
- Test: `tests/test_notify.py` (new), `tests/test_app.py`, `tests/test_config.py`

## Global constraints
- Work in your assigned worktree (lead gives path); verify branch first.
- venv: `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`. Tests: `.venv/bin/python -m pytest`.
- Notifier must be **non-blocking** (run in an executor), **timeout-bounded**, and
  **swallow all errors** — a notification failure must never break the render loop.
- Notification content: agent label, repo/branch, server id (only when multi-server),
  elapsed-blocked if known. **NEVER** include raw prompt text, command output, or tokens.
- Escape strings passed to `osascript` (no shell concatenation of untrusted text).
- Defaults must keep existing `Config(...)` construction in tests working — add new
  config with a default that means "no-op when absent".

---

### Task 1: Notifier abstraction (`notify.py`)
- [ ] **Failing test** `tests/test_notify.py`:
```python
from herdeck.notify import Notifier, NoopNotifier, escape_applescript


def test_escape_applescript_quotes_and_backslashes():
    assert escape_applescript('a"b\\c') == 'a\\"b\\\\c'


def test_noop_notifier_never_raises():
    NoopNotifier().notify("t", "b", sound=True)   # no exception, no side effect


def test_notifier_uses_injected_sink():
    calls = []
    n = Notifier(sink=lambda title, body, sound: calls.append((title, body, sound)))
    n.notify("Blocked", "api · main", sound=True)
    assert calls == [("Blocked", "api · main", True)]


def test_notifier_swallows_sink_errors():
    def boom(*a): raise RuntimeError("x")
    Notifier(sink=boom).notify("t", "b")   # must not raise
```
- [ ] Run → FAIL (`ImportError`).
- [ ] Implement `src/herdeck/notify.py`:
```python
from __future__ import annotations
import logging, subprocess
from collections.abc import Callable

log = logging.getLogger("herdeck.notify")


def escape_applescript(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _macos_sink(title: str, body: str, sound: bool) -> None:
    t, b = escape_applescript(title), escape_applescript(body)
    script = f'display notification "{b}" with title "{t}"'
    if sound:
        script += ' sound name "Glass"'
    subprocess.run(["osascript", "-e", script], timeout=5,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


class Notifier:
    """Fires notifications via an injectable sink; never raises."""
    def __init__(self, sink: Callable[[str, str, bool], None] = _macos_sink):
        self._sink = sink

    def notify(self, title: str, body: str, sound: bool = False) -> None:
        try:
            self._sink(title, body, sound)
        except Exception:
            log.debug("notify failed", exc_info=True)


class NoopNotifier(Notifier):
    def __init__(self):
        super().__init__(sink=lambda *a: None)
```
- [ ] Run → PASS.
- [ ] Commit: `feat(notify): notifier abstraction with applescript escaping`.
- [ ] `roborev show <sha>`; fix findings.

### Task 2: notification config
- [ ] **Failing test** `tests/test_config.py`:
```python
def test_notifications_default_disabled_when_absent(tmp_path):
    cfg = load_config(_write(tmp_path, "[deck]\ngrid=\"5x3\"\n"))
    assert cfg.notifications.enabled is False
    assert cfg.notifications.on == ["blocked"]

def test_notifications_parsed(tmp_path):
    cfg = load_config(_write(tmp_path,
        "[notifications]\nenabled=true\nsound=false\non=[\"blocked\"]\n"))
    assert cfg.notifications.enabled is True and cfg.notifications.sound is False
```
- [ ] Run → FAIL.
- [ ] Implement in `config.py`: add a dataclass and field, parsed in `load_config`:
```python
@dataclass
class Notifications:
    enabled: bool = False
    on: list[str] = field(default_factory=lambda: ["blocked"])
    sound: bool = True
```
  Add `notifications: Notifications = field(default_factory=Notifications)` to `Config`.
  In `load_config`, parse `data.get("notifications", {})` into it (defaults when absent).
- [ ] Run → PASS, full suite green (existing `Config(...)` calls still work via default).
- [ ] Commit: `feat(config): [notifications] config (default disabled)`.
- [ ] `roborev show <sha>`; fix findings.

### Task 3: block-transition detection (pure) + app wiring
- [ ] **Failing test** `tests/test_app.py` (pure helper first):
```python
def test_newly_blocked_detects_transition_and_avoids_dup():
    from herdeck.app import newly_blocked
    from herdeck.model import AgentKey, AgentState, Status
    k = AgentKey("s", "p1")
    s_block = [AgentState(k, "claude", "api", Status.BLOCKED)]
    s_work  = [AgentState(k, "claude", "api", Status.WORKING)]
    to, seen = newly_blocked(set(), s_block)          # first time -> notify
    assert k in to and k in seen
    to2, seen2 = newly_blocked(seen, s_block)         # same blocked -> no dup
    assert to2 == set() and seen2 == seen
    to3, seen3 = newly_blocked(seen2, s_work)         # left blocked -> reset
    assert to3 == set() and k not in seen3
```
- [ ] Run → FAIL.
- [ ] Implement `newly_blocked(prev: set, states) -> tuple[set, set]` in `app.py`:
```python
def newly_blocked(prev, states):
    """Keys that just entered BLOCKED (vs prev), and the updated blocked set.
    Eligibility resets when a key leaves BLOCKED, so a re-block notifies again."""
    blocked_now = {s.key for s in states if s.status is Status.BLOCKED}
    to_notify = blocked_now - prev
    return to_notify, blocked_now
```
- [ ] Run → PASS.
- [ ] **Failing test** (app fires notifier on transition, not on dup):
```python
def test_app_notifies_on_block_transition(monkeypatch):
    from herdeck.app import App
    from herdeck.notify import Notifier
    from herdeck.model import AgentKey, AgentState, Status
    from herdeck.driver.fake import FakeRenderer
    calls = []
    cfg = make_config()                 # this file's helper
    cfg.notifications.enabled = True
    app = App(cfg, FakeRenderer(13), send=lambda c: None,
              notifier=Notifier(sink=lambda t, b, s: calls.append((t, b))))
    app.handle_snapshot("dev", [AgentState(AgentKey("dev","p1"),"claude","api",Status.BLOCKED)])
    assert len(calls) == 1 and "api" in calls[0][1]
    app.handle_snapshot("dev", [AgentState(AgentKey("dev","p1"),"claude","api",Status.BLOCKED)])
    assert len(calls) == 1            # no duplicate while still blocked
```
- [ ] Run → FAIL.
- [ ] Implement in `app.py`: `App.__init__` accepts `notifier=None` (default
  `NoopNotifier()` unless config enables — the lead's `_run`/`_run_local` will build a
  real `Notifier` when `config.notifications.enabled`). Track `self._blocked_keys=set()`.
  In `handle_snapshot` and `handle_event`, after applying state, call a helper:
```python
    def _maybe_notify(self, states):
        if not self.config.notifications.enabled:
            return
        to, self._blocked_keys = newly_blocked(self._blocked_keys, states)
        multi = len(self.config.overview_order) > 1
        for s in (x for x in states if x.key in to):
            label = s.repo or s.label
            parts = [p for p in (s.branch, s.key.server_id if multi else None) if p]
            body = f"{label}" + (f" · {' · '.join(parts)}" if parts else "")
            self._schedule_notify(s.agent_type, body)
```
  `_schedule_notify` runs `self.notifier.notify(...)` off the loop (executor) so it never
  blocks; in tests the injected sink is synchronous. For `handle_event` (single state),
  pass `[state]` and union into the same tracking. Keep `handle_snapshot` passing the
  full server states list. (Ensure per-server tracking doesn't drop other servers'
  blocked keys — track by AgentKey which includes server_id.)
- [ ] Run → PASS, full suite green.
- [ ] Commit: `feat(app): notify on agent block transitions`.
- [ ] `roborev show <sha>`; fix findings.

### Task 4 (F2): answer-profile docs + config example
- [ ] **Failing test** `tests/test_config.py`: assert shipped `DEFAULT_PROFILES` match
  what the example documents for the verified agents (claude, codex), and that a partial
  override still merges over defaults (this may already be covered — if so, add only the
  doc-alignment assertion):
```python
def test_default_profiles_claude_codex_documented():
    from herdeck.config import DEFAULT_PROFILES
    assert DEFAULT_PROFILES["claude"].approve == ["1", "enter"]
    assert DEFAULT_PROFILES["codex"].approve == ["y", "enter"]
```
- [ ] Run → PASS or FAIL (if FAIL, fix the default to the documented value).
- [ ] Update `config.example.toml`: above `[answer_profiles.*]`, add a comment block
  explaining these are keystroke sequences sent on Approve/Deny/Stop, that `claude`/`codex`
  are confirmed, that `cursor`/`gemini` are **not yet verified** and should be confirmed
  against a live prompt before relying on them, and how to override per agent. Do NOT add
  unverified `cursor`/`gemini` answer profiles to `DEFAULT_PROFILES`.
- [ ] Run full suite → green.
- [ ] Commit: `docs(config): document answer-profile verification and overrides`.
- [ ] `roborev show <sha>`; fix findings.

### Done
- Full suite green. Short summary (commits + result). If stuck, describe and stop.
- NOTE for the lead (do in `_run`/`_run_local`): construct
  `Notifier()` when `config.notifications.enabled` else `NoopNotifier()`, pass to `App`.
  Add a line to README about `[notifications]`. (Plan covers App accepting the notifier;
  the run-path wiring is a 2-line lead follow-up if the worker doesn't reach it.)
