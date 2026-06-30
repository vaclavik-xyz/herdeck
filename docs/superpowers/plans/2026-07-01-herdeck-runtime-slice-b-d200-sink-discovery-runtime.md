# herdeck Runtime — Slice B (D200 sink + discovery + headless runtime entry) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the deckapp's `DeckApp` the converged herdeck runtime — render once per tick and fan the orchestrator's `RenderState` out to multiple sinks (the existing HTTP tile buffer + a new D200 USB sink), then add a headless `herdeck.runtime` entry that drives a physical D200 *and* serves HTTP from ONE Orchestrator + ONE bridge connection + ONE clock, publishing its address in a `runtime.json` discovery file.

**Architecture:** `DeckApp` already owns an Orchestrator, a `LiveSource` (the single herdr-bridge connection), a tick loop (Slice A), and an HTTP server. This slice (1) adds a `RenderSink` fan-out so each tick's `RenderState` is delivered to a list of sinks, with the ticker producing working-only frames most ticks and a full frame every N ticks (mirroring the D200 path that lives in `app.py` today); (2) adds a `D200Sink` that wraps the existing `D200Driver` (full vs working writes, plus the physical press reader routed back into the same Orchestrator under the same lock); (3) adds a `runtime.json` discovery writer; (4) adds a `herdeck.runtime` headless entry that builds the live serving app, attaches a D200 sink when a device is present (else HTTP-only), and writes the discovery file. The herdr coupling stays entirely behind the unchanged `LiveSource`/`StateSource` seam.

**Tech Stack:** Python 3.12+ (threading, asyncio, dataclasses, typing.Protocol), pytest, ruff. No new dependencies. `strmdck`/`hid` are NOT in the local venv — every test is HW-free (fake drivers / fake sinks / fake app; the real D200 path only runs on macbench).

**Spec:** `docs/superpowers/specs/2026-06-30-herdeck-runtime-convergence-design.md`. This is **Slice B** (the Python runtime). **Slice C** (Tauri attach-or-spawn reading `runtime.json`, launchd plist switch, and the manual D200↔window convergence gate) is a SEPARATE plan built on this one — see the scope note at the end.

## Global Constraints

- Comms in Czech; code, comments, identifiers, and commit messages in English.
- Conventional Commits; NO `Co-Authored-By` trailer; never squash-merge. After each commit check `roborev show <sha>` and fix findings.
- Gate before "done": `.venv/bin/python -m pytest <touched test files>` green AND `.venv/bin/ruff check src tests` clean.
- **Single lock:** every render + fan-out happens under `DeckApp._lock` (the same lock presses, bridge callbacks, and the ticker already take). A sink's `deliver` runs inside that lock.
- **A failing sink must never break the HTTP buffer or another sink** — the fan-out wraps each `deliver` in try/except and logs a WARNING.
- **Reuse, not rewrite:** do not restructure the HTTP tile/version path (`_apply_rendered_locked` and its byte-equality version diff stay byte-for-byte as-is — it is the Slice A idle-quiet guarantee). The D200 sink is an ADDITIONAL output; the HTTP buffer remains `DeckApp`'s own.
- **herdr coupling is frozen behind `LiveSource`/`StateSource`** (`deckapp/live.py`, `deckapp/source.py`). Do not touch the bridge `Connector` wiring.
- **Discovery file contract:** `runtime.json` holds exactly `{"url": str, "host": str, "port": int, "token": str, "source": str}` — the SAME fields the sidecar prints to stdout today (`deckapp/__main__.py`) and the SAME shape the Tauri `Discovery` struct deserializes (Slice C reads this). File perms `0600`; deleted on clean exit.
- **D200 sink built only when a device is present:** constructing a `D200Driver` probes the USB device and raises when absent — the runtime catches that and runs HTTP-only. No hotplug (a later restart picks up the device).
- **HW-free tests:** no test may import `strmdck`/`hid` or open a device. Use fake drivers, fake sinks, and an injected app factory.

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `src/herdeck/deckapp/sinks.py` (new) | `RenderFrame` dataclass + `RenderSink` Protocol (Task 1); `D200Sink` (Task 2) | 1, 2 |
| `src/herdeck/deckapp/server.py` (modify) | `DeckApp` gains a sink list, `add_sink`, render fan-out, working/full ticker cadence, `config`/`slots` accessors | 1 |
| `src/herdeck/deckapp/discovery.py` (new) | `runtime.json` writer/reader/clearer (0600, atomic) | 3 |
| `src/herdeck/runtime.py` (new) | headless entry: build live serving app + attach D200 sink when present + write discovery + run forever | 4 |
| `tests/test_deckapp_live.py` (modify) | Task 1 fan-out + ticker-cadence tests (reuses Slice A helpers) | 1 |
| `tests/test_d200_sink.py` (new) | Task 2 D200Sink tests (fake driver) | 2 |
| `tests/test_discovery.py` (new) | Task 3 discovery file tests | 3 |
| `tests/test_runtime.py` (new) | Task 4 runtime-entry tests (fake app + fake driver) | 4 |

