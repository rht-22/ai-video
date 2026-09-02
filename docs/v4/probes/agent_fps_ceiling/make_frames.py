#!/usr/bin/env python3
"""200 PNG frames, one per 100ms slot, each showing a distinct random 3-digit number."""
import json
import os
import random

from PIL import Image, ImageDraw, ImageFont

BASE = os.path.dirname(os.path.abspath(__file__))
FRAMES = os.path.join(BASE, "frames")
W, H = 854, 480
N = 200
SEED = 20260901

FONT_PATH = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"

rng = random.Random(SEED)
pool = list(range(100, 1000))
rng.shuffle(pool)
numbers = pool[:N]  # 200 distinct 3-digit numbers, no ordering pattern

font = ImageFont.truetype(FONT_PATH, 360)

for i, num in enumerate(numbers):
    img = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(img)
    text = f"{num:03d}"
    bbox = d.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (W - tw) // 2 - bbox[0]
    y = (H - th) // 2 - bbox[1]
    d.text((x, y), text, font=font, fill=(255, 255, 255))
    img.save(os.path.join(FRAMES, f"f{i:03d}.png"))

with open(os.path.join(BASE, "truth.json"), "w") as fh:
    json.dump(
        {
            "seed": SEED,
            "slot_ms": 100,
            "count": N,
            "numbers": [f"{n:03d}" for n in numbers],
            "slots": [
                {"index": i, "start_sec": round(i * 0.1, 3), "end_sec": round((i + 1) * 0.1, 3), "text": f"{n:03d}"}
                for i, n in enumerate(numbers)
            ],
        },
        fh,
        indent=1,
    )

print("frames:", N, "first10:", [f"{n:03d}" for n in numbers[:10]])
# glyph height sanity: fraction of canvas height
print("text height px:", th, "=", round(th / H * 100, 1), "% of canvas height")
