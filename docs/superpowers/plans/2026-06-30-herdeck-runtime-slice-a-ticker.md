# herdeck Runtime — Slice A (DeckApp ticker) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the desktop deckapp window a background ticker so a working agent's tile animates (the spinner advances) the same way it does on the D200 — fixing the "spinner nefunguje / okno zaseknuté" symptom and laying the foundation for the converged runtime.

**Architecture:** The `DeckApp` (herdeck.deckapp.server) already renders an Orchestrator's `RenderState` into a version-diffed HTTP tile buffer served at `/state`; it just never advances the spinner. This slice adds a daemon **ticker thread** that, every `tick_interval` seconds, calls `orch.tick()` (advance the spinner phase) then re-renders. The existing per-tile version diff means only WORKING tiles (whose PNG changes with the phase) bump their version — idle tiles stay quiet, so the `/state` poller re-fetches only the animating tiles. The live build enables the ticker; the mock build leaves it off (deterministic).

**Tech Stack:** Python 3.12+ (threading, dataclasses, Pillow), pytest.

**Spec:** `docs/superpowers/specs/2026-06-30-herdeck-runtime-convergence-design.md` (this is Slice A; Slice B = D200 sink + discovery + runtime entry + Tauri attach-or-spawn, separate plan).

## Global Constraints

- The ticker MUST run only when the app is actually serving AND `tick_interval > 0`. Gate: `if serve and tick_interval > 0`. The mock/test path (default `tick_interval=0.0`) MUST NOT spawn a ticker — `/state` stays deterministic for existing tests.
- The ticker MUST NOT churn idle decks: a tick with no working agent must not bump any tile version (guaranteed by the existing PNG-equality version diff in `_apply_rendered_locked` — do not change that).
- `orch.tick()` + the re-render MUST happen together under `self._lock` (the same lock presses/bridge-callbacks take), so a tick never races a press or a bridge update.
- `tick_interval` default is `0.0` on `DeckApp.__init__`. The live build uses `config.hardware.tick_interval` (default `0.4`).
- `close()` MUST stop and join the ticker thread before tearing down the source/server.
- Commits: Conventional Commits, English; NO `Co-Authored-By` trailer; never squash. After each commit check `roborev show <sha>` and fix findings.
- Before done: `ruff check src tests` clean and `pytest` green.

---

### Task 1: DeckApp ticker mechanism (`_tick_once` + ticker thread + lifecycle)

**Files:**
- Modify: `src/herdeck/deckapp/server.py` (`DeckApp.__init__` at lines 44-101; `close()` at lines 170-202; add two methods)
- Test: `tests/test_deckapp_live.py`

**Interfaces:**
- Produces: `DeckApp(..., tick_interval: float = 0.0)`. New methods `DeckApp._tick_once() -> None` (advance spinner phase + re-render under the lock) and `DeckApp._ticker_loop() -> None` (the daemon loop). New attrs `self._tick_interval: float`, `self._ticker_stop: threading.Event`, `self._ticker_thread: threading.Thread | None`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_deckapp_live.py` (the `make_live`, `agent`, `StubIcons`, `Status` helpers already exist; `io`, `Image` already imported). Add this spinner-aware stub and a builder near `StubIcons` (top of file):

```python
class SpinIcons:
    """Like StubIcons but its bytes depend on the spinner phase, so a tick that
    advances a working tile's phase changes that tile's PNG (and only that one)."""

    def render_tile_bytes(self, tile):
        sig = f"{tile.index}|{tile.status_text}|{tile.spinner}"
        buf = io.BytesIO()
        c = sum(sig.encode()) % 256
        Image.new("RGB", (4, 4), (c, c, c)).save(buf, "PNG")
        return buf.getvalue()


def make_live_icons(icons, *, serve=False, tick_interval=0.0):
    config, server = live_config()
    src = LiveSource(config, server)
    src.attach_runner(FakeRunner())
    app = DeckApp(src, serve=serve, icon_provider=icons, tick_interval=tick_interval)
    return app, src, server
```

Then add these tests at the end of the file:

```python
def test_tick_once_advances_spinner_phase():
    app, src, server = make_live_icons(StubIcons())
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    app.refresh()
    before = app._orch._phase
    app._tick_once()
    assert app._orch._phase == before + 1


