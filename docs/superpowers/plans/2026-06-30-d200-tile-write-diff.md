# D200 spinner-stall fix — per-index tile write-diff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the D200 spinner from freezing for seconds by only writing tiles whose rendered image actually changed (per-index write-diff), and add timing instrumentation to the USB write path.

**Architecture:** The D200 driver (`src/herdeck/driver/d200.py`) repaints all 13 tiles every 10s full-refresh with no diffing, shipping ~120KB / 118 HID packets that strmdck's zip-rebuild retry loop occasionally amplifies to seconds. Fix: track the last icon filename written per index and send only changed indices via `set_buttons(update_only=True)`. Because `render_tile` returns a deterministic `tile_<sha1(signature)>.png` filename, an unchanged tile has an identical filename, so the diff is exact and free. Add `set_buttons` timing logs first for before/after device measurement.

**Tech Stack:** Python 3.12+ (local venv is 3.14), pytest, ruff. No new dependencies (stdlib `logging`, `time`).

## Global Constraints

- Comms in Czech; code, comments, identifiers, and commit messages in English.
- Conventional Commits; NO `Co-Authored-By` trailer; never squash-merge.
- Gates per task: `.venv/bin/python -m pytest <files>` and `.venv/bin/ruff check src tests` — both green.
- Scope is a single production file: `src/herdeck/driver/d200.py` (+ its test `tests/test_d200_panel.py`). Do NOT touch `app.py`, `render_pump.py`, `orchestrator.py`, or `icons.py`.
- `_last_icon` is updated ONLY after a successful `set_buttons` (transactional — a swallowed device error must leave the tile eligible for retry).
- First paint (empty `_last_icon`) sends ALL tiles with `update_only=False` to establish the layout; subsequent writes diff with `update_only=True`.
- A failed device write must never kill the render worker thread (preserve the existing swallow-and-continue contract).

---

### Task 1: Timing instrumentation in the D200 USB write path

**Files:**
- Modify: `src/herdeck/driver/d200.py` (module imports; `D200Driver.__init__`; `_write_tiles`/`_write_working`/`_write_panel`; new `_timed_set_buttons`)
- Test: `tests/test_d200_panel.py`

**Interfaces:**
- Consumes: existing `_FakeDev` (records `set_buttons(buttons, update_only)` in `.calls`, optional `block` Event that makes the write wait 1.0s), `_FakeIcons` (`render_tile` → `f"icon_{tile.index}.png"`), `_make_driver(tmp_path, dev)`, `_wait_until(pred, timeout)` in `tests/test_d200_panel.py`.
- Produces: `D200Driver._timed_set_buttons(channel: str, buttons: dict[int, dict], *, update_only: bool) -> bool` (writes + times + logs; returns success); driver attrs `self._last_write_ms: float | None`, `self._last_write_count: int`; class const `SLOW_WRITE_MS = 250.0`; module logger `log = logging.getLogger(__name__)`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_d200_panel.py` (top of file already imports `os, threading, time, pytest`; add `import logging` near the other stdlib imports):

```python
def test_d200_write_records_timing_and_count(tmp_path):
    dev = _FakeDev()
    before = os.getcwd()
    driver = _make_driver(tmp_path, dev)
    try:
        driver.render([TileView(0, "x", "blue"), TileView(1, "y", "green")])
        assert _wait_until(lambda: dev.calls)
        assert driver._last_write_count == 2
        assert driver._last_write_ms is not None
    finally:
        driver.close()
        os.chdir(before)


def test_d200_slow_write_warns(tmp_path, caplog):
    block = threading.Event()  # never set -> set_buttons waits the full 1.0s
    dev = _FakeDev(block=block)
    before = os.getcwd()
    driver = _make_driver(tmp_path, dev)
    try:
        with caplog.at_level(logging.WARNING, logger="herdeck.driver.d200"):
            driver.render([TileView(0, "x", "blue")])
            assert _wait_until(lambda: dev.calls, timeout=3.0)
        assert "slow tiles write" in caplog.text
    finally:
        block.set()
        driver.close()
        os.chdir(before)


def test_d200_write_failure_is_swallowed_and_warns(tmp_path, caplog):
    class _RaisingDev(_FakeDev):
        def set_buttons(self, buttons, update_only=False):
            raise RuntimeError("usb boom")

    dev = _RaisingDev()
    before = os.getcwd()
    driver = _make_driver(tmp_path, dev)
    pump_thread = driver._pump._thread
    try:
        with caplog.at_level(logging.WARNING, logger="herdeck.driver.d200"):
            driver.render([TileView(0, "x", "blue")])
            assert _wait_until(lambda: "tiles write failed" in caplog.text, timeout=2.0)
        assert pump_thread.is_alive()  # a failed write must not kill the worker
    finally:
        driver.close()
        os.chdir(before)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_d200_panel.py -q`
Expected: the 3 new tests FAIL (`AttributeError: '_Driver' object has no attribute '_last_write_count'` / no "slow tiles write" in log).

- [ ] **Step 3: Add module imports + logger**

In `src/herdeck/driver/d200.py`, the current top is:
```python
from __future__ import annotations

