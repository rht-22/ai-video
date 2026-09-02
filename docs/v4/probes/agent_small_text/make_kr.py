#!/usr/bin/env python3
"""한글 크기 스윕 — 1920x1080 렌더 후 854x480 축소(실전 프록시 경로)."""
import json
import os
import random
import subprocess

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
FFMPEG = "/opt/homebrew/bin/ffmpeg"
FONT = "/System/Library/Fonts/AppleSDGothicNeo.ttc"
# 1080p 글자높이 -> 480p 환산(x0.4448)
HEIGHTS = [12, 16, 20, 24, 28, 36]

# 무작위 조합 — 문맥으로 추측할 수 없게 (실제 낱말이지만 짝은 무의미)
A = ["푸른", "낡은", "붉은", "깊은", "젖은", "높은", "둥근", "얇은", "굳은", "밝은",
     "차가운", "느린", "무거운", "조용한", "따뜻한", "거친"]
B = ["바람", "골목", "손끝", "그림자", "종소리", "물결", "새벽", "기둥", "발자국",
     "언덕", "구름", "천장", "빗줄기", "모래", "울타리", "창문"]
C = ["열둘", "스물", "서른", "마흔", "쉰넷", "예순", "일곱", "여덟", "아홉", "백삼"]


def font_for(target):
    best = None
    for s in range(4, 300):
        f = ImageFont.truetype(FONT, s)
        b = f.getbbox("가힣문")
        h = b[3] - b[1]
        if best is None or abs(h - target) < abs(best[2] - target):
            best = (s, f, h)
        if h > target + 8:
            break
    return best


def main():
    W, H = 1920, 1080
    rng = random.Random(4242)
    fonts = {t: font_for(t) for t in HEIGHTS}
    print("[kr] 1080p target->size/actual: "
          + ", ".join(f"{t}->{fonts[t][0]}/{fonts[t][2]}" for t in HEIGHTS))
    d0 = os.path.join(HERE, "r5")
    os.makedirs(d0, exist_ok=True)
    truth = []
    for i in range(8):
        bg = Image.new("L", (W // 8, H // 8))
        bg.putdata([128 + rng.randint(-18, 18) for _ in range(bg.width * bg.height)])
        img = bg.resize((W, H), Image.BILINEAR).convert("RGB")
        dr = ImageDraw.Draw(img)
        lines = []
        used = set()
        slot = H / len(HEIGHTS)
        for j, t in enumerate(HEIGHTS):
            while True:
                s = f"{rng.choice(A)} {rng.choice(B)} {rng.choice(C)}"
                if s not in used:
                    used.add(s)
                    break
            size, f, actual = fonts[t]
            b = f.getbbox(s)
            tw, th = b[2] - b[0], b[3] - b[1]
            x = (W - tw) / 2 - b[0]
            y = slot * (j + 0.5) - th / 2 - b[1]
            dr.text((x, y), s, font=f, fill=(255, 255, 255),
                    stroke_width=max(1, round(t / 14)), stroke_fill=(0, 0, 0))
            lines.append({"height_px": t, "eff480": round(t * 0.4448, 1),
                          "code": s, "row": j})
        img.save(os.path.join(d0, f"frame_{i:02d}.png"))
        truth.append({"frame": i, "lines": lines})
    json.dump(truth, open(os.path.join(d0, "truth.json"), "w"),
              ensure_ascii=False, indent=1)
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-framerate", "0.5",
                    "-i", os.path.join(d0, "frame_%02d.png"),
                    "-vf", "scale=854:480:flags=bicubic,fps=10",
                    "-c:v", "libx264", "-crf", "16", "-preset", "slow",
                    "-pix_fmt", "yuv420p", os.path.join(d0, "clip.mp4")], check=True)
    print(f"[ok] {d0}/clip.mp4")


if __name__ == "__main__":
    main()
