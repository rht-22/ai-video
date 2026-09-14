"""5.5단계 — 얼굴(주인물) 기준 크롭 x 결정. Gemini 가 컷마다 프레임 3장을 보고 주인물 얼굴 중심을 준다(2026-09-11 사용자 제안).

왜 Gemini 인가: Haar 캐스케이드(`app/modules/reframe`)는 옆얼굴·뒷모습·작은 얼굴을 놓치고 여러 얼굴 중 누가 주인공인지
모른다. 컷 하나에 프레임 3장(20·50·80%)을 주면 "말하는 사람/카메라가 따라가는 인물"을 의미로 고른다. 비용은 컷당 이미지
3장(≈1k 토큰) — 쇼츠 한 편 20컷 ≈ 2만 토큰.

산출 `framing_v{n}.json`: 구간 키 `{in:.3f}-{out:.3f}` → {x0, x1, y0, y1(정규화 0~1 주인물 중심, 첫·끝 표본), faces, note, raw, img}.
컷 안에 샷 경계(scenecuts)가 있으면 샷별 구간으로 나눠 따로 판정하고(`split_by_shots`), 컷에는 `frame_segs` 로 싣는다 — 렌더는
구간별 crop x 식(`crop_x_expr_segs`: 같은 샷 안에서만 팬, 경계에서 점프)을 쓴다. 표본에서 주인물 얼굴이 안 보이면(뒷모습·오버숄더)
상대의 보이는 얼굴을 쓴다(`parse_points`). 인물이 구간 안에서 40px 이상 움직이면 첫→끝 선형 팬, 아니면 평균 고정.
실패·미검출은 중앙(0.5) — 안전장치가 연출을 막지 않는다(기록).
"""
from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.tikitaka.common import Job, find_bin, ms3
from app.tikitaka.llm import Gemini

SAMPLE_FRACS = (0.2, 0.5, 0.8)
FRAME_HEIGHT = 480
PAN_MIN_PX = 40          # 소스 픽셀 기준 — 이보다 작은 이동은 팬 하지 않는다(미세 떨림 방지)

FRAMING_PROMPT = """첨부한 사진 {n}장은 드라마의 **같은 컷**을 시간순(앞→뒤)으로 뽑은 프레임이다. 이 컷은 쇼츠에서 아래 용도로 쓰인다:
[용도] {context}

세로 쇼츠(5:6)로 잘라 넣을 때 **어떻게 화면을 잡을지** 정하라(둘 중 하나).
- "close": 주인물 한 명을 중심에. 리액션·대사 컷의 기본.
- "two_shot": 두 인물이 마주 보거나 함께 반응하는 컷 — 둘 사이 중점을 잡는다(둘 다 points/partner 로 적는다).
  둘이 화면 폭의 절반 이상 떨어져 있으면 two_shot 이 성립하지 않으니 close 로 주인물만.
주인물 고르는 우선순위(위가 이긴다):
1. **얼굴이 화면에 보이는 인물** — 말하는 사람의 얼굴이 보이면 그 사람. 말하는 사람이 뒷모습·어깨 너머(오버숄더)로만 잡혀
   얼굴이 안 보이면 그 사람이 아니라 **얼굴이 보이는 상대(듣는 사람)** — 뒷머리·몸통을 따라가면 크롭에 사람이 없다.
2. 얼굴이 아무도 안 보이면 **전신·옆모습이 보이는 인물**(카메라가 따라가는/움직이는 인물, 대개 말하는 사람) — view="body".
3. **전경에 크게 잡힌 뒷모습·실루엣**(카메라 바로 앞의 등·뒷머리)은 마지막 선택이다 — view="back". 2번 인물이 작더라도 그쪽을 잡아라.
각 사진에서 그 인물의 얼굴(없으면 몸통) 중심을 사진 기준 **0~1 사이 소수**(x: 왼쪽 0.0 → 오른쪽 1.0, y: 위 0.0 → 아래 1.0)로 적어라 —
픽셀 값 금지. {n}장 모두 **같은 인물**이어야 한다(사진마다 다른 사람을 적지 마라). view 는 사진마다 face|body|back 중 하나.
출력 JSON 하나(코드블록 금지):
{{"framing": "close|two_shot", "subject": "주인물(≤12자)", "faces": 보이는 얼굴 수,
 "points": [{{"x": 0.42, "y": 0.35, "face": true, "view": "face"}}, …({n}개, 사진 순서)],
 "partner": [{{"x": 0.7, "y": 0.4, "face": true, "view": "face"}}, …]  (two_shot 일 때 상대 인물, 아니면 [])}}
"""

