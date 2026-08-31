"""M8-A — exception 경계 정밀 2-pass. 발주서 orders/v3-m8-uncertainty-refine.md.

에이전트 방식("의심되면 확대해 다시 본다")의 결정적 이식: **트리거 명시**(Stage 1
이 잡은 exception 경계 각각 + exception 전무 편의 말미 180s), **호출 상한**
(편당 Flash ≤ MAX_PROBES), **scene cut 스냅**(모델은 후보 id 만 고른다 — 시각
무출력 규율 그대로). 무제한 재량 루프 금지.

계기(M7 실측 3건): 가왕쇼 teaser 50초 지각 — 0.5fps·360p 훑기에는 예고 도입부
(본편형 풀스크린 하이라이트)의 판별 근거가 없다. 신병4·포핸즈2 경계 과잉(59s·96s).
10fps/480p 국소 창은 그 신호(장식 프레임 등장 컷·서사 단절)를 본다.

정밀 실패는 원판정 유지 — 2-pass 가 본편을 죽이면 안 된다(소진·후보 없음·none).
경계 이동 시 sequences 재타일링으로 커버리지(빈틈·겹침 0)를 유지한다.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from app.modules.ffmpeg_utils import find_ffmpeg_command
from app.modules.gemini_client import _extract_json_from_markdown, _loads_first_json
from app.v3 import schemas
from app.v3.seq_analyze import MAX_REASKS

WINDOW_BACK_SEC = 90.0     # 경계 시작 쪽으로 — 가왕쇼 실측 49.5s 지각을 덮는 크기
WINDOW_FWD_SEC = 30.0
TAIL_WINDOW_SEC = 180.0    # exception 전무 편의 말미 검사 창
MAX_PROBES = 5             # 경계 프로브 상한
MAX_VERIFIES = 3           # zone 실체 검증 상한 — 편당 Flash 총 ≤8(발주 개정)
VERIFY_SAMPLE_SEC = 60.0   # zone 중앙 표본 — 머리는 본편형 도입이라 애매(가왕쇼 실측)
PROBE_WINDOW_CAP_SEC = 180.0
FLASH_BUDGET = 8           # 편당 총 Flash 호출 예산(재질의 포함 — 강제·감사)
PROBE_HEIGHT = 480
PROBE_FPS = 10
PROBE_SAMPLE_FPS = 6.0     # Gemini 표본 fps(2026-08-31 사용자 설정 — 종전 기본 1fps)

ZONE_DESC = {"intro": "타이틀/인트로", "recap": "지난 화 요약",
             "teaser": "다음 화 예고", "credit": "엔딩 크레딧",
             "end": "방송 종료 꼬리"}


# ── 순수 로직 ───────────────────────────────────────────────────────────────

def boundary_probe_windows(exception_sector: dict, duration: float) -> list[dict]:
    """트리거 목록(명시) — 경계마다 하나, exception 전무면 말미 1개. 순수.

    시작 경계 창은 [b−90, b+30](지각을 뒤로 넓게), 끝 경계 창은 [b−30, b+90].
    러닝타임 양 끝(0·duration)에 붙은 경계는 검사 대상이 아니다(움직일 곳이 없다)."""
    probes: list[dict] = []
    zones = []
    for key, z in (exception_sector or {}).items():
        if isinstance(z, dict) and z.get("start") is not None:
            zones.append((key, schemas.parse_ts(z["start"]), schemas.parse_ts(z["end"])))
    for key, s, e in zones:
        # 창은 **zone 전체 + 본편 쪽 여유**를 덮는다(포핸즈2 실측: 진짜 경계 42.5 가
        # [e−90, e+30] 창 밖 — 과잉 zone 은 경계가 zone 안쪽 깊숙이 있다). 상한 180s.
        # 창 = **원경계 중심 ±90s** — 원경계는 항상 창 안(리뷰 확정: zone 기준
        # 캡은 큰 zone 에서 원경계를 창 밖으로 밀어 축소만 가능하게 했다).
        # zone 깊은 내부의 진짜 경계는 부분 표본 재프로브가 맡는다(포핸즈2 실증
        # — 138.5 창 밖의 42.5 를 재프로브가 찾았다). 캡 180 은 자동 충족.
        if s > 0.5:
            probes.append({"zone": key, "edge": "start",
                           "t0": max(0.0, s - WINDOW_BACK_SEC),
                           "t1": min(duration, s + WINDOW_BACK_SEC), "orig": s})
        if e < duration - 0.5:
            probes.append({"zone": key, "edge": "end",
                           "t0": max(0.0, e - WINDOW_BACK_SEC),
                           "t1": min(duration, e + WINDOW_BACK_SEC), "orig": e})
    if not zones and duration > TAIL_WINDOW_SEC / 2:
        probes.append({"zone": "tail", "edge": "start",
                       "t0": max(0.0, duration - TAIL_WINDOW_SEC),
                       "t1": duration, "orig": None})
    return probes                       # 상한 적용·탈락 기록은 호출자(감사 의무)


def scene_cut_candidates(grid: dict, t0: float, t1: float) -> list[dict]:
    """창 안 장면 전환 후보 — 모델이 고를 id 목록(창 상대초 병기). 순수."""
    out = []
    for c in grid.get("scene_cuts") or []:
        c = float(c)
        if t0 + 0.2 <= c <= t1 - 0.2:
            out.append({"id": f"c{len(out):02d}", "t": round(c, 3),
                        "rel": round(c - t0, 2)})
    return out


def apply_boundary(exception_sector: dict, probe: dict, new_t: float,
                   duration: float) -> dict:
    """경계 이동을 exception_sector 에 적용(불변 입력 — 사본 반환). 순수.

    tail 프로브는 새 teaser 를 만든다(발견 컷 ~ 러닝타임 끝)."""
    out = {k: (dict(v) if isinstance(v, dict) else v)
           for k, v in (exception_sector or {}).items()}
    ts = schemas.format_ts(new_t)
    if probe["zone"] == "tail":
        out["teaser"] = {"start": ts, "end": schemas.format_ts(duration)}
        return out
    zone = out.get(probe["zone"])
    if not isinstance(zone, dict):
        return out
    zone["start" if probe["edge"] == "start" else "end"] = ts
    s, e = schemas.parse_ts(zone["start"]), schemas.parse_ts(zone["end"])
    original = {k: (dict(v) if isinstance(v, dict) else v)
                for k, v in (exception_sector or {}).items()}
    if e <= s:                       # 역전 방어 — 이동 기각(원판정 유지)
        return original
    # 인접 zone 침범 방어(리뷰 확정): 프로브 창이 이웃 zone 위로 뻗을 수 있어
    # 이동 결과가 다른 zone 과 겹치면 기각 — 커버리지 계약(겹침 0)이 우선
    ivs = sorted((schemas.parse_ts(z["start"]), schemas.parse_ts(z["end"]))
                 for z in out.values()
                 if isinstance(z, dict) and z.get("start") is not None)
    for (a0, a1), (b0, b1) in zip(ivs, ivs[1:]):
        if b0 < a1 - 1e-9:
            return original
    return out


def retile_sequences(stage1_doc: dict, new_exception: dict,
                     duration: float) -> dict:
    """경계 이동 후 sequences/chunks 를 비-exception 타임라인에 재타일링. 순수.

    규칙: 각 sequence 구간 ∩ 허용 구간(비-exception). 잘려서 비면 sequence 제거,
    chunk 는 sequence 안으로 클램프(비면 제거, 전멸이면 sequence 전체 = chunk 1개).
    번호는 순서대로 재부여. 커버리지(빈틈 0)는 허용 구간을 sequence 들이 원래
    덮고 있었다는 전제에서 유지된다 — 검증은 호출자(schemas 커버리지 검사)."""
    ex_ivs = []
    for _k, z in (new_exception or {}).items():
        if isinstance(z, dict) and z.get("start") is not None:
            ex_ivs.append((schemas.parse_ts(z["start"]), schemas.parse_ts(z["end"])))
    ex_ivs = sorted(ex_ivs)

    def clip_to_allowed(s: float, e: float) -> list[tuple[float, float]]:
        parts = [(s, e)]
        for a, b in ex_ivs:
            nxt = []
            for p0, p1 in parts:
                if b <= p0 or p1 <= a:
                    nxt.append((p0, p1))
                    continue
                if p0 < a:
                    nxt.append((p0, a))
                if b < p1:
                    nxt.append((b, p1))
            parts = nxt
        return [(p0, p1) for p0, p1 in parts if p1 - p0 > 0.05]

    new_seqs = []
    for sq in stage1_doc.get("sequences") or []:
        s = schemas.parse_ts(sq["time"]["start"])
        e = schemas.parse_ts(sq["time"]["end"])
        for p0, p1 in clip_to_allowed(s, e):
            chunks = []
            for ch in sq.get("chunks") or []:
                c0 = max(schemas.parse_ts(ch["time"]["start"]), p0)
                c1 = min(schemas.parse_ts(ch["time"]["end"]), p1)
                if c1 - c0 > 0.05:
                    chunks.append({**ch, "time": {"start": schemas.format_ts(c0),
                                                  "end": schemas.format_ts(c1)}})
            if not chunks:
                chunks = [{"number": 0, "meanings": [],
                           "time": {"start": schemas.format_ts(p0),
                                    "end": schemas.format_ts(p1)}}]
            # chunk 타일링 강제: 첫 시작=구간 시작 · 끝=구간 끝 · 인접 맞물림
            chunks[0]["time"]["start"] = schemas.format_ts(p0)
            chunks[-1]["time"]["end"] = schemas.format_ts(p1)
            # 10분 상한 방어 — 클램프·이전 보수로 늘어난 chunk 는 균등 분할
            split: list[dict] = []
            for ch in chunks:
                c0 = schemas.parse_ts(ch["time"]["start"])
                c1 = schemas.parse_ts(ch["time"]["end"])
                if c1 - c0 <= schemas.CHUNK_MAX_SEC:
                    split.append(ch)
                    continue
                n = int((c1 - c0) // schemas.CHUNK_MAX_SEC) + 1
                for j in range(n):
                    a = c0 + (c1 - c0) * j / n
                    b = c0 + (c1 - c0) * (j + 1) / n
                    split.append({**ch, "meanings": [],
                                  "time": {"start": schemas.format_ts(a),
                                           "end": schemas.format_ts(b)}})
            chunks = split
            for i, ch in enumerate(chunks):
                ch["number"] = i
            new_seqs.append({**sq, "time": {"start": schemas.format_ts(p0),
                                            "end": schemas.format_ts(p1)},
                             "chunks": chunks})
    # 해방 구간 메움(리뷰 확정 critical): zone 축소·폐기로 비-exception 이 된
    # 구간은 어떤 sequence 도 안 덮는다(Stage 1 은 여집합만 덮었으므로) — 인접
    # sequence 를 확장하거나(우선) 새 sequence 로 채워 빈틈 0 을 복원한다.
    new_seqs.sort(key=lambda q: schemas.parse_ts(q["time"]["start"]))
    covered = sorted([(schemas.parse_ts(q["time"]["start"]),
                       schemas.parse_ts(q["time"]["end"])) for q in new_seqs]
                     + ex_ivs)
    gaps = []
    cursor = 0.0
    for a, b in covered:
        if a - cursor > schemas.COVERAGE_EPS_SEC:
            gaps.append((cursor, a))
        cursor = max(cursor, b)
    if duration - cursor > schemas.COVERAGE_EPS_SEC:
        gaps.append((cursor, duration))
    for g0, g1 in gaps:
        prev = next((q for q in new_seqs
                     if abs(schemas.parse_ts(q["time"]["end"]) - g0) < 0.05), None)
        nxt = next((q for q in new_seqs
                    if abs(schemas.parse_ts(q["time"]["start"]) - g1) < 0.05), None)
        def _gap_chunks(a: float, b: float) -> list[dict]:
            """갭 → ≤10분 chunk 목록 — 기존 chunk 를 늘리면 상한(600s)을 뚫는다
            (포핸즈2 보수 실측: 648.2s chunk)."""
            out = []
            cur = a
            while b - cur > 1e-9:
                nxt_t = min(b, cur + schemas.CHUNK_MAX_SEC)
                out.append({"number": 0, "meanings": [],
                            "time": {"start": schemas.format_ts(cur),
                                     "end": schemas.format_ts(nxt_t)}})
                cur = nxt_t
            return out

        if prev is not None:
            prev["time"]["end"] = schemas.format_ts(g1)
            prev["chunks"].extend(_gap_chunks(g0, g1))
            for i, ch in enumerate(prev["chunks"]):
                ch["number"] = i
        elif nxt is not None:
            nxt["time"]["start"] = schemas.format_ts(g0)
            nxt["chunks"][:0] = _gap_chunks(g0, g1)
            for i, ch in enumerate(nxt["chunks"]):
                ch["number"] = i
        else:
            chunks = _gap_chunks(g0, g1)
            for i, ch in enumerate(chunks):
                ch["number"] = i
            new_seqs.append({"number": 0, "content": "(정밀 재관찰로 회수된 구간)",
                             "time": {"start": schemas.format_ts(g0),
                                      "end": schemas.format_ts(g1)},
                             "chunks": chunks})
    new_seqs.sort(key=lambda q: schemas.parse_ts(q["time"]["start"]))
    for i, sq in enumerate(new_seqs):
        sq["number"] = i
    return {**stage1_doc, "sequences": new_seqs,
            "exception_sector": new_exception}


def validate_probe_response(resp: Any, candidates: list[dict]) \
        -> tuple[str | None, list[str]]:
    """모델 응답 → (선택 id | 'none', 반려 사유). 순수."""
    if not isinstance(resp, dict):
        return None, ["응답이 객체가 아니다"]
    b = resp.get("boundary")
    if b == "none":
        return "none", []
    known = {c["id"] for c in candidates}
    if not isinstance(b, str) or b not in known:
        return None, [f"boundary 는 후보 id 또는 'none': {b!r} (후보: {sorted(known)[:8]})"]
    return b, []


def verify_sample_window(s: float, e: float) -> tuple[float, float]:
    """zone 실체 검증용 표본 창 — **중앙** 표본. 순수.

    예고/크레딧 몽타주는 본편형 하이라이트로 문을 열므로(가왕쇼 실측) 머리 표본은
    구조적으로 애매하다 — 중앙이 장식 프레임·스태프롤 같은 확정 신호를 담는다."""
    if e - s <= VERIFY_SAMPLE_SEC:
        return s, e
    mid = (s + e) / 2
    return mid - VERIFY_SAMPLE_SEC / 2, mid + VERIFY_SAMPLE_SEC / 2


def validate_verify_response(resp) -> tuple[str | None, list[str]]:
    """실체 검증 응답 → ('main'|'exception'|None, 반려). 순수."""
    if not isinstance(resp, dict):
        return None, ["응답이 객체가 아니다"]
    k = resp.get("kind")
    if k in ("main", "exception"):
        return k, []
    return None, [f"kind 는 main|exception: {k!r}"]


# ── 프로브 실행 ─────────────────────────────────────────────────────────────

PROBE_PROMPT = """당신은 방송 편집 검수자다. 첨부한 클립은 원본의 {t0}~{t1} 구간(창 안 0초 = 원본 {t0})이다. 이 창 어딘가에 **{desc}와 본편의 경계**가 있는지 정밀하게 찾아라.

