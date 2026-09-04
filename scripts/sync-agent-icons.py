#!/usr/bin/env python3
"""Refresh the committed agent marks from pinned, checksummed sources."""

from __future__ import annotations

import hashlib
import io
import re
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src" / "herdeck" / "assets"

LOBE_VERSION = "1.94.0"
LOBE_URL = (
    "https://registry.npmjs.org/@lobehub/icons-static-svg/-/"
    f"icons-static-svg-{LOBE_VERSION}.tgz"
)
LOBE_SHA256 = "a813cbb544624f51344ceab00b21c3fb0e760a989453ca447c502098698b1ec2"
LOBE_ICONS = {
    "agy": "antigravity",
    "amp": "amp",
    "cline": "cline",
    "devin": "devin",
    "grok": "grok",
    "hermes": "hermesagent",
    "kilo": "kilocode",
    "kimi": "kimi",
    "kiro": "kiro",
    "mastracode": "mastra",
    "pi": "pi",
    "qodercli": "qoder",
    "qwen": "qwen",
}

FACTORY_DROID_URL = "https://factory.ai/favicon.svg"
FACTORY_DROID_SHA256 = "416ea4962d7b0b8be8bec7f7190c13c22d5f20fdcac401aa72886fd5c81d2fb2"
OMP_URL = (
    "https://raw.githubusercontent.com/unsigned-gg/omp/"
    "0c6c981ee5838d97700180383077eb0a3790637c/packages/collab-web/public/favicon.svg"
)
OMP_SHA256 = "9419975a0c24961341221c4cec18703db26a989fa037768f92cda74e3769fe05"
MAKI_URL = (
    "https://raw.githubusercontent.com/tontinton/maki/"
    "33b31d9ba2decaca31e387e9bda77c7b9b47b387/site/apple-touch-icon.png"
)
MAKI_SHA256 = "cfc50d26903d72d9dd9b20b61393555f772d7779364e7522e877d1708379f1a2"


def _download(url: str, expected_sha256: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "herdeck-icon-sync"})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_sha256:
        raise RuntimeError(f"checksum mismatch for {url}: {actual}")
    return data


def _monochrome(svg: str) -> str:
    return svg.replace("currentColor", "#ffffff").replace("#FAFAFA", "#ffffff")


def _single_path_mark(svg: str, view_box: str) -> str:
    paths = re.findall(r"<path\b[^>]*(?:/>|>.*?</path>)", svg, flags=re.DOTALL)
    if len(paths) != 1:
        raise RuntimeError(f"expected one path, found {len(paths)}")
    path = re.sub(r'\s+fill="[^"]*"', "", paths[0])
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view_box}" '
        f'fill="#ffffff">{path}</svg>\n'
    )


def main() -> None:
    archive_data = io.BytesIO(_download(LOBE_URL, LOBE_SHA256))
    with tarfile.open(fileobj=archive_data, mode="r:gz") as archive:
        for agent_type, source_name in LOBE_ICONS.items():
            member = archive.extractfile(f"package/icons/{source_name}.svg")
            if member is None:
                raise RuntimeError(f"missing Lobe icon: {source_name}")
            (ASSETS / f"{agent_type}.svg").write_text(
                _monochrome(member.read().decode("utf-8")) + "\n", encoding="utf-8"
            )

    droid = _download(FACTORY_DROID_URL, FACTORY_DROID_SHA256).decode("utf-8")
    (ASSETS / "droid.svg").write_text(_single_path_mark(droid, "0 0 508 508"), encoding="utf-8")

    omp = _download(OMP_URL, OMP_SHA256).decode("utf-8")
    (ASSETS / "omp.svg").write_text(_single_path_mark(omp, "0 0 64 64"), encoding="utf-8")

    (ASSETS / "maki.png").write_bytes(_download(MAKI_URL, MAKI_SHA256))


if __name__ == "__main__":
    main()
