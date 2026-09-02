#!/usr/bin/env python3
"""Confirm the encoded mp4 shows slot i's number during [i*0.1,(i+1)*0.1)."""
import json
import os
import subprocess
import sys

from PIL import Image

BASE = os.path.dirname(os.path.abspath(__file__))
FF = "/opt/homebrew/bin/ffmpeg"
truth = json.load(open(os.path.join(BASE, "truth.json")))

# reference thumbnails of every source png
refs = []
for i in range(truth["count"]):
    im = Image.open(os.path.join(BASE, "frames", f"f{i:03d}.png")).convert("L").resize((64, 36))
    refs.append(list(im.getdata()))


def nearest(path):
    im = Image.open(path).convert("L").resize((64, 36))
    d = list(im.getdata())
    best, bi = None, -1
    for i, r in enumerate(refs):
        s = sum(abs(a - b) for a, b in zip(d, r))
        if best is None or s < best:
            best, bi = s, i
    return bi, best


bad = 0
checks = [0.05, 0.15, 0.55, 1.05, 4.95, 9.95, 10.05, 15.05, 19.05, 19.95]
for t in checks:
    out = os.path.join(BASE, "_probe.png")
    subprocess.run([FF, "-y", "-loglevel", "error", "-ss", str(t), "-i",
                    os.path.join(BASE, "src20fps.mp4"), "-frames:v", "1", out], check=True)
    got, dist = nearest(out)
    want = int(t / 0.1)
    ok = got == want
    bad += 0 if ok else 1
    print(f"t={t:6.2f}  expect slot {want:3d} ({truth['numbers'][want]})  got slot {got:3d} ({truth['numbers'][got]})  dist={dist}  {'OK' if ok else 'MISMATCH'}")
os.path.exists(os.path.join(BASE, "_probe.png")) and os.remove(os.path.join(BASE, "_probe.png"))
sys.exit(1 if bad else 0)