def _extract_frames(job: Job, cut_key: str, t_in: float, t_out: float) -> list[Path]:
    d = job.path("framing_frames")
    d.mkdir(exist_ok=True)
    ffmpeg = find_bin("ffmpeg")
    out: list[Path] = []
    for k, f in enumerate(SAMPLE_FRACS):
        p = d / f"{cut_key}_{k}.jpg"
        if not p.exists():
            t = t_in + (t_out - t_in) * f
            subprocess.run([ffmpeg, "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(job.source), "-frames:v", "1",
                            "-vf", f"scale=-2:{FRAME_HEIGHT}", "-q:v", "4", str(p)], check=True)
        out.append(p)
    return out


def _norm_coord(v: float, size: int) -> float | None:
    """0~1 소수면 그대로, 픽셀(1 < v ≤ size)이면 size 로, 천분율(size < v ≤ 1000)이면 1000 으로 나눈다.
    2026-09-11 실측: 같은 프롬프트에 x=196,y=331(픽셀) · x=0.511,y=484(혼합)이 섞여 왔다 — 범위 밖을 버리면 40% 가 중앙 폴백."""
    if v < 0:
        return None
    if v <= 1.0:
        return v
    if v <= size:
        return v / size
    if v <= 1000:
        return v / 1000.0
    return None


FRAMINGS = ("close", "two_shot")      # wide 는 2026-09-11 사용자 지시로 제외(참고 쇼츠는 늘 인물을 채운다)
TWO_SHOT_MARGIN = 0.14      # 두 얼굴 사이 폭에 더할 여유(소스 폭 비율) — 둘 다 얼굴이 잘리지 않게


VIEW_RANK = {"face": 2, "body": 1, "back": 0}      # 표본별 '얼마나 잘 보이는가' — 얼굴 > 전신/옆모습 > 전경 뒷모습


def _view_rank(p: dict) -> int:
    v = str(p.get("view") or "").strip().lower()
    if v in VIEW_RANK:
        return VIEW_RANK[v]
    return 2 if bool(p.get("face", True)) else 0    # view 없는 옛 응답: face 플래그로


def _pts(items, n, img_w, img_h) -> list[tuple[float, float, bool]]:
    out = []
    for p in (items or [])[:n]:
        try:
            x, y = _norm_coord(float(p.get("x")), img_w), _norm_coord(float(p.get("y")), img_h)
        except (TypeError, ValueError, AttributeError):
            continue
        if x is not None and y is not None:
            out.append((x, y, bool(p.get("face", True))))
    return out


def _ranks(items, n) -> list[int]:
    out = []
    for p in (items or [])[:n]:
        try:
            float(p.get("x")), float(p.get("y"))
        except (TypeError, ValueError, AttributeError):
            continue
        out.append(_view_rank(p))
    return out


