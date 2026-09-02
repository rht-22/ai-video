"""fps 정보 이득 실측 — video_metadata.fps 를 올리면 모델이 실제로 더 보는가?

설계:
  · 12초 영상, 200ms 마다 서로 다른 3자리 숫자가 하나씩 뜬다(총 60개).
  · 숫자는 **고정 시드 무작위 배열** — 모델이 수열을 추측해 맞히는 것을 막는다.
  · 소재 규격은 레포 스캔 프록시와 같다(854x480 · 파일 10fps).

판정:
  ① 회수 개수 — fps 를 올리면 더 많이 회수하는가
  ② **격자 밖 회수** — 1초 경계에 없는 숫자(index%5 != 0)를 회수하는가   ← 결정적
     Files API 가 1 FPS 로 저장한다면 fps=5 를 요청해도 초 경계 프레임만
     반복해 보게 되므로 격자 밖 회수가 0 에 머문다.
  ③ 환각 — 소재에 없는 숫자를 답하는가(추측 여부의 역검증)
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
VIDEO = os.path.join(SP, "fpsgrid.mp4")
FRAMES = os.path.join(SP, "frames")
MODEL = "gemini-3.7-flash"
BASE = "https://generativelanguage.googleapis.com"
FFMPEG = "/opt/homebrew/bin/ffmpeg"

SLOT = 0.2          # 숫자 하나가 떠 있는 시간
N_SLOTS = 60        # 12초
GRID = 5            # 1초 = 5슬롯 → index % 5 == 0 이 '1초 격자'

rng = random.Random(20260901)
NUMBERS = rng.sample(range(100, 1000), N_SLOTS)   # 서로 다른 3자리 60개, 무작위 순서
BY_NUM = {n: i for i, n in enumerate(NUMBERS)}


def ts(sec: float) -> str:
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(round((s % 1) * 1000)):03d}"


def build_video():
    """이 노드의 ffmpeg 에는 libass·drawtext 가 없다 — PIL 로 프레임을 직접 그린다.

    슬롯당 PNG 1장 → `-framerate 5`(=200ms/장) 로 읽어 10fps 로 인코딩한다
    (레포 스캔 프록시와 같은 854x480 · 파일 10fps)."""
    from PIL import Image, ImageDraw, ImageFont
    font = None
    for cand in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                 "/System/Library/Fonts/Supplemental/Arial.ttf",
                 "/Library/Fonts/Arial.ttf"):
        if os.path.exists(cand):
            font = ImageFont.truetype(cand, 300)
            break
    if font is None:
        raise SystemExit("숫자를 그릴 폰트를 못 찾았다")
    os.makedirs(FRAMES, exist_ok=True)
    for i, n in enumerate(NUMBERS):
        img = Image.new("RGB", (854, 480), (0, 0, 0))
        d = ImageDraw.Draw(img)
        text = str(n)
        box = d.textbbox((0, 0), text, font=font)
        d.text(((854 - (box[2] - box[0])) / 2 - box[0],
                (480 - (box[3] - box[1])) / 2 - box[1]),
               text, font=font, fill=(255, 255, 255))
        img.save(os.path.join(FRAMES, f"f{i:03d}.png"))
    subprocess.run(
        [FFMPEG, "-y", "-framerate", f"{1/SLOT:g}", "-i", "frames/f%03d.png",
         "-r", "10", "-c:v", "libx264", "-preset", "medium", "-crf", "16",
         "-pix_fmt", "yuv420p", "fpsgrid.mp4"],
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
    st, hdrs, _ = req(f"{BASE}/upload/v1beta/files?key={KEY}",
                      {"file": {"display_name": "fpsgrid"}},
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
    for _ in range(25):
        _, _, body = req(f"{BASE}/v1beta/{name}?key={KEY}")
        if json.loads(body).get("state") == "ACTIVE":
            return name, uri
        time.sleep(3)
    raise SystemExit("ACTIVE 실패")


PROMPT = ("이 영상에는 화면 한가운데에 세 자리 숫자가 빠르게 바뀌며 나타난다. "
          "네가 **실제로 본** 숫자를 나타난 순서대로 전부 나열하라. "
          "보지 못한 것을 추측해서 채우지 마라. "
          "숫자만 쉼표로 구분해 한 줄로 답하고, 다른 말은 쓰지 마라.")


def run(uri, fps, media_res=None):
    vm = {"fps": fps}
    parts = [{"file_data": {"file_uri": uri, "mime_type": "video/mp4"},
              "video_metadata": vm},
             {"text": PROMPT}]
    cfg = {"temperature": 0.0, "maxOutputTokens": 8192}
    if media_res:
        cfg["mediaResolution"] = media_res
    st, _, body = req(f"{BASE}/v1beta/models/{MODEL}:generateContent?key={KEY}",
                      {"contents": [{"role": "user", "parts": parts}],
                       "generationConfig": cfg}, method="POST")
    doc = json.loads(body)
    if st != 200:
        return {"error": json.dumps(doc)[:200]}
    cand = (doc.get("candidates") or [{}])[0]
    text = "".join(p.get("text", "")
                   for p in (cand.get("content") or {}).get("parts", []))
    um = doc.get("usageMetadata", {})
    got = [int(x) for x in re.findall(r"\b\d{3}\b", text)]
    seen, uniq = set(), []
    for g in got:
        if g not in seen:
            seen.add(g)
            uniq.append(g)
    hit = [g for g in uniq if g in BY_NUM]
    halluc = [g for g in uniq if g not in BY_NUM]
    on_grid = [g for g in hit if BY_NUM[g] % GRID == 0]
    off_grid = [g for g in hit if BY_NUM[g] % GRID != 0]
    # 회수한 것의 시간 순서가 실제 순서와 맞는가(단조 비율)
    idx = [BY_NUM[g] for g in hit]
    mono = sum(1 for a, b in zip(idx, idx[1:]) if b > a)
    return {
        "fps": fps, "media_res": media_res or "기본",
        "prompt_tokens": um.get("promptTokenCount"),
        "answered": len(uniq), "hit": len(hit), "halluc": len(halluc),
        "on_grid": len(on_grid), "off_grid": len(off_grid),
        "mono_ratio": round(mono / max(1, len(idx) - 1), 2),
        "finish": cand.get("finishReason"),
        "off_grid_sample": off_grid[:8],
        "halluc_sample": halluc[:5],
    }


print("소재 생성 중… (12초 · 200ms 마다 숫자 · 854x480 · 파일 10fps)")
build_video()
print(f"  정답지 앞 10개: {NUMBERS[:10]}")
print(f"  1초 격자 위 숫자(index%5==0) {N_SLOTS//GRID}개 · 격자 밖 {N_SLOTS - N_SLOTS//GRID}개\n")

name, uri = upload(VIDEO)
print(f"업로드 완료: {uri}\n")

print(f"{'조건':22s} {'토큰':>7s} {'답':>4s} {'적중':>4s} {'격자위':>6s} "
      f"{'격자밖':>6s} {'환각':>5s} {'순서':>5s}")
print("-" * 72)
results = []
for mr in [None, "MEDIA_RESOLUTION_HIGH"]:
    for fps in [1, 2, 5]:
        r = run(uri, fps, mr)
        results.append(r)
        if "error" in r:
            print(f"fps={fps} {mr}: ERR {r['error']}")
            continue
        label = f"fps={fps} · {r['media_res']}"
        print(f"{label:22s} {r['prompt_tokens']:7d} {r['answered']:4d} "
              f"{r['hit']:4d} {r['on_grid']:6d} {r['off_grid']:6d} "
              f"{r['halluc']:5d} {r['mono_ratio']:5.2f}")

print("\n[격자 밖 회수 표본]")
for r in results:
    if "error" not in r:
        print(f"  fps={r['fps']} {r['media_res']:6s} -> {r['off_grid_sample']}")

json.dump({"ground_truth": NUMBERS, "results": results},
          open(os.path.join(SP, "fps_gain_result.json"), "w"),
          ensure_ascii=False, indent=1)
_, _, _ = req(f"{BASE}/v1beta/{name}?key={KEY}", method="DELETE")
print("\n테스트 파일 삭제 완료")
