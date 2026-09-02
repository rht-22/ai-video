"""offset 멀티파트 vs 실렌더 — 이음새 판정이 같은가?

수정 설계(§5)는 후보를 물리 렌더하지 않고 offset 멀티파트로 보여준다.
우려: offset 은 조각 경계가 모델에게 **구조적으로 명시**되지만 실렌더는 한 덩어리라
'컷이 튀는가' 판정이 갈릴 수 있다. 이 실험이 그 등가성을 잰다.

공정성 규약 — 두 모드에 **같은 정보**를 준다:
  · 같은 프롬프트, 같은 표본 fps(2), 같은 조각(6초 × 3 = 18초)
  · 이음새 위치(편집본 기준 6.0s · 12.0s)를 양쪽 다 알려준다
    (실제 운용에서도 우리가 만든 후보라 경계를 늘 안다)
  ⇒ 남는 차이는 '경계가 구조적으로 드러나는가' 하나뿐이다.

소재: 유미의 세포들 시즌3 480p 프록시(파이프라인 산출물 그대로).
후보는 이음새 강도가 다르도록 손으로 설계했다(A 매끄러움 < C 중간 < B 강한 튐).
⚠ 이 설계 의도는 정답지가 아니다 — 재는 것은 **두 모드의 일치**다.
"""
import json
import os
import subprocess
import time
import urllib.request
import urllib.error

SP = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(SP, "src.mp4")
MODEL = "gemini-3.7-flash"
BASE = "https://generativelanguage.googleapis.com"
FFMPEG = "/opt/homebrew/bin/ffmpeg"

SEG = 6.0        # 조각 길이
SAMPLE_FPS = 2   # v4 제안 설정
REPEATS = 2

# 1차(설계 기울기: A 매끄러움 < C 중간 < B 강한 튐) — 일치 12/12 확인 완료
# 2차(애매한 사례 — 정답을 사람도 모른다): 쉬운 문제라 일치했을 가능성을 배제한다
ROUND = os.environ.get("SEAM_ROUND", "1")
if ROUND == "1":
    CANDIDATES = {
        "A_매끄러움": [341.0, 353.0, 365.0],   # 같은 집 실내 · 같은 인물
        "B_강한튐":   [204.0, 644.0, 864.0],   # 애니메이션 → 지하철 → 밤거리
        "C_중간":     [498.0, 571.0, 791.0],   # 전부 밤 외경 · 인물 일부 공유
    }
else:
    CANDIDATES = {
        "D_같은장소_시간차": [341.0, 371.0, 401.0],   # 같은 공간, 30초씩 뒤
        "E_애니메이션끼리":  [60.0, 133.0, 206.0],    # 전부 세포 세계, 다른 장면
        "F_같은인물_다른장소": [426.0, 866.0, 1013.0],  # 유미 실내 3곳
    }
SEAMS = [SEG, SEG * 2]   # 편집본 기준 이음새 시각


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


def upload(path, label):
    size = os.path.getsize(path)
    _, hdrs, _ = req(f"{BASE}/upload/v1beta/files?key={KEY}",
                     {"file": {"display_name": label}},
                     {"X-Goog-Upload-Protocol": "resumable",
                      "X-Goog-Upload-Command": "start",
                      "X-Goog-Upload-Header-Content-Length": str(size),
                      "X-Goog-Upload-Header-Content-Type": "video/mp4"},
                     method="POST")
    up = hdrs.get("X-Goog-Upload-URL") or hdrs.get("x-goog-upload-url")
    blob = open(path, "rb").read()
    t0 = time.time()
    _, _, body = req(up, blob, {"Content-Length": str(size),
                                "X-Goog-Upload-Offset": "0",
                                "X-Goog-Upload-Command": "upload, finalize"},
                     method="POST", raw=True)
    doc = json.loads(body)["file"]
    name, uri = doc["name"], doc["uri"]
    for _ in range(60):
        _, _, body = req(f"{BASE}/v1beta/{name}?key={KEY}")
        if json.loads(body).get("state") == "ACTIVE":
            return name, uri, round(time.time() - t0, 1)
        time.sleep(2)
    raise SystemExit(f"{label}: ACTIVE 실패")