def parse_points(raw: dict, n: int, *, img_w: int = 853, img_h: int = 480, crop_frac: float = 900 / 1920) -> dict:
    """모델 응답 → {framing, x0,x1,y0,y1, faces, note, ok, wide}. 좌표 단위 정규화 · two_shot 은 중점(둘이 크롭 폭에 안 들면 wide).
    순수 — 테스트 대상."""
    pts = _pts(raw.get("points"), n, img_w, img_h)
    partner = _pts(raw.get("partner"), n, img_w, img_h)
    framing = str(raw.get("framing") or "close").strip().lower()
    if framing not in FRAMINGS:
        framing = "close"                        # wide 등 모르는 값은 close(주인물)로
    if not pts:
        return {"framing": "close", "x0": 0.5, "x1": 0.5, "y0": 0.5, "y1": 0.5, "faces": 0, "note": "미검출→중앙", "ok": False, "wide": False}
    # 표본마다 '화면에 보이는 얼굴'을 고른다: 주인물이 그 표본에서 얼굴이 안 보이고(뒷모습·오버숄더) 상대 얼굴은 보이면 상대를 쓴다.
    # 2026-09-11 실측(지금불륜 v3 19s): 화자 안수정이 뒷머리(x 0.16)로만 잡혔는데 그 점을 따라가 얼굴이 보이는 박경희가 크롭 밖으로 나갔다.
    # 우선순위는 view(face > body > back): 주인물이 그 표본에서 상대보다 덜 보이고(뒷모습) 상대는 몸이라도 보이면 상대를 쓴다.
    rk_s, rk_p = _ranks(raw.get("points"), n), _ranks(raw.get("partner"), n)
    vis: list[tuple[float, float, bool]] = []
    swapped = 0
    for i, (x, y, f) in enumerate(pts):
        rs = rk_s[i] if i < len(rk_s) else (2 if f else 0)
        rp = rk_p[i] if i < len(rk_p) else -1
        if i < len(partner) and rp > rs and rp >= 1:
            vis.append(partner[i])
            swapped += 1
        else:
            vis.append((x, y, f))
    x0, y0, _ = pts[0]
    x1, y1, _ = pts[-1]
    wide = False
    if framing == "two_shot" and partner:
        px0, _py0, _ = partner[0]
        px1, _py1, _ = partner[-1]
        span = max(abs(x0 - px0), abs(x1 - px1)) + TWO_SHOT_MARGIN
        if span <= crop_frac:
            x0, x1 = (x0 + px0) / 2, (x1 + px1) / 2
        else:
            framing = "close"                    # 둘이 5:6 크롭에 같이 안 들어간다 → 클로즈(와이드 금지)
    if framing == "close":
        x0, y0, _ = vis[0]                       # 클로즈는 '보이는 얼굴' 점으로
        x1, y1, _ = vis[-1]
    faces = int(raw.get("faces") or 0)
    note = str(raw.get("subject") or "")[:24] + (f" (상대 얼굴 {swapped}표본)" if swapped and framing == "close" else "")
    return {"framing": framing, "x0": round(x0, 4), "x1": round(x1, 4), "y0": round(y0, 4), "y1": round(y1, 4), "faces": faces,
            "note": note, "ok": any(f for _, _, f in vis), "wide": wide, "swapped": swapped}


def _x_term(x0: float, x1: float, *, src_w: int, crop_w: int, dur: float, t0: float = 0.0) -> str:
    """따옴표 없는 x 식 한 조각(구간 시작 t0 부터 dur 동안 x0→x1)."""
    lo, hi = 0, max(0, src_w - crop_w)
    px0 = min(hi, max(lo, x0 * src_w - crop_w / 2))
    px1 = min(hi, max(lo, x1 * src_w - crop_w / 2))
    if abs(px1 - px0) < PAN_MIN_PX or dur <= 0.2:
        return f"{int(round((px0 + px1) / 2))}"
    tt = "t" if t0 <= 0 else f"(t-{t0:.3f})"
    return f"clip({px0:.1f}+({px1:.1f}-{px0:.1f})*{tt}/{dur:.3f},{lo},{hi})"


def crop_x_expr(x0: float, x1: float, *, src_w: int, crop_w: int, dur: float) -> str:
    """정규화 중심 x0→x1 → ffmpeg crop x 식(소스 픽셀). 이동이 작으면 고정, 크면 컷 길이에 걸쳐 선형 팬. 순수 — 테스트 대상."""
    term = _x_term(x0, x1, src_w=src_w, crop_w=crop_w, dur=dur)
    return term if term.isdigit() else f"'{term}'"


