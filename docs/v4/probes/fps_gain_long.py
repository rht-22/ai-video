"""fps 정보 이득 — 장편 확인판.

12초 실측은 fps 요청이 그대로 반영됨을 보였다(12/24/60 회수). 다만 실제 소재는
60분급이고, Files API 가 **긴 영상**을 더 거칠게 저장할 가능성은 그 실측으로
배제되지 않는다. 이 판은 그 구멍을 막는다.

설계 — 희소 신호(sparse flash):
  · 5분(300초) 영상. 대부분 검은 화면.
  · 무작위 시각 20곳에 **400ms 동안만** 세 자리 숫자가 번쩍인다.
  · 숫자는 고정 시드 무작위 — 추측 불가.

기대(순수 표본 이론):
  fps=1 (1000ms 간격) → 400ms 창에 표본이 들 확률 0.4 → 약 8/20
  fps=2 ( 500ms 간격) → 0.8                          → 약 16/20
  fps=5 ( 200ms 간격) → 창 안에 반드시 1개 이상        → 20/20
저장이 1 FPS 로 제한된다면 fps 를 올려도 8/20 부근에 머문다.
"""
import json
import os
import random
import re
import subprocess
import time
import urllib.request
import urllib.error

SP = os.path.dirname(os.path.abspath(__file__))
VIDEO = os.path.join(SP, "fpslong.mp4")
LONG_FRAMES = os.path.join(SP, "lframes")
MODEL = "gemini-3.7-flash"
BASE = "https://generativelanguage.googleapis.com"
FFMPEG = "/opt/homebrew/bin/ffmpeg"

DURATION = 300.0     # 5분
N_FLASH = 20
FLASH = 0.4          # 번쩍임 지속

rng = random.Random(20260901)
NUMBERS = rng.sample(range(100, 1000), N_FLASH)
# 최소 6초 간격으로 흩는다(모여 있으면 한 표본이 둘을 잡는 혼선)
_slots = sorted(rng.sample(range(2, int(DURATION) - 3), N_FLASH * 4))
TIMES, last = [], -99.0
for t in _slots:
    if t - last >= 6.0 and len(TIMES) < N_FLASH:
        TIMES.append(float(t) + rng.random() * 0.8)   # 초 경계에서 일부러 어긋냄
        last = t
NUMBERS = NUMBERS[:len(TIMES)]
BY_NUM = {n: i for i, n in enumerate(NUMBERS)}


