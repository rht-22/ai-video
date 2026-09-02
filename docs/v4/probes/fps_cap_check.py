"""코드 상수가 될 두 사실만 독립 재확인.
  ① fps 하드캡이 24 인가 (25 이상은 요청이 거부되는가)
  ② 파일 fps 를 넘겨 요청하면 과금이 늘고 정보는 안 느는가
소재: fpsgrid.mp4 (12초 · 파일 10fps · 200ms 마다 다른 숫자 60개)
"""
import json
import os
import random
import re
import time
import urllib.request
import urllib.error

SP = os.path.dirname(os.path.abspath(__file__))
VIDEO = os.path.join(SP, "fpsgrid.mp4")
MODEL = "gemini-3.7-flash"
BASE = "https://generativelanguage.googleapis.com"

# fps_gain.py 와 같은 시드·같은 정답지
rng = random.Random(20260901)
NUMBERS = rng.sample(range(100, 1000), 60)
TRUTH = set(NUMBERS)


def load_key():
    if os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"]
    for line in open("/Users/gimsewon/rhoonart/ai-video/.env", encoding="utf-8"):
        line = line.strip()
        if line.startswith("GEMINI_API_KEY="):
            return line.split("=", 1)[1].strip().strip("'\"")
    raise SystemExit("키 없음")


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
                     {"file": {"display_name": "capchk"}},
                     {"X-Goog-Upload-Protocol": "resumable",
                      "X-Goog-Upload-Command": "start",
                      "X-Goog-Upload-Header-Content-Length": str(size),
                      "X-Goog-Upload-Header-Content-Type": "video/mp4"},
                     method="POST")
    up = hdrs.get("X-Goog-Upload-URL") or hdrs.get("x-goog-upload-url")
    _, _, body = req(up, open(path, "rb").read(),
                     {"Content-Length": str(size), "X-Goog-Upload-Offset": "0",
                      "X-Goog-Upload-Command": "upload, finalize"},
                     method="POST", raw=True)
    doc = json.loads(body)["file"]
    for _ in range(30):
        _, _, b = req(f"{BASE}/v1beta/{doc['name']}?key={KEY}")
        if json.loads(b).get("state") == "ACTIVE":
            return doc["name"], doc["uri"]
        time.sleep(2)
    raise SystemExit("ACTIVE 실패")


PROMPT = ("이 영상에는 세 자리 숫자가 빠르게 바뀌며 나타난다. 실제로 본 숫자를 "
          "순서대로 전부 나열하라. 추측 금지. 숫자만 쉼표로 구분해 답하라.")


def run(uri, fps):
    st, _, body = req(
        f"{BASE}/v1beta/models/{MODEL}:generateContent?key={KEY}",
        {"contents": [{"role": "user", "parts": [
            {"file_data": {"file_uri": uri, "mime_type": "video/mp4"},
             "video_metadata": {"fps": fps}},
            {"text": PROMPT}]}],
         "generationConfig": {"temperature": 0.0, "maxOutputTokens": 8192}},
        method="POST")
    doc = json.loads(body)
    if st != 200:
        msg = (doc.get("error") or {}).get("message", "")[:110]
        return {"status": st, "error": msg}
    cand = (doc.get("candidates") or [{}])[0]
    text = "".join(p.get("text", "")
                   for p in (cand.get("content") or {}).get("parts", []))
    um = doc.get("usageMetadata", {})
    det = {x.get("modality"): x.get("tokenCount")
           for x in um.get("promptTokensDetails", [])}
    got = {int(x) for x in re.findall(r"\b\d{3}\b", text)}
    return {"status": 200, "video_tok": det.get("VIDEO"),
            "hit": len(got & TRUTH), "halluc": len(got - TRUTH)}


name, uri = upload(VIDEO)
print(f"소재: 12초 · 파일 10fps · 숫자 60개 (200ms 간격)\n")

print("① 파일 fps(10) 초과 요청 — 과금과 정보")
print(f"{'요청fps':>7s} {'영상토큰':>9s} {'대비10fps':>9s} {'적중':>5s} {'환각':>5s}")
base = None
for fps in [5, 10, 15, 20, 24]:
    r = run(uri, fps)
    if r["status"] != 200:
        print(f"{fps:7d}  ERR {r['status']} {r['error']}")
        continue
    if fps == 10:
        base = r["video_tok"]
    ratio = f"{r['video_tok']/base:.2f}x" if base else "-"
    print(f"{fps:7d} {r['video_tok']:9d} {ratio:>9s} "
          f"{r['hit']:5d} {r['halluc']:5d}")

print("\n② fps 하드캡")
for fps in [24, 25, 30, 60]:
    r = run(uri, fps)
    if r["status"] == 200:
        print(f"  fps={fps:<3d} 통과 (영상토큰 {r['video_tok']})")
    else:
        print(f"  fps={fps:<3d} 거부 HTTP {r['status']} — {r['error']}")

req(f"{BASE}/v1beta/{name}?key={KEY}", method="DELETE")
print("\n테스트 파일 삭제 완료")