import contextlib
import io
import os
from collections.abc import Callable

from PIL import Image

from ..icons import compose_panel
from .base import DeckDriver, PanelView, TileView
from .render_pump import RenderPump
```
Change the stdlib import block to add `logging` and `time`, and add a module logger after the imports:
```python
from __future__ import annotations

import contextlib
import io
import logging
import os
import time
from collections.abc import Callable

from PIL import Image

from ..icons import compose_panel
from .base import DeckDriver, PanelView, TileView
from .render_pump import RenderPump

log = logging.getLogger(__name__)
```
Also remove the now-redundant local `import time` lines inside `_open_device` (currently first line of that method) and `run_reader` (currently first line of that method) — the module-level `import time` covers both. Leave the local `import hid` / `from strmdck...` imports in `_open_device` untouched (they stay lazy).

- [ ] **Step 4: Add the class constant + init state**

Add the class constant near the other class constants at the top of `class D200Driver` (next to `KEEP_ALIVE_INTERVAL = 5.0`):
```python
    SLOW_WRITE_MS = 250.0  # device writes slower than this get a WARNING log
```
In `__init__`, right after the line `self.KEEP_ALIVE_INTERVAL = keep_alive_interval`, add:
```python
        self._last_write_ms: float | None = None
        self._last_write_count = 0
```

- [ ] **Step 5: Add `_timed_set_buttons` and route the write methods through it**

Add this method (e.g. directly above `_write_tiles`):
```python
    def _timed_set_buttons(
        self, channel: str, buttons: dict[int, dict], *, update_only: bool
    ) -> bool:
        """Write buttons to the device, timing and logging the USB write. Returns
        True on success; False if the device write raised (the caller must then
        treat the buttons as not-yet-applied)."""
        if not buttons:
            return False
        t0 = time.perf_counter()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self._dev.set_buttons(buttons, update_only=update_only)
        except Exception:
            log.warning("d200 %s write failed (%d tiles)", channel, len(buttons))
            return False
        dt_ms = (time.perf_counter() - t0) * 1000.0
        self._last_write_ms = dt_ms
        self._last_write_count = len(buttons)
        level = logging.WARNING if dt_ms >= self.SLOW_WRITE_MS else logging.DEBUG
        prefix = "slow " if dt_ms >= self.SLOW_WRITE_MS else ""
        log.log(
            level,
            "d200 %s%s write: %.1fms, %d tiles, update_only=%s",
            prefix,
            channel,
            dt_ms,
            len(buttons),
            update_only,
        )
        return True
```
Replace `_write_tiles` and `_write_working` bodies with routed versions (behaviour unchanged — still send every tile; the diff comes in Task 2):
```python
    def _write_tiles(self, tiles: list[TileView]) -> None:
        self._timed_set_buttons("tiles", self._tile_buttons(tiles), update_only=False)

    def _write_working(self, tiles: list[TileView]) -> None:
        self._timed_set_buttons("working", self._tile_buttons(tiles), update_only=True)
