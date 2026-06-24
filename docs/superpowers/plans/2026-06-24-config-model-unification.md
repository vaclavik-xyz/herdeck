# Config Model Unification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace herdeck's two divergent config formats (legacy flat vs named-block profiles) with one model — all sections flat as the base, optional `[profiles.X]` overlays over the same sections.

**Architecture:** One resolver in `settings.py`: read the flat base sections, build an ordered overlay chain from the active profile's `extends`, merge per section (tables merge field-by-field; scalars/lists replace; `servers` selects ids), then build `Config` once. Legacy flat configs are the base (backward compatible). The unused named-block code path (`_runtime_config`, `_resolve_legacy`, `_named_block`) is deleted and its tests rewritten.

**Tech Stack:** Python 3.14, `tomllib` (read), dataclasses, pytest. No new dependencies.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-24-config-model-unification-design.md`.
- TDD: failing test first, watch it fail, minimal code, watch it pass, commit.
- Branch: `feat/config-unification` (already created off `main`).
- Run tests with the venv active: `source .venv/bin/activate`; run the full suite with `PYTHONPATH=. python -m pytest -q` (some tests import `tests.*`, which needs repo-root on the path).
- `token_env` secrets stay env-only — never read or write secret values to TOML.
- Public API of `settings.py` keeps the same signatures: `load_settings`, `resolve_profile(snapshot, name=None)`, `list_profiles(snapshot)`, `set_active_profile(snapshot, name, *, persist=True)`, `validate_settings(snapshot)`.
- `"default"` is the reserved name for the base; `[profiles.default]` is an error.
- Reuse existing section helpers in `settings.py` (`_theme_config`, `_view_config`, `_notifications_config`, `_safety_config`, `_macro_set`, `_launcher`, `_server_config`, `_hardware_config`, `_active_profile_name`, `_toml_line`, `_toml_value`) — do not reimplement them.

---

## File Structure

- `src/herdeck/settings.py` — **primary change.** New pure helpers (`_merge_section`, `_profile_overlays`, `_merged_sections`, `_build_config`); rewrite `resolve_profile`, `list_profiles`, `validate_settings`, `set_active_profile` to the unified model; delete `_runtime_config`, `_resolve_legacy`, `_named_block`.
- `src/herdeck/config.py` — simplify `load_config` to delegate to the resolver; delete `_load_legacy_config` (its logic moves into `settings._build_config`). Keep `_parse_grid`, `_parse_profile`, `parse_notifications` (imported by settings/used elsewhere).
- `tests/test_settings.py` — rewrite named-block fixtures/tests to the overlay model.
- `tests/test_config.py` — keep; add/confirm legacy-flat-compat coverage.

---

### Task 1: `_merge_section` — per-section overlay merge

**Files:**
- Modify: `src/herdeck/settings.py`
- Test: `tests/test_settings.py`

**Interfaces:**
- Produces: `_merge_section(base, overlay) -> object` — if both are dicts, merge field-by-field recursively; otherwise (scalar/list/None overlay) return `overlay` (replace).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_settings.py`:

```python
from herdeck.settings import _merge_section


def test_merge_section_merges_tables_field_by_field():
    base = {"management": "launcher_menu", "tile_fields": ["repo"]}
    overlay = {"management": "bottom_row"}
    assert _merge_section(base, overlay) == {
        "management": "bottom_row",
        "tile_fields": ["repo"],
    }


def test_merge_section_replaces_lists_and_scalars():
    assert _merge_section(["a", "b"], ["c"]) == ["c"]
    assert _merge_section("5x3", "4x3") == "4x3"


def test_merge_section_recurses_into_nested_tables():
    base = {"colors": {"blocked": "amber", "idle": "blue"}}
    overlay = {"colors": {"blocked": "red"}}
    assert _merge_section(base, overlay) == {
        "colors": {"blocked": "red", "idle": "blue"}
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source .venv/bin/activate && python -m pytest tests/test_settings.py -k merge_section -q`
Expected: FAIL — `ImportError: cannot import name '_merge_section'`.

- [ ] **Step 3: Write minimal implementation**

Add to `src/herdeck/settings.py`:

```python
def _merge_section(base, overlay):
    """Overlay a config section onto a base: tables merge field-by-field
    (recursively), scalars and lists replace wholesale."""
    if isinstance(base, dict) and isinstance(overlay, dict):
        out = dict(base)
        for key, value in overlay.items():
            out[key] = _merge_section(out.get(key), value)
        return out
    return overlay
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_settings.py -k merge_section -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/herdeck/settings.py tests/test_settings.py
git commit -m "feat(settings): add _merge_section overlay helper"
```

