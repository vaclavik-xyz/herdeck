# Deck icons + saved-connection demo-exit — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bundle monochrome SVG marks for the 5 mapped agent types so the frozen `.app` renders real icons (not letter fallbacks), and add a one-click "connect to saved connection" escape from the demo trap.

**Architecture:** Two independent slices sharing one spec. **Icons:** drop 5 white SVGs into `src/herdeck/assets/` (filenames = `agent_type` keys), bake them to content-keyed PNGs (committed alongside the SVGs), guard with real-asset tests + the smoke gate. **Saved connection:** a new `"saved"` choice in `connect()` that resolves the on-disk remote (token from keychain), clears the demo marker transactionally, and swaps to live; a keychain-free `saved_remote_available` status flag drives a button shown in both onboarding views.

**Tech Stack:** Python 3.12+ (deckapp sidecar, Pillow, cairosvg build-time), Svelte 5 + TypeScript (desktop), Vitest, pytest, bash freeze/smoke scripts.

**Spec:** `docs/superpowers/specs/2026-06-30-deck-icons-and-saved-connection-design.md`

## Global Constraints

These bind EVERY task. Exact values, copied from the spec / CLAUDE.md:

- **SVG filenames = `agent_type` keys, NOT Simple Icons slugs.** `_base_glyph` looks up `assets/{_safe_name(agent_type)}.svg`. The 5 files are exactly: `claude.svg`, `cursor.svg`, `copilot.svg`, `gemini.svg`, `opencode.svg`. (`agent_type` verified live against the herdr socket = bare lowercase `['claude','codex']`; herdr normalizes aliases before emission, so no alias entry is needed.)
- **Baked PNGs MUST be committed** in the same commit as their SVG. The baker writes content-keyed PNGs into the source-tree assets dir; a clean checkout that has the SVG but not the baked PNG makes the frozen rasterizer (`make_png_rasterizer`, no cairosvg) silently degrade the glyph to a letter.
- **Monochrome white:** every bundled mark is `fill="#ffffff"`, `viewBox="0 0 24 24"` (matches existing `codex.svg`; `/white` variant stays legible on any status background).
- **Offline-first deck stays offline.** Do NOT add any runtime Simple-Icons fetch. `_default_icons()` keeps `fetch=lambda slug: None` in both frozen and dev. The only change is more bundled assets.
- **Secret values are one-way.** A token is never returned, logged, or written to TOML. `select_live()` resolves it from the keychain internally; `_has_saved_remote()` does a raw TOML read with NO token/keychain resolution.
- **Keyring service literal is `"herdeck"`** (unchanged; only relevant to existing keychain reads the saved flow reuses).
- **`saved_remote_available` is keychain-free + mock-gated.** It is `_has_saved_remote(config_service)` (raw TOML, returns False under `HERDECK_MOCK`), NEVER `select_live() is not None` (which reads the keychain on the hot `/setup` poll and is masked by `HERDECK_MOCK`).
- **Code + commit messages in English; conventional-commit format** (`feat:`, `fix:`, `test:`, `docs:`, `chore:`). No `Co-Authored-By` trailers.
- **Attribution:** Simple Icons icon data is CC0 1.0, but the rendered marks remain the trademarks of their owners — ship a NOTICE/attribution file (covers the pre-existing `codex.svg`/OpenAI too).

---

## File Structure

**Icons slice:**
- Create: `src/herdeck/assets/{claude,cursor,copilot,gemini,opencode}.svg` (+ their baked `*.png`)
- Create: `src/herdeck/assets/ATTRIBUTION.md`
- Create: `tests/test_bundled_agent_icons.py`
- Modify: `desktop/scripts/smoke-sidecar.sh` (loop all SVGs, not just codex)

**Saved-connection slice:**
- Modify: `src/herdeck/deckapp/server.py` (`_has_saved_remote` helper, `_setup_status` key, `"saved"` branch in `connect`)
- Modify: `tests/test_deckapp_setup_routes.py` (status + connect-saved tests)
- Modify: `desktop/src/lib/onboardingClient.ts` (`SetupStatus.savedRemoteAvailable`, `parseSetupStatus`, `ConnectRequest`)
- Modify: `desktop/src/lib/onboardingClient.test.ts` (parse + transport tests)
- Modify: `desktop/src/lib/Onboarding.svelte` (button in both view blocks)