def crop_x_expr_segs(segs: list[dict], *, src_w: int, crop_w: int) -> str:
    """샷별 프레이밍 구간(컷 상대 t0~t1 · x0→x1)을 하나의 crop x 식으로 — 구간 경계에서 즉시 점프(같은 샷 안에서만 팬).
    구간이 하나면 crop_x_expr 와 같다. 순수 — 테스트 대상."""
    if not segs:
        return "0"
    if len(segs) == 1:
        s = segs[0]
        return crop_x_expr(s["x0"], s["x1"], src_w=src_w, crop_w=crop_w, dur=s["t1"] - s["t0"])
    terms = [_x_term(s["x0"], s["x1"], src_w=src_w, crop_w=crop_w, dur=s["t1"] - s["t0"], t0=s["t0"]) for s in segs]
    expr = terms[-1]
    for s, term in zip(reversed(segs[:-1]), reversed(terms[:-1])):
        expr = f"if(lt(t,{s['t1']:.3f}),{term},{expr})"
    return f"'{expr}'"


SPLIT_MIN_SEG_SEC = 0.5      # 샷 경계로 나눈 조각이 이보다 짧으면 앞 조각에 붙인다(표본 3장을 뽑을 길이가 안 된다)


def split_by_shots(t_in: float, t_out: float, scene_cuts: list[float] | None, *, min_seg: float = SPLIT_MIN_SEG_SEC) -> list[tuple[float, float]]:
    """컷 안에 샷 경계가 있으면 샷별 구간으로 나눈다(절대초). 2026-09-11 실측: 4.6s 대사 컷이 552.8 에서 앵글이 바뀌는데 표본 3장을
    한 인물로 묶어 선형 팬 하나를 만들었다 — 두 앵글이 섞여 첫 앵글에서 사람이 안 잡혔다. 순수 — 테스트 대상."""
    bounds = [t_in]
    for b in sorted(scene_cuts or []):
        if b - bounds[-1] >= min_seg and t_out - b >= min_seg:
            bounds.append(ms3(b))
    bounds.append(t_out)
    return [(a, b) for a, b in zip(bounds, bounds[1:])]


def frame_at(cut: dict, t_rel: float) -> tuple[float, float]:
    """컷 상대 시각의 주인물 중심(정규화 x, y) — 샷 구간이 있으면 그 구간, 없으면 컷 값. 효과자막 얼굴 회피가 쓴다."""
    segs = cut.get("frame_segs") or []
    for s in segs:
        if s["t0"] - 1e-6 <= t_rel < s["t1"] + 1e-6:
            return (s["x0"] + s["x1"]) / 2, (s["y0"] + s["y1"]) / 2
    if segs:
        s = segs[-1]
        return (s["x0"] + s["x1"]) / 2, (s["y0"] + s["y1"]) / 2
    return (cut.get("frame_x0", 0.5) + cut.get("frame_x1", 0.5)) / 2, (cut.get("frame_y0", 0.5) + cut.get("frame_y1", 0.5)) / 2


def _reparse_cached(cache: dict, key: str, n: int) -> dict | None:
    """캐시에 모델 원응답이 있으면 지금 규칙(parse_points)으로 다시 해석한다 — 해석 규칙을 고쳤을 때 재호출 없이 반영."""
    f = cache.get(key)
    if not f or not isinstance(f.get("raw"), dict):
        return f
    iw, ih = (f.get("img") or [853, 480])
    res = parse_points(f["raw"], n, img_w=int(iw), img_h=int(ih))
    res["raw"], res["img"] = f["raw"], [iw, ih]
    return res