---

### Task 2: `_profile_overlays` — extends chain + cycle detection

**Files:**
- Modify: `src/herdeck/settings.py`
- Test: `tests/test_settings.py`

**Interfaces:**
- Consumes: `ConfigError` (already imported in settings).
- Produces: `_profile_overlays(profiles: dict, name: str) -> list[dict]` — the overlay dicts ordered base-most-parent first, ending with `name`. Unknown name or `extends` cycle → `ConfigError`.

- [ ] **Step 1: Write the failing test**

```python
import pytest

from herdeck.config import ConfigError
from herdeck.settings import _profile_overlays


def test_profile_overlays_orders_parents_before_child():
    profiles = {
        "base": {"view": {"management": "launcher_menu"}},
        "work": {"extends": "base", "view": {"management": "bottom_row"}},
    }
    chain = _profile_overlays(profiles, "work")
    assert chain == [profiles["base"], profiles["work"]]


def test_profile_overlays_single_profile_without_extends():
    profiles = {"mobile": {"servers": ["local"]}}
    assert _profile_overlays(profiles, "mobile") == [profiles["mobile"]]


def test_profile_overlays_unknown_name_raises():
    with pytest.raises(ConfigError, match="unknown profile 'ghost'"):
        _profile_overlays({}, "ghost")


def test_profile_overlays_cycle_raises_with_chain():
    profiles = {"a": {"extends": "b"}, "b": {"extends": "a"}}
    with pytest.raises(ConfigError, match="inheritance cycle"):
        _profile_overlays(profiles, "a")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_settings.py -k profile_overlays -q`
Expected: FAIL — `ImportError: cannot import name '_profile_overlays'`.

- [ ] **Step 3: Write minimal implementation**

```python
def _profile_overlays(profiles: dict, name: str) -> list[dict]:
    """Overlay dicts from the base-most parent down to `name` (inclusive)."""
    chain: list[str] = []
    seen: set[str] = set()
    cur: str | None = name
    while cur:
        if cur in seen:
            raise ConfigError("profile inheritance cycle: " + " -> ".join(chain + [cur]))
        if cur not in profiles:
            raise ConfigError(f"unknown profile '{cur}'")
        seen.add(cur)
        chain.append(cur)
        cur = profiles[cur].get("extends")
    return [profiles[n] for n in reversed(chain)]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_settings.py -k profile_overlays -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/herdeck/settings.py tests/test_settings.py
git commit -m "feat(settings): add _profile_overlays extends-chain resolver"
```

---

### Task 3: `_merged_sections` — base sections + overlay chain → merged dict + server selection

**Files:**
- Modify: `src/herdeck/settings.py`
- Test: `tests/test_settings.py`

**Interfaces:**
- Consumes: `_merge_section`, `_profile_overlays`.
- Produces: `_merged_sections(data: dict, profile_name: str | None) -> tuple[dict, list[str] | None]` — `(merged, selection)`. `merged` maps each section name in `_OVERLAY_SECTIONS` to its merged value (or `None` if absent). `selection` is the server-id list from the most-derived profile that set `servers`, else `None`. With `profile_name` of `None`/`"default"`, returns the base sections and `None`.
- Also produces module constant `_OVERLAY_SECTIONS = ("deck", "answer_profiles", "macros", "start_profiles", "notifications", "theme", "view", "safety")`.

- [ ] **Step 1: Write the failing test**

```python
from herdeck.settings import _merged_sections


def test_merged_sections_base_only_when_default():
    data = {"view": {"management": "launcher_menu"}, "deck": {"grid": "5x3"}}
    merged, selection = _merged_sections(data, "default")
    assert merged["view"] == {"management": "launcher_menu"}
    assert merged["deck"] == {"grid": "5x3"}
    assert selection is None


def test_merged_sections_applies_profile_overlay():
    data = {
        "view": {"management": "launcher_menu", "tile_fields": ["repo"]},
        "profiles": {"mobile": {"view": {"management": "bottom_row"}}},
    }
    merged, selection = _merged_sections(data, "mobile")
    assert merged["view"] == {"management": "bottom_row", "tile_fields": ["repo"]}
    assert selection is None


def test_merged_sections_captures_server_selection_from_profile():
    data = {"profiles": {"mobile": {"servers": ["local"]}}}
    _merged, selection = _merged_sections(data, "mobile")
    assert selection == ["local"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_settings.py -k merged_sections -q`
