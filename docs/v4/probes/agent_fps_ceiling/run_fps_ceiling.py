#!/usr/bin/env python3
"""Sample-fps ceiling probe: how high can requested fps go before recall stops improving?

Uploads one 20s/20fps/854x480 clip (200 distinct 3-digit numbers, one per 100ms slot)
via Files API once, then asks gemini-3.7-flash to read the numbers back at a range of
requested sampling fps. Billing is read from usageMetadata (never countTokens).
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
MODEL = "gemini-3.7-flash"
HOST = "https://generativelanguage.googleapis.com"
ENV = "/Users/gimsewon/rhoonart/ai-video/.env"


def api_key():
    for line in open(ENV):
        line = line.strip()
        if line.startswith("GEMINI_API_KEY="):
            return line.split("=", 1)[1].strip().strip("'\"")
    raise SystemExit("GEMINI_API_KEY not found")


KEY = api_key()


def post(url, data, headers, method="POST"):
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def upload(path):
    size = os.path.getsize(path)
    st, hdr, body = post(
        f"{HOST}/upload/v1beta/files?key={KEY}",
        json.dumps({"file": {"display_name": os.path.basename(path)}}).encode(),
        {
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(size),
            "X-Goog-Upload-Header-Content-Type": "video/mp4",
            "Content-Type": "application/json",
        },
    )
    assert st == 200, (st, body[:400])
    up = hdr.get("X-Goog-Upload-URL") or hdr.get("x-goog-upload-url")
    st, _, body = post(
        up,
        open(path, "rb").read(),
        {
            "Content-Length": str(size),
            "X-Goog-Upload-Offset": "0",
            "X-Goog-Upload-Command": "upload, finalize",
        },
    )
    assert st == 200, (st, body[:400])
    f = json.loads(body)["file"]
    name, uri = f["name"], f["uri"]
    for _ in range(120):
        req = urllib.request.Request(f"{HOST}/v1beta/{name}?key={KEY}")
        with urllib.request.urlopen(req, timeout=60) as r:
            info = json.loads(r.read())
        if info.get("state") == "ACTIVE":
            return name, uri, info
        if info.get("state") == "FAILED":
            raise SystemExit("upload FAILED " + json.dumps(info)[:400])
        time.sleep(2)
    raise SystemExit("file never became ACTIVE")


def delete(name):
    st, _, body = post(f"{HOST}/v1beta/{name}?key={KEY}", None, {}, method="DELETE")
    return st, body[:200]


PROMPT_LIST = (
    "This video shows a single large 3-digit number at a time on a black background.\n"
    "The number changes many times through the clip.\n\n"
    "Read out EVERY number you actually see, in the order they appear.\n"
    "Answer with ONLY a JSON array of 3-digit strings, e.g. [\"350\",\"241\",\"831\"].\n"
    "Rules:\n"
    "- Do NOT invent or guess numbers you did not see. There is no pattern to extrapolate; the numbers are random.\n"
    "- If the same number appears twice in a row, list it once.\n"
    "- No commentary, no markdown fences, JSON array only."
)

PROMPT_COUNT = (
    "This video shows a single large 3-digit number at a time on a black background.\n"
    "The number changes many times through the clip.\n\n"
    "How many DISTINCT number-values did you see in total? "
    "Answer with ONLY a JSON object: {\"count\": <integer>}. No commentary."
)


def call(uri, fps, prompt, max_out):
    part = {"file_data": {"file_uri": uri, "mime_type": "video/mp4"}}
    if fps is not None:
        part["video_metadata"] = {"fps": fps}
    payload = {
        "contents": [{"role": "user", "parts": [part, {"text": prompt}]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": max_out},
    }
    t0 = time.time()
    st, _, body = post(
        f"{HOST}/v1beta/models/{MODEL}:generateContent?key={KEY}",
        json.dumps(payload).encode(),
        {"Content-Type": "application/json"},
    )
    dt = time.time() - t0
    return st, json.loads(body), dt


def extract_text(resp):
    out = []
    for c in resp.get("candidates", []):
        for p in c.get("content", {}).get("parts", []):
            if "text" in p and not p.get("thought"):
                out.append(p["text"])
    return "".join(out)


def parse_numbers(text):
    m = re.findall(r"\b(\d{3})\b", text)
    return m


def lis_len(seq):
    """longest strictly increasing subsequence length (order monotonicity)."""
    import bisect
    tails = []
    for x in seq:
        i = bisect.bisect_left(tails, x)
        if i == len(tails):
            tails.append(x)
        else:
            tails[i] = x
    return len(tails)


def score(pred, truth_numbers):
    idx = {n: i for i, n in enumerate(truth_numbers)}
    seen = set()
    hits, halluc, dup, order = 0, 0, 0, []
    for p in pred:
        if p in idx:
            if p in seen:
                dup += 1
            else:
                seen.add(p)
                hits += 1
                order.append(idx[p])
        else:
            halluc += 1
    mono = lis_len(order) / len(order) if order else 0.0
    return {
        "emitted": len(pred),
        "hits": hits,
        "hallucinations": halluc,
        "duplicates": dup,
        "monotonicity": round(mono, 4),
        "matched_indices_first10": order[:10],
        "matched_indices_last10": order[-10:],
    }


def main():
    truth = json.load(open(os.path.join(BASE, "truth.json")))
    tn = truth["numbers"]
    video = os.path.join(BASE, "src20fps.mp4")
    dur = 20.0

    fps_list = [float(x) for x in (sys.argv[1].split(",") if len(sys.argv) > 1 else
                                   ["1", "2", "5", "8", "10", "15", "20"])]
    mode = sys.argv[2] if len(sys.argv) > 2 else "list"
    tag = sys.argv[3] if len(sys.argv) > 3 else mode

    name, uri, info = upload(video)
    print(f"uploaded name={name} uri={uri} sizeBytes={info.get('sizeBytes')}", flush=True)
    results = []
    try:
        for fps in fps_list:
            prompt = PROMPT_LIST if mode == "list" else PROMPT_COUNT
            st, resp, dt = call(uri, fps, prompt, 32768 if mode == "list" else 4096)
            if st != 200:
                print(f"fps={fps} HTTP {st} {json.dumps(resp)[:400]}", flush=True)
                results.append({"fps": fps, "http": st, "error": json.dumps(resp)[:600]})
                continue
            um = resp.get("usageMetadata", {})
            cand = resp.get("candidates", [{}])[0]
            fin = cand.get("finishReason")
            text = extract_text(resp)
            pred = parse_numbers(text)
            ceiling = min(int(round(dur * fps)), truth["count"])
            sc = score(pred, tn) if mode == "list" else {}
            row = {
                "fps": fps,
                "mode": mode,
                "elapsed_sec": round(dt, 3),
                "finishReason": fin,
                "truncated": fin == "MAX_TOKENS",
                "promptTokenCount": um.get("promptTokenCount"),
                "promptTokensDetails": um.get("promptTokensDetails"),
                "candidatesTokenCount": um.get("candidatesTokenCount"),
                "thoughtsTokenCount": um.get("thoughtsTokenCount"),
                "totalTokenCount": um.get("totalTokenCount"),
                "theoretical_ceiling": ceiling,
                "expected_video_tokens_F66": int(round(dur * fps)) * 66 + int(dur * 25),
            }
            row.update(sc)
            if mode == "list":
                row["recall_vs_ceiling"] = round(sc["hits"] / ceiling, 4) if ceiling else None
                row["recall_vs_200"] = round(sc["hits"] / 200, 4)
            else:
                row["raw_text"] = text.strip()[:200]
            results.append(row)
            print(json.dumps({k: v for k, v in row.items()
                              if k not in ("matched_indices_first10", "matched_indices_last10",
                                           "promptTokensDetails")},
                             ensure_ascii=False), flush=True)
            with open(os.path.join(BASE, f"raw_{tag}_fps{fps}.txt"), "w") as fh:
                fh.write(text)
    finally:
        print("delete:", delete(name), flush=True)
    with open(os.path.join(BASE, f"results_{tag}.json"), "w") as fh:
        json.dump({"truth_count": truth["count"], "duration_sec": dur, "results": results}, fh, indent=1)
    print("wrote", f"results_{tag}.json")


if __name__ == "__main__":
    main()