def frame_cuts(job: Job, gemini: Gemini, table: dict, *, scene_cuts: list[float] | None = None, workers: int = 4) -> dict:
    """테이블의 모든 컷에 주인물 x 를 붙인다(캐시 `framing_v{n}.json`, 구간 키 기준 — 바뀐 구간만 새로 부른다).
    컷 안에 샷 경계가 있으면 샷별로 따로 판정해 `frame_segs`(컷 상대 t0/t1 + x0/x1/y0/y1)로 싣고, 렌더는 구간별 crop x 식을 쓴다."""
    n = table["version"]["n"]
    name = f"framing_v{n}.json"
    cache: dict = job.load(name) if job.has(name) else {}
    for key in list(cache):
        cache[key] = _reparse_cached(cache, key, len(SAMPLE_FRACS)) or cache[key]
    cuts = []
    n_split = 0
    for r in table["rows"]:
        ctx = {"N": f"[내레이션 밑 리액션/상황 컷] 내레이션: \"{r.get('text','')}\"",
               "S": f"[대사 립싱크 컷] {r.get('speaker','')}: \"{r.get('text','')[:60]}\"",
               "A": f"[현장음 액션 컷] {r.get('text','')}"}[r["mode"]]
        for c in r["cuts"]:
            segs = split_by_shots(c["in"], c["out"], scene_cuts)
            n_split += len(segs) > 1
            for s0, s1 in segs:
                cuts.append((f"{s0:.3f}-{s1:.3f}", s0, s1, ctx + (f" · 컷 설명: {c.get('desc','')}" if c.get("desc") else "")))
    todo = [c for c in cuts if c[0] not in cache]

    def one(key: str, t_in: float, t_out: float, ctx: str) -> tuple[str, dict]:
        frames = _extract_frames(job, key, t_in, t_out)
        try:
            raw = gemini.images_json(FRAMING_PROMPT.format(n=len(frames), context=ctx), frames, kind="framing", thinking="low")
            try:
                from PIL import Image
                with Image.open(frames[0]) as im:
                    iw, ih = im.size
            except Exception:  # noqa: BLE001
                iw, ih = 853, 480
            res = parse_points(raw, len(frames), img_w=iw, img_h=ih)
            res["raw"], res["img"] = raw, [iw, ih]
        except Exception as e:  # noqa: BLE001 — 크롭은 연출이라 실패해도 본편(중앙 크롭)을 막지 않는다
            res = {"framing": "close", "x0": 0.5, "x1": 0.5, "y0": 0.5, "y1": 0.5, "faces": 0, "note": f"실패 {type(e).__name__}", "ok": False, "wide": False}
        return key, res

    if todo:
        job.log(f"[framing] 구간 {len(cuts)}개(샷 분할 컷 {n_split}) 중 {len(todo)}개 화면 잡기 판정(Gemini · 구간당 프레임 {len(SAMPLE_FRACS)}장 + 행 문맥)")
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for key, res in ex.map(lambda c: one(*c), todo):
                cache[key] = res
    job.save(name, cache)                                    # 재해석 결과도 저장(raw 는 그대로)
    used: list[dict] = []
    for r in table["rows"]:
        for c in r["cuts"]:
            segs = []
            for s0, s1 in split_by_shots(c["in"], c["out"], scene_cuts):
                f = cache.get(f"{s0:.3f}-{s1:.3f}") or {}
                used.append(f)
                segs.append({"t0": ms3(s0 - c["in"]), "t1": ms3(s1 - c["in"]), "x0": f.get("x0", 0.5), "x1": f.get("x1", 0.5),
                             "y0": f.get("y0", 0.5), "y1": f.get("y1", 0.5), "mode": f.get("framing", "close"), "note": f.get("note", "")})
            c["frame_segs"] = segs
            c["frame_x0"], c["frame_x1"] = segs[0]["x0"], segs[-1]["x1"]     # 구간이 하나면 종전과 같은 값
            c["frame_y0"], c["frame_y1"] = segs[0]["y0"], segs[-1]["y1"]
            c["frame_wide"] = False
            c["frame_mode"] = segs[0]["mode"]
            c["frame_note"] = " / ".join(s["note"] for s in segs if s["note"])
    n_ok = sum(1 for f in used if f.get("ok"))
    n_fallback = sum(1 for f in used if str(f.get("note", "")).startswith(("미검출", "실패")))
    n_two = sum(1 for f in used if f.get("framing") == "two_shot")
    n_swap = sum(1 for f in used if f.get("swapped"))
    pans = sum(1 for f in used if abs(f.get("x1", .5) - f.get("x0", .5)) * 1920 >= PAN_MIN_PX)
    job.log(f"[framing] 클로즈 {len(used) - n_two} · 투샷 {n_two} / {len(used)}구간(샷 분할 컷 {n_split}) · 얼굴 기준 {n_ok} · "
            f"상대 얼굴로 대체 {n_swap} · 중앙 폴백 {n_fallback} · 팬 {pans}")
    job.record_step(f"framing_v{n}", segments=len(used), split_cuts=n_split, faces=n_ok, fallback=n_fallback, pans=pans, two_shot=n_two, swapped=n_swap)
    return cache