Expected: FAIL — `ImportError: cannot import name '_merged_sections'`.

- [ ] **Step 3: Write minimal implementation**

```python
_OVERLAY_SECTIONS = (
    "deck",
    "answer_profiles",
    "macros",
    "start_profiles",
    "notifications",
    "theme",
    "view",
    "safety",
)


def _merged_sections(data: dict, profile_name: str | None) -> tuple[dict, list[str] | None]:
    merged = {sec: data.get(sec) for sec in _OVERLAY_SECTIONS}
    selection: list[str] | None = None
    if profile_name and profile_name != "default":
        for overlay in _profile_overlays(data.get("profiles", {}), profile_name):
            for sec in _OVERLAY_SECTIONS:
                if sec in overlay:
                    merged[sec] = _merge_section(merged.get(sec), overlay[sec])
            if "servers" in overlay:
                selection = list(overlay["servers"])
    return merged, selection
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_settings.py -k merged_sections -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/herdeck/settings.py tests/test_settings.py
git commit -m "feat(settings): add _merged_sections base+overlay merge"
```

---

### Task 4: `_build_config` — merged sections → `Config`

**Files:**
- Modify: `src/herdeck/settings.py` (add import of `_parse_grid`, `_parse_profile` from `.config`)
- Test: `tests/test_settings.py`

**Interfaces:**
- Consumes: `_server_config`, `_macro_set`, `_launcher`, `_notifications_config`, `_theme_config`, `_view_config`, `_safety_config`, `_hardware_config` (existing); `_parse_grid`, `_parse_profile`, `DEFAULT_PROFILES`, `Config`, `ConfigMeta`, `ConfigError` (from `.config`).
- Produces: `_build_config(data, merged, selection, local_data, *, profile_name, env_profile) -> Config`. `data` is the raw top-level dict (for `[[servers]]` defs + `profiles` names); `merged`/`selection` come from `_merged_sections`. Server selection priority: `selection` → `merged["deck"].overview_order` → all `[[servers]]` ids. `answer_profiles` start from `DEFAULT_PROFILES` then apply the merged `answer_profiles` table. `theme`/`view`/`safety` now read for any config (flat included).

- [ ] **Step 1: Write the failing test**

```python
from herdeck.settings import _build_config


def test_build_config_reads_flat_base_including_theme_view_safety():
    data = {
        "servers": [{"id": "local", "url": "ws://x", "token_env": "TOK"}],
        "deck": {"grid": "4x3"},
        "theme": {"colors": {"blocked": "red"}},
        "view": {"management": "bottom_row"},
        "safety": {"approve_always": False},
    }
    merged, selection = _merged_sections(data, "default")
    import os

    os.environ["TOK"] = "secret"
    try:
        cfg = _build_config(
            data, merged, selection, {}, profile_name="default", env_profile=None
        )
    finally:
        del os.environ["TOK"]
    assert cfg.grid == (4, 3)
    assert cfg.theme.colors["blocked"] == "red"
    assert cfg.view.management == "bottom_row"
    assert cfg.safety.approve_always is False
    assert [s.id for s in cfg.servers] == ["local"]
    assert cfg.overview_order == ["local"]


def test_build_config_profile_overrides_grid_and_answer_profiles(monkeypatch):
    monkeypatch.setenv("TOK", "secret")
    data = {
        "servers": [{"id": "local", "url": "ws://x", "token_env": "TOK"}],
        "deck": {"grid": "5x3"},
        "answer_profiles": {"claude": {"approve": ["1"], "deny": ["esc"], "stop": ["ctrl+c"]}},
        "profiles": {
            "mobile": {
                "deck": {"grid": "4x3"},
                "answer_profiles": {"claude": {"approve": ["y"]}},
            }
        },
    }
    merged, selection = _merged_sections(data, "mobile")
    cfg = _build_config(data, merged, selection, {}, profile_name="mobile", env_profile=None)
    assert cfg.grid == (4, 3)
    assert cfg.profiles["claude"].approve == ["y"]
    assert cfg.profiles["claude"].deny == ["esc"]  # kept from base (field merge)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_settings.py -k build_config -q`
Expected: FAIL — `ImportError: cannot import name '_build_config'`.

- [ ] **Step 3: Write minimal implementation**

Add the import near the top of `settings.py` (extend the existing `from .config import (...)`): add `ConfigError` is already imported; add `_parse_grid` and `_parse_profile`:

```python
from .config import _parse_grid, _parse_profile  # add to existing config imports
```

Add the builder:

```python
def _build_config(
    data: dict,
    merged: dict,
    selection: list[str] | None,
    local_data: dict,
    *,
    profile_name: str,
    env_profile: str | None,
) -> Config:
    servers_by_id = {s["id"]: s for s in data.get("servers", [])}
    if selection is None:
        deck_sel = merged.get("deck") or {}
        selection = list(deck_sel.get("overview_order") or servers_by_id)
    servers = []
    for sid in selection:
        if sid not in servers_by_id:
            raise ConfigError(f"unknown server '{sid}'")
        servers.append(_server_config(servers_by_id[sid]))

    deck = merged.get("deck") or {}
    grid = _parse_grid(deck.get("grid", "5x3"))

    answer_profiles = dict(DEFAULT_PROFILES)
    for name, raw in (merged.get("answer_profiles") or {}).items():
        answer_profiles[name] = _parse_profile(name, raw)

    return Config(
        servers=servers,
        profiles=answer_profiles,
        overview_order=selection,
        grid=grid,
        macros=_macro_set(merged.get("macros")),
        start_profiles=_launcher(merged.get("start_profiles")),
        notifications=_notifications_config(merged.get("notifications")),
        theme=_theme_config(merged.get("theme")),
        view=_view_config(merged.get("view")),
        safety=_safety_config(merged.get("safety")),
        hardware=_hardware_config(local_data),
        meta=ConfigMeta(
            active_profile=profile_name,
            profile_names=["default"] + sorted(data.get("profiles", {})),
            env_locked_profile=env_profile is not None,
        ),
    )
```

Note: `_macro_set(None)` already returns `DEFAULT_MACROS`; `_launcher(None)` returns `DEFAULT_START_PROFILES`; `_*_config(None)` return defaults — so absent sections fall back correctly.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_settings.py -k build_config -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/herdeck/settings.py tests/test_settings.py
git commit -m "feat(settings): add _build_config unified Config builder"
```

---

### Task 5: Cut over `resolve_profile`/`list_profiles`/`validate_settings`/`load_config`; delete named-block code; rewrite named-block tests

**Files:**
- Modify: `src/herdeck/settings.py` (rewrite `resolve_profile`, `list_profiles`, `validate_settings`; delete `_runtime_config`, `_resolve_legacy`, `_named_block`)
- Modify: `src/herdeck/config.py` (simplify `load_config`; delete `_load_legacy_config`)
- Rewrite: `tests/test_settings.py` (named-block fixtures/tests → overlay model)

**Interfaces:**
- Consumes: `_merged_sections`, `_build_config`, `_active_profile_name` (existing).
- Produces: `resolve_profile(snapshot, name=None) -> ResolvedSettings`; `list_profiles(snapshot) -> list[dict]`; `validate_settings(snapshot) -> list[str]`; `config.load_config(path) -> Config`. `"default"` and unset resolve to the base; `[profiles.default]` is rejected by `validate_settings`.

- [ ] **Step 1: Write the failing tests**

Replace the named-block `NEW_CONFIG` fixture and add overlay-model tests. Add these tests (the cutover makes them pass via the real resolver):

```python
OVERLAY_CONFIG = """
[[servers]]
id = "local"
url = "ws://x"
token_env = "TOK"

[deck]
grid = "5x3"

[view]
management = "launcher_menu"

[profiles.mobile]
servers = ["local"]
[profiles.mobile.view]
management = "bottom_row"
"""


def _write(tmp_path, text):
    p = tmp_path / "config.toml"
    p.write_text(text)
    return p


def test_resolve_default_is_base(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "secret")
    snap = load_settings(_write(tmp_path, OVERLAY_CONFIG))
    cfg = resolve_profile(snap).config
    assert cfg.view.management == "launcher_menu"  # base, no profile active
    assert cfg.meta.active_profile == "default"
    assert cfg.meta.profile_names == ["default", "mobile"]


def test_resolve_named_profile_applies_overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "secret")
    snap = load_settings(_write(tmp_path, OVERLAY_CONFIG))
    cfg = resolve_profile(snap, "mobile").config
    assert cfg.view.management == "bottom_row"


def test_local_toml_active_profile_selects_overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "secret")
    config_p = _write(tmp_path, OVERLAY_CONFIG)
    (tmp_path / "local.toml").write_text('active_profile = "mobile"\n')
    snap = load_settings(config_p)
    assert resolve_profile(snap).config.view.management == "bottom_row"


