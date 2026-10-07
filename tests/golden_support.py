"""Shared pixel-golden harness for tests/test_render_golden.py and
tests/test_theme_goldens.py.

A golden is a SHA-256 of the raw RGB pixels (not of the PNG bytes, which zlib
may encode differently). Regenerate with HERDECK_UPDATE_GOLDENS=1.
"""

from __future__ import annotations

import hashlib
import json
import os

import pytest
from PIL import Image

UPDATE_ENV = "HERDECK_UPDATE_GOLDENS"


def digest(img: Image.Image) -> str:
    rgb = img.convert("RGB")
    h = hashlib.sha256(f"{rgb.width}x{rgb.height}\0".encode())
    h.update(rgb.tobytes())
    return h.hexdigest()


def load_goldens(golden_file: str) -> dict[str, str]:
    try:
        with open(golden_file, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}


def check_goldens(
    rendered: dict[str, Image.Image],
    golden_file: str,
    tmp_path,
    *,
    title: str,
    regenerate: str,
    note: str = "",
    after: str = "",
    show_new_digests: bool = False,
) -> None:
    """Compare rendered images with the golden file (or rewrite it when
    UPDATE_ENV is set and skip). `regenerate` is the shell command shown on
    failure; `note` precedes it and `after` follows it in the failure message."""
    actual = {name: digest(img) for name, img in sorted(rendered.items())}
    if os.environ.get(UPDATE_ENV):
        os.makedirs(os.path.dirname(golden_file), exist_ok=True)
        with open(golden_file, "w", encoding="utf-8") as fh:
            json.dump(actual, fh, indent=2, sort_keys=True)
            fh.write("\n")
        pytest.skip(f"{UPDATE_ENV} set: rewrote {golden_file}")
    goldens = load_goldens(golden_file)
    mismatched = sorted(n for n in actual if goldens.get(n) != actual[n])
    stale = sorted(set(goldens) - set(actual))
    if not mismatched and not stale:
        return
    out_dir = os.environ.get("HERDECK_GOLDEN_OUT") or str(tmp_path / "golden-actual")
    os.makedirs(out_dir, exist_ok=True)
    lines = []
    for name in mismatched:
        path = os.path.join(out_dir, f"{name}.png")
        rendered[name].convert("RGB").save(path)
        lines.append(
            f"  {name}: expected {goldens.get(name, '<missing>')[:16]}"
            f" got {actual[name][:16]}  -> {path}"
        )
    lines += [f"  {name}: golden has no matching render (remove it)" for name in stale]
    msg = f"{title}:\n" + "\n".join(lines) + f"\n\n{note}\n  {regenerate}\n{after}\n"
    if show_new_digests:
        msg += "New digests:\n" + json.dumps({n: actual[n] for n in mismatched}, indent=2)
    pytest.fail(msg, pytrace=False)