판별 신호: 콜라주/장식 프레임 테두리, 스태프롤·제작진 자막, "다음 이야기" 문구, 본편 서사와 단절된 빠른 몽타주(장소·의상이 컷마다 바뀜), 전용 카드.
⚠ 예고/크레딧 몽타주는 종종 **본편처럼 보이는 풀스크린 하이라이트 컷으로 문을 연다** — 경계는 장식이 뜨는 순간이 아니라 본편 서사가 끝나는 첫 컷이다.

## 경계 후보 (클립 내 상대초 | id) — 이 중에서만 고른다
{cands}

## 출력 (JSON 만)
{{"boundary": "c03"}}  — 경계가 이 창에 없으면 {{"boundary": "none"}}"""

VERIFY_PROMPT = """당신은 방송 편집 검수자다. 첨부한 클립은 원본 {t0}~{t1} 구간이다. 이 클립의 **주 내용**이 본편(스토리 진행)인가, {desc}인가만 판정하라.

{desc} 신호: 콜라주/장식 프레임 테두리, 스태프롤·제작진 자막, "다음 이야기" 문구, 전용 카드, 본편과 단절된 빠른 몽타주.
본편 신호: 인물 대화·사건이 실시간으로 진행, 위 장식 요소 없음. 어둡거나 정적인 연출 신도 본편이다 — 분위기만으로 인트로/크레딧이라 단정하지 마라.

