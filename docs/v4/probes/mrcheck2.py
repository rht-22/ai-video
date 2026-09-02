"""멀티파트 영상 프레임 과금이 조각 합계인지 좁혀 확인 (stdlib only)."""
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
        {"file": {"display_name": "mrtest2"}},
        {"X-Goog-Upload-Protocol": "resumable",
         "X-Goog-Upload-Command": "start",
         "X-Goog-Upload-Header-Content-Length": str(size),
         "X-Goog-Upload-Header-Content-Type": "video/mp4"},
        method="POST")
    up = hdrs.get("X-Goog-Upload-URL") or hdrs.get("x-goog-upload-url")
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
    raise SystemExit("ACTIVE 실패")


def count(uri, specs, media_res=None):
    parts = []
    for spec in specs:
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
        return f"ERR {st}: {json.dumps(doc)[:160]}"
    d = {x.get("modality"): x.get("tokenCount")
         for x in doc.get("promptTokensDetails", [])}
    v, a = d.get("VIDEO", 0), d.get("AUDIO", 0)
    return f"VIDEO {v:5d} ({v/71:5.1f}프레임)  AUDIO {a:4d} ({a/32:4.1f}초)"


name, uri = upload(VIDEO)
print(f"ACTIVE: {uri}\n")

cases = [
    ("1파트 0-2s          fps1", [{"fps": 1, "start": 0, "end": 2}]),
    ("1파트 0-6s          fps1", [{"fps": 1, "start": 0, "end": 6}]),
    ("2파트 0-2,4-6       fps1", [{"fps": 1, "start": 0, "end": 2},
                                  {"fps": 1, "start": 4, "end": 6}]),
    ("3파트 0-2,4-6,8-10  fps1", [{"fps": 1, "start": 0, "end": 2},
                                  {"fps": 1, "start": 4, "end": 6},
                                  {"fps": 1, "start": 8, "end": 10}]),
    ("3파트 같은구간       fps1", [{"fps": 1, "start": 0, "end": 2}] * 3),
    ("3파트 0-2,4-6,8-10  fps2", [{"fps": 2, "start": 0, "end": 2},
                                  {"fps": 2, "start": 4, "end": 6},
                                  {"fps": 2, "start": 8, "end": 10}]),
    ("3파트 0-3,3-6,6-9   fps1", [{"fps": 1, "start": 0, "end": 3},
                                  {"fps": 1, "start": 3, "end": 6},
                                  {"fps": 1, "start": 6, "end": 9}]),
    ("3파트 fps 미지정",         [{"start": 0, "end": 2},
                                  {"start": 4, "end": 6},
                                  {"start": 8, "end": 10}]),
]
for label, specs in cases:
    print(f"  {label:26s} -> {count(uri, specs)}")

print("\n참고: 프레임당 71 · 오디오 32/초 (앞선 실측 상수)")
st, _, _ = req(f"{BASE}/v1beta/{name}?key={KEY}", method="DELETE")
print(f"테스트 파일 삭제 status={st}")
