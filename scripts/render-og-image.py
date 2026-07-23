#!/usr/bin/env python3
"""Compose a generated Herdeck background into a precise 1200x630 OG image."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parents[1]
SIZE = (1200, 630)
FONT = Path("/System/Library/Fonts/SFNS.ttf")


def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT), size=size)


def compose(source: Path, output: Path) -> None:
    background = Image.open(source).convert("RGB")
    background = ImageOps.fit(background, SIZE, method=Image.Resampling.LANCZOS)
    background = ImageEnhance.Color(background).enhance(0.92)
    background = ImageEnhance.Contrast(background).enhance(1.06).convert("RGBA")

    gradient = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    pixels = gradient.load()
    for x in range(SIZE[0]):
        left = max(0.0, min(1.0, (760 - x) / 560))
        edge = max(0.0, min(1.0, (x - 960) / 240))
        alpha = int(235 * left + 105 * edge)
        for y in range(SIZE[1]):
            vertical = 1.0 - 0.15 * abs((y / SIZE[1]) - 0.5) * 2
            pixels[x, y] = (3, 8, 30, int(alpha * vertical))
    background.alpha_composite(gradient)

    glow = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse((15, 12, 265, 262), fill=(0, 229, 232, 55))
    background.alpha_composite(glow.filter(ImageFilter.GaussianBlur(55)))

    logo = Image.open(ROOT / "desktop/src-tauri/icon-source.png").convert("RGBA")
    logo.thumbnail((92, 92), Image.Resampling.LANCZOS)
    background.alpha_composite(logo, (72, 64))

    draw = ImageDraw.Draw(background)
    draw.text((72, 184), "HERDECK", font=font(76), fill=(248, 247, 242, 255))
    draw.rounded_rectangle((73, 282, 130, 290), radius=4, fill=(0, 229, 232, 255))
    draw.text(
        (72, 324),
        "Control AI coding agents.",
        font=font(34),
        fill=(248, 247, 242, 255),
    )
    draw.text(
        (72, 373),
        "Approve · Deny · Stop — in one press.",
        font=font(24),
        fill=(194, 205, 224, 255),
    )
    draw.text(
        (73, 532),
        "HARDWARE  ·  WEB  ·  DESKTOP",
        font=font(16),
        fill=(117, 226, 231, 255),
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    background.convert("RGB").save(output, format="PNG", optimize=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    compose(args.source, args.output)


if __name__ == "__main__":
    main()