def test_tick_once_animates_working_tile():
    app, src, server = make_live_icons(SpinIcons())
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    app.refresh()
    v = app._state()["version"]
    app._tick_once()
    assert app._state()["version"] > v  # the working tile re-keyed (spinner advanced)


def test_tick_once_quiet_when_all_idle():
    app, src, server = make_live_icons(SpinIcons())
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.IDLE)])
    app.refresh()
    v = app._state()["version"]
    app._tick_once()
    assert app._state()["version"] == v  # idle deck does not churn /state


def test_ticker_thread_not_started_without_serve_or_interval():
    app_a, _, _ = make_live_icons(StubIcons(), serve=False, tick_interval=0.0)
    app_b, _, _ = make_live_icons(StubIcons(), serve=False, tick_interval=0.4)
    assert app_a._ticker_thread is None  # tick_interval 0 -> no ticker
    assert app_b._ticker_thread is None  # serve=False -> no ticker (not actually serving)


def test_ticker_thread_runs_and_stops_when_serving():
    app, src, server = make_live_icons(SpinIcons(), serve=True, tick_interval=0.02)
    try:
        assert app._ticker_thread is not None and app._ticker_thread.is_alive()
    finally:
        app.close()
    assert not app._ticker_thread or not app._ticker_thread.is_alive()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_deckapp_live.py -k "tick_once or ticker_thread" -v`
Expected: FAIL — `DeckApp.__init__()` rejects `tick_interval` (unexpected keyword) / `_tick_once` and `_ticker_thread` do not exist.

- [ ] **Step 3: Add the `tick_interval` param and ticker startup to `__init__`**

In `src/herdeck/deckapp/server.py`, add the param to the `DeckApp.__init__` signature (after `clock=None,` at line 54):

```python
        clock=None,
        tick_interval: float = 0.0,
        config_service=None,
        reloader=None,
    ):
```

Then at the END of `__init__` (right after `self.host, self.port = host, port` at line 101), add the ticker setup:

```python
            self.host, self.port = host, port

        # Background ticker: advance the spinner phase + re-render every
        # tick_interval seconds so working tiles animate in the served /state.
        # Only when actually serving (mock/test path leaves it off -> deterministic).
        self._tick_interval = tick_interval
        self._ticker_stop = threading.Event()
        self._ticker_thread: threading.Thread | None = None
        if serve and tick_interval > 0:
            self._ticker_thread = threading.Thread(
                target=self._ticker_loop, name="herdeck-deckapp-tick", daemon=True
            )
            self._ticker_thread.start()
```

- [ ] **Step 4: Add `_tick_once` and `_ticker_loop` methods**

In `src/herdeck/deckapp/server.py`, add these two methods right after `_refresh_locked` (after line 160):

```python
    def _tick_once(self) -> None:
        """Advance the spinner phase and re-render once, atomically w.r.t. presses
        and bridge updates (same lock). Only working tiles change bytes, so the
        version diff bumps just them."""
        with self._lock:
            self._orch.tick()
            self._refresh_locked()

    def _ticker_loop(self) -> None:
        # Event.wait returns False on timeout (a tick is due) and True once close()
        # sets the event (clean stop) — so this never busy-waits and exits promptly.
        while not self._ticker_stop.wait(self._tick_interval):
            self._tick_once()
```

- [ ] **Step 5: Stop and join the ticker in `close()`**

In `src/herdeck/deckapp/server.py`, at the START of `close()` (right after `def close(self) -> None:` at line 170, before the `watcher = ...` line), add:

```python
    def close(self) -> None:
        ticker = getattr(self, "_ticker_thread", None)
        if ticker is not None:
            self._ticker_stop.set()
            if ticker is not threading.current_thread():
                ticker.join(timeout=2)
            self._ticker_thread = None
        watcher = getattr(self, "_watcher", None)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_deckapp_live.py -k "tick_once or ticker_thread" -v`
Expected: PASS (5 tests).

- [ ] **Step 7: Run the full deckapp_live suite for regressions**

Run: `.venv/bin/python -m pytest tests/test_deckapp_live.py -q`
Expected: PASS — no existing live test regresses (default `tick_interval=0.0` → no behavior change).

- [ ] **Step 8: Commit**

```bash
git add src/herdeck/deckapp/server.py tests/test_deckapp_live.py
git commit -m "feat(deckapp): add background ticker so working tiles animate"
```

---

### Task 2: Enable the ticker for the live build

**Files:**
- Modify: `src/herdeck/deckapp/server.py` (`create_live_app` at lines 663-695)
- Test: `tests/test_deckapp_live.py`

**Interfaces:**
- Consumes: `DeckApp(..., tick_interval=...)` (Task 1).
- Produces: `create_live_app` builds the `DeckApp` with `tick_interval=config.hardware.tick_interval`, so a live serving app animates while the mock build (default `0.0`) does not.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_deckapp_live.py` (the `FakeConnector` fake is already defined in this file):

