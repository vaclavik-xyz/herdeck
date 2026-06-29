"""Turn the herdeck logo master (a dark squircle on a near-black frame) into a
clean macOS icon source: transparent OUTSIDE the squircle, 1024x1024.

macOS .icns does NOT auto-round icons — the squircle silhouette must live in the
artwork with transparent corners. We take the bounding box of the non-near-black
(the squircle) and apply a rounded-rectangle alpha mask at that bbox, so the
corners become transparent regardless of the gradient.
"""
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent  # desktop/
SRC = ROOT / "branding" / "herdeck-logo.png"
OUT = ROOT / "src-tauri" / "icon-source.png"

im = Image.open(SRC).convert("RGBA")
px = im.load()
w, h = im.size

# bbox of the squircle = pixels brighter than the near-black frame (~ (7,9,14))
xs, ys = [], []
for y in range(h):
    for x in range(w):
        r, g, b, _ = px[x, y]
        if max(r, g, b) > 22:
            xs.append(x)
            ys.append(y)
x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)

mask = Image.new("L", (w, h), 0)
radius = int(0.22 * (x1 - x0))  # macOS-squircle-ish corner radius
ImageDraw.Draw(mask).rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=255)
im.putalpha(mask)

im = im.resize((1024, 1024), Image.LANCZOS)
im.save(OUT)
print(f"wrote {OUT} ({im.size[0]}x{im.size[1]} RGBA)")
