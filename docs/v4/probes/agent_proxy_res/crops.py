"""프록시 파일 자체의 글리프 보존도를 눈으로 확인.

각 크기의 코드를 세 프록시의 '원해상도 픽셀' 그대로 잘라 같은 배율로 확대한다.
→ 480p 파일에서 이미 글자가 뭉개졌다면 병목은 프록시(720p 가 도움)이고,
   세 파일 다 멀쩡하다면 병목은 Gemini 내부 리사이즈다(720p 무용).
"""
import json
import os
import subprocess
import sys

from PIL import Image

SP = os.path.dirname(os.path.abspath(__file__))
TAG = os.environ.get("TAG", "b1")
WORK = os.path.join(SP, f"work_{TAG}" if TAG != "b1" else "work")
FFMPEG = "/opt/homebrew/bin/ffmpeg"
ZOOM = int(os.environ.get("ZOOM", "6"))
SCENE = int(os.environ.get("SCENE", "0"))


def main():
    T = json.load(open(os.path.join(WORK, "truth.json")))
    heights = T["heights"]
    cells = {t["height"]: t for t in T["truth"] if t["scene"] == SCENE}
    ts = SCENE * T["scene_sec"] + 0.5

    frames = {}
    for p, ph in (("480p", 480), ("720p", 720), ("1080p", 1080)):
        fp = os.path.join(WORK, f"fr_{p}_{SCENE}.png")
        subprocess.run([FFMPEG, "-y", "-v", "error", "-ss", str(ts),
                        "-i", os.path.join(WORK, f"proxy_{p}.mp4"),
                        "-frames:v", "1", fp], check=True)
        frames[p] = (Image.open(fp), ph)

    # 각 크기 = 한 행, 열 = 480p/720p/1080p. 1080p 픽셀 기준으로 같은 물리 영역.
    CW = 520
    rows = []
    for hh in heights:
        c = cells[hh]
        cy, cw = c["y_center"], max(140, int(hh * 11))
        strip = []
        for p in ("480p", "720p", "1080p"):
            im, ph = frames[p]
            sc = ph / 1080.0
            y, half = cy * sc, (hh * 2.4 * sc) / 2 + 4
            x0 = im.width / 2 - cw * sc / 2
            crop = im.crop((int(x0), int(y - half),
                            int(x0 + cw * sc), int(y + half)))
            # 같은 물리 크기로 보이도록 1080p 기준 배율로 확대
            tgt_w = int(cw * ZOOM * 0.55)
            crop = crop.resize((tgt_w, int(crop.height / crop.width * tgt_w)),
                               Image.NEAREST)
            strip.append(crop)
        rows.append((hh, c["code"], strip))

    pad, lab = 8, 34
    w = 3 * rows[0][2][0].width + 4 * pad
    h = sum(r[2][0].height for r in rows) + (len(rows) + 1) * pad + lab
    out = Image.new("RGB", (w, h), (25, 25, 25))
    from PIL import ImageDraw, ImageFont
    d = ImageDraw.Draw(out)
    f = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 20)
    for i, t in enumerate(("480p", "720p", "1080p")):
        d.text((pad + i * (rows[0][2][0].width + pad) + 10, 8), t,
               font=f, fill=(120, 220, 120))
    y = lab
    for hh, code, strip in rows:
        x = pad
        for c in strip:
            out.paste(c, (x, y))
            x += c.width + pad
        d.text((6, y + 4), f"{hh}px", font=f, fill=(230, 200, 90))
        d.text((6, y + 26), code, font=f, fill=(150, 150, 150))
        y += strip[0].height + pad
    p = os.path.join(WORK, f"crops_s{SCENE}.png")
    out.save(p)
    print(p, out.size)


if __name__ == "__main__":
    main()