**Gates:** pytest (`.venv/bin/python -m pytest`) · ruff (`.venv/bin/ruff check src tests`) · npm test + npm run build (in `desktop/`). The icon freeze/smoke gate (`build-sidecar.sh` + `smoke-sidecar.sh`) is the controller's manual gate (needs the full toolchain + a frozen binary) — see the macbench gate at the end.

**Pre-fetched assets (network-free):** The 5 SVGs are already fetched + verified (200, monochrome white) from `cdn.simpleicons.org/<slug>/white` and staged at:
`/private/tmp/claude-501/-Users-admin-projects-herdeck/315621a2-c018-471e-8eaa-98a9c5d2bdd6/scratchpad/`
Tasks copy from there (deterministic, no network). Provenance is documented in `ATTRIBUTION.md`.

---

## Task 1: Bundle the 5 agent SVGs + attribution

**Files:**
- Create: `src/herdeck/assets/claude.svg`, `cursor.svg`, `copilot.svg`, `gemini.svg`, `opencode.svg`
- Create: `src/herdeck/assets/ATTRIBUTION.md`
- Test: `tests/test_bundled_agent_icons.py`

**Interfaces:**
- Consumes: `herdeck.icons._ASSETS_DIR` (the source-tree assets dir), `herdeck.icons.DEFAULT_AGENT_SLUGS`.
- Produces: 5 committed monochrome SVGs named by `agent_type`; `tests/test_bundled_agent_icons.py` with the asset-sanity test (Task 2 appends the baked-PNG + provider tests to this file).

- [ ] **Step 1: Write the failing asset-sanity test**

Create `tests/test_bundled_agent_icons.py`:

```python
import os
import xml.etree.ElementTree as ET

from herdeck.icons import _ASSETS_DIR

# The 5 agent types that must ship a bundled monochrome mark (codex already does).
# Filenames are the agent_type keys, NOT Simple Icons slugs (see _base_glyph lookup).
BUNDLED_AGENT_ICONS = ("claude", "cursor", "copilot", "gemini", "opencode")


def test_bundled_svgs_exist_and_are_monochrome_white():
    for name in BUNDLED_AGENT_ICONS:
        path = os.path.join(_ASSETS_DIR, f"{name}.svg")
        assert os.path.exists(path), f"missing bundled SVG: {name}.svg"
        text = open(path, encoding="utf-8").read()
        root = ET.fromstring(text)  # parses as XML (raises on malformed)
        assert root.get("fill") == "#ffffff", f"{name}.svg root fill must be #ffffff"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_bundled_agent_icons.py -v`
Expected: FAIL — `missing bundled SVG: claude.svg` (assets not added yet).

- [ ] **Step 3: Add the 5 SVG files (network-free copy from the verified scratch stage)**

```bash
SCRATCH=/private/tmp/claude-501/-Users-admin-projects-herdeck/315621a2-c018-471e-8eaa-98a9c5d2bdd6/scratchpad
for n in claude cursor copilot gemini opencode; do
  cp "$SCRATCH/$n.svg" "src/herdeck/assets/$n.svg"
done
```

If the scratch stage is gone, re-fetch from the canonical source (verified to return 200 + monochrome white):
```bash
for pair in claude:claude cursor:cursor copilot:githubcopilot gemini:googlegemini opencode:opencode; do
  n=${pair%%:*}; slug=${pair##*:}
  curl -fsS "https://cdn.simpleicons.org/$slug/white" -o "src/herdeck/assets/$n.svg"
done
```

The smallest three for reference (must be byte-identical to what ships; the larger `claude.svg`/`copilot.svg` come from the copy/fetch above):
```
opencode.svg:
<svg fill="#ffffff" role="img" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><title>OpenCode</title><path d="M22 24H2V0h20zM17 4.8H7v14.4h10z"/></svg>

cursor.svg:
<svg fill="#ffffff" role="img" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><title>Cursor</title><path d="M11.503.131 1.891 5.678a.84.84 0 0 0-.42.726v11.188c0 .3.162.575.42.724l9.609 5.55a1 1 0 0 0 .998 0l9.61-5.55a.84.84 0 0 0 .42-.724V6.404a.84.84 0 0 0-.42-.726L12.497.131a1.01 1.01 0 0 0-.996 0M2.657 6.338h18.55c.263 0 .43.287.297.515L12.23 22.918c-.062.107-.229.064-.229-.06V12.335a.59.59 0 0 0-.295-.51l-9.11-5.257c-.109-.063-.064-.23.061-.23"/></svg>
```

