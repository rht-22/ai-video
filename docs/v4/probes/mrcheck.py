"""media_resolution 이 영상에서 실제로 토큰을 바꾸는지 독립 재확인 (stdlib only)."""
import json
import os
import time
import urllib.request
import urllib.error

SP = os.path.dirname(os.path.abspath(__file__))
VIDEO = os.path.join(SP, "mrtest.mp4")
MODEL = "gemini-3.7-flash"
BASE = "https://generativelanguage.googleapis.com"


def load_key() -> str:
    if os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"]
    for path in ("/Users/gimsewon/rhoonart/ai-video/.env",):
        if not os.path.exists(path):
            continue
        for line in open(path, encoding="utf-8"):
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
        {"file": {"display_name": "mrtest"}},
        {"X-Goog-Upload-Protocol": "resumable",
         "X-Goog-Upload-Command": "start",
         "X-Goog-Upload-Header-Content-Length": str(size),
         "X-Goog-Upload-Header-Content-Type": "video/mp4"},
        method="POST")
    up = hdrs.get("X-Goog-Upload-URL") or hdrs.get("x-goog-upload-url")
    if not up:
        raise SystemExit(f"업로드 URL 없음 (status {st}) hdrs={list(hdrs)[:10]}")
    with open(path, "rb") as f:
        blob = f.read()
    st, _, body = req(up, blob,
                      {"Content-Length": str(size), "X-Goog-Upload-Offset": "0",
                       "X-Goog-Upload-Command": "upload, finalize"},
                      method="POST", raw=True)
    doc = json.loads(body)["file"]
    name, uri = doc["name"], doc["uri"]
    for _ in range(20):
        st, _, body = req(f"{BASE}/v1beta/{name}?key={KEY}")
        state = json.loads(body).get("state")
        if state == "ACTIVE":
            return name, uri
        time.sleep(3)
    raise SystemExit(f"파일이 ACTIVE 가 되지 않음: {state}")


def count(uri, media_res=None, fps=None, start=None, end=None, parts_spec=None):
    """countTokens 로 입력 토큰만 잰다(생성 없음 = 무료)."""
    parts = []
    for spec in (parts_spec or [{"fps": fps, "start": start, "end": end}]):
        vm = {}
        if spec.get("fps") is not None:
            vm["fps"] = spec["fps"]
        if spec.get("start") is not None:
            vm["startOffset"] = f"{spec['start']}s"
        if spec.get("end") is not None:
            vm["endOffset"] = f"{spec['end']}s"
        p = {"file_data": {"file_uri": uri, "mime_type": "video/mp4"}}
        if vm:
            p["video_metadata"] = vm
        parts.append(p)
    parts.append({"text": "x"})
    gcr = {"model": f"models/{MODEL}",
           "contents": [{"role": "user", "parts": parts}]}
    if media_res:
        gcr["generationConfig"] = {"mediaResolution": media_res}
    st, _, body = req(f"{BASE}/v1beta/models/{MODEL}:countTokens?key={KEY}",
                      {"generateContentRequest": gcr}, method="POST")
    doc = json.loads(body)
    if st != 200:
        return f"ERR {st}: {json.dumps(doc)[:200]}"
    total = doc.get("totalTokens")
    detail = {d.get("modality"): d.get("tokenCount")
              for d in doc.get("promptTokensDetails", [])}
    return total, detail


print("업로드 중…")
name, uri = upload(VIDEO)
print(f"ACTIVE: {uri}\n")

print("=== A. media_resolution 비교 (10초 · fps 1) ===")
for mr in [None, "MEDIA_RESOLUTION_LOW", "MEDIA_RESOLUTION_MEDIUM",
           "MEDIA_RESOLUTION_HIGH"]:
    print(f"  {str(mr):32s} -> {count(uri, media_res=mr, fps=1)}")

print("\n=== B. fps 비례 확인 (기본 해상도) ===")
for fps in [0.5, 1, 2, 5]:
    print(f"  fps={fps:<5} -> {count(uri, fps=fps)}")

print("\n=== C. offset 단일 파트 (0~4초만) ===")
print(f"  0-4s   -> {count(uri, fps=1, start=0, end=4)}")

print("\n=== D. offset 멀티파트 — 같은 파일 3조각 짜집기 ===")
multi = [{"fps": 1, "start": 0, "end": 2},
         {"fps": 1, "start": 4, "end": 6},
         {"fps": 1, "start": 8, "end": 10}]
print(f"  3파트(0-2,4-6,8-10) -> {count(uri, parts_spec=multi)}")

print("\n=== E. 정리 ===")
st, _, _ = req(f"{BASE}/v1beta/{name}?key={KEY}", method="DELETE")
print(f"  테스트 파일 삭제 status={st}")