## 출력 (JSON 만)
{{"kind": "exception"}} 또는 {{"kind": "main"}}"""


def _cut_probe_clip(ffmpeg: str, video: Path, t0: float, t1: float,
                    out: Path) -> None:
    subprocess.run(
        [ffmpeg, "-y", "-ss", f"{t0:.3f}", "-to", f"{t1:.3f}", "-i", str(video),
         "-vf", f"scale=-2:{PROBE_HEIGHT},fps={PROBE_FPS}",
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-an",
         str(out)], check=True, capture_output=True)


def _call_probe(gemini, clip: Path, prompt: str) -> dict:
    from app.v3.seq_analyze import _upload_video
    types = gemini.types
    uploaded = _upload_video(gemini, clip, log=lambda *a: None)
    try:
        part = types.Part(file_data=types.FileData(file_uri=uploaded.uri,
                                                   mime_type="video/mp4"),
                          video_metadata=types.VideoMetadata(fps=PROBE_SAMPLE_FPS))
        resp = gemini.client.models.generate_content(
            model=gemini.config.flash_model_name,       # 국소 창 — Flash 로 충분
            contents=[part, prompt],
            config=types.GenerateContentConfig(
                temperature=0.0, response_mime_type="application/json",
                max_output_tokens=1024))
        text = _extract_json_from_markdown(resp.text or "")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            obj, _rest = _loads_first_json(text)
            if isinstance(obj, dict):
                return obj
            raise ValueError(f"프로브 JSON 파싱 실패: {text[:120]!r}")
    finally:
        try:
            gemini.client.files.delete(name=uploaded.name)
        except Exception:  # noqa: BLE001
            pass


def refine_exception(gemini, stage1_doc: dict, grid: dict, video_path: Path,
                     work_dir: Path, *, log=print) -> tuple[dict, dict]:
    """경계 정밀 2-pass 실행 → (재타일링된 stage1 문서, 감사). 실패는 원판정 유지."""
    duration = float((grid.get("source") or {}).get("duration_sec") or 0)
    exception = stage1_doc.get("exception_sector") or {}
    all_probes = boundary_probe_windows(exception, duration)
    probes = all_probes[:MAX_PROBES]
    audit: dict[str, Any] = {"probes": [], "moved": 0, "flash_calls": 0}
    for dropped in all_probes[MAX_PROBES:]:   # 조용한 절단 금지(리뷰 확정)
        audit["probes"].append({**{k: dropped[k] for k in ("zone", "edge", "orig")},
                                "result": "상한 초과 — 미검사"})
    if not probes:
        return stage1_doc, audit
    ffmpeg = find_ffmpeg_command("ffmpeg")
    work_dir.mkdir(parents=True, exist_ok=True)

    def call_probe_budgeted(clip, prompt):
        """편당 Flash 총예산 강제(리뷰 확정: 재질의 합산 최악 21콜) — 소진 시
        RuntimeError 로 호출자에 '원판정 유지' 경로를 태운다."""
        if audit["flash_calls"] >= FLASH_BUDGET:
            raise RuntimeError("Flash 예산 소진 — 원판정 유지")
        audit["flash_calls"] += 1
        return _call_probe(gemini, clip, prompt)

    new_exception = exception
    for i, probe in enumerate(probes):
        cands = scene_cut_candidates(grid, probe["t0"], probe["t1"])
        rec: dict[str, Any] = {**{k: probe[k] for k in ("zone", "edge", "t0", "t1", "orig")},
                               "candidates": len(cands)}
        if not cands:
            rec["result"] = "후보 없음 — 원판정 유지"
            audit["probes"].append(rec)
            continue
        clip = work_dir / f"probe_{i}_{probe['zone']}_{probe['edge']}.mp4"
        t0 = time.time()
        try:
            _cut_probe_clip(ffmpeg, video_path, probe["t0"], probe["t1"], clip)
        except subprocess.CalledProcessError as e:
            rec["result"] = (f"재단 실패 — 원판정 유지: "
                             f"{(e.stderr or b'')[-120:]!r}")
            audit["probes"].append(rec)
            continue
        desc = ZONE_DESC.get(probe["zone"], "예고/크레딧")
        cand_lines = "\n".join(f"- {c['rel']:.1f}s | {c['id']}" for c in cands)
        chosen: str | None = None
        reject = ""
        for attempt in range(1 + MAX_REASKS):
            prompt = PROBE_PROMPT.format(
                t0=schemas.format_ts(probe["t0"]), t1=schemas.format_ts(probe["t1"]),
                desc=desc, cands=cand_lines) + (f"\n\n⚠ 직전 반려: {reject}" if reject else "")
            try:
                resp = call_probe_budgeted(clip, prompt)
                chosen, problems = validate_probe_response(resp, cands)
            except (ValueError, Exception) as e:  # noqa: BLE001 — 프로브 실패 = 원판정
                chosen, problems = None, [f"호출 오류: {e}"]
            if chosen is not None:
                break
            reject = "; ".join(problems[:3])
        rec["elapsed"] = round(time.time() - t0, 1)
        if chosen is None or chosen == "none":
            rec["result"] = "none(경계 없음)" if chosen == "none" else "재질의 소진 — 원판정 유지"
            audit["probes"].append(rec)
            continue
        new_t = next(c["t"] for c in cands if c["id"] == chosen)
        moved = round(new_t - probe["orig"], 3) if probe["orig"] is not None else None
        # 오염 방지 비대칭(리뷰·실사고 2호): zone 을 **줄이는** 이동은 해방 구간의
        # 실체 검증(main 판정) 없이는 기각한다 — 가왕쇼 재실행 실측: teaser.end
        # 프로브가 예고 속 텍스트 카드를 경계로 오인해 예고 후반 45.5s 를 본편으로
        # 해방(채점 FAIL 재현). 확대 방향은 손실 위험뿐이라 그대로 간다.
        shrink_iv = None
        if probe["orig"] is not None:
            if probe["edge"] == "start" and new_t > probe["orig"] + 0.01:
                shrink_iv = (probe["orig"], new_t)
            elif probe["edge"] == "end" and new_t < probe["orig"] - 0.01:
                shrink_iv = (new_t, probe["orig"])
        if shrink_iv and shrink_iv[1] - shrink_iv[0] >= 8.0:
            vclip = work_dir / f"shrinkcheck_{probe['zone']}_{probe['edge']}.mp4"
            kind = None
            try:
                _cut_probe_clip(ffmpeg, video_path, shrink_iv[0], shrink_iv[1], vclip)
                vresp = call_probe_budgeted(vclip, VERIFY_PROMPT.format(
                    t0=schemas.format_ts(shrink_iv[0]),
                    t1=schemas.format_ts(shrink_iv[1]),
                    desc=ZONE_DESC.get(probe["zone"], "예고/크레딧")))
                kind, _vp = validate_verify_response(vresp)
            except Exception:  # noqa: BLE001 — 판정 불가 = 기각(보수)
                kind = None
            if kind != "main":
                rec["result"] = (f"축소 기각(해방 구간 실체={kind or '판정 불가'}): "
                                 f"{chosen}={new_t}")
                audit["probes"].append(rec)
                continue
        applied = apply_boundary(new_exception, probe, new_t, duration)
        if probe["orig"] is not None and abs(new_t - probe["orig"]) < 0.01:
            rec["result"] = f"원판정 확인(동일 컷 {chosen}={new_t})"
        elif applied == new_exception and probe["zone"] != "tail":
            rec["result"] = f"이동 기각(구간 역전 방어): {chosen}={new_t}"
        else:
            new_exception = applied
            audit["moved"] += 1
            rec["result"] = {"chosen": chosen, "new_t": new_t, "moved_sec": moved}
            log(f"  [v3/refine] {probe['zone']}.{probe['edge']} "
                f"{probe['orig']} → {new_t} (이동 {moved}s)")
        audit["probes"].append(rec)

    # ── zone 실체 검증 — 경계 프로브가 오탐을 '확인'해버리는 유형 방어 ─────
    # (신병4 실측: 본편 장면을 credit 판정 → 경계 프로브는 none, 원판정 잔존.
    #  포핸즈2 실측: 본편 오프닝을 intro 138.5 판정 → 프로브가 동일 컷 재확인.)
    # 중앙 표본을 고해상으로 보고 main 이면 zone 폐기. 실패·반려 소진은 유지(보수).
    verified = 0
    for key in ("intro", "recap", "teaser", "credit", "end"):
        if verified >= MAX_VERIFIES:
            break
        zone = new_exception.get(key)
        if not isinstance(zone, dict) or zone.get("start") is None:
            continue
        zs, ze = schemas.parse_ts(zone["start"]), schemas.parse_ts(zone["end"])
        if ze - zs < 8.0:
            continue                       # 짧은 카드류 — 표본 판정이 더 위험
        if ze - zs > VERIFY_SAMPLE_SEC + 1.0:
            # 긴 zone 은 전체 폐기 불가(보수) — 대신 **닻 반대쪽 30s 표본**을 검증해
            # main 이면 경계를 좁힌 창에서 1회 재프로브(포핸즈2 실측: intro 를
            # 138.5 로 과잉 판정, 후반 표본 42.5~138.5 는 명백한 본편).
            head_anchored = zs <= 0.5
            if head_anchored:
                v0, v1 = max(zs, ze - 35.0), ze - 5.0
            elif ze >= duration - 0.5:
                v0, v1 = zs + 5.0, min(ze, zs + 35.0)
            else:
                continue                   # 중간 zone 은 부분 표본 근거 불충분 — 유지
            clip = work_dir / f"verify_part_{key}.mp4"
            rec = {"zone": key, "edge": "verify_part",
                   "t0": round(v0, 3), "t1": round(v1, 3)}
            try:
                _cut_probe_clip(ffmpeg, video_path, v0, v1, clip)
            except subprocess.CalledProcessError:
                audit["probes"].append({**rec, "result": "재단 실패 — 유지"})
                continue
            verified += 1
            kind = None
            try:
                resp = call_probe_budgeted(clip, VERIFY_PROMPT.format(
                    t0=schemas.format_ts(v0), t1=schemas.format_ts(v1),
                    desc=ZONE_DESC.get(key, "예고/크레딧")))
                kind, _pr = validate_verify_response(resp)
            except Exception:  # noqa: BLE001
                kind = None
            if kind != "main":
                audit["probes"].append({**rec, "result": f"유지({kind or '판정 불가'})"})
                continue
            # 표본이 본편 — 좁힌 창에서 경계 1회 재프로브(main 표본 구간 제외)
            if head_anchored:
                w1 = v0 + 5.0
                w0, edge = max(zs, w1 - PROBE_WINDOW_CAP_SEC), "end"
            else:
                w0 = v1 - 5.0
                w1, edge = min(ze, w0 + PROBE_WINDOW_CAP_SEC), "start"
            cands2 = scene_cut_candidates(grid, w0, w1)
            rec["result"] = ("표본 본편 — 재프로브" if cands2
                             else "표본 본편 — 경계 후보 없음, 유지")
            audit["probes"].append(rec)
            if not cands2:
                continue
            clip2 = work_dir / f"reprobe_{key}.mp4"
            try:
                _cut_probe_clip(ffmpeg, video_path, w0, w1, clip2)
                resp2 = call_probe_budgeted(clip2, PROBE_PROMPT.format(
                    t0=schemas.format_ts(w0), t1=schemas.format_ts(w1),
                    desc=ZONE_DESC.get(key, "예고/크레딧"),
                    cands="\n".join(f"- {c['rel']:.1f}s | {c['id']}" for c in cands2)))
                chosen2, _pr2 = validate_probe_response(resp2, cands2)
            except Exception:  # noqa: BLE001
                chosen2 = None
            rec2 = {"zone": key, "edge": f"reprobe_{edge}",
                    "t0": round(w0, 3), "t1": round(w1, 3)}
            if chosen2 and chosen2 != "none":
                new_t = next(c["t"] for c in cands2 if c["id"] == chosen2)
                shrunk = (new_t < ze - 0.01) if head_anchored else (new_t > zs + 0.01)
                if shrunk:
                    new_exception = apply_boundary(
                        new_exception, {"zone": key, "edge": edge, "orig": None},
                        new_t, duration)
                    audit["moved"] += 1
                    rec2["result"] = {"chosen": chosen2, "new_t": new_t}
                    log(f"  [v3/refine] {key} 부분 표본 축소: {edge} → {new_t}")
                else:
                    rec2["result"] = "축소 아님 — 기각"
            else:
                rec2["result"] = "재프로브 경계 없음 — 유지"
            audit["probes"].append(rec2)
            continue
        v0, v1 = verify_sample_window(zs, ze)
        clip = work_dir / f"verify_{key}.mp4"
        rec = {"zone": key, "edge": "verify", "t0": round(v0, 3), "t1": round(v1, 3)}
        t0v = time.time()
        try:
            _cut_probe_clip(ffmpeg, video_path, v0, v1, clip)
        except subprocess.CalledProcessError as e:
            rec["result"] = f"재단 실패 — 유지: {(e.stderr or b'')[-120:]!r}"
            audit["probes"].append(rec)
            continue
        verified += 1
        kind = None
        reject = ""
        for _attempt in range(2):
            prompt = VERIFY_PROMPT.format(
                t0=schemas.format_ts(v0), t1=schemas.format_ts(v1),
                desc=ZONE_DESC.get(key, "예고/크레딧")) \
                + (f"\n\n⚠ 직전 반려: {reject}" if reject else "")
            try:
                resp = call_probe_budgeted(clip, prompt)
                kind, problems = validate_verify_response(resp)
            except Exception as e:  # noqa: BLE001 — 검증 실패 = 유지(보수)
                kind, problems = None, [f"호출 오류: {e}"]
            if kind is not None:
                break
            reject = "; ".join(problems[:2])
        rec["elapsed"] = round(time.time() - t0v, 1)
        if kind == "main":
            new_exception = {k: (dict(v) if isinstance(v, dict) else v)
                             for k, v in new_exception.items()}
            new_exception[key] = None
            audit["moved"] += 1
            rec["result"] = "본편 판정 — zone 폐기"
            log(f"  [v3/refine] {key} 실체 검증: 본편 — 폐기({zs:.1f}~{ze:.1f})")
        else:
            rec["result"] = f"유지({kind or '판정 불가'})"
        audit["probes"].append(rec)

    if audit["moved"] == 0:
        return stage1_doc, audit
    doc = retile_sequences(stage1_doc, new_exception, duration)
    # validate_coverage 는 정규화 표현(초 float)을 받는다 — 문서 표기에서 변환
    norm = {"sequences": [{"start": schemas.parse_ts(q["time"]["start"]),
                           "end": schemas.parse_ts(q["time"]["end"]),
                           "chunks": [{"start": schemas.parse_ts(c["time"]["start"]),
                                       "end": schemas.parse_ts(c["time"]["end"])}
                                      for c in q.get("chunks") or []]}
                          for q in doc["sequences"]],
            "exception_sector": {k: (None if not isinstance(z, dict)
                                     or z.get("start") is None else
                                     {"start": schemas.parse_ts(z["start"]),
                                      "end": schemas.parse_ts(z["end"])})
                                 for k, z in doc["exception_sector"].items()}}
    problems = schemas.validate_coverage(norm, duration)
    if problems:
        # 재타일링이 계약(빈틈·겹침 0)을 못 지키면 정밀 결과를 통째로 버린다 —
        # 원판정 유지가 깨진 문서보다 낫다(오염 방지 비대칭·리뷰 확정)
        audit["retile_failed"] = problems[:5]
        log(f"  [v3/refine] ⚠ 재타일링 커버리지 위반 {len(problems)}건 — 원판정 유지")
        return stage1_doc, audit
    return doc, audit
