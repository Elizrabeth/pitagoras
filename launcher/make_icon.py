"""Draws launcher/pitagoras.ico: the yellow ball (colors from appearance.json if it's there)."""
import json
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent


def color(v, default):
    if isinstance(v, list) and len(v) >= 3:
        return tuple(int(x) for x in v[:3])
    if isinstance(v, str) and v.startswith("#") and len(v) == 7:
        return tuple(int(v[i:i + 2], 16) for i in (1, 3, 5))
    return default


try:
    look = json.loads((ROOT / "appearance.json").read_text(encoding="utf-8"))
except (OSError, ValueError):
    look = {}
body = color(look.get("body"), (255, 210, 63))
outline = color(look.get("outline"), (120, 85, 0))
face = color(look.get("mouth"), (21, 21, 21))

S = 1024  # drawn big, scaled down sharp
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
m = 40
d.ellipse([m, m, S - m, S - m], fill=body, outline=outline, width=44)
shine = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(shine).ellipse([m + 170, m + 120, m + 400, m + 270], fill=(255, 255, 255, 110))
img.alpha_composite(shine)
d = ImageDraw.Draw(img)
for cx in (S * 0.37, S * 0.63):  # eyes
    d.ellipse([cx - 52, S * 0.40 - 80, cx + 52, S * 0.40 + 80], fill=face)
d.arc([S * 0.33, S * 0.42, S * 0.67, S * 0.72], 20, 160, fill=face, width=40)  # smile
out = Path(__file__).with_name("pitagoras.ico")
img.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("wrote", out)
