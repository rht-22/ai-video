#!/usr/bin/env python3
"""작은 글자 OCR 실험용 프레임 생성.

854x480 (레포 프록시 규격), 회색 배경 + 흰 글자 + 얇은 검은 외곽선.
한 화면에 글자 높이(대문자/숫자 cap height) 10/14/18/24/32/44px 6줄.
"""
import json
import os
import random
import sys

from PIL import Image, ImageDraw, ImageFont

W, H = 854, 480
HEIGHTS = [10, 14, 18, 24, 32, 44]
FONT_PATH = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
# 0/O, 1/I, Q/O 혼동은 '해상도 때문에 못 읽음'과 구분이 안 되므로 문자에서 제외.
LETTERS = "ABCDEFGHJKLMNPRSTUVWXYZ"
DIGITS = "0123456789"
OUT = os.path.dirname(os.path.abspath(__file__))


def font_for_cap_height(target_px):
    """digits/uppercase 실제 잉크 높이가 target_px 가 되는 폰트 크기를 찾는다."""
    best = None
    for size in range(4, 200):
        f = ImageFont.truetype(FONT_PATH, size)
        bbox = f.getbbox("AB1234")  # 잉크 바운딩 박스
        h = bbox[3] - bbox[1]
        if best is None or abs(h - target_px) < abs(best[2] - target_px):
            best = (size, f, h)
        if h > target_px + 6:
            break
    return best  # (size, font, actual_ink_height)


def make_code(rng):
    return (
        rng.choice(LETTERS)
        + rng.choice(LETTERS)
        + "-"
        + "".join(rng.choice(DIGITS) for _ in range(4))
    )


def main():
    rng = random.Random(20260901)
    fonts = {t: font_for_cap_height(t) for t in HEIGHTS}
    print("[font] target -> size / actual ink height")
    for t in HEIGHTS:
        s, _, a = fonts[t]
        print(f"  {t:>2}px -> size {s:>3} / actual {a}px")

    truth = []
    for i in range(8):
        # 배경: 중간 회색 + 약한 노이즈 (실제 영상 배경 흉내)
        bg = Image.new("L", (W // 4, H // 4))
        bg.putdata([128 + rng.randint(-18, 18) for _ in range(bg.width * bg.height)])
        img = bg.resize((W, H), Image.BILINEAR).convert("RGB")
        d = ImageDraw.Draw(img)

        # 6줄 세로 균등 배치
        codes = []
        used = set()
        slot_h = H / 6
        for j, t in enumerate(HEIGHTS):
            while True:
                c = make_code(rng)
                if c not in used:
                    used.add(c)
                    break
            size, f, actual = fonts[t]
            cy = slot_h * (j + 0.5)
            bbox = f.getbbox(c)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            x = (W - tw) / 2 - bbox[0]
            y = cy - th / 2 - bbox[1]
            # 얇은 검은 외곽선 + 흰 글자
            sw = max(1, round(t / 22))
            d.text((x, y), c, font=f, fill=(255, 255, 255),
                   stroke_width=sw, stroke_fill=(0, 0, 0))
            codes.append({"height_px": t, "actual_ink_px": actual,
                          "font_size": size, "code": c, "row": j})
        img.save(os.path.join(OUT, f"frame_{i:02d}.png"))
        truth.append({"frame": i, "lines": codes})

    with open(os.path.join(OUT, "truth.json"), "w") as fh:
        json.dump(truth, fh, ensure_ascii=False, indent=1)
    print(f"[ok] 8 frames + truth.json -> {OUT}")


if __name__ == "__main__":
    main()