def render_concat(starts, out_path):
    """입력 seek 로 조각 3개를 잘라 이어 붙인다(실렌더 모드)."""
    cmd = [FFMPEG, "-y"]
    for s in starts:
        cmd += ["-ss", f"{s:.3f}", "-t", f"{SEG:.3f}", "-i", SRC]
    n = len(starts)
    chain = "".join(f"[{i}:v][{i}:a]" for i in range(n))
    cmd += ["-filter_complex", f"{chain}concat=n={n}:v=1:a=1[v][a]",
            "-map", "[v]", "-map", "[a]", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", out_path]
    t0 = time.time()
    subprocess.run(cmd, check=True, capture_output=True)
    return round(time.time() - t0, 1)


PROMPT = (
    f"첨부한 영상은 한 방송에서 잘라낸 조각 3개를 순서대로 이어 붙인 편집본이다"
    f"(각 {SEG:g}초, 총 {SEG*3:g}초). 조각 경계(이음새)는 편집본 기준 "
    f"{SEAMS[0]:g}초와 {SEAMS[1]:g}초 지점이다.\n"
    "각 이음새에 대해, 인물·장소·상황이 설명 없이 갑자기 바뀌어 시청자가 "
    "따라가기 어려운지 판단하라. 취향 평가 금지, 점수 금지 — 사실 판정만.\n"
    "또 첫 2초 안에 사건(대사·동작·리액션)이 있는지 판단하라.\n"
    'JSON 만 출력: {"seams":[{"at":6.0,"jump":true,"why":"한 줄"},'
    '{"at":12.0,"jump":false,"why":"한 줄"}],"hook_weak":false}'
)


def ask(parts):
    _, _, body = req(
        f"{BASE}/v1beta/models/{MODEL}:generateContent?key={KEY}",
        {"contents": [{"role": "user", "parts": parts + [{"text": PROMPT}]}],
         "generationConfig": {"temperature": 0.0, "maxOutputTokens": 2048,
                              "responseMimeType": "application/json"}},
        method="POST")
    doc = json.loads(body)
    cand = (doc.get("candidates") or [{}])[0]
    text = "".join(p.get("text", "")
                   for p in (cand.get("content") or {}).get("parts", []))
    um = doc.get("usageMetadata", {})
    try:
        parsed = json.loads(text)
    except Exception:
        return {"error": text[:200], "tokens": um.get("promptTokenCount")}
    jumps = [bool(s.get("jump")) for s in parsed.get("seams", [])]
    return {"jumps": jumps, "hook_weak": bool(parsed.get("hook_weak")),
            "why": [s.get("why", "")[:60] for s in parsed.get("seams", [])],
            "tokens": um.get("promptTokenCount")}


def offset_parts(uri, starts):
    return [{"file_data": {"file_uri": uri, "mime_type": "video/mp4"},
             "video_metadata": {"startOffset": f"{s:.3f}s",
                                "endOffset": f"{s+SEG:.3f}s",
                                "fps": SAMPLE_FPS}} for s in starts]


print("원본 프록시 업로드 중… (80MB · offset 모드는 이 하나만 쓴다)")
src_name, src_uri, src_up = upload(SRC, "yumi_src")
print(f"  완료 {src_up}s\n")

rows, uploaded = [], [src_name]
for label, starts in CANDIDATES.items():
    out = os.path.join(SP, f"cand_{label}.mp4")
    rt = render_concat(starts, out)
    rname, ruri, rup = upload(out, f"cand_{label}")
    uploaded.append(rname)
    mb = os.path.getsize(out) / 1e6
    print(f"[{label}] 실렌더 {rt}s · {mb:.1f}MB · 업로드 {rup}s")
    for rep in range(REPEATS):
        o = ask(offset_parts(src_uri, starts))
        r = ask([{"file_data": {"file_uri": ruri, "mime_type": "video/mp4"},
                  "video_metadata": {"fps": SAMPLE_FPS}}])
        rows.append({"cand": label, "rep": rep + 1, "offset": o, "render": r,
                     "render_sec": rt, "upload_sec": rup})
        print(f"   회차{rep+1}  offset  jump={o.get('jumps')} "
              f"hook_weak={o.get('hook_weak')} tok={o.get('tokens')}")
        print(f"          실렌더  jump={r.get('jumps')} "
              f"hook_weak={r.get('hook_weak')} tok={r.get('tokens')}")

# ── 일치도 ────────────────────────────────────────────────────────────
seam_tot = seam_ok = hook_tot = hook_ok = 0
for row in rows:
    o, r = row["offset"], row["render"]
    if "jumps" in o and "jumps" in r and len(o["jumps"]) == len(r["jumps"]):
        for a, b in zip(o["jumps"], r["jumps"]):
            seam_tot += 1
            seam_ok += int(a == b)
    if "hook_weak" in o and "hook_weak" in r:
        hook_tot += 1
        hook_ok += int(o["hook_weak"] == r["hook_weak"])

print("\n" + "=" * 62)
print(f"이음새 판정 일치 : {seam_ok}/{seam_tot}"
      f"  ({100*seam_ok/max(1,seam_tot):.0f}%)")
print(f"훅 판정 일치     : {hook_ok}/{hook_tot}"
      f"  ({100*hook_ok/max(1,hook_tot):.0f}%)")


def jump_count(mode):
    return {c: sum(sum(row[mode].get("jumps", [])) for row in rows
                   if row["cand"] == c) for c in CANDIDATES}


print(f"\n후보별 '튐' 판정 합계 (회차 {REPEATS}회 × 이음새 2개 = 최대 4)")
jo, jr = jump_count("offset"), jump_count("render")
print(f"{'후보':14s} {'offset':>8s} {'실렌더':>8s}")
for c in CANDIDATES:
    print(f"{c:14s} {jo[c]:8d} {jr[c]:8d}")
rank_o = sorted(CANDIDATES, key=lambda c: -jo[c])
rank_r = sorted(CANDIDATES, key=lambda c: -jr[c])
print(f"\n순위(튐 많은 순)  offset: {rank_o}")
print(f"                  실렌더: {rank_r}")
print(f"순위 일치: {'예' if rank_o == rank_r else '아니오'}")

json.dump(rows, open(os.path.join(SP, "seam_equiv_result.json"), "w"),
          ensure_ascii=False, indent=1)
for n in uploaded:
    req(f"{BASE}/v1beta/{n}?key={KEY}", method="DELETE")
print("\n업로드 파일 전부 삭제 완료")
