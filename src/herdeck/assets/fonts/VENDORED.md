# Vendored font: Inter 4.1

The deck renders every tile and panel with these two files, so text metrics
(line wraps, shrink-to-fit sizes, truncation points) are the same on macOS,
Linux and inside the frozen bundles. The system fonts in `herdeck.icons` are
only a fallback for a broken install.

| File | Source | SHA-256 |
| --- | --- | --- |
| `Inter-Bold.ttf` | `extras/ttf/Inter-Bold.ttf` in `Inter-4.1.zip` | `288316099b1e0a47a4716d159098005eef7c0066921f34e3200393dbdb01947f` |
| `Inter-Regular.ttf` | `extras/ttf/Inter-Regular.ttf` in `Inter-4.1.zip` | `40d692fce188e4471e2b3cba937be967878f631ad3ebbbdcd587687c7ebe0c82` |
| `LICENSE.txt` | `LICENSE.txt` in `Inter-4.1.zip` | |

Release: <https://github.com/rsms/inter/releases/tag/v4.1>
(`https://github.com/rsms/inter/releases/download/v4.1/Inter-4.1.zip`).

Licence: SIL Open Font License 1.1 (`LICENSE.txt`), Copyright (c) 2016 The
Inter Project Authors. The files are unmodified.

Replacing or upgrading the font changes rendered pixels: bump
`herdeck.icons.TILE_VERSION` and regenerate the goldens in
`tests/test_render_golden.py` (`HERDECK_UPDATE_GOLDENS=1 pytest tests/test_render_golden.py`).