def test_validate_rejects_reserved_default_profile(tmp_path):
    text = OVERLAY_CONFIG + "\n[profiles.default]\nservers = []\n"
    snap = load_settings(_write(tmp_path, text))
    errors = validate_settings(snap)
    assert any("default" in e for e in errors)
```

Keep these existing behaviors covered (rewrite their fixtures to `OVERLAY_CONFIG`-style if they used named blocks): legacy hardware merge, missing-token failure, unknown-server failure, env-profile lock, inheritance cycle, `set_active_profile` persist/escape/refuse-invalid/refuse-env-locked, `list_profiles` active marking and legacy default.

**Delete** these named-block-specific tests (they test a dropped format): `test_new_schema_resolves_active_profile`, `test_unknown_named_block_reference_fails`, `test_profile_inheritance_overrides_named_blocks`, `test_unknown_block_reference_fails`, and the `NEW_CONFIG` fixture.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_settings.py -k "resolve_default_is_base or resolve_named_profile or local_toml_active_profile or validate_rejects_reserved" -q`
Expected: FAIL — `resolve_profile` still routes to the old named-block path / `NameError` for removed helpers.

- [ ] **Step 3: Rewrite the resolver and delete the old paths**

In `settings.py`, replace `resolve_profile`, `list_profiles`, `validate_settings` with:

```python
def resolve_profile(snapshot: SettingsSnapshot, name: str | None = None) -> ResolvedSettings:
    active = name or _active_profile_name(snapshot)
    merged, selection = _merged_sections(snapshot.data, active)
    config = _build_config(
        snapshot.data,
        merged,
        selection,
        snapshot.local_data,
        profile_name=active,
        env_profile=snapshot.env_profile,
    )
    return ResolvedSettings(config=config, local_path=snapshot.local_path)


def list_profiles(snapshot: SettingsSnapshot) -> list[dict]:
    locked = snapshot.env_profile is not None
    active = _active_profile_name(snapshot)
    names = ["default"] + sorted(snapshot.data.get("profiles", {}))
    return [{"name": n, "active": n == active, "locked": locked} for n in names]


def validate_settings(snapshot: SettingsSnapshot) -> list[str]:
    errors: list[str] = []
    if "default" in snapshot.data.get("profiles", {}):
        errors.append("profile 'default' is reserved (it is the base config)")
    try:
        resolve_profile(snapshot)
    except ConfigError as exc:
        errors.append(f"active: {exc}")
    for name in sorted(snapshot.data.get("profiles", {})):
        if name == "default":
            continue
        try:
            resolve_profile(snapshot, name)
        except ConfigError as exc:
            errors.append(f"{name}: {exc}")
    return errors
```

Delete `_runtime_config`, `_resolve_legacy`, and `_named_block` from `settings.py`.

In `config.py`, replace `load_config` and delete `_load_legacy_config`:

```python
def load_config(path: str | Path) -> Config:
    from .bootstrap import _discover_local_config_path
    from .settings import load_settings, resolve_profile

    return resolve_profile(load_settings(path, _discover_local_config_path(str(path)))).config
```

- [ ] **Step 4: Run the full settings + config suites**

Run: `PYTHONPATH=. python -m pytest tests/test_settings.py tests/test_config.py -q`
Expected: PASS. Fix any test still referencing a deleted helper or the named-block format by porting it to `OVERLAY_CONFIG` shape.

- [ ] **Step 5: Run the whole suite + lint**

Run: `PYTHONPATH=. python -m pytest -q && ruff check src/herdeck/settings.py src/herdeck/config.py && ruff format --check src/herdeck/settings.py src/herdeck/config.py`
Expected: all pass. Consumers (`ctl.py`, `doctor.py`, `app.py`, deckapp) load config via the same resolver — confirm green.

- [ ] **Step 6: Commit**

```bash
git add src/herdeck/settings.py src/herdeck/config.py tests/test_settings.py
git commit -m "refactor(config): one resolver — flat base + optional profile overlays

Drop the named-block profiles format (_runtime_config/_resolve_legacy/
_named_block) and the legacy/profiles split. resolve_profile now reads the
flat base (incl. theme/view/safety) and applies the active profile's overlay
chain. Legacy flat configs resolve identically. 'default' is the base."
```

---

### Task 6: `set_active_profile` over the unified model + legacy-compat verification

**Files:**
- Modify: `src/herdeck/settings.py` (`set_active_profile`)
- Test: `tests/test_settings.py`, `tests/test_config.py`