- [ ] **Step 4: Add the attribution file**

Create `src/herdeck/assets/ATTRIBUTION.md`:

```markdown
# Bundled agent icons — attribution

The agent marks in this directory (`claude.svg`, `codex.svg`, `copilot.svg`,
`cursor.svg`, `gemini.svg`, `opencode.svg`) are sourced from
[Simple Icons](https://simpleicons.org) (the `/white` monochrome variant).

- **Icon data** is distributed by Simple Icons under **CC0 1.0** (public domain).
- **The brand marks themselves remain trademarks of their respective owners**
  (Anthropic, OpenAI, Microsoft/GitHub, Cursor, Google, OpenCode). They are
  bundled solely to identify which agent a deck tile represents. Their use here
  does not imply any affiliation with or endorsement by those owners.

The SVGs are committed as static assets; herdeck performs no build-time or
runtime fetch from Simple Icons for the deck (the deck renders fully offline).
```

- [ ] **Step 5: Run the sanity test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_bundled_agent_icons.py -v`
Expected: PASS (`test_bundled_svgs_exist_and_are_monochrome_white`).

- [ ] **Step 6: Commit**

```bash
git add src/herdeck/assets/claude.svg src/herdeck/assets/cursor.svg \
        src/herdeck/assets/copilot.svg src/herdeck/assets/gemini.svg \
        src/herdeck/assets/opencode.svg src/herdeck/assets/ATTRIBUTION.md \
        tests/test_bundled_agent_icons.py
git commit -m "feat(icons): bundle white SVG marks for claude/cursor/copilot/gemini/opencode + attribution"
```

---

## Task 2: Bake PNGs + real-asset/provider guards + smoke loop

**Files:**
- Create: `src/herdeck/assets/{5 content-keyed}.png` (baked, committed)
- Modify: `tests/test_bundled_agent_icons.py` (append baked-PNG + provider tests)
- Modify: `desktop/scripts/smoke-sidecar.sh:35-50` (loop all `*.svg`)

**Interfaces:**
- Consumes: `herdeck.frozen.prerasterize_assets`, `herdeck.frozen.glyph_png_name`, `herdeck.frozen.make_png_rasterizer`, `herdeck.frozen.baked_assets_dir`, `herdeck.frozen.is_frozen`, `herdeck.deckapp.server._default_icons`, `herdeck.icons.ICON_SIZE`, `_ASSETS_DIR`, `IconProvider._base_glyph` / `_letter_glyph`.
- Produces: committed baked PNGs for all bundled SVGs; the invariant "committed SVG ⇒ committed decodable 196×196 baked PNG" guarded in pytest; smoke gate covers every SVG.

- [ ] **Step 1: Write the failing baked-PNG + provider tests**

Append to `tests/test_bundled_agent_icons.py`:

```python
import glob

from PIL import Image

from herdeck import frozen
from herdeck.deckapp import server
from herdeck.icons import ICON_SIZE


def _all_bundled_svgs():
    return sorted(glob.glob(os.path.join(_ASSETS_DIR, "*.svg")))


def test_every_bundled_svg_has_committed_decodable_baked_png():
    """Invariant guard against the silent Q1 regression: every committed SVG must
    have its committed content-keyed baked PNG, decodable at 196x196 (what the
    frozen rasterizer loads, with NO cairosvg). Covers the 5 new marks + codex."""
    svgs = _all_bundled_svgs()
    assert len(svgs) >= 6  # codex + the 5 new marks
    for svg_path in svgs:
        svg = open(svg_path, encoding="utf-8").read()
        png = os.path.join(_ASSETS_DIR, frozen.glyph_png_name(svg))
        assert os.path.exists(png), f"missing committed baked PNG for {os.path.basename(svg_path)}"
        im = Image.open(png)
        im.load()  # full decode (raises on corrupt data)
        im = im.convert("RGBA")
        assert im.size == (ICON_SIZE, ICON_SIZE), f"{png} is {im.size}, want {(ICON_SIZE, ICON_SIZE)}"


