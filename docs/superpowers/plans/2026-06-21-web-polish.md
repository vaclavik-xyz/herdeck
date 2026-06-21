# F4 — Web simulator polish — Implementation Plan

> **For agentic workers:** strict TDD for any Python change; client JS/CSS is verified by reading the served page and by a server test where applicable. Checkbox steps. Conventional commits (English, no Co-Authored-By). After each commit run `roborev show <sha>` and fix findings.

**Goal:** Make the browser simulator nicer to use on a phone: keyboard shortcuts to press tiles, a visual highlight of the last-pressed cell, and a responsive layout for phone landscape — without changing the pixel-faithful tile rendering or the per-tile fetch logic.

**Files:**
- Modify: `src/herdeck/driver/web.py` (the `_PAGE` HTML/JS/CSS string only; do NOT change `render`, `render_working`, `_state`, version logic, or the HTTP routes)
- Test: `tests/test_driver_web.py`

## Global constraints
- Work in your assigned worktree (lead gives the path); verify branch first.
- Create venv: `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`.
- Do NOT touch per-tile/panel versioning or partial-render code paths.
- Keep all existing tests green.

---

### Task 1: served page exposes the new affordances (server-side assertable)
Because the behavior is client-side, lock it in with a test that asserts the served
HTML contains the wiring, so regressions are caught.

- [ ] **Failing test** in `tests/test_driver_web.py`:
```python
def test_page_has_keyboard_and_highlight_support():
    d = make_deck()
    # the page is the module-level _PAGE served at "/"
    from herdeck.driver import web
    page = web._PAGE
    assert "keydown" in page                 # keyboard shortcuts wired
    assert "@media" in page                   # responsive layout present
    assert "press" in page                    # still posts presses
```
- [ ] Run: `.venv/bin/python -m pytest tests/test_driver_web.py::test_page_has_keyboard_and_highlight_support -v` → FAIL (no `keydown`/`@media` yet).
- [ ] **Implement** in `web.py` `_PAGE`:
  - Keyboard: `document.addEventListener('keydown', e => {...})` mapping digit keys
    `1`..`9` → press tiles `0`..`8`, `0` → tile `9`; ignore when modifier keys held.
    Reuse the existing `fetch('/press/'+i,{method:'POST'})`.
  - Highlight: track last-pressed index in JS; add/remove a CSS class
    (`.cell.active{outline:3px solid #5af}`) on the corresponding cell on press.
  - Responsive: add a `<meta name=viewport content="width=device-width,initial-scale=1">`
    and an `@media (max-width:560px)` block that shrinks the grid columns/cell sizes so
    the 5-wide deck fits a phone screen (use `min()`/viewport units for cell size).
  - Keep the existing 13-button + panel construction and the poll loop intact.
- [ ] Run the test → PASS.
- [ ] Manual sanity: start `HERDECK_MOCK=1 HERDECK_DECK=web .venv/bin/python -m herdeck.app`,
  open the URL, confirm digit keys press tiles, last-pressed cell is outlined, and the
  layout fits a narrow window. (Describe what you observed.)
- [ ] Commit: `feat(web): keyboard shortcuts, last-press highlight, responsive layout`.
- [ ] `roborev show <sha>`; fix any finding.

### Task 2: full suite
- [ ] `.venv/bin/python -m pytest -q` → all pass.
- [ ] Short summary (commits + manual observation). If stuck, describe and stop.
