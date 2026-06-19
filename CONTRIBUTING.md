# Contributing

## Development setup
Use Python 3.12 or newer, then create a virtual environment and install the
development extras:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Tests
Run the test suite with:

```bash
pytest
```

## Commits and pull requests
- Use conventional commits, for example `fix: handle disconnected bridge`.
- Open pull requests against `main`.
- Keep changes focused and include tests when behavior changes.