def test_frozen_provider_renders_bundled_mark_not_letter(monkeypatch):
    """A frozen-style provider (PNG rasterizer + baked assets dir = the real source
    assets dir) returns the BUNDLED mark, not the letter fallback, for each type."""
    monkeypatch.setattr(frozen, "is_frozen", lambda: True)
    monkeypatch.setattr(frozen, "baked_assets_dir", lambda: _ASSETS_DIR)
    icons = server._default_icons()
    for name in BUNDLED_AGENT_ICONS:
        glyph = icons._base_glyph(name)
        letter = icons._letter_glyph(name)
        assert glyph.size == (ICON_SIZE, ICON_SIZE)
        assert list(glyph.getdata()) != list(letter.getdata()), (
            f"{name}: asset branch missed -> degraded to letter glyph"
        )
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_bundled_agent_icons.py -v`
Expected: FAIL — `missing committed baked PNG for claude.svg` (and the provider test degrades to letter), because the 5 PNGs are not baked yet.

- [ ] **Step 3: Bake the PNGs into the source assets dir (idempotent)**

```bash
.venv/bin/python -c "from herdeck.frozen import prerasterize_assets; print(prerasterize_assets('src/herdeck/assets', 'src/herdeck/assets'))"
```
Expected: prints a list of 6 PNG filenames; 5 new content-keyed PNGs now exist in `src/herdeck/assets/` (codex's `a6817b9c…png` is left untouched). Verified hashes for the new marks: claude `1acd5e33…`, copilot `a2c933eb…`, cursor `c69e3ad3…`, gemini `b9b010e3…`, opencode `f81556c7…`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_bundled_agent_icons.py -v`
Expected: PASS (all 3 tests).

- [ ] **Step 5: Extend the smoke gate to cover every SVG (not just codex)**

In `desktop/scripts/smoke-sidecar.sh`, replace the codex-only baked-asset proof. Change the comment header at lines 14-19 to reflect "every bundled SVG", and replace the heredoc body (lines 35-50) so it loops all `*.svg`:

```bash
"$DECODER" - "$ASSETS_DIR" <<'PY'
import glob, hashlib, os, sys
from PIL import Image
d = sys.argv[1]
svgs = sorted(glob.glob(os.path.join(d, "*.svg")))
assert svgs, f"FAIL: no bundled SVGs in {d}"
for svg_path in svgs:
    svg = open(svg_path, encoding="utf-8").read()
    name = hashlib.sha1(svg.encode("utf-8")).hexdigest() + ".png"
    png = os.path.join(d, name)
    assert os.path.exists(png), f"FAIL: baked PNG {name} missing for {os.path.basename(svg_path)}"
    im = Image.open(png)
    im.load()                  # forces full IDAT inflate + decode (raises on corrupt data)
    im = im.convert("RGBA")    # the exact op herdeck.frozen.make_png_rasterizer performs
    assert im.size == (196, 196), f"FAIL: baked PNG dims {im.size} for {name}, want (196, 196)"
print(f"OK: {len(svgs)} bundled SVG(s) have decodable 196x196 baked PNGs")
PY
```

Also update the comment block just above (lines 14-19) from the codex-specific wording to: "Assert every bundled SVG glyph has a decodable baked PNG in the bundle — proves prerasterize + bundling worked for all marks (a missing/broken baked PNG silently degrades to a letter glyph)."

- [ ] **Step 6: Syntax-check the smoke script**