---

### Task 1: Render-sink fan-out + working/full ticker cadence in `DeckApp`

**Files:**
- Create: `src/herdeck/deckapp/sinks.py`
- Modify: `src/herdeck/deckapp/server.py` (`DeckApp`: `__init__`, `_render_locked`, `_prepare_swap`, `_refresh_locked`, `_tick_once`, `close`; add `_fan_out_locked`, `add_sink`, `config`, `slots`, `FULL_REFRESH_TICKS`)
- Test: `tests/test_deckapp_live.py`

**Interfaces:**
- Produces:
  - `sinks.RenderFrame` — `@dataclass(frozen=True)` with `render` (the orchestrator `RenderState`; `.tiles` is `list[TileView]`, `.panel` is a `PanelView`), `working: list[int] | None` (working tile indices on a non-full tick, else `None`), `full: bool`.
  - `sinks.RenderSink` — `Protocol` with `deliver(self, frame: RenderFrame) -> None` and `close(self) -> None`.
  - `DeckApp.add_sink(sink: RenderSink) -> None` — append a sink and immediately deliver it one full frame.
  - `DeckApp.config` (property → `self._source.config`), `DeckApp.slots` (property → `self._slots`), `DeckApp.FULL_REFRESH_TICKS` (class const `= 25`).
  - Fan-out semantics: every render delivers a `RenderFrame` to each registered sink under `self._lock`; the ticker delivers working-only frames (`full=False`) except every `FULL_REFRESH_TICKS`-th tick (`full=True`).
- Consumes: `self._orch.render()` (returns `RenderState`), `self._orch.tick()` (returns `list[int]` of working indices), the existing `_apply_rendered_locked`, `log = logging.getLogger(__name__)` (add if absent).

- [ ] **Step 1: Create the sinks module (RenderFrame + RenderSink protocol)**

Create `src/herdeck/deckapp/sinks.py`:

```python
"""Render sinks: the converged DeckApp renders once per tick and fans the
orchestrator's RenderState out to a list of sinks. The HTTP tile buffer stays
inside DeckApp; additional sinks (the physical D200 USB display) consume the
same frame. Keeping each output behind this small protocol is what lets one
Orchestrator + one bridge connection drive several displays in lockstep."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class RenderFrame:
    """One rendered deck state handed to a sink.

    ``render`` is the orchestrator's RenderState (``.tiles`` is a list of
    TileView, ``.panel`` is a PanelView). ``working`` lists the tile indices
    that are spinner-advancing on a partial tick (``None`` on a full frame).
    ``full`` is True for a complete repaint (all tiles + panel), False for a
    working-only tick frame."""

    render: object
    working: list[int] | None
    full: bool


@runtime_checkable
class RenderSink(Protocol):
    """A render target. ``deliver`` is called under DeckApp's lock on every
    render; it must not block for long. ``close`` tears the sink down."""

    def deliver(self, frame: RenderFrame) -> None: ...

    def close(self) -> None: ...
```

- [ ] **Step 2: Write the failing fan-out tests**

These reuse the Slice A helpers already in `tests/test_deckapp_live.py` (`make_live_icons`, `StubIcons`, `agent`, `Status`). Add a recording sink near the top of the file (after `StubIcons`):

```python
from herdeck.deckapp.sinks import RenderFrame  # noqa: E402  (add to the imports block at top)


class RecordingSink:
    def __init__(self):
        self.frames = []
        self.closed = False

    def deliver(self, frame):
        self.frames.append(frame)

    def close(self):
        self.closed = True
```

Then add these tests at the end of the file:

```python
def test_add_sink_delivers_initial_full_frame():
    app, src, server = make_live_icons(StubIcons())
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    app.refresh()
    sink = RecordingSink()
    app.add_sink(sink)
    assert len(sink.frames) == 1
    assert sink.frames[0].full is True
    assert sink.frames[0].render is not None  # the RenderState was handed over


def test_press_delivers_full_frame_to_sink():
    app, src, server = make_live_icons(StubIcons())
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.IDLE)])
    sink = RecordingSink()
    app.add_sink(sink)
    sink.frames.clear()
    app.press(0)
    assert sink.frames and sink.frames[-1].full is True


def test_tick_delivers_working_frame_with_indices():
    app, src, server = make_live_icons(StubIcons())
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    sink = RecordingSink()
    app.add_sink(sink)
    sink.frames.clear()
    app._tick_once()
    frame = sink.frames[-1]
    assert frame.full is False
    assert frame.working == [0]  # the single working agent is tile 0


def test_full_refresh_tick_delivers_exactly_one_full_frame():
    app, src, server = make_live_icons(StubIcons())
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    sink = RecordingSink()
    app.add_sink(sink)
    sink.frames.clear()
    for _ in range(app.FULL_REFRESH_TICKS):
        app._tick_once()
    fulls = [f for f in sink.frames if f.full]
    assert len(fulls) == 1  # one full frame at the Nth tick, working frames otherwise


def test_failing_sink_does_not_break_http_or_other_sinks():
    app, src, server = make_live_icons(StubIcons())
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])

    class BoomSink:
        def deliver(self, frame):
            raise RuntimeError("boom")

        def close(self):
            pass

    good = RecordingSink()
    app.add_sink(BoomSink())
    app.add_sink(good)
    good.frames.clear()
    v = app._state()["version"]
    app._tick_once()  # must NOT raise despite BoomSink
    assert app._state()["version"] >= v  # HTTP buffer still advanced
    assert good.frames  # the healthy sink still got its frame


def test_close_closes_sinks():
    app, src, server = make_live_icons(StubIcons())
    sink = RecordingSink()
    app.add_sink(sink)
    app.close()
    assert sink.closed is True
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_deckapp_live.py -k "sink or working_frame or full_refresh_tick" -v`
Expected: FAIL — `DeckApp` has no `add_sink` / `FULL_REFRESH_TICKS`; `RenderFrame` import may already resolve (Step 1) but the fan-out does not exist.

- [ ] **Step 4: Add the imports, class constant, and new `__init__` state**

In `src/herdeck/deckapp/server.py`, ensure these module-level imports exist near the other relative imports (add the `sinks` import; add `logging` + the logger only if not already present):

```python
import logging

from .sinks import RenderFrame

log = logging.getLogger(__name__)
```

Add the class constant at the top of the `DeckApp` class body (right after `class DeckApp...:` / its docstring, before `__init__`):

```python
    FULL_REFRESH_TICKS = 25  # every Nth tick re-renders all tiles + panel (advances idle elapsed on the D200); other ticks send working-only frames
```

Then in `DeckApp.__init__`, right after `self._version = 0` (server.py:84), add:

```python
        self._version = 0
        self._sinks: list = []  # RenderSink fan-out targets (HTTP buffer is DeckApp's own)
        self._ticks = 0
```

- [ ] **Step 5: Add `config` and `slots` accessor properties**

In `src/herdeck/deckapp/server.py`, right after the existing `source_name` property (server.py:120-122), add:

```python
    @property
    def config(self):
        """The live config (from the source) — the runtime entry builds the D200 driver from config.hardware."""
        return self._source.config

    @property
    def slots(self) -> int:
        return self._slots
```

- [ ] **Step 6: Expose `rs` from `_render_locked` and fan out from `_refresh_locked`**

In `src/herdeck/deckapp/server.py`, change `_render_locked` to return the `RenderState` as the first element. Replace its `return` line (server.py:151):

```python
        sections = {t.index: t.section for t in rs.tiles if t.index < slots and t.section}
        return rs, tiles, buf.getvalue(), sections
```

Update the swap path's destructure in `_prepare_swap` (server.py:260) to ignore `rs` (the swap path keeps its existing 6-tuple `prepared` bundle and `_commit_swap` is unchanged):

```python
        _, tiles, panel_png, sections = self._render_locked(new_source, orch, slots)
```

Replace `_refresh_locked` (server.py:171-173) with the fan-out version:

```python
    def _refresh_locked(self, *, working=None, full=True) -> None:
        rs, tiles, panel_png, sections = self._render_locked(self._source, self._orch, self._slots)
        self._apply_rendered_locked(tiles, panel_png, sections)
        self._fan_out_locked(rs, working, full)

    def _fan_out_locked(self, rs, working, full) -> None:
        """Deliver the rendered frame to every sink under self._lock. A sink that
        raises is isolated — the HTTP buffer (already updated above) and the other
        sinks must not be affected."""
        if not self._sinks:
            return
        frame = RenderFrame(render=rs, working=working, full=full)
        for sink in self._sinks:
            try:
                sink.deliver(frame)
            except Exception:
                log.warning("render sink %r failed to deliver a frame", sink, exc_info=True)
```

- [ ] **Step 7: Make the ticker produce working-only frames with a periodic full refresh**

In `src/herdeck/deckapp/server.py`, replace `_tick_once` (server.py:175-181) with:

```python
    def _tick_once(self) -> None:
        """Advance the spinner phase and re-render once, atomically w.r.t. presses
        and bridge updates (same lock). Most ticks fan out a WORKING-only frame
        (cheap on the D200); every FULL_REFRESH_TICKS-th tick is a full frame so
        idle elapsed advances on every sink. The HTTP buffer is fully re-rendered
        and version-diffed every tick regardless (idle tiles stay quiet via the diff)."""
        with self._lock:
            working = self._orch.tick()
            self._ticks += 1
            if self._ticks % self.FULL_REFRESH_TICKS == 0:
                self._refresh_locked(working=None, full=True)
            else:
                self._refresh_locked(working=working, full=False)
```

