# D200 strmdck retry-sleep fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the D200 spinner freeze by neutralizing strmdck's retry-loop `time.sleep(0.05)` (a pure-CPU throttle that ran ~8-15x per `set_buttons`, blocking the render worker 400-800ms — up to 5889ms — on every spinner frame).

**Architecture:** A runtime monkeypatch applied when the D200 driver opens the device: replace the `time` reference inside `strmdck.devices.ulanzi_d200` with a thin proxy whose `sleep()` is a no-op and whose every other attribute passes through to the real `time` module. On-device verified: worst write 400-800ms→101ms, worst worker-block 5889ms→258ms.

**Tech Stack:** Python 3.12+ (local venv 3.14), pytest, ruff. No new dependencies. (strmdck is NOT installed in the local venv — the unit test fakes it via `sys.modules`.)

## Global Constraints

- Comms in Czech; code, comments, identifiers, and commit messages in English.
- Conventional Commits; NO `Co-Authored-By` trailer; never squash-merge.
- Gate: `.venv/bin/python -m pytest <files>` and `.venv/bin/ruff check src tests` — both green.
- Scope is a single production file: `src/herdeck/driver/d200.py` (+ its test `tests/test_d200_panel.py`).
- The patch must be idempotent (re-open / multiple decks must not re-wrap) and fail-safe (any error → log a WARNING and continue; never crash the driver).
- Patch ONLY the `time` reference inside the `strmdck.devices.ulanzi_d200` module — never global `time.sleep`. (herdeck's own `time.sleep(delay)` in `_open_device` uses the module-level `time` and must stay real.)
- Do NOT revert or alter the merged write-diff; this is an additive fix.

---

### Task 1: Neutralize strmdck's retry-loop sleep

**Files:**
- Modify: `src/herdeck/driver/d200.py` (add `_SleeplessTime` + `_neutralize_retry_sleep`; call it in `_open_device`)
- Test: `tests/test_d200_panel.py`

**Interfaces:**
- Produces: module-level `_SleeplessTime` (proxy class) and `_neutralize_retry_sleep() -> None` (idempotent, fail-safe). Called from `D200Driver._open_device` right after the strmdck import.
- Consumes: module-level `log = logging.getLogger(__name__)` (already present).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_d200_panel.py`:

```python
def test_neutralize_retry_sleep_noops_strmdck_sleep(monkeypatch):
    # The function must replace strmdck.devices.ulanzi_d200's `time` so sleep() is a
    # no-op (kills the retry-loop throttle) while every other time.* passes through.
    # strmdck is not installed locally, so fake the module tree in sys.modules.
    import sys
    import time as real_time
    import types

    fake_strmdck = types.ModuleType("strmdck")
    fake_devices = types.ModuleType("strmdck.devices")
    fake_mod = types.ModuleType("strmdck.devices.ulanzi_d200")
    fake_mod.time = real_time
    fake_devices.ulanzi_d200 = fake_mod
    fake_strmdck.devices = fake_devices
    monkeypatch.setitem(sys.modules, "strmdck", fake_strmdck)
    monkeypatch.setitem(sys.modules, "strmdck.devices", fake_devices)
    monkeypatch.setitem(sys.modules, "strmdck.devices.ulanzi_d200", fake_mod)

    from herdeck.driver.d200 import _neutralize_retry_sleep

    _neutralize_retry_sleep()

    # sleep() is now a no-op: a 2s sleep returns instantly
    t0 = real_time.monotonic()
    fake_mod.time.sleep(2.0)
    assert real_time.monotonic() - t0 < 0.1
    # other time.* still work (passthrough to the real module)
    assert isinstance(fake_mod.time.monotonic(), float)
    # idempotent: a second call does not re-wrap the proxy
    patched = fake_mod.time
    _neutralize_retry_sleep()
    assert fake_mod.time is patched


def test_neutralize_retry_sleep_is_failsafe_when_strmdck_missing(monkeypatch):
    # If strmdck can't be imported, the function must swallow the error (driver still works).
    import sys

    monkeypatch.setitem(sys.modules, "strmdck", None)  # forces ImportError on import
    from herdeck.driver.d200 import _neutralize_retry_sleep

    _neutralize_retry_sleep()  # must not raise
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_d200_panel.py::test_neutralize_retry_sleep_noops_strmdck_sleep tests/test_d200_panel.py::test_neutralize_retry_sleep_is_failsafe_when_strmdck_missing -v`
Expected: FAIL with `ImportError: cannot import name '_neutralize_retry_sleep'`.

- [ ] **Step 3: Add `_SleeplessTime` + `_neutralize_retry_sleep` to d200.py**

In `src/herdeck/driver/d200.py`, add these at module level (after `log = logging.getLogger(__name__)` and the `_PANEL_RIGHT_INDEX = 14` constants block, right before `def split_panel(...)`):

```python
class _SleeplessTime:
    """Proxy for the time module whose sleep() is a no-op; every other attribute
    passes through to the real module. Used to neutralize strmdck's retry-loop
    time.sleep(0.05) (a pure-CPU throttle between zip-rebuild attempts) without
    touching the global time.sleep."""

    def __init__(self, real):
        self._real = real

    def sleep(self, *_args, **_kwargs):
        return None

    def __getattr__(self, name):
        return getattr(self._real, name)


def _neutralize_retry_sleep() -> None:
    """strmdck's UlanziD200._prepare_zip retries zip-building in a tight loop with
    time.sleep(0.05) between attempts (working around a device firmware bug where
    bytes 0x00/0x7c at packet boundaries glitch the deck). That sleep waits on
    nothing but ran ~8-15x per set_buttons, freezing the D200 render worker
    400-800ms (occasionally seconds) on every spinner frame. Replace the module's
    time with a sleepless proxy so retries spin without delay. Idempotent and
    fail-safe."""
    try:
        import strmdck.devices.ulanzi_d200 as ud

        if isinstance(ud.time, _SleeplessTime):
            return
        ud.time = _SleeplessTime(ud.time)
    except Exception:
        log.warning("could not neutralize strmdck retry sleep; D200 spinner may stall")
```

- [ ] **Step 4: Call it in `_open_device`**

In `src/herdeck/driver/d200.py`, in `_open_device`, add the call right after the strmdck import. Change:

```python
    def _open_device(self, retries: int = 5, delay: float = 1.0):
        import hid
        from strmdck.devices.ulanzi_d200 import UlanziD200Device

        vid, pid = UlanziD200Device.USB_VENDOR_ID, UlanziD200Device.USB_PRODUCT_ID
```
to:
```python
    def _open_device(self, retries: int = 5, delay: float = 1.0):
        import hid
        from strmdck.devices.ulanzi_d200 import UlanziD200Device

        _neutralize_retry_sleep()
        vid, pid = UlanziD200Device.USB_VENDOR_ID, UlanziD200Device.USB_PRODUCT_ID
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_d200_panel.py -q`
Expected: PASS (the 2 new tests + all existing).

- [ ] **Step 6: Run the broader driver suite + ruff**

Run: `.venv/bin/python -m pytest tests/test_d200_panel.py tests/test_render_pump.py tests/test_local_mode.py -q`
Expected: PASS (no regression; fake-device tests override `_open_device`, so they never import strmdck).
Run: `.venv/bin/ruff check src tests`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add src/herdeck/driver/d200.py tests/test_d200_panel.py
git commit -m "fix: neutralize strmdck retry-loop sleep to stop D200 spinner freeze"
```

---

## Manual device gate (macbench D200) — after the task

Already verified once by temporarily patching strmdck in the venv (worst write 5889ms→101ms, worst block→258ms). After deploying THIS commit (the proper in-herdeck monkeypatch): full `src/herdeck/` sync to macbench, gate on `import herdeck.app`, kickstart, and confirm via the timing logs (thresholds temporarily lowered, or HERDECK_DEBUG) that writes are ≤~100ms and no worker-block exceeds a few hundred ms. The patch is applied by `_open_device`, so it activates on the real device path only.

## Self-Review

- **Spec coverage:** `_SleeplessTime` + `_neutralize_retry_sleep` (idempotent + fail-safe) + call in `_open_device` (spec "Fix"); unit test via fake sys.modules + failsafe test (spec "Testy"); manual gate (spec). Non-goals untouched. ✅
- **Placeholder scan:** all code complete; commands have expected output. ✅
- **Type consistency:** `_neutralize_retry_sleep() -> None`, `_SleeplessTime(real)` proxy; idempotency via `isinstance(ud.time, _SleeplessTime)`. ✅