```
Replace `_write_panel` so the device write routes through `_timed_set_buttons` (the PNG compose/save keeps its own guard):
```python
    def _write_panel(self, panel: PanelView) -> None:
        try:
            left, right = split_panel(compose_panel(panel))
            os.makedirs(_ICON_DIR, exist_ok=True)
            left.save(os.path.join(_ICON_DIR, "panel_left.png"))
            right.save(os.path.join(_ICON_DIR, "panel_right.png"))
        except Exception:
            return
        # update_only so refreshing the panel never clears the 13 tiles.
        self._timed_set_buttons(
            "panel",
            {
                _PANEL_LEFT_INDEX: {"name": "", "icon": "panel_left.png"},
                _PANEL_RIGHT_INDEX: {"name": "", "icon": "panel_right.png"},
            },
            update_only=True,
        )
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_d200_panel.py -q`
Expected: PASS (all, including the 3 new + existing 7).

- [ ] **Step 7: Run ruff**

Run: `.venv/bin/ruff check src tests`
Expected: clean (no errors).

- [ ] **Step 8: Commit**

```bash
git add src/herdeck/driver/d200.py tests/test_d200_panel.py
git commit -m "feat: instrument D200 set_buttons write timing"
```

---

### Task 2: Per-index tile write-diff

**Files:**
- Modify: `src/herdeck/driver/d200.py` (`__init__` adds `_last_icon`; new `_diff`/`_record`; rewrite `_write_tiles`/`_write_working` to diff)
- Test: `tests/test_d200_panel.py`

**Interfaces:**
- Consumes: `_timed_set_buttons(channel, buttons, *, update_only) -> bool` and `_last_write_*` from Task 1; `_tile_buttons(tiles) -> dict[int, dict]` (each value `{"name": "", "icon": <filename>}`).
- Produces: `self._last_icon: dict[int, str]` (last written icon filename per index); `_diff(buttons) -> dict[int, dict]`; `_record(buttons) -> None`. `_write_tiles`: full `update_only=False` paint while `_last_icon` empty, else diffed `update_only=True`. `_write_working`: always diffed `update_only=True`.

- [ ] **Step 1: Write the failing tests**

First generalize the test harness — replace the existing `_make_driver` so callers can inject a custom icons provider (keep the default `_FakeIcons`), and add a content-dependent fake:

```python
class _ContentIcons:
    """render_tile filename varies with tile content (time_text), so a changed
    tile yields a different filename and the driver's diff can detect it."""

    def render_tile(self, tile):
        return f"icon_{tile.index}_{tile.time_text}.png"


def _make_driver(tmp_path, dev, icons=None):
    class _Driver(D200Driver):
        def _open_device(self, retries=5, delay=1.0):
            return dev

        def _set_panel_background_mode(self):
            pass

    return _Driver(workdir=str(tmp_path), icon_provider=icons or _FakeIcons())
```

Then add the diff tests:

```python
def test_d200_first_paint_sends_all_update_only_false(tmp_path):
    dev = _FakeDev()
    before = os.getcwd()
    driver = _make_driver(tmp_path, dev)
    try:
        driver.render([TileView(0, "a", "blue"), TileView(1, "b", "green")])
        assert _wait_until(lambda: dev.calls)
        buttons, update_only = dev.calls[0]
        assert set(buttons) == {0, 1}
        assert update_only is False
    finally:
        driver.close()
        os.chdir(before)


def test_d200_unchanged_tiles_skip_write(tmp_path):
    dev = _FakeDev()
    before = os.getcwd()
    driver = _make_driver(tmp_path, dev)
    tiles = [TileView(0, "a", "blue"), TileView(1, "b", "green")]
    try:
        driver.render(tiles)
        assert _wait_until(lambda: len(dev.calls) == 1)
        driver.render(tiles)  # identical icons -> empty diff -> no write
        time.sleep(0.15)
        assert len(dev.calls) == 1
    finally:
        driver.close()
        os.chdir(before)


def test_d200_only_changed_tile_is_sent(tmp_path):
    dev = _FakeDev()
    before = os.getcwd()
    driver = _make_driver(tmp_path, dev, icons=_ContentIcons())
    try:
        driver.render(
            [TileView(0, "a", "blue", time_text="1s"), TileView(1, "b", "green", time_text="2s")]
        )
        assert _wait_until(lambda: len(dev.calls) == 1)
        driver.render(
            [TileView(0, "a", "blue", time_text="9s"), TileView(1, "b", "green", time_text="2s")]
        )  # only index 0 changed
        assert _wait_until(lambda: len(dev.calls) == 2)
        buttons, update_only = dev.calls[1]
        assert set(buttons) == {0}
        assert update_only is True
    finally:
        driver.close()
        os.chdir(before)


def test_d200_working_write_updates_diff_state(tmp_path):
    dev = _FakeDev()
    before = os.getcwd()
    driver = _make_driver(tmp_path, dev, icons=_ContentIcons())
    try:
        driver.render([TileView(0, "a", "blue", time_text="1s")])
        assert _wait_until(lambda: len(dev.calls) == 1)
        driver.render_working([TileView(0, "a", "blue", time_text="2s")])
        assert _wait_until(lambda: len(dev.calls) == 2)
        # full render with the SAME content the working frame already painted -> skipped
        driver.render([TileView(0, "a", "blue", time_text="2s")])
        time.sleep(0.15)
        assert len(dev.calls) == 2
    finally:
        driver.close()
        os.chdir(before)