- [ ] **Step 8: Add `add_sink` (with an initial full paint)**

In `src/herdeck/deckapp/server.py`, add this method right after `_fan_out_locked` (from Step 6):

```python
    def add_sink(self, sink) -> None:
        """Register a render sink and immediately paint it one full frame so it
        starts in sync with the current deck state (the live ticker keeps it
        animated thereafter)."""
        with self._lock:
            self._sinks.append(sink)
            self._refresh_locked(working=None, full=True)
```

- [ ] **Step 9: Close sinks in `close()`**

In `src/herdeck/deckapp/server.py`, in `close()` (server.py:197), after the ticker stop/join block and before the `watcher = getattr(...)` line, add:

```python
            self._ticker_thread = None
        for sink in getattr(self, "_sinks", []):
            try:
                sink.close()
            except Exception:
                pass
        self._sinks = []
        watcher = getattr(self, "_watcher", None)
```

- [ ] **Step 10: Run the new tests + the full deckapp suites**

Run: `.venv/bin/python -m pytest tests/test_deckapp_live.py -k "sink or working_frame or full_refresh_tick" -v`
Expected: PASS (6 tests).
Run: `.venv/bin/python -m pytest tests/test_deckapp_live.py tests/test_deckapp.py -q`
Expected: PASS — no regression (default `working=None, full=True` keeps every non-ticker caller's behavior identical; mock app registers no sinks so the fan-out is a no-op).
Run: `.venv/bin/ruff check src tests`
Expected: clean.

- [ ] **Step 11: Commit**

```bash
git add src/herdeck/deckapp/sinks.py src/herdeck/deckapp/server.py tests/test_deckapp_live.py
git commit -m "feat(deckapp): render-sink fan-out with working/full ticker cadence"
```

---

### Task 2: `D200Sink` — drive the physical D200 from rendered frames

**Files:**
- Modify: `src/herdeck/deckapp/sinks.py` (append `D200Sink`)
- Test: `tests/test_d200_sink.py` (new)

**Interfaces:**
- Consumes: `RenderFrame` (Task 1); the existing `D200Driver` public API — `render(tiles: list[TileView])`, `render_panel(panel)`, `render_working(tiles: list[TileView])`, `on_press(cb: Callable[[int], None])`, `run_reader()` (async), `close()`.
- Produces: `D200Sink(driver, *, on_press: Callable[[int], None], slots: int, start_reader: bool = True)` implementing `RenderSink`. Full frames push all in-range tiles + the panel; working frames push only the working tiles via `render_working`; physical presses route to `on_press`; `close()` stops the driver.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_d200_sink.py`:

```python
import threading

from herdeck.deckapp.sinks import D200Sink, RenderFrame


class _Tile:
    def __init__(self, index):
        self.index = index


class _RS:
    """Stand-in for the orchestrator RenderState (just .tiles + .panel)."""

    def __init__(self, tiles, panel="PANEL"):
        self.tiles = tiles
        self.panel = panel


class FakeDriver:
    def __init__(self):
        self.full_renders = []      # list of [tile.index, ...]
        self.panels = []
        self.working_renders = []   # list of [tile.index, ...]
        self.press_cb = None
        self.closed = False
        self.reader_ran = threading.Event()

    def render(self, tiles):
        self.full_renders.append([t.index for t in tiles])

    def render_panel(self, panel):
        self.panels.append(panel)

    def render_working(self, tiles):
        self.working_renders.append([t.index for t in tiles])

    def on_press(self, cb):
        self.press_cb = cb

    async def run_reader(self):
        self.reader_ran.set()  # returns immediately (no device)

    def close(self):
        self.closed = True


def _sink(driver, *, slots=13, on_press=None, start_reader=False):
    return D200Sink(driver, on_press=(on_press or (lambda i: None)), slots=slots, start_reader=start_reader)


def test_full_frame_renders_all_in_range_tiles_and_panel():
    drv = FakeDriver()
    sink = _sink(drv)
    rs = _RS([_Tile(0), _Tile(1), _Tile(13), _Tile(14)])  # 13/14 are panel cells, not tiles
    sink.deliver(RenderFrame(render=rs, working=None, full=True))
    assert drv.full_renders == [[0, 1]]  # only indices < slots(13)
    assert drv.panels == ["PANEL"]
    assert drv.working_renders == []


def test_working_frame_renders_only_working_tiles():
    drv = FakeDriver()
    sink = _sink(drv)
    rs = _RS([_Tile(0), _Tile(1), _Tile(2)])
    sink.deliver(RenderFrame(render=rs, working=[1], full=False))
    assert drv.working_renders == [[1]]
    assert drv.full_renders == []
    assert drv.panels == []


def test_working_frame_with_no_working_tiles_is_a_noop():
    drv = FakeDriver()
    sink = _sink(drv)
    rs = _RS([_Tile(0), _Tile(1)])
    sink.deliver(RenderFrame(render=rs, working=[], full=False))
    assert drv.working_renders == []
    assert drv.full_renders == []


def test_press_callback_is_registered_on_the_driver():
    drv = FakeDriver()
    got = []
    _sink(drv, on_press=got.append)
    assert drv.press_cb is not None
    drv.press_cb(7)  # a physical button fires
    assert got == [7]


def test_close_closes_the_driver():
    drv = FakeDriver()
    sink = _sink(drv)
    sink.close()
    assert drv.closed is True


def test_start_reader_runs_the_driver_reader():
    drv = FakeDriver()
    sink = D200Sink(drv, on_press=lambda i: None, slots=13, start_reader=True)
    try:
        assert drv.reader_ran.wait(timeout=2.0)  # the reader thread ran run_reader()
    finally:
        sink.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_d200_sink.py -v`
Expected: FAIL — `ImportError: cannot import name 'D200Sink' from 'herdeck.deckapp.sinks'`.

- [ ] **Step 3: Implement `D200Sink`**

Append to `src/herdeck/deckapp/sinks.py`:

```python
import asyncio
import logging
import threading
from typing import Callable

log = logging.getLogger(__name__)


class D200Sink:
    """RenderSink that drives a physical Ulanzi D200 via an open ``D200Driver``.

    Full frames push every in-range tile plus the panel; working frames push only
    the spinner-advancing tiles (cheap partial USB writes). The driver's own
    per-index write diff and the neutralized strmdck retry-sleep keep those writes
    fast. Physical button presses are read on a private thread+event-loop and
    routed to ``on_press`` (the DeckApp's thread-safe ``press``), so a D200 press
    flows through the SAME Orchestrator + bridge as a window press."""

    def __init__(
        self,
        driver,
        *,
        on_press: Callable[[int], None],
        slots: int,
        start_reader: bool = True,
    ):
        self._driver = driver
        self._slots = slots
        driver.on_press(on_press)
        self._reader_thread: threading.Thread | None = None
        if start_reader:
            self._reader_thread = threading.Thread(
                target=self._run_reader, name="herdeck-d200-reader", daemon=True
            )
            self._reader_thread.start()

    def deliver(self, frame) -> None:
        rs = frame.render
        if frame.full or frame.working is None:
            self._driver.render([t for t in rs.tiles if t.index < self._slots])
            self._driver.render_panel(rs.panel)
            return
        wanted = set(frame.working)
        tiles = [t for t in rs.tiles if t.index in wanted]
        if tiles:
            self._driver.render_working(tiles)

    def _run_reader(self) -> None:
        try:
            asyncio.run(self._driver.run_reader())
        except Exception:
            log.warning("D200 press reader stopped", exc_info=True)

    def close(self) -> None:
        try:
            self._driver.close()  # closes the device, which ends run_reader()
        except Exception:
            log.warning("D200 driver close failed", exc_info=True)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_d200_sink.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: ruff + confirm no import-time strmdck dependency**

Run: `.venv/bin/ruff check src tests`
Expected: clean.
Run: `.venv/bin/python -c "import herdeck.deckapp.sinks"`
Expected: no error (the module must NOT import `strmdck`/`hid` at module load — `D200Sink` only touches the driver object it is given).

- [ ] **Step 6: Commit**

```bash
git add src/herdeck/deckapp/sinks.py tests/test_d200_sink.py
git commit -m "feat(deckapp): D200Sink — drive the physical deck from rendered frames"
```

---

### Task 3: `runtime.json` discovery file (writer / reader / clearer)

**Files:**
- Create: `src/herdeck/deckapp/discovery.py`
- Test: `tests/test_discovery.py` (new)

**Interfaces:**
- Produces:
  - `runtime_file_path() -> str` — `$HERDECK_RUNTIME_DIR/runtime.json` or `~/.cache/herdeck/runtime.json`.
  - `write_runtime_file(path: str, info: dict) -> None` — atomic write, `chmod 0600`.
  - `read_runtime_file(path: str) -> dict | None` — parsed dict, or `None` if missing/malformed.
  - `clear_runtime_file(path: str) -> None` — best-effort delete.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_discovery.py`:

```python
import os
import stat

from herdeck.deckapp.discovery import (
    clear_runtime_file,
    read_runtime_file,
    runtime_file_path,
    write_runtime_file,
)

INFO = {"url": "http://127.0.0.1:8800", "host": "127.0.0.1", "port": 8800, "token": "t0ken", "source": "live"}


def test_write_then_read_round_trips(tmp_path):
    p = str(tmp_path / "runtime.json")
    write_runtime_file(p, INFO)
    assert read_runtime_file(p) == INFO


def test_written_file_is_0600(tmp_path):
    p = str(tmp_path / "runtime.json")
    write_runtime_file(p, INFO)
    mode = stat.S_IMODE(os.stat(p).st_mode)
    assert mode == 0o600


def test_write_creates_missing_parent_dir(tmp_path):
    p = str(tmp_path / "nested" / "dir" / "runtime.json")
    write_runtime_file(p, INFO)
    assert read_runtime_file(p) == INFO


def test_read_missing_returns_none(tmp_path):
    assert read_runtime_file(str(tmp_path / "absent.json")) is None


def test_read_malformed_returns_none(tmp_path):
    p = tmp_path / "runtime.json"
    p.write_text("{not json")
    assert read_runtime_file(str(p)) is None


def test_clear_removes_file_and_is_idempotent(tmp_path):
    p = str(tmp_path / "runtime.json")
    write_runtime_file(p, INFO)
    clear_runtime_file(p)
    assert not os.path.exists(p)
    clear_runtime_file(p)  # second call must not raise


def test_runtime_file_path_honors_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERDECK_RUNTIME_DIR", str(tmp_path))
    assert runtime_file_path() == str(tmp_path / "runtime.json")


def test_runtime_file_path_default(monkeypatch):
    monkeypatch.delenv("HERDECK_RUNTIME_DIR", raising=False)
    assert runtime_file_path().endswith("/.cache/herdeck/runtime.json")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_discovery.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'herdeck.deckapp.discovery'`.

- [ ] **Step 3: Implement the discovery module**

Create `src/herdeck/deckapp/discovery.py`:

```python
"""runtime.json discovery file. The headless runtime publishes its localhost
HTTP address + token here (perms 0600) so the Tauri desktop window can ATTACH
to a running runtime instead of spawning its own sidecar. The file is the same
{url,host,port,token,source} shape the sidecar prints to stdout. Deleted on a
clean exit; a stale file is detected by the window's /health ping failing."""

from __future__ import annotations

import json
import os


def runtime_file_path() -> str:
    base = os.environ.get("HERDECK_RUNTIME_DIR") or os.path.expanduser("~/.cache/herdeck")
    return os.path.join(base, "runtime.json")


def write_runtime_file(path: str, info: dict) -> None:
    """Atomically write `info` as JSON with 0600 perms (create parent dirs)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(info, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)  # atomic on POSIX


def read_runtime_file(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def clear_runtime_file(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_discovery.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: ruff**

Run: `.venv/bin/ruff check src tests`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/herdeck/deckapp/discovery.py tests/test_discovery.py
git commit -m "feat(deckapp): runtime.json discovery file (atomic, 0600)"
```

---

### Task 4: `herdeck.runtime` headless entry — converged runtime

**Files:**
- Create: `src/herdeck/runtime.py`
- Test: `tests/test_runtime.py` (new)

**Interfaces:**
- Consumes: `create_app` (server.py), `D200Sink` (Task 2), `write_runtime_file`/`clear_runtime_file`/`runtime_file_path` (Task 3); `DeckApp.config`, `DeckApp.slots`, `DeckApp.add_sink`, `DeckApp.press`, `DeckApp.host/port/token/source_name` (Task 1 + existing).
- Produces:
  - `build_runtime(*, host="127.0.0.1", port=0, app_factory=None, driver_factory=None, write_discovery=True) -> tuple[app, sink_or_None, info_dict, path]` — builds the serving app, attaches a D200 sink when a device is present (else HTTP-only), writes `runtime.json`.
  - `main() -> int` — the launchd/CLI entry: build, print the stdout discovery fallback, run until stopped, then clear the discovery file and tear down.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_runtime.py`:

```python
import os

from herdeck import runtime


class FakeApp:
    """Minimal DeckApp stand-in for the runtime entry (no bridge, no HTTP)."""

    def __init__(self):
        self.host = "127.0.0.1"
        self.port = 8800
        self.token = "t0ken"
        self.source_name = "live"
        self.slots = 13
        self.config = object()
        self.sinks = []
        self.closed = False

    def add_sink(self, sink):
        self.sinks.append(sink)

    def press(self, index):
        pass

    def close(self):
        self.closed = True


class FakeDriver:
    def __init__(self):
        self.press_cb = None

    def on_press(self, cb):
        self.press_cb = cb

    async def run_reader(self):
        return None

    def close(self):
        pass


def _runtime_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HERDECK_RUNTIME_DIR", str(tmp_path))
    return str(tmp_path / "runtime.json")


def test_build_runtime_attaches_d200_sink_when_device_present(monkeypatch, tmp_path):
    path = _runtime_dir(monkeypatch, tmp_path)
    app = FakeApp()
    app2, sink, info, p = runtime.build_runtime(
        app_factory=lambda host, port: app,
        driver_factory=lambda config: FakeDriver(),
    )
    assert app2 is app
    assert sink is not None and app.sinks == [sink]  # a sink was attached
    assert p == path
    assert info == {
        "url": "http://127.0.0.1:8800",
        "host": "127.0.0.1",
        "port": 8800,
        "token": "t0ken",
        "source": "live",
    }
    assert os.path.exists(path)  # discovery written


def test_build_runtime_http_only_when_no_device(monkeypatch, tmp_path):
    path = _runtime_dir(monkeypatch, tmp_path)
    app = FakeApp()

    def boom_factory(config):
        raise RuntimeError("No openable Ulanzi D200")

    app2, sink, info, p = runtime.build_runtime(
        app_factory=lambda host, port: app,
        driver_factory=boom_factory,
    )
    assert sink is None  # HTTP-only
    assert app.sinks == []
    assert os.path.exists(path)  # discovery still written


def test_build_runtime_can_skip_discovery_write(monkeypatch, tmp_path):
    path = _runtime_dir(monkeypatch, tmp_path)
    app = FakeApp()
    runtime.build_runtime(
        app_factory=lambda host, port: app,
        driver_factory=lambda config: FakeDriver(),
        write_discovery=False,
    )
    assert not os.path.exists(path)


def test_d200_press_routes_to_app_press(monkeypatch, tmp_path):
    _runtime_dir(monkeypatch, tmp_path)
    app = FakeApp()
    pressed = []
    app.press = pressed.append
    drv = FakeDriver()
    runtime.build_runtime(
        app_factory=lambda host, port: app,
        driver_factory=lambda config: drv,
    )
    drv.press_cb(5)  # a physical D200 button
    assert pressed == [5]  # routed into the converged app's press
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_runtime.py -v`
Expected: FAIL — `AttributeError: module 'herdeck.runtime' has no attribute 'build_runtime'` (or ModuleNotFoundError).

- [ ] **Step 3: Implement the runtime entry**

Create `src/herdeck/runtime.py`:

```python
"""Headless converged herdeck runtime.

One process owns one Orchestrator + one herdr-bridge connection (via the
deckapp LiveSource) + one tick loop + one clock, and fans render frames out to
sinks: the HTTP tile buffer (served to the desktop window / web) and — when a
physical Ulanzi D200 is attached — a D200 USB sink. It publishes its localhost
address in runtime.json so the desktop window can attach instead of spawning
its own sidecar. Run as the launchd service on a machine with a D200; on a
machine without one it is simply an HTTP-only deck server."""

from __future__ import annotations

import json
import logging
import os
import threading

from .deckapp.discovery import clear_runtime_file, runtime_file_path, write_runtime_file
from .deckapp.server import create_app
from .deckapp.sinks import D200Sink

log = logging.getLogger(__name__)


def _default_driver_factory(config):
    """Build (and open) a D200Driver from config.hardware. Raises if no device."""
    from .driver.d200 import D200Driver

    hw = config.hardware
    return D200Driver(
        brightness=hw.brightness,
        debounce=hw.debounce,
        keep_alive_interval=hw.keep_alive_interval,
        icons_dir=hw.icons_dir,
    )


def _build_d200_sink(app, *, driver_factory):
    """Probe + attach a D200 sink. Returns the sink, or None when no device is
    present / strmdck is unavailable (the runtime then serves HTTP only)."""
    try:
        driver = driver_factory(app.config)
    except Exception as exc:  # device absent, busy, or strmdck/hid missing
        log.info("no D200 attached (%s); running HTTP-only", exc)
        return None
    sink = D200Sink(driver, on_press=app.press, slots=app.slots)
    app.add_sink(sink)
    return sink


def build_runtime(
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    app_factory=None,
    driver_factory=None,
    write_discovery: bool = True,
):
    """Build the serving deck app, attach a D200 sink when possible, and publish
    runtime.json. Returns (app, sink_or_None, info, path)."""
    app_factory = app_factory or (lambda host, port: create_app(host=host, port=port))
    driver_factory = driver_factory or _default_driver_factory
    app = app_factory(host, port)
    sink = _build_d200_sink(app, driver_factory=driver_factory)
    info = {
        "url": f"http://{app.host}:{app.port}",
        "host": app.host,
        "port": app.port,
        "token": app.token,
        "source": app.source_name,
    }
    path = runtime_file_path()
    if write_discovery:
        write_runtime_file(path, info)
    return app, sink, info, path


def main() -> int:
    if os.environ.get("HERDECK_DEBUG"):
        logging.basicConfig(level=logging.DEBUG)
    port = int(os.environ.get("HERDECK_DECKAPP_PORT", "0"))
    app, sink, info, path = build_runtime(host="127.0.0.1", port=port)
    print(json.dumps(info), flush=True)  # stdout discovery fallback (parity with the sidecar)
    stop = threading.Event()
    try:
        stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        clear_runtime_file(path)
        if sink is not None:
            sink.close()
        app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_runtime.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Import-safety + ruff**

Run: `.venv/bin/python -c "import herdeck.runtime"`
Expected: no error (module import must NOT pull in `strmdck`/`hid` — the D200 driver import is lazy inside `_default_driver_factory`).
Run: `.venv/bin/ruff check src tests`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/herdeck/runtime.py tests/test_runtime.py
git commit -m "feat(runtime): headless converged runtime entry (D200 sink + discovery)"
```

---

## Final verification (before finishing the branch)

- [ ] `.venv/bin/ruff check src tests` → clean
- [ ] `.venv/bin/python -m pytest` → green (whole suite; the mock/HTTP path is unchanged, Slice A ticker tests still pass)
- [ ] `.venv/bin/python -c "import herdeck.runtime, herdeck.deckapp.sinks, herdeck.deckapp.discovery"` → no error (no import-time HW deps)
- [ ] Optional macbench D200 smoke (remote-verifiable, like the D200 fix; does NOT touch the live launchd plist): run `HERDECK_CONFIG=... .venv/bin/python -m herdeck.runtime` by hand on macbench → the D200 paints + a WORKING agent animates, `~/.cache/herdeck/runtime.json` exists with `{url,host,port,token,source}` and `0600` perms, and `curl http://127.0.0.1:<port>/health?token=<token>` returns ok. Full D200↔window convergence + the launchd plist switch + Tauri attach come in Slice C.

## Scope note — this is Slice B (Python runtime only)

This plan delivers the converged **Python** runtime: one Orchestrator + one bridge + one clock fanning out to the HTTP buffer and a D200 USB sink, with a `runtime.json` discovery file. It does NOT yet change the desktop window or the deployment:

- **Slice C** (separate plan): Tauri **attach-or-spawn** — on startup read `runtime.json` + ping `/health`; if live, attach (use `{url,token}`, no spawn) via the existing `SidecarPlan::External` shape in `desktop/src-tauri/src/lib.rs`; if stale/absent, spawn the bundled sidecar as today. Plus the launchd plist switch on macbench (`com.herdeck.app` → `herdeck.runtime`) and the manual convergence gate: D200 animates + window attaches + **same elapsed** + kill-runtime → window falls back to its own sidecar.
- **Deliberate Slice B limitation:** a config reload (`swap_source`) repaints the HTTP buffer immediately but the D200 sink only catches up on the next full-refresh tick (≤ `FULL_REFRESH_TICKS × tick_interval` ≈ 10 s); working tiles still update every tick. The swap path is intentionally left untouched (its no-half-swap guarantee is delicate) — wiring a swap-time D200 repaint is a low-value follow-up, not a Slice B requirement.

## Self-Review (completed by plan author)

**Spec coverage:** `sinks.py` `RenderSink` + `D200Sink` (spec "Komponenty" item 1) → Tasks 1+2. `DeckApp` sink list + ticker fan-out (spec item 2) → Task 1. `runtime.py` headless entry absorbing the app.py D200 wiring + deck-kind detection (spec item 3) → Task 4 (`_build_d200_sink` catches the `D200Driver` probe failure = the deck-kind/absence detection). Discovery file writer/reader (spec item 4) → Task 3. Data-flow: bridge→deck unchanged (LiveSource); ticker→animace = Task 1 working/full cadence; press from D200 → `D200Sink` reader → `app.press` → same Orchestrator (Task 2+4); config/onboarding "don't move" = create_app already serves them (Task 4 reuse). Migration seam (`StateSource`/`LiveSource`) untouched ✓. Tauri attach + deploy + manual gate explicitly deferred to Slice C (stated) — not gaps. Non-goals (hotplug, multi-server, remote clients, WS push) untouched ✓.

**Placeholder scan:** none — every code step carries complete code; every command has an expected result.

**Type/name consistency:** `RenderFrame(render, working, full)` constructed in `_fan_out_locked` and consumed in `D200Sink.deliver` and the tests with identical field names. `RenderSink.deliver/close` matches `RecordingSink`, `BoomSink`, `D200Sink`. `add_sink`, `FULL_REFRESH_TICKS`, `config`, `slots` defined in Task 1 and consumed in Task 4 (`app.add_sink`, `app.slots`, `app.config`). `D200Sink(driver, *, on_press, slots, start_reader=True)` signature identical across Task 2 impl, Task 2 tests, and Task 4 `_build_d200_sink`. `build_runtime(*, host, port, app_factory, driver_factory, write_discovery)` returns the 4-tuple the Task 4 tests destructure. Discovery fields `{url,host,port,token,source}` identical in `build_runtime`, the Task 3 round-trip test, and the spec contract (= Rust `Discovery` struct).