Run: `bash -n desktop/scripts/smoke-sidecar.sh && echo "syntax ok"`
Expected: `syntax ok` (full smoke needs a frozen binary — run in the controller's freeze gate).

- [ ] **Step 7: Commit**

```bash
git add src/herdeck/assets/*.png tests/test_bundled_agent_icons.py desktop/scripts/smoke-sidecar.sh
git commit -m "feat(icons): bake + commit agent glyph PNGs, guard with real-asset tests + smoke loop"
```

---

## Task 3: `saved_remote_available` status flag (keychain-free)

**Files:**
- Modify: `src/herdeck/deckapp/server.py` (add `_has_saved_remote`; add key in `_setup_status` ~line 309-316)
- Test: `tests/test_deckapp_setup_routes.py` (append helper + HTTP status tests)

**Interfaces:**
- Consumes: `DeckApp._config_service` (has `._config_path: pathlib.Path`), `os.environ`.
- Produces: `server._has_saved_remote(config_service) -> bool` (raw TOML, mock-gated, no keychain); `_setup_status()` returns an extra `"saved_remote_available": bool` key. Task 5 (frontend) consumes the JSON key `saved_remote_available`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_deckapp_setup_routes.py`:

```python
def test_has_saved_remote_true_with_servers(tmp_path):
    import types

    from herdeck.deckapp import server as s

    cfg = tmp_path / "config.toml"
    cfg.write_text('[[servers]]\nid = "herdr"\nurl = "ws://1.2.3.4:8788"\ntoken_env = "HERDECK_HERDR_TOKEN"\n')
    svc = types.SimpleNamespace(_config_path=cfg)
    assert s._has_saved_remote(svc) is True


def test_has_saved_remote_false_when_mock_env(tmp_path, monkeypatch):
    import types

    from herdeck.deckapp import server as s

    cfg = tmp_path / "config.toml"
    cfg.write_text('[[servers]]\nid = "herdr"\nurl = "ws://1.2.3.4:8788"\ntoken_env = "HERDECK_HERDR_TOKEN"\n')
    monkeypatch.setenv("HERDECK_MOCK", "1")  # masked even with a real saved config
    svc = types.SimpleNamespace(_config_path=cfg)
    assert s._has_saved_remote(svc) is False


def test_has_saved_remote_false_without_servers(tmp_path, monkeypatch):
    import types

    from herdeck.deckapp import server as s

    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    missing = tmp_path / "config.toml"
    assert s._has_saved_remote(types.SimpleNamespace(_config_path=missing)) is False  # no file
    empty = tmp_path / "empty.toml"
    empty.write_text("[base]\n")  # parses, but no [[servers]]
    assert s._has_saved_remote(types.SimpleNamespace(_config_path=empty)) is False
    assert s._has_saved_remote(None) is False  # no config service


def test_setup_status_exposes_saved_remote_available(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    cfg = tmp_path / "config.toml"
    cfg.write_text('[[servers]]\nid = "herdr"\nurl = "ws://1.2.3.4:8788"\ntoken_env = "HERDECK_HERDR_TOKEN"\n')
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.setenv("HERDR_SOCKET", str(tmp_path / "nope.sock"))
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        _, body = _get(app, f"/setup?token={app.token}")
        assert body["saved_remote_available"] is True
    finally:
        app.close()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_deckapp_setup_routes.py -k saved_remote -v`
Expected: FAIL — `AttributeError: module 'herdeck.deckapp.server' has no attribute '_has_saved_remote'`.

- [ ] **Step 3: Add the `_has_saved_remote` helper**

In `src/herdeck/deckapp/server.py`, add this function right after `select_live()` (which ends at line 599, before `select_source_kind`):

```python
def _has_saved_remote(config_service) -> bool:
    """True when an on-disk config has at least one ``[[servers]]`` entry — a RAW
    TOML read with NO token/keychain resolution, so it is safe to call on the hot
    ``/setup`` poll. Authoritative resolution (does the token actually resolve?) is
    deferred to connect-time ``select_live()`` (fail-soft "no saved connection").
    Mock-gated: under ``HERDECK_MOCK`` there is no saved button, matching the
    existing ``reason="mock_env"`` special-casing."""
    import tomllib

    if os.environ.get("HERDECK_MOCK") or config_service is None:
        return False
    path = config_service._config_path
    if not path.exists():
        return False
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    servers = data.get("servers")
    return isinstance(servers, list) and len(servers) > 0
```

- [ ] **Step 4: Surface it in `_setup_status`**

In `_setup_status()` (the return dict at lines 309-316), add the key:

```python
        return {
            "mode": mode,
            "connected": self._source.connected,
            "reason": reason,
            "local_herdr_available": socket_exists,
            "saved_remote_available": _has_saved_remote(self._config_service),
            "choice": choice,
            "socket_path": socket_path,
        }
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_deckapp_setup_routes.py -k saved_remote -v`
Expected: PASS (all 4 tests).

- [ ] **Step 6: Run ruff + the full setup-routes module**

Run: `.venv/bin/ruff check src tests && .venv/bin/python -m pytest tests/test_deckapp_setup_routes.py -q`
Expected: ruff clean; all setup-route tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/herdeck/deckapp/server.py tests/test_deckapp_setup_routes.py
git commit -m "feat(deckapp): keychain-free saved_remote_available in /setup status"
```

---

## Task 4: `"saved"` choice in `connect()`

**Files:**
- Modify: `src/herdeck/deckapp/server.py` (`connect()` imports ~line 837; new branch before `return None` ~line 969)
- Test: `tests/test_deckapp_setup_routes.py` (append connect-saved tests)

**Interfaces:**
- Consumes: `select_live()`, `build_live_source_for_connect()`, `DeckApp._prepare_swap`/`_commit_swap`/`_set_local_bridge`, `_reloader_for`, `_select_source`, `_restore_choice`, `onboarding.read_choice`/`clear_choice`.
- Produces: `connect(app, {"choice": "saved"})` → swaps to the on-disk remote + clears the demo/local marker, or `{"ok": False, "error": "no saved connection"}` when nothing resolves. Task 5's `ConnectRequest` adds `{ choice: "saved" }`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_deckapp_setup_routes.py`:

```python
def _write_saved_config(tmp_path, monkeypatch):
    """A resolvable saved remote: config.toml with one server + its keychain token +
    a pre-existing demo marker (the trap we are escaping). Returns the config path."""
    cfg = tmp_path / "config.toml"
    cfg.write_text('[[servers]]\nid = "herdr"\nurl = "ws://10.0.0.5:8788"\ntoken_env = "HERDECK_HERDR_TOKEN"\n')
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    fake.store[("herdeck", "HERDECK_HERDR_TOKEN")] = "saved-tok"  # so select_live() resolves
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    from herdeck.deckapp import onboarding

    onboarding.write_choice(str(cfg), "demo")  # currently trapped in demo
    return str(cfg)


def test_connect_saved_swaps_live_and_clears_marker(tmp_path, monkeypatch):
    from herdeck.deckapp import onboarding

    cfg = _write_saved_config(tmp_path, monkeypatch)
    # Build a non-networked live source that reports connected=False (honest async dial).
    monkeypatch.setattr(srv, "build_live_source_for_connect", lambda config, server: _DisconnectedSource())
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    prev = app._source
    try:
        status, body = _post(app, "/setup/connect", {"choice": "saved"})
        assert status == 200 and body["ok"] is True
        assert body["connected"] is False  # honest: the just-built source isn't connected yet
        assert app._source is not prev  # swapped to the saved remote
        assert onboarding.read_choice(cfg) is None  # demo marker cleared
    finally:
        app.close()


def test_connect_saved_no_config_is_soft_error_keeps_marker(tmp_path, monkeypatch):
    from herdeck.deckapp import onboarding

    monkeypatch.setenv("HERDECK_CONFIG", str(tmp_path / "config.toml"))  # absent
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    onboarding.write_choice(str(tmp_path / "config.toml"), "demo")
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    prev = app._source
    try:
        status, body = _post(app, "/setup/connect", {"choice": "saved"})
        assert status == 200 and body["ok"] is False and body["error"] == "no saved connection"
        assert app._source is prev  # no swap
        assert onboarding.read_choice(str(tmp_path / "config.toml")) == "demo"  # marker untouched
    finally:
        app.close()


def test_connect_saved_build_failure_restores_marker(tmp_path, monkeypatch):
    from herdeck.deckapp import onboarding

    cfg = _write_saved_config(tmp_path, monkeypatch)

    def _boom(config, server):
        raise RuntimeError("connector blew up")

    monkeypatch.setattr(srv, "build_live_source_for_connect", _boom)
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    prev = app._source
    try:
        status, body = _post(app, "/setup/connect", {"choice": "saved"})
        assert status == 200 and body["ok"] is False
        assert "could not restore saved connection" in body["error"]
        assert app._source is prev  # previous source intact
        assert onboarding.read_choice(cfg) == "demo"  # marker restored (build failed before clear)
    finally:
        app.close()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_deckapp_setup_routes.py -k connect_saved -v`
Expected: FAIL — the `"saved"` choice is unknown, so `connect()` returns `None` → HTTP 400 (`urllib.error.HTTPError: 400`), not the expected 200/body.

- [ ] **Step 3: Add `clear_choice` to the `connect()` imports**

In `connect()` (line 837), extend the onboarding import:

```python
    from .onboarding import clear_choice, read_choice, write_choice
```

- [ ] **Step 4: Add the `"saved"` branch**

In `connect()`, insert this branch immediately before the final `return None  # unknown choice -> 400` (line 969):

```python
    if choice == "saved":
        # One-click escape from the demo trap: re-select the on-disk remote (token from
        # the keychain) and clear the demo/local marker. Transactional like the others —
        # build + prepare BEFORE clearing the marker; any failure restores it and closes
        # the just-built source. NO _suppress_reload (this writes only onboarding.toml,
        # which the watcher does not track) and NO probe (select_live() confirms token
        # PRESENCE, not validity; the live source dials async, so connected may be False).
        remote = select_live()  # (config, server) from disk + keychain, or None
        if remote is None:
            return {"ok": False, "error": "no saved connection"}
        config, server = remote
        prior_choice = read_choice(config_path)
        new_source = None
        try:
            new_source = build_live_source_for_connect(config, server)  # build (fallible)
            prepared = app._prepare_swap(new_source, clock=time.monotonic)  # render (fallible)
            clear_choice(config_path)  # persist: drop the demo/local marker
        except Exception:
            _restore_choice(config_path, prior_choice)  # marker untouched / restored
            if new_source is not None:
                new_source.close()
            return {"ok": False, "error": "could not restore saved connection"}
        app._commit_swap(new_source, prepared)  # assignment-only, non-failing
        app._set_local_bridge(None)  # saved targets remote; drop any local bridge
        app._reloader = _reloader_for(app, ("remote",), _select_source)
        return {"ok": True, "connected": app._source.connected}

    return None  # unknown choice -> 400
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_deckapp_setup_routes.py -k connect_saved -v`
Expected: PASS (all 3 tests).

- [ ] **Step 6: Run ruff + the full module**

Run: `.venv/bin/ruff check src tests && .venv/bin/python -m pytest tests/test_deckapp_setup_routes.py -q`
Expected: ruff clean; all setup-route tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/herdeck/deckapp/server.py tests/test_deckapp_setup_routes.py
git commit -m "feat(deckapp): saved-connection choice in /setup/connect (clears demo marker, swaps to live)"
```

---

## Task 5: Frontend — `savedRemoteAvailable` + saved button in both views

**Files:**
- Modify: `desktop/src/lib/onboardingClient.ts` (`SetupStatus`, `parseSetupStatus`, `ConnectRequest`)
- Modify: `desktop/src/lib/onboardingClient.test.ts` (parse + transport tests)
- Modify: `desktop/src/lib/Onboarding.svelte` (button in reconnect + welcome blocks, `connectSaved` handler)

**Interfaces:**
- Consumes: the `saved_remote_available` JSON key (Task 3); `{ choice: "saved" }` accepted by `/setup/connect` (Task 4); existing `run()`, `transport.connect`, `status` prop, `onConnected` (App.svelte re-polls `/setup` after `ok`).
- Produces: `SetupStatus.savedRemoteAvailable: boolean`; `ConnectRequest | { choice: "saved" }`; a "Připojit k uloženému spojení" button gated on `status?.savedRemoteAvailable === true` in BOTH view blocks.

- [ ] **Step 1: Write the failing parse + transport tests**

In `desktop/src/lib/onboardingClient.test.ts`, add `saved_remote_available: false` to the top-of-file `full` fixture (so existing cases keep a complete object), then add:

```typescript
  it("maps saved_remote_available -> savedRemoteAvailable (true)", () => {
    const s = parseSetupStatus({ ...full, saved_remote_available: true });
    expect(s?.savedRemoteAvailable).toBe(true);
  });

  it("defaults savedRemoteAvailable to false when absent or wrong-typed", () => {
    expect(parseSetupStatus({ mode: "mock" })?.savedRemoteAvailable).toBe(false);
    expect(parseSetupStatus({ mode: "mock", saved_remote_available: "yes" })?.savedRemoteAvailable).toBe(false);
  });
```

And a transport-forwarding case inside the existing `describe("setupTransport", ...)` block:

```typescript
  it("connect() forwards a saved request as the body arg", async () => {
    let seen: unknown;
    const invoke = (async (_cmd: string, args?: unknown) => {
      seen = args;
      return { ok: true, connected: false };
    }) as InvokeFn;
    const t = setupTransport(invoke);
    const r = await t.connect({ choice: "saved" });
    expect(seen).toEqual({ body: { choice: "saved" } });
    expect(r.ok).toBe(true);
  });
```

If `InvokeFn` is not already imported in the test file, add it: `import type { InvokeFn } from "./deckClient";` (check the existing imports first — do not duplicate).

- [ ] **Step 2: Run them to verify they fail**

Run: `cd desktop && npm test -- --run onboardingClient`
Expected: FAIL — `savedRemoteAvailable` is `undefined` on the parsed object (property does not exist yet).

- [ ] **Step 3: Add the field + parse + request type**

In `desktop/src/lib/onboardingClient.ts`:

In the `SetupStatus` interface, add (after `localHerdrAvailable`):
```typescript
  savedRemoteAvailable: boolean;
```

In `parseSetupStatus`'s returned object, add (after the `localHerdrAvailable` line):
```typescript
    savedRemoteAvailable: v.saved_remote_available === true,
```

In the `ConnectRequest` union, add the saved variant:
```typescript
export type ConnectRequest =
  | { choice: "local" }
  | { choice: "demo" }
  | { choice: "saved" }
  | { choice: "remote"; url: string; token: string; id?: string };
```

- [ ] **Step 4: Run the client tests to verify they pass**

Run: `cd desktop && npm test -- --run onboardingClient`
Expected: PASS (new + existing cases).

- [ ] **Step 5: Add the button to Onboarding.svelte (both view blocks)**

In `desktop/src/lib/Onboarding.svelte`:

Add a derived flag next to `localAvailable` (line 34):
```typescript
  const savedAvailable = $derived(status?.savedRemoteAvailable === true);
```

Add a handler next to `connectDemo` (after line 53):
```typescript
  function connectSaved(): void {
    void run({ choice: "saved" });
  }
```

In the **reconnect** block, add the saved button as the first action (inside `{#if view === "reconnect"}`, before the existing `<div class="actions">` at line 72):
```svelte
    {#if savedAvailable}
      <p class="lead">Máš uložené spojení.</p>
      <div class="actions">
        <button class="primary" disabled={busy} onclick={connectSaved}>Připojit k uloženému spojení</button>
      </div>
    {/if}
```

In the **welcome** block, add the saved button immediately after `<h1>Připojit herdeck</h1>` (line 79), before the `{#if localAvailable}`:
```svelte
    {#if savedAvailable}
      <p class="lead">Máš uložené spojení.</p>
      <div class="actions">
        <button class="primary" disabled={busy} onclick={connectSaved}>Připojit k uloženému spojení</button>
      </div>
    {/if}
```

- [ ] **Step 6: Run the full desktop test suite + build**

Run: `cd desktop && npm test -- --run && npm run build`
Expected: all Vitest pass (incl. `onboarding.smoke.test.ts` compile-smoke); `npm run build` clean.

- [ ] **Step 7: Commit**

```bash
git add desktop/src/lib/onboardingClient.ts desktop/src/lib/onboardingClient.test.ts desktop/src/lib/Onboarding.svelte
git commit -m "feat(desktop): saved-connection button in onboarding card (both views)"
```

---

## Self-Review (run after all tasks)

- [ ] **Spec coverage:** Component 1 → Tasks 1-2 (SVGs + attribution + bake + guards + smoke). Component 2 → Tasks 3-5 (status flag + connect branch + frontend button in both views). Manual macbench gate (below) covers the two visual checks.
- [ ] **Type consistency:** `saved_remote_available` (JSON) ↔ `savedRemoteAvailable` (TS) ↔ `_has_saved_remote` (Py). `{choice:"saved"}` accepted by both `connect()` (Py) and `ConnectRequest` (TS).
- [ ] **No placeholders:** all code blocks are complete; SVGs come from a verified network-free copy with a documented fetch fallback.

## Manual gate (macbench — controller, after merge)

Rebuild the `.app` (freeze bakes the assets) + redeploy, then verify:
1. **Icons:** the Claude tile shows the **Claude mark** (not "C"); codex still OpenAI; cursor/copilot/gemini when present.
2. **Demo exit:** "Prozkoumat demo" → demo deck → `⚙` → the card shows **"Připojit k uloženému spojení"** → click → flips to the deck with no URL/token typing; `onboarding.toml` is gone. **Confirm the deck reaches `live · connected`** (saved does not probe — the source dials async; a card flip alone is not proof).
