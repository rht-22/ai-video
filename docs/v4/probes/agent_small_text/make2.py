#!/usr/bin/env python3
"""라운드 2/3 소재 생성.

mode=direct : 854x480 에 직접 렌더 (지정 글자높이 그대로)
mode=downscale : 1920x1080 에 렌더 후 ffmpeg 으로 854x480 축소
                 (= 실전 '1080p 방송 텔롭 -> 480p 프록시' 경로 재현)
"""
import json
import os
import random
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

FONT_PATH = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
LETTERS = "ABCDEFGHJKLMNPRSTUVWXYZ"
DIGITS = "0123456789"
HERE = os.path.dirname(os.path.abspath(__file__))
FFMPEG = "/opt/homebrew/bin/ffmpeg"


def font_for_cap_height(target_px):
    best = None
    for size in range(3, 260):
        f = ImageFont.truetype(FONT_PATH, size)
        b = f.getbbox("AB1234")
        h = b[3] - b[1]
        if best is None or abs(h - target_px) < abs(best[2] - target_px):
            best = (size, f, h)
        if h > target_px + 8:
            break
    return best


def build(tag, heights, canvas, seed, n_frames=8, stroke_div=22):
    W, H = canvas
    rng = random.Random(seed)
    fonts = {t: font_for_cap_height(t) for t in heights}
    print(f"[{tag}] canvas {W}x{H}  target->size/actual: "
          + ", ".join(f"{t}->{fonts[t][0]}/{fonts[t][2]}" for t in heights))
    outdir = os.path.join(HERE, tag)
    os.makedirs(outdir, exist_ok=True)
    truth = []
    for i in range(n_frames):
        bg = Image.new("L", (W // 4, H // 4))
        bg.putdata([128 + rng.randint(-18, 18) for _ in range(bg.width * bg.height)])
        img = bg.resize((W, H), Image.BILINEAR).convert("RGB")
        d = ImageDraw.Draw(img)
        lines = []
        used = set()
        slot = H / len(heights)
        for j, t in enumerate(heights):
            while True:
                c = (rng.choice(LETTERS) + rng.choice(LETTERS) + "-"
                     + "".join(rng.choice(DIGITS) for _ in range(4)))
                if c not in used:
                    used.add(c)
                    break
            size, f, actual = fonts[t]
            b = f.getbbox(c)
            tw, th = b[2] - b[0], b[3] - b[1]
            x = (W - tw) / 2 - b[0]
            y = slot * (j + 0.5) - th / 2 - b[1]
            sw = max(1, round(t / stroke_div))
            d.text((x, y), c, font=f, fill=(255, 255, 255),
                   stroke_width=sw, stroke_fill=(0, 0, 0))
            lines.append({"height_px": t, "actual_ink_px": actual,
                          "code": c, "row": j})
        img.save(os.path.join(outdir, f"frame_{i:02d}.png"))
        truth.append({"frame": i, "lines": lines})
    json.dump(truth, open(os.path.join(outdir, "truth.json"), "w"),
              ensure_ascii=False, indent=1)
    return outdir


def encode(outdir, scale=None, name="clip.mp4"):
    vf = "fps=10"
    if scale:
        vf = f"scale={scale[0]}:{scale[1]}:flags=bicubic,fps=10"
    out = os.path.join(outdir, name)
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-framerate", "0.5",
                    "-i", os.path.join(outdir, "frame_%02d.png"),
                    "-vf", vf, "-c:v", "libx264", "-crf", "16",
                    "-preset", "slow", "-pix_fmt", "yuv420p", out],
                   check=True)
    print(f"  -> {out}")
    return out


if __name__ == "__main__":
    which = sys.argv[1]
    if which == "r2":
        # 바닥 찾기: 4~9px, 854x480 직접 렌더
        d = build("r2", [4, 5, 6, 7, 8, 9], (854, 480), seed=777)
        encode(d)
    elif which == "r3":
        # 실전 경로: 1080p 텔롭 크기 -> 480p 축소
        # 1080p 글자높이 24/32/40/48/64/80 -> 480p 환산 10.7/14.2/17.8/21.3/28.4/35.6
        d = build("r3", [24, 32, 40, 48, 64, 80], (1920, 1080), seed=1234)
        encode(d, scale=(854, 480))
    elif which == "r4":
        # 실전 경로 + 더 작게: 1080p 12/16/20/24/28/36 -> 480p 5.3/7.1/8.9/10.7/12.4/16.0
        d = build("r4", [12, 16, 20, 24, 28, 36], (1920, 1080), seed=555)
        encode(d, scale=(854, 480))
