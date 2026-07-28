# Herdeck desktop settings prototype

Throwaway UI prototype answering one question: which information architecture
should the next Herdeck desktop settings use?

It contains three structurally different variants on one route:

- `?variant=A` — control room with an interactive live deck and layout workbench
- `?variant=B` — deck-first canvas
- `?variant=C` — macOS inspector

Run from the repository root:

```bash
python3 -m http.server 4174 --bind 0.0.0.0 --directory desktop/prototype-settings
```

No control writes to a real Herdeck configuration. Delete the prototype or fold
the selected direction into production components after a verdict is recorded.