```python
def test_create_live_app_enables_ticker_from_config():
    config, server = live_config()
    app = create_live_app(config, server, serve=False, connector_factory=FakeConnector)
    assert app._tick_interval == config.hardware.tick_interval
    assert config.hardware.tick_interval == 0.4  # the live default that drives animation
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_deckapp_live.py::test_create_live_app_enables_ticker_from_config -v`
Expected: FAIL — `app._tick_interval` is `0.0` (create_live_app does not pass it yet).

- [ ] **Step 3: Pass `tick_interval` from config in `create_live_app`**

In `src/herdeck/deckapp/server.py`, in the `DeckApp(...)` call inside `create_live_app` (lines 686-695), add the `tick_interval` argument:

```python
    return DeckApp(
        source,
        host=host,
        port=port,
        icon_provider=icon_provider,
        serve=serve,
        clock=time.monotonic,
        tick_interval=config.hardware.tick_interval,
        config_service=config_service,
        reloader=reloader,
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_deckapp_live.py::test_create_live_app_enables_ticker_from_config -v`
Expected: PASS.

- [ ] **Step 5: Run ruff + the full deckapp/icons/orchestrator suites**

Run: `.venv/bin/ruff check src tests && .venv/bin/python -m pytest tests/test_deckapp_live.py tests/test_deckapp.py -q`
Expected: ruff clean; pytest PASS (the mock-backed `test_deckapp.py` stays deterministic — it does not use `create_live_app`, so its `tick_interval` is `0.0`).

- [ ] **Step 6: Commit**

```bash
git add src/herdeck/deckapp/server.py tests/test_deckapp_live.py
git commit -m "feat(deckapp): drive the live app ticker from config.hardware.tick_interval"
```

---

## Final verification (before finishing the branch)

- [ ] `.venv/bin/ruff check src tests` → clean
- [ ] `.venv/bin/python -m pytest` → green (whole suite; deterministic mock tests unaffected)
- [ ] Manual gate (macbench, after deploy): with the desktop app attached to a live deckapp, a WORKING agent's tile in the window now **animates** (spinner advances) instead of standing still; an all-idle deck does not flicker. (Full D200↔window convergence + consistent elapsed comes in Slice B.)

## Scope note — this is Slice A

This plan delivers the ticker only: the desktop window animates, and the `DeckApp` now owns the tick loop the converged runtime will reuse. It does NOT yet unify the two processes — elapsed numbers still differ and there are still two bridge connections until **Slice B** (D200 render sink + `runtime.json` discovery + headless runtime entry + Tauri attach-or-spawn + deploy), which gets its own plan built on this foundation.

## Self-Review (completed by plan author)

**Spec coverage (Slice A portion):** the spec's "ticker → animace" data-flow item and the "DeckApp gains a ticker" component are implemented (Task 1 mechanism + Task 2 live wiring). The sink fan-out, D200 sink, discovery file, runtime entry, and Tauri attach-or-spawn are explicitly deferred to Slice B (stated above) — not gaps.

**Placeholder scan:** none — every code step has the full code; no TBD/TODO.

**Type/name consistency:** `tick_interval` (param, default 0.0, attr `self._tick_interval`); `_tick_once` / `_ticker_loop` / `_ticker_stop` (Event) / `_ticker_thread` consistent across __init__, the methods, close(), and the tests; `create_live_app` passes `config.hardware.tick_interval`; gate `serve and tick_interval > 0` identical in __init__ and asserted in the tests.
