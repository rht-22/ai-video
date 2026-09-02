"""결정적 검증: 멀티파트 짜집기를 모델이 실제로 다 보는가 + 실제 과금은 얼마인가.
countTokens 가 아니라 generateContent 의 usageMetadata 로 잰다."""
import json
import os
import subprocess
import time
import urllib.request
import urllib.error

SP = os.path.dirname(os.path.abspath(__file__))
VIDEO = os.path.join(SP, "colors12.mp4")
MODEL = "gemini-3.7-flash"
BASE = "https://generativelanguage.googleapis.com"
FFMPEG = "/opt/homebrew/bin/ffmpeg"

COLORS = ["red", "green", "blue", "yellow", "magenta", "cyan"]  # 2초씩 12초


def build_video():
    """2초씩 6색 블록 = 12초. 구간을 색으로 식별할 수 있게."""
    if os.path.exists(VIDEO):
        return
    inputs, filters, labels = [], [], ""
    for i, c in enumerate(COLORS):
        inputs += ["-f", "lavfi", "-t", "2",
                   "-i", f"color=c={c}:s=640x360:r=10"]
        labels += f"[{i}:v]"
    filters.append(f"{labels}concat=n={len(COLORS)}:v=1:a=0[v]")
    cmd = ([FFMPEG, "-y"] + inputs
           + ["-f", "lavfi", "-t", "12", "-i", "sine=frequency=440",
              "-filter_complex", ";".join(filters), "-map", "[v]",
              "-map", f"{len(COLORS)}:a", "-c:v", "libx264",
              "-preset", "ultrafast", "-pix_fmt", "yuv420p",
              "-c:a", "aac", VIDEO])
    subprocess.run(cmd, check=True, capture_output=True)


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
    st, hdrs, _ = req(
        f"{BASE}/upload/v1beta/files?key={KEY}",
        {"file": {"display_name": "colors12"}},
        {"X-Goog-Upload-Protocol": "resumable",
         "X-Goog-Upload-Command": "start",
         "X-Goog-Upload-Header-Content-Length": str(size),
         "X-Goog-Upload-Header-Content-Type": "video/mp4"},
        method="POST")
    up = hdrs.get("X-Goog-Upload-URL") or hdrs.get("x-goog-upload-url")
    with open(path, "rb") as f:
        blob = f.read()
    req(up, blob, {"Content-Length": str(size), "X-Goog-Upload-Offset": "0",
                   "X-Goog-Upload-Command": "upload, finalize"},
        method="POST", raw=True)
    st, _, body = req(f"{BASE}/upload/v1beta/files?key={KEY}") if False else (0, 0, b"")
    # finalize 응답을 다시 받기 위해 목록에서 최신 파일을 찾는다
    st, _, body = req(f"{BASE}/v1beta/files?key={KEY}&pageSize=5")
    files = json.loads(body).get("files", [])
    doc = files[0]
    name, uri = doc["name"], doc["uri"]
    for _ in range(20):
        st, _, body = req(f"{BASE}/v1beta/{name}?key={KEY}")
        state = json.loads(body).get("state")
        if state == "ACTIVE":
            return name, uri
        time.sleep(3)
    raise SystemExit("ACTIVE 실패")


def generate(uri, specs, prompt):
    parts = []
    for spec in specs:
        vm = {"fps": spec.get("fps", 1)}
        if spec.get("start") is not None:
            vm["startOffset"] = f"{spec['start']}s"
        if spec.get("end") is not None:
            vm["endOffset"] = f"{spec['end']}s"
        parts.append({"file_data": {"file_uri": uri, "mime_type": "video/mp4"},
                      "video_metadata": vm})
    parts.append({"text": prompt})
    payload = {"contents": [{"role": "user", "parts": parts}],
               "generationConfig": {"temperature": 0.0}}
    st, _, body = req(
        f"{BASE}/v1beta/models/{MODEL}:generateContent?key={KEY}",
        payload, method="POST")
    doc = json.loads(body)
    if st != 200:
        return f"ERR {st}: {json.dumps(doc)[:200]}", {}
    try:
        text = "".join(p.get("text", "") for p in
                       doc["candidates"][0]["content"]["parts"])
    except Exception:
        text = f"(응답 파싱 실패) {json.dumps(doc)[:200]}"
    um = doc.get("usageMetadata", {})
    detail = {d.get("modality"): d.get("tokenCount")
              for d in um.get("promptTokensDetails", [])}
    return text.strip().replace("\n", " ")[:160], {
        "prompt_total": um.get("promptTokenCount"), **detail}


build_video()
print(f"소재: 12초 · 2초씩 {COLORS}")
name, uri = upload(VIDEO)
print(f"ACTIVE: {uri}\n")

PROMPT = ("첨부된 영상 클립들을 순서대로 보고, 각 클립의 화면 주 색상을 "
          "영어 색 이름으로만 쉼표로 나열하라. 다른 말은 하지 마라.")

cases = [
    ("전체 12초 (참조)", [{"start": 0, "end": 12, "fps": 1}], "red…cyan 6색"),
    ("1파트 4-6s", [{"start": 4, "end": 6, "fps": 1}], "blue"),
    ("3파트 0-2,4-6,10-12", [{"start": 0, "end": 2, "fps": 1},
                            {"start": 4, "end": 6, "fps": 1},
                            {"start": 10, "end": 12, "fps": 1}],
     "red, blue, cyan"),
    ("3파트 역순 10-12,0-2,4-6", [{"start": 10, "end": 12, "fps": 1},
                                {"start": 0, "end": 2, "fps": 1},
                                {"start": 4, "end": 6, "fps": 1}],
     "cyan, red, blue"),
]
for label, specs, expect in cases:
    text, usage = generate(uri, specs, PROMPT)
    print(f"  [{label}]")
    print(f"    기대: {expect}")
    print(f"    응답: {text}")
    print(f"    과금: {usage}")
    print()

st, _, _ = req(f"{BASE}/v1beta/{name}?key={KEY}", method="DELETE")
print(f"테스트 파일 삭제 status={st}")
