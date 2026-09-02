"""실소재 배터리 — 가왕쇼 완성본 46~58s 를 프록시 3종으로.

⚠ 이 소재는 1080x1920 세로 완성본이다(운영 프록시의 입력인 16:9 롱폼이 아니다).
   그래서 '높이 480' 이 아니라 **운영과 같은 선형 축소비**로 맞춘다:
     운영 1920x1080 → 854x480 = 선형 0.444
   세로 소재에 같은 0.444 를 걸면 480x854 다(폭 기준 scale=480:-2).
   글자가 줄어드는 비율이 운영과 같아야 비교가 성립한다.
"""
import json
import os
import subprocess
import time

SP = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(SP, "work_b3")
FFMPEG = "/opt/homebrew/bin/ffmpeg"
SRC = "/Users/gimsewon/Downloads/가왕쇼-6화로 만든 영상-v3.mp4"
START, DUR = 46, 12
FPS, CRF, PRESET = 10, 30, "ultrafast"
# (이름, 폭, 선형축소비) — 세로 소재라 폭으로 건다
VARIANTS = [("480p", 480, 0.444), ("720p", 720, 0.667), ("1080p", 1080, 1.0)]


def main():
    os.makedirs(WORK, exist_ok=True)
    meta = []
    for name, w, lin in VARIANTS:
        out = os.path.join(WORK, f"proxy_{name}.mp4")
        t0 = time.time()
        subprocess.run(
            [FFMPEG, "-y", "-v", "error", "-ss", str(START), "-t", str(DUR),
             "-i", SRC, "-vf", f"scale={w}:-2,fps={FPS}", "-fps_mode", "cfr",
             "-c:v", "libx264", "-preset", PRESET, "-crf", str(CRF),
             "-c:a", "aac", "-ac", "1", "-ar", "22050",
             "-threads", "4", out], check=True)
        el = time.time() - t0
        sz = os.path.getsize(out)
        pr = subprocess.run(
            ["/opt/homebrew/bin/ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "json", out],
            capture_output=True, text=True)
        st = json.loads(pr.stdout)["streams"][0]
        meta.append({"name": name, "path": out, "bytes": sz,
                     "mb": round(sz / 1e6, 3), "encode_sec": round(el, 2),
                     "width": st["width"], "height": st["height"],
                     "linear": lin})
        print(f"{name:6s} {st['width']}x{st['height']}  {sz/1e6:6.3f} MB  "
              f"인코딩 {el:5.2f}s  선형 {lin}")
    json.dump(meta, open(os.path.join(WORK, "proxies.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
