#!/usr/bin/env python3
"""Generic slot-source builder.

usage: make_source.py <tag> <n_slots> <slot_ms> <file_fps> <seed>
Builds <tag>/frames + <tag>_truth.json + <tag>.mp4 (854x480, no audio track).
Each slot shows one distinct random 3-digit number for slot_ms.
"""
import json
import os
import random
import shutil
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

BASE = os.path.dirname(os.path.abspath(__file__))
FF = "/opt/homebrew/bin/ffmpeg"
W, H = 854, 480
FONT_PATH = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"

tag, n, slot_ms, file_fps, seed = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
DIGITS = int(sys.argv[6]) if len(sys.argv) > 6 else 3
fdir = os.path.join(BASE, tag + "_frames")
shutil.rmtree(fdir, ignore_errors=True)
os.makedirs(fdir)

rng = random.Random(seed)
lo, hi = 10**(DIGITS-1), 10**DIGITS
pool = list(range(lo, hi))
rng.shuffle(pool)
assert n <= len(pool), "not enough distinct 3-digit numbers"
numbers = pool[:n]
FMT = "{:0%dd}" % DIGITS

font = ImageFont.truetype(FONT_PATH, 360)
for i, num in enumerate(numbers):
    img = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(img)
    t = FMT.format(num)
    b = d.textbbox((0, 0), t, font=font)
    d.text(((W - (b[2] - b[0])) // 2 - b[0], (H - (b[3] - b[1])) // 2 - b[1]), t, font=font, fill=(255, 255, 255))
    img.save(os.path.join(fdir, f"f{i:04d}.png"))

# input framerate = one png per slot; output framerate = file_fps
in_fps = 1000.0 / slot_ms
mp4 = os.path.join(BASE, f"{tag}.mp4")
subprocess.run([FF, "-y", "-loglevel", "error", "-framerate", f"{in_fps:g}", "-i",
                os.path.join(fdir, "f%04d.png"), "-r", str(file_fps),
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", "-preset", "medium", mp4], check=True)

dur = n * slot_ms / 1000.0
json.dump({"tag": tag, "count": n, "slot_ms": slot_ms, "file_fps": file_fps,
           "duration_sec": dur, "seed": seed,
           "digits": DIGITS,
           "numbers": [FMT.format(x) for x in numbers]},
          open(os.path.join(BASE, f"{tag}_truth.json"), "w"), indent=1)

probe = subprocess.run(["/opt/homebrew/bin/ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height,r_frame_rate,nb_frames",
                        "-show_entries", "format=duration,nb_streams", "-of", "default=nw=1", mp4],
                       capture_output=True, text=True)
print(f"{tag}: slots={n} slot_ms={slot_ms} expected_dur={dur}s")
print(probe.stdout.strip())
