"""프록시 해상도 x media_resolution 교차 실측.

3 프록시(480p/720p/1080p) x 2 media_resolution(미지정/HIGH) x N 반복.
표본 fps 는 1 고정(해상도 실험이지 시간 실험이 아니다).
과금은 countTokens 가 아니라 응답 usageMetadata 로 읽는다.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

SP = os.path.dirname(os.path.abspath(__file__))
TAG = os.environ.get("TAG", "b1")
WORK = os.path.join(SP, f"work_{TAG}" if TAG != "b1" else "work")
BASE = "https://generativelanguage.googleapis.com"
MODEL = "gemini-3.7-flash"
SAMPLE_FPS = 1.0
REPS = int(os.environ.get("REPS", "3"))

PROMPT = (
    "이 영상의 각 화면에는 'AB-1234' 형식(대문자 2개 + 하이픈 + 숫자 4자리)의 "
    "코드가 여러 개 표시됩니다.\n"
    "영상에 나오는 모든 코드를, 각 화면에서 위에서 아래 순서로 빠짐없이 나열하세요.\n"
    "규칙:\n"
    "- 읽을 수 없을 만큼 흐린 코드는 그냥 빼세요.\n"
    "- 절대 지어내거나 추측하지 마세요. 확실히 읽힌 것만 적으세요.\n"
    "- 코드만 쉼표로 구분해서 답하세요. 다른 말은 쓰지 마세요."
)
if os.environ.get("PROMPT_FILE"):
    PROMPT = open(os.environ["PROMPT_FILE"], encoding="utf-8").read().strip()


def load_key():
    for line in open("/Users/gimsewon/rhoonart/ai-video/.env", encoding="utf-8"):
        line = line.strip()
        if line.startswith("GEMINI_API_KEY="):
            return line.split("=", 1)[1].strip().strip("'\"")
    raise SystemExit("GEMINI_API_KEY 없음")


KEY = load_key()


def req(url, data=None, headers=None, method=None, raw=False, timeout=600):
    h = dict(headers or {})
    body = data
    if data is not None and not raw:
        body = json.dumps(data).encode()
        h.setdefault("Content-Type", "application/json")
    r = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def upload(path, name):
    """resumable 2단계 업로드. 소요 시간을 함께 돌려준다."""
    size = os.path.getsize(path)
    t0 = time.time()
    st, hdrs, body = req(
        f"{BASE}/upload/v1beta/files?key={KEY}",
        {"file": {"display_name": name}},
        {"X-Goog-Upload-Protocol": "resumable",
         "X-Goog-Upload-Command": "start",
         "X-Goog-Upload-Header-Content-Length": str(size),
         "X-Goog-Upload-Header-Content-Type": "video/mp4"},
        method="POST")
    if st != 200:
        raise SystemExit(f"업로드 start 실패 {st}: {body[:300]}")
    up = hdrs.get("X-Goog-Upload-URL") or hdrs.get("x-goog-upload-url")
    with open(path, "rb") as f:
        blob = f.read()
    st, _, body = req(up, blob,
                      {"Content-Length": str(size),
                       "X-Goog-Upload-Offset": "0",
                       "X-Goog-Upload-Command": "upload, finalize"},
                      method="POST", raw=True)
    if st != 200:
        raise SystemExit(f"업로드 finalize 실패 {st}: {body[:300]}")
    info = json.loads(body)["file"]
    upload_sec = time.time() - t0
    # ACTIVE 대기
    t1 = time.time()
    while info.get("state") != "ACTIVE":
        if info.get("state") == "FAILED":
            raise SystemExit(f"파일 처리 실패: {info}")
        time.sleep(1.0)
        st, _, b = req(f"{BASE}/v1beta/{info['name']}?key={KEY}")
        info = json.loads(b)
    return {"name": info["name"], "uri": info["uri"],
            "bytes": size, "upload_sec": round(upload_sec, 2),
            "active_wait_sec": round(time.time() - t1, 2)}


def call(uri, media_res, tag):
    part = {"fileData": {"fileUri": uri, "mimeType": "video/mp4"},
            "videoMetadata": {"fps": SAMPLE_FPS}}
    gen = {"maxOutputTokens": 8192}
    if media_res:
        gen["mediaResolution"] = media_res
    payload = {"contents": [{"role": "user",
                             "parts": [part, {"text": PROMPT}]}],
               "generationConfig": gen}
    url = f"{BASE}/v1beta/models/{MODEL}:generateContent?key={KEY}"
    for attempt in range(5):
        t0 = time.time()
        st, _, body = req(url, payload)
        el = time.time() - t0
        if st == 200:
            d = json.loads(body)
            cand = (d.get("candidates") or [{}])[0]
            txt = "".join(p.get("text", "")
                          for p in cand.get("content", {}).get("parts", []))
            return {"tag": tag, "ok": True, "latency_sec": round(el, 2),
                    "text": txt, "finish": cand.get("finishReason"),
                    "usage": d.get("usageMetadata", {})}
        if st in (429, 500, 503) and attempt < 4:
            time.sleep(6 * (attempt + 1))
            continue
        return {"tag": tag, "ok": False, "status": st,
                "error": body.decode("utf-8", "replace")[:600]}
    return {"tag": tag, "ok": False, "error": "재시도 소진"}


CODE_RE = re.compile(r"[A-Za-z]{1,3}\s*[-–—]?\s*\d{2,5}")


def norm(s):
    s = s.upper().replace(" ", "")
    m = re.match(r"^([A-Z]{1,3})[-–—]?(\d{2,5})$", s)
    return f"{m.group(1)}-{m.group(2)}" if m else s


def extract(text):
    out, seen = [], set()
    for m in CODE_RE.finditer(text or ""):
        c = norm(m.group(0))
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def main():
    proxies = json.load(open(os.path.join(WORK, "proxies.json")))
    up_path = os.path.join(WORK, "uploads.json")
    if os.path.exists(up_path):
        ups = json.load(open(up_path))
        print("업로드 재사용")
    else:
        ups = {}
        for p in proxies:
            u = upload(p["path"], f"resprobe_{p['name']}")
            ups[p["name"]] = u
            print(f"업로드 {p['name']:6s} {p['bytes']/1e6:6.3f} MB  "
                  f"{u['upload_sec']:5.2f}s  ACTIVE 대기 {u['active_wait_sec']:.2f}s")
        json.dump(ups, open(up_path, "w"), indent=1)

    jobs = []
    for p in proxies:
        for mr_name, mr in (("default", None), ("HIGH", "MEDIA_RESOLUTION_HIGH")):
            for r in range(REPS):
                jobs.append((ups[p["name"]]["uri"], mr,
                             f"{p['name']}|{mr_name}|r{r}"))
    print(f"\n호출 {len(jobs)}건 시작…")
    results = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        for res in ex.map(lambda j: call(*j), jobs):
            results.append(res)
            if res.get("ok"):
                codes = extract(res["text"])
                print(f"  {res['tag']:22s} {res['latency_sec']:6.1f}s  "
                      f"prompt={res['usage'].get('promptTokenCount')}  "
                      f"코드 {len(codes):2d}개  finish={res['finish']}")
            else:
                print(f"  {res['tag']:22s} 실패 {res.get('status')} "
                      f"{str(res.get('error'))[:200]}")
    json.dump(results, open(os.path.join(WORK, "results.json"), "w"),
              ensure_ascii=False, indent=1)
    print("\nresults.json 저장")


if __name__ == "__main__":
    main()
