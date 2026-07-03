# Contributing

Thanks for your interest in herdeck! Please also read our
[Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

Use Python 3.12 or newer, then create a virtual environment and install the
development extras:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Tests and linting

Before pushing, run the linter and the test suite:

```bash
ruff check src tests          # lint (run against BOTH src and tests)
pytest                        # Python test suite
```

The `desktop/` (Tauri + Svelte) and `streamdeck/` (Elgato plugin shell)
sub-projects have their own JavaScript/TypeScript suites:

```bash
cd desktop    && npm install && npm test    # desktop frontend (vitest)
cd streamdeck && npm install && npm test    # plugin shell (vitest)
```

CI runs `ruff check src tests` + `pytest` on Python 3.12 and 3.13.

## Bilingual UI (English + Czech) — required

herdeck's UI is fully bilingual: **English is the default**, Czech is enabled
with `[view].language = "cs"`. Every new user-visible string must be added in
**both** languages — never hard-coded:

- Desktop UI strings: `defineMessages({ en: {...}, cs: {...} })` from
  `desktop/src/lib/i18n.svelte.ts` (types enforce matching en/cs keys).
- Editor field help tooltips: `desktop/src/lib/help.ts` (`FIELD_HELP.en` +
  `FIELD_HELP.cs`). Every settings field needs a `help` tooltip in both
  languages.
- Rendered deck text (tiles/panel/web simulator): `src/herdeck/i18n.py`
  `STRINGS` (`tr(lang, key)`), driven by `config.view.language`.
- Tray menu (Rust): `tray_labels()` in `desktop/src-tauri/src/lib.rs`.

These rules are enforced by tests
(`desktop/src/lib/sections/sections.help.test.ts` and `tests/test_i18n.py`), so
a PR that adds a string in only one language will fail CI. Field labels in the
editor stay in English (they are the exact TOML config keys); the tooltip
explains them. CLI, logs, README, and code comments stay in English.

## Commits and pull requests

- Use conventional commits, for example `fix: handle disconnected bridge`.
- Open pull requests against `main`.
- Keep changes focused and include tests when behavior changes.
