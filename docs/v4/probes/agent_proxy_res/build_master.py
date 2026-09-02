"""프록시 해상도 실험 — 1080p 마스터 + 프록시 3종 생성.

마스터: 1920x1080 / 10fps / 16초 = 8장면 x 2초.
각 장면에 글자높이 20·28·38·52·72·100px 의 "AB-1234" 코드 6개.
배경 중간회색(#808080) + 흰 글자 + 얇은 검은 외곽선(실제 텔롭 조건).

⚠ 크기와 화면 위치의 교락을 피하려고 장면마다 6개 크기의 세로 순서를
   무작위로 섞는다(시드 고정). 채점은 코드 문자열 신원으로 하므로
   순서가 섞여도 크기별 회수율을 정확히 낼 수 있다.

프록시 인코딩은 운영 scan 프록시 인자 그대로:
  libx264 -preset ultrafast -crf 30, fps=10, scale=-2:H
해상도만 854x480 / 1280x720 / 1920x1080 로 바꾼다.
"""
import json
import os
import random
import subprocess
import time

from PIL import Image, ImageDraw, ImageFont

SP = os.path.dirname(os.path.abspath(__file__))
FFMPEG = "/opt/homebrew/bin/ffmpeg"
FONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"

W, H = 1920, 1080
N_SCENES = 8
SCENE_SEC = 2
FILE_FPS = 10
TAG = os.environ.get("TAG", "b1")
# 1080p 기준 글자(대문자/숫자) 높이 px
HEIGHTS = [int(x) for x in
           os.environ.get("HEIGHTS", "20,28,38,52,72,100").split(",")]
SEED = int(os.environ.get("SEED", "20260901"))

# 운영 scan 프록시 인자(app/v3/seq_analyze.build_scan_proxy)
PROXY_CRF = 30
PROXY_PRESET = "ultrafast"
PROXIES = [("480p", 480), ("720p", 720), ("1080p", 1080)]


def font_for_height(target_h: int) -> ImageFont.FreeTypeFont:
    """대문자+숫자 실제 글리프 높이가 target_h 가 되는 폰트 크기를 이분탐색."""
    lo, hi = 4, 400
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        f = ImageFont.truetype(FONT, mid)
        x0, y0, x1, y1 = f.getbbox("AB-1234")
        h = y1 - y0
        if h == target_h:
            return f
        if h < target_h:
            best = f
            lo = mid + 1
        else:
            hi = mid - 1
    return best or ImageFont.truetype(FONT, max(4, target_h))


def gen_codes(rng, n):
    """전역 유일한 'AB-1234' 코드 n개."""
    seen, out = set(), []
    while len(out) < n:
        c = ("%s%s-%04d" % (rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ"),
                            rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ"),
                            rng.randrange(10000)))
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def main():
    rng = random.Random(SEED)
    outdir = os.path.join(SP, f"work_{TAG}" if TAG != "b1" else "work")
    os.makedirs(outdir, exist_ok=True)

    codes = gen_codes(rng, N_SCENES * len(HEIGHTS))
    fonts = {h: font_for_height(h) for h in HEIGHTS}
    for h in HEIGHTS:
        bb = fonts[h].getbbox("AB-1234")
        print(f"  높이 {h:3d}px → 폰트 size={fonts[h].size:3d} 실측 {bb[3]-bb[1]}px")

    band = H // len(HEIGHTS)
    truth = []          # [{scene, height, code, band, y_center}]
    ci = 0
    for s in range(N_SCENES):
        img = Image.new("RGB", (W, H), (0x80, 0x80, 0x80))
        d = ImageDraw.Draw(img)
        order = HEIGHTS[:]
        rng.shuffle(order)                     # 크기 x 위치 교락 제거
        for slot, hh in enumerate(order):
            code = codes[ci]
            ci += 1
            cy = band * slot + band // 2
            sw = max(1, round(hh / 28))
            d.text((W // 2, cy), code, font=fonts[hh], fill=(255, 255, 255),
                   anchor="mm", stroke_width=sw, stroke_fill=(0, 0, 0))
            truth.append({"scene": s, "height": hh, "code": code,
                          "band": slot, "y_center": cy})
        img.save(os.path.join(outdir, f"scene_{s:02d}.png"))

    with open(os.path.join(outdir, "truth.json"), "w") as f:
        json.dump({"seed": SEED, "heights": HEIGHTS, "n_scenes": N_SCENES,
                   "scene_sec": SCENE_SEC, "truth": truth}, f,
                  ensure_ascii=False, indent=1)
    print(f"\n정답지 {len(truth)}개 ({N_SCENES}장면 x {len(HEIGHTS)}크기)")

    # ── 마스터: 고품질(프록시가 유일한 열화 지점이 되도록) ──────────────
    master = os.path.join(outdir, "master_1080.mp4")
    t0 = time.time()
    subprocess.run(
        [FFMPEG, "-y", "-framerate", f"{1/SCENE_SEC}",
         "-i", os.path.join(outdir, "scene_%02d.png"),
         "-f", "lavfi", "-t", str(N_SCENES * SCENE_SEC),
         "-i", "anullsrc=channel_layout=mono:sample_rate=22050",
         "-vf", f"fps={FILE_FPS}", "-fps_mode", "cfr",
         "-c:v", "libx264", "-preset", "medium", "-crf", "14",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "1", "-ar", "22050",
         "-shortest", master], check=True, capture_output=True)
    print(f"마스터: {os.path.getsize(master)/1e6:.2f} MB "
          f"({time.time()-t0:.1f}s)")

    # ── 프록시 3종: 운영 인자 동일, 해상도만 변경 ─────────────────────
    meta = []
    for name, ph in PROXIES:
        out = os.path.join(outdir, f"proxy_{name}.mp4")
        t0 = time.time()
        subprocess.run(
            [FFMPEG, "-y", "-i", master,
             "-vf", f"scale=-2:{ph},fps={FILE_FPS}", "-fps_mode", "cfr",
             "-c:v", "libx264", "-preset", PROXY_PRESET, "-crf", str(PROXY_CRF),
             "-c:a", "aac", "-ac", "1", "-ar", "22050",
             "-threads", "4", out], check=True, capture_output=True)
        el = time.time() - t0
        sz = os.path.getsize(out)
        probe = subprocess.run(
            ["/opt/homebrew/bin/ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,nb_frames,duration",
             "-of", "json", out], capture_output=True, text=True)
        st = json.loads(probe.stdout)["streams"][0]
        meta.append({"name": name, "path": out, "bytes": sz,
                     "mb": round(sz / 1e6, 3), "encode_sec": round(el, 2),
                     "width": st["width"], "height": st["height"],
                     "nb_frames": st.get("nb_frames"),
                     "duration": st.get("duration")})
        print(f"프록시 {name:6s} {st['width']}x{st['height']}  "
              f"{sz/1e6:6.3f} MB  인코딩 {el:5.2f}s")

    with open(os.path.join(outdir, "proxies.json"), "w") as f:
        json.dump(meta, f, indent=1)


if __name__ == "__main__":
    main()