def build_video():
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype(
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf", 300)
    os.makedirs(LONG_FRAMES, exist_ok=True)
    black = os.path.join(LONG_FRAMES, "black.png")
    Image.new("RGB", (854, 480), (0, 0, 0)).save(black)
    for i, n in enumerate(NUMBERS):
        img = Image.new("RGB", (854, 480), (0, 0, 0))
        d = ImageDraw.Draw(img)
        box = d.textbbox((0, 0), str(n), font=font)
        d.text(((854 - (box[2] - box[0])) / 2 - box[0],
                (480 - (box[3] - box[1])) / 2 - box[1]),
               str(n), font=font, fill=(255, 255, 255))
        img.save(os.path.join(LONG_FRAMES, f"n{i:03d}.png"))

    lines, cur = [], 0.0
    for i, t in enumerate(TIMES):
        gap = t - cur
        if gap > 0.01:
            lines.append(f"file 'lframes/black.png'\nduration {gap:.3f}")
        lines.append(f"file 'lframes/n{i:03d}.png'\nduration {FLASH:.3f}")
        cur = t + FLASH
    lines.append(f"file 'lframes/black.png'\nduration {DURATION - cur:.3f}")
    lines.append("file 'lframes/black.png'")   # concat 데뮤서는 마지막 항목을 한 번 더 요구
    open(os.path.join(SP, "llist.txt"), "w").write("\n".join(lines) + "\n")
    subprocess.run(
        [FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", "llist.txt",
         "-vf", "fps=10", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "20", "-pix_fmt", "yuv420p", "fpslong.mp4"],
        check=True, capture_output=True, cwd=SP)


def load_key() -> str:
    if os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"]
    for line in open("/Users/gimsewon/rhoonart/ai-video/.env", encoding="utf-8"):
        line = line.strip()
        if line.startswith("GEMINI_API_KEY="):
            return line.split("=", 1)[1].strip().strip("'\"")
    raise SystemExit("GEMINI_API_KEY 없음")


KEY = load_key()


def req(url, data=None, headers=None, method=None, raw=False):
    h = dict(headers or {})
    body = data
    if data is not None and not raw:
        body = json.dumps(data).encode()
        h.setdefault("Content-Type", "application/json")
    r = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def upload(path):
    size = os.path.getsize(path)
    _, hdrs, _ = req(f"{BASE}/upload/v1beta/files?key={KEY}",
                     {"file": {"display_name": "fpslong"}},
                     {"X-Goog-Upload-Protocol": "resumable",
                      "X-Goog-Upload-Command": "start",
                      "X-Goog-Upload-Header-Content-Length": str(size),
                      "X-Goog-Upload-Header-Content-Type": "video/mp4"},
                     method="POST")
    up = hdrs.get("X-Goog-Upload-URL") or hdrs.get("x-goog-upload-url")
    blob = open(path, "rb").read()
    _, _, body = req(up, blob, {"Content-Length": str(size),
                                "X-Goog-Upload-Offset": "0",
                                "X-Goog-Upload-Command": "upload, finalize"},
                     method="POST", raw=True)
    doc = json.loads(body)["file"]
    name, uri = doc["name"], doc["uri"]
    for _ in range(40):
        _, _, body = req(f"{BASE}/v1beta/{name}?key={KEY}")
        if json.loads(body).get("state") == "ACTIVE":
            return name, uri
        time.sleep(3)
    raise SystemExit("ACTIVE 실패")


PROMPT = ("이 영상은 대부분 검은 화면이고, 아주 가끔 세 자리 숫자가 잠깐 번쩍인다. "
          "네가 **실제로 본** 숫자를 나타난 순서대로 전부 나열하라. "
          "보지 못한 것을 추측해 채우지 마라. "
          "숫자만 쉼표로 구분해 한 줄로 답하고, 다른 말은 쓰지 마라.")


def run(uri, fps):
    parts = [{"file_data": {"file_uri": uri, "mime_type": "video/mp4"},
              "video_metadata": {"fps": fps}},
             {"text": PROMPT}]
    _, _, body = req(f"{BASE}/v1beta/models/{MODEL}:generateContent?key={KEY}",
                     {"contents": [{"role": "user", "parts": parts}],
                      "generationConfig": {"temperature": 0.0,
                                           "maxOutputTokens": 4096}},
                     method="POST")
    doc = json.loads(body)
    cand = (doc.get("candidates") or [{}])[0]
    text = "".join(p.get("text", "")
                   for p in (cand.get("content") or {}).get("parts", []))
    um = doc.get("usageMetadata", {})
    got, seen = [], set()
    for x in re.findall(r"\b\d{3}\b", text):
        v = int(x)
        if v not in seen:
            seen.add(v)
            got.append(v)
    hit = [g for g in got if g in BY_NUM]
    halluc = [g for g in got if g not in BY_NUM]
    return {"fps": fps, "prompt_tokens": um.get("promptTokenCount"),
            "hit": len(hit), "of": len(NUMBERS), "halluc": len(halluc),
            "missed": [n for n in NUMBERS if n not in seen],
            "finish": cand.get("finishReason")}


print(f"소재 생성 중… ({DURATION:.0f}초 · {len(NUMBERS)}회 번쩍임 · 각 {FLASH}초)")
build_video()
print(f"  번쩍임 시각(앞 6): {[round(t,2) for t in TIMES[:6]]}")
print(f"  숫자(앞 6): {NUMBERS[:6]}\n")

name, uri = upload(VIDEO)
print(f"업로드 완료: {uri}\n")
print(f"{'조건':12s} {'입력토큰':>9s} {'회수':>8s} {'환각':>5s}  이론 기댓값")
print("-" * 58)
expect = {1: FLASH / 1.0, 2: FLASH / 0.5, 5: 1.0}
results = []
for fps in [1, 2, 5]:
    r = run(uri, fps)
    results.append(r)
    e = min(1.0, expect[fps]) * len(NUMBERS)
    print(f"fps={fps:<9d} {r['prompt_tokens']:9d} {r['hit']:4d}/{r['of']:<3d} "
          f"{r['halluc']:5d}   ≈{e:.0f}/{len(NUMBERS)}")

json.dump({"numbers": NUMBERS, "times": TIMES, "results": results},
          open(os.path.join(SP, "fps_long_result.json"), "w"),
          ensure_ascii=False, indent=1)
req(f"{BASE}/v1beta/{name}?key={KEY}", method="DELETE")
print("\n테스트 파일 삭제 완료")
