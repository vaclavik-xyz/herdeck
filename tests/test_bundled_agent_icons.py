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
