#!/usr/bin/env python3
"""Gemini REST 헬퍼 — 표준 라이브러리만. Files API resumable 업로드 + generateContent."""
import json
import mimetypes
import os
import time
import urllib.error
import urllib.request

BASE = "https://generativelanguage.googleapis.com"
MODEL = "gemini-3.7-flash"
ENV = "/Users/gimsewon/rhoonart/ai-video/.env"


def api_key():
    with open(ENV) as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("GEMINI_API_KEY"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("GEMINI_API_KEY not found")


KEY = api_key()


def _req(url, data=None, headers=None, method=None):
    r = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(r, timeout=600) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            body = e.read()
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            raise SystemExit(f"HTTP {e.code}: {body[:2000].decode('utf8','replace')}")
        except urllib.error.URLError:
            if attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            raise


def upload(path, display_name=None):
    size = os.path.getsize(path)
    mime = mimetypes.guess_type(path)[0] or "video/mp4"
    meta = json.dumps({"file": {"display_name": display_name or os.path.basename(path)}}).encode()
    _, hdrs, _ = _req(
        f"{BASE}/upload/v1beta/files?key={KEY}",
        data=meta,
        headers={
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(size),
            "X-Goog-Upload-Header-Content-Type": mime,
            "Content-Type": "application/json",
        },
    )
    up = hdrs.get("X-Goog-Upload-URL") or hdrs.get("x-goog-upload-url")
    with open(path, "rb") as fh:
        blob = fh.read()
    _, _, body = _req(
        up,
        data=blob,
        headers={
            "Content-Length": str(size),
            "X-Goog-Upload-Offset": "0",
            "X-Goog-Upload-Command": "upload, finalize",
        },
    )
    f = json.loads(body)["file"]
    name, uri = f["name"], f["uri"]
    for _ in range(120):
        _, _, b = _req(f"{BASE}/v1beta/{name}?key={KEY}")
        st = json.loads(b)
        if st.get("state") == "ACTIVE":
            return name, uri, mime
        if st.get("state") == "FAILED":
            raise SystemExit(f"upload FAILED: {st}")
        time.sleep(2)
    raise SystemExit("upload timeout")


def delete(name):
    try:
        _req(f"{BASE}/v1beta/{name}?key={KEY}", method="DELETE")
        return True
    except SystemExit as e:
        print(f"  [delete fail] {name}: {e}")
        return False


def generate(file_uri, mime, prompt, fps=None, media_resolution=None,
             temperature=0.0, max_output_tokens=8192, json_out=True):
    part = {"file_data": {"mime_type": mime, "file_uri": file_uri}}
    if fps is not None:
        part["video_metadata"] = {"fps": fps}
    gen = {"temperature": temperature, "maxOutputTokens": max_output_tokens}
    if json_out:
        gen["responseMimeType"] = "application/json"
    if media_resolution:
        gen["mediaResolution"] = media_resolution
    payload = {
        "contents": [{"role": "user", "parts": [part, {"text": prompt}]}],
        "generationConfig": gen,
    }
    _, _, body = _req(
        f"{BASE}/v1beta/models/{MODEL}:generateContent?key={KEY}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(body)


def text_of(resp):
    out = []
    for c in resp.get("candidates", []):
        for p in c.get("content", {}).get("parts", []):
            if "text" in p:
                out.append(p["text"])
    return "".join(out)