def test_d200_failed_write_is_retried(tmp_path):
    class _FlakyDev(_FakeDev):
        def __init__(self):
            super().__init__()
            self.fail_next = True

        def set_buttons(self, buttons, update_only=False):
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("usb boom")
            super().set_buttons(buttons, update_only)

    dev = _FlakyDev()
    before = os.getcwd()
    driver = _make_driver(tmp_path, dev)
    tiles = [TileView(0, "a", "blue")]
    try:
        driver.render(tiles)  # first paint raises -> _last_icon stays empty
        time.sleep(0.15)
        assert dev.calls == []
        driver.render(tiles)  # retry -> succeeds as a fresh first paint
        assert _wait_until(lambda: len(dev.calls) == 1)
        buttons, update_only = dev.calls[0]
        assert set(buttons) == {0}
        assert update_only is False
    finally:
        driver.close()
        os.chdir(before)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_d200_panel.py -q`
Expected: the new diff tests FAIL — `test_d200_first_paint_sends_all_update_only_false` fails because Task 1's `_write_tiles` still uses `update_only=False` BUT the others fail: `test_d200_unchanged_tiles_skip_write` writes twice (no diff yet), `test_d200_only_changed_tile_is_sent` sends `{0, 1}` not `{0}`, etc. (`test_d200_first_paint...` may already pass from Task 1 — that's fine, it guards the first-paint path stays full.)

- [ ] **Step 3: Add `_last_icon` init state**

In `__init__`, right after the Task 1 lines `self._last_write_ms = None` / `self._last_write_count = 0`, add:
```python
        self._last_icon: dict[int, str] = {}
```

- [ ] **Step 4: Add `_diff`/`_record` and rewrite the write methods**

Add the two helpers (e.g. directly above `_timed_set_buttons`):
```python
    def _diff(self, buttons: dict[int, dict]) -> dict[int, dict]:
        """Keep only buttons whose icon filename differs from the last write."""
        return {
            i: b for i, b in buttons.items() if b.get("icon") != self._last_icon.get(i)
        }

    def _record(self, buttons: dict[int, dict]) -> None:
        for i, b in buttons.items():
            icon = b.get("icon")
            if icon is not None:
                self._last_icon[i] = icon
```
Rewrite `_write_tiles` and `_write_working` to diff (replacing the Task 1 routed versions):
```python
    def _write_tiles(self, tiles: list[TileView]) -> None:
        buttons = self._tile_buttons(tiles)
        if not self._last_icon:
            # First paint establishes the full button layout.
            if self._timed_set_buttons("tiles", buttons, update_only=False):
                self._record(buttons)
            return
        changed = self._diff(buttons)
        if not changed:
            return
        if self._timed_set_buttons("tiles", changed, update_only=True):
            self._record(changed)

    def _write_working(self, tiles: list[TileView]) -> None:
        changed = self._diff(self._tile_buttons(tiles))
        if not changed:
            return
        if self._timed_set_buttons("working", changed, update_only=True):
            self._record(changed)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_d200_panel.py -q`
Expected: PASS (all diff tests + Task 1 tests + original 7).

- [ ] **Step 6: Run the full driver/render suite + ruff**

Run: `.venv/bin/python -m pytest tests/test_d200_panel.py tests/test_render_pump.py tests/test_local_mode.py -q`
Expected: PASS (no regression in offload/close/panel/local-mode behaviour).
Run: `.venv/bin/ruff check src tests`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add src/herdeck/driver/d200.py tests/test_d200_panel.py
git commit -m "feat: per-index tile write-diff in D200 driver"
```

---

## Manual device gate (macbench D200) — after both tasks

Not part of SDD's automated flow; run when ready to deploy. For a true before/after:
1. Deploy ONLY Task 1's commit (instrumentation) to macbench (full `src/herdeck/` sync, see [[working-animation-phase]] deploy gotchas), set the d200 logger to DEBUG or watch WARNINGs, kickstart. Watch `~/.cache/herdeck/herdeck-app.err.log` for the periodic (~10s) `tiles` write and its ms/tile-count — baseline shows a heavy 13-tile write, occasionally "slow".
2. Deploy Task 2's commit (diff). The log should now show `tiles` writes of 1–3 tiles, tens of ms, no "slow" warnings on the 10s cadence — and the spinner no longer freezes.

If freezes persist (not eliminated), reopen the deferred pump `working`-drop change (non-goal in the spec) and the strmdck retry-loop angle.

## Self-Review

- **Spec coverage:** Task 1 = instrumentation (spec "Observabilita"); Task 2 = write-diff + transactional `_last_icon` + first-paint-full (spec "Fix" + "Error handling"). Manual gate = spec "Manuální gate". Non-goals untouched. ✅
- **Placeholder scan:** all code blocks are complete; commands have expected output. ✅
- **Type consistency:** `_timed_set_buttons(...) -> bool` used identically in both tasks; `_diff`/`_record` operate on `dict[int, dict]` matching `_tile_buttons` output; `_last_icon: dict[int, str]`. ✅