**Interfaces:**
- Produces: `set_active_profile(snapshot, name, *, persist=True) -> bool` — accepts `"default"` (switches back to base); a name must be `"default"` or exist in `[profiles.*]`, else `ConfigError`; refuses when `env_profile` is set (returns `False`); writes `active_profile` to `local.toml` preserving other local sections.

- [ ] **Step 1: Write the failing test**

```python
def test_set_active_profile_accepts_default_and_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "secret")
    config_p = _write(tmp_path, OVERLAY_CONFIG)
    (tmp_path / "local.toml").write_text('active_profile = "mobile"\n[local]\ndeck = "d200"\n')
    snap = load_settings(config_p)
    assert set_active_profile(snap, "default") is True
    local_text = (tmp_path / "local.toml").read_text()
    assert 'active_profile = "default"' in local_text
    assert 'deck = "d200"' in local_text  # other local sections preserved


def test_set_active_profile_rejects_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "secret")
    snap = load_settings(_write(tmp_path, OVERLAY_CONFIG))
    with pytest.raises(ConfigError, match="unknown profile 'ghost'"):
        set_active_profile(snap, "ghost")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_settings.py -k "set_active_profile_accepts_default or set_active_profile_rejects_unknown" -q`
Expected: FAIL — current `set_active_profile` only consults `[profiles.*]` (rejects `"default"`).

- [ ] **Step 3: Update `set_active_profile`**

Replace the name-validation head of `set_active_profile` so `"default"` is always valid and other names must exist:

```python
def set_active_profile(snapshot: SettingsSnapshot, name: str, *, persist: bool = True) -> bool:
    if name != "default" and name not in snapshot.data.get("profiles", {}):
        raise ConfigError(f"unknown profile '{name}'")
    if snapshot.env_profile is not None:
        return False
    if name != "default":
        resolve_profile(snapshot, name)  # validate it builds
    if not persist:
        return True
    # ... unchanged local.toml write logic below ...
```

Keep the existing local.toml-writing tail (`local_path` guard, preserve other sections, `_toml_value`/`_toml_line`).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_settings.py -k "set_active_profile" -q`
Expected: PASS.

- [ ] **Step 5: Legacy-compat smoke + full suite + lint**

Verify a real legacy flat config resolves unchanged:

```bash
HERDECK_TOKEN=x python -c "from herdeck.config import load_config; c=load_config('$HOME/.config/herdeck/config.toml'); print([s.id for s in c.servers], c.grid, c.meta.profile_names)"
```
Expected: prints the server ids, `(5, 3)`, `['default']` (no profiles) — no error.

Run: `PYTHONPATH=. python -m pytest -q && ruff check src/herdeck && ruff format --check src/herdeck`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/herdeck/settings.py tests/test_settings.py
git commit -m "feat(settings): set_active_profile supports the base 'default' profile"
```

---

## Self-Review

**1. Spec coverage:**
- Unified schema (base + overlays) → Tasks 1–4. ✓
- Resolve precedence (env > name > local > default) → `_active_profile_name` (existing) used in Task 5 `resolve_profile`; env-lock in `_build_config` meta + `set_active_profile` (Task 6). ✓
- Merge semantics (tables merge / scalars+lists replace / servers select) → Task 1 + Task 4. ✓
- Flat now reads theme/view/safety → Task 4 test. ✓
- Profile overrides grid + answer_profiles → Task 4 test. ✓
- extends + cycle → Task 2. ✓
- `"default"` reserved, `[profiles.default]` error → Task 5 `validate_settings`. ✓
- Drop named-block format + rewrite tests → Task 5. ✓
- Backward compat (legacy = base) → Task 4 + Task 6 smoke. ✓
- `validate_settings` base + per profile → Task 5. ✓
- `set_active_profile` incl. default + env-lock → Task 6. ✓
- Deck Profiles menu keeps working → `list_profiles`/`set_active_profile`/`make_profile_switcher` unchanged signatures, verified by full suite (Task 5/6). ✓

**2. Placeholder scan:** No TBD/TODO; every code step shows real code; the only "unchanged below" reference (Task 6 Step 3) points at concrete existing code in the same function. ✓

**3. Type consistency:** `_merge_section`, `_profile_overlays`, `_merged_sections`, `_build_config` signatures are used consistently across Tasks 1–6; `resolve_profile`/`list_profiles`/`validate_settings`/`set_active_profile` keep their public signatures. ✓
