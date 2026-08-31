"""Stage 1 — 전체 간단 분석(seq_analyze). Pro 1회가 경계를 **제안**하고 코드가 확정한다.

기획서 §3 그대로: 초저fps 프록시 전체 + research + 격자 요약 + 휴리스틱 후보(사전
힌트) → sequences · chunks(>10분 sequence 만 분할) · exception_sector(intro/recap/
teaser/credit/end, null 허용) 제안 → 코드가 격자 스냅(오차 >2s 반려·재질의 ≤2회) ·
커버리지 검증(러닝타임 = sequences ∪ exception, 빈틈·겹침 0) · sequence 당 chunk ≥1.

모델 규칙: 영상을 실제로 보는 호출 = **Pro 슬롯**(GeminiConfig.model_name — CLAUDE.md
모델 표의 두 번째 Pro 호출이 된다. provenance roles 는 v3 pipeline 이 별도 기록).
temperature=0.0 명시 — 합격 기준의 결정성 조항("temperature 0 기준 경계 변동이 스냅
격자 안에서만")이 이 호출 하나에 걸려 있다. 업로드-폴링-삭제는 analyze_chunk 의
인라인 패턴(:1708~1737)을 함수로 추출해 재사용한다(공용 헬퍼 부재 실측).

토큰 예산: 67분 소재 = 0.5fps 표본 ×2,032 프레임 ×(LOW 해상도 ≈70tok) + 오디오
≈13만+13만 — 1M 컨텍스트의 1/3 이하. 2시간+ 영화가 넘치면 발주서 멈춤 시점 2.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from app.modules.ffmpeg_utils import find_ffmpeg_command
from app.modules.gemini_client import (
    _extract_json_from_markdown,
    _loads_first_json,
    _max_tokens_usage,
    _safe_upload_path,
)
from app.modules.intro_credits_detector import detect_exclusion_zones
from app.modules.speech import SpeechSegment
from app.v3 import schemas
from app.v3.timegrid import grid_snap_times

SCAN_PROXY_HEIGHT = 480          # 전체 훑기용(2026-08-31 사용자 설정: 360→480)
SCAN_PROXY_FILE_FPS = 10.0       # 파일 자체 fps(2026-08-31: 1→10 — 표본 프레임의 시각 정밀)
SCAN_SAMPLE_FPS = 1.0            # Gemini 표본 fps(video_metadata · 2026-08-31: 0.5→1)
MAX_REASKS = 2                   # 반려·재질의 상한(기획서 §3)
BOUNDARY_CLUSTER_EPS = 1.0       # 인접 구간이 공유해야 할 경계의 허용 어긋남(스냅 전 정준화)
PROMPT_SCENE_CUTS_CAP = 1500     # 프롬프트에 실을 장면 전환 어휘 상한(넘으면 결정적 솎음)
NO_DIALOGUE_NOTABLE_SEC = 30.0   # 격자 요약에 적을 무발화 구간 하한


def build_scan_proxy(video_path: Path, out_path: Path, *, log=print) -> Path:
    """전체 훑기용 초저fps 프록시 — 기존 480p 프록시 인자(pipeline [4/15])를 낮춘 변형.

    오디오는 유지한다(모노 22050 — 기존 프록시와 같은 인자): intro/teaser 판정에
    음악·톤 변화가 실질 단서다."""
    if out_path.exists():
        log(f"  [v3/scan-proxy] 재사용: {out_path.name}")
        return out_path
    ffmpeg = find_ffmpeg_command("ffmpeg")
    cmd = [ffmpeg, "-y", "-i", str(Path(video_path).resolve()),
           "-vf", f"scale=-2:{SCAN_PROXY_HEIGHT},fps={SCAN_PROXY_FILE_FPS}",
           "-fps_mode", "cfr",
           "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
           "-c:a", "aac", "-ac", "1", "-ar", "22050",
           "-threads", "4", str(out_path)]
    subprocess.run(cmd, check=True, capture_output=True)
    return out_path


# ── 격자 요약 · 휴리스틱 힌트 ───────────────────────────────────────────────

def summarize_grid(grid: dict) -> str:
    """Pro 프롬프트에 실을 격자 요약 — 경계 어휘(장면 전환)와 발화 분포."""
    dur = float(grid["source"]["duration_sec"])
    spans = grid["span_candidates"]
    voiced = [s for s in spans if s["is_audio"]]
    talk = sum(s["t_out"] - s["t_in"] for s in voiced)
    lines = [
        f"러닝타임: {schemas.format_ts(dur)} ({dur:.1f}초)",
        f"발화 span {len(voiced)}개 · 무성 span {len(spans) - len(voiced)}개 · "
        f"발화 커버리지 {talk / dur * 100:.0f}%",
    ]
    if voiced:
        lines.append(f"첫 발화 {schemas.format_ts(voiced[0]['t_in'])} · "
                     f"마지막 발화 {schemas.format_ts(voiced[-1]['t_out'])}")
    gaps = []
    prev = 0.0
    for s in voiced:
        if s["t_in"] - prev >= NO_DIALOGUE_NOTABLE_SEC:
            gaps.append((prev, s["t_in"]))
        prev = max(prev, s["t_out"])
    if dur - prev >= NO_DIALOGUE_NOTABLE_SEC:
        gaps.append((prev, dur))
    if gaps:
        lines.append("무발화 30초+ 구간: " + ", ".join(
            f"{schemas.format_ts(a)}~{schemas.format_ts(b)}" for a, b in gaps[:20]))

    cuts = [float(t) for t in grid["scene_cuts"]]
    if len(cuts) > PROMPT_SCENE_CUTS_CAP:
        step = len(cuts) / PROMPT_SCENE_CUTS_CAP
        cuts = [cuts[int(i * step)] for i in range(PROMPT_SCENE_CUTS_CAP)]
    lines.append(f"장면 전환 {len(grid['scene_cuts'])}개 — 경계는 반드시 아래 시각(초) "
                 "중 하나 근처(±2초)로 제안하라:")
    lines.append(", ".join(f"{t:.1f}" for t in cuts))
    return "\n".join(lines)


def heuristic_hints(grid: dict) -> dict:
    """intro_credits_detector 휴리스틱 → 사전 후보(프롬프트 첨부 + 사후 대조용).

    기존 detect_exclusion_zones 는 앞/뒤 2-zone 모델(kind 어휘 없음 — 실측)이라
    intro·credit 후보까지만 나온다. recap/teaser/end 는 모델 판정 몫."""
    dur = float(grid["source"]["duration_sec"])
    segs = [SpeechSegment(start_sec=s["t_in"], end_sec=s["t_out"], text=s["text"])
            for s in grid["span_candidates"] if s["is_audio"]]
    z = detect_exclusion_zones(dur, srt_segments=segs or None, auto_detect=True)
    out: dict[str, Any] = {"method": z.detection_method, "confidence": z.confidence}
    if z.intro_end_sec > 0:
        out["intro"] = {"start": 0.0, "end": round(z.intro_end_sec, 3)}
    if z.credits_start_sec < dur:
        out["credit"] = {"start": round(z.credits_start_sec, 3), "end": round(dur, 3)}
    return out


PROMPT_TEMPLATE = """당신은 방송 영상의 구조 분석가다. 첨부한 영상 전체(초저fps 프록시)를 훑고, 아래 격자 요약을 참고해 **구조만** 잡아라. 장면의 재미 판단은 하지 않는다.

{research_block}## 격자 요약
{grid_summary}

## 휴리스틱 사전 후보 (참고용 — 코드 휴리스틱이 자막 텍스트로 추정한 값이다. 화면과 다르면 화면이 정답이다)
{hints_block}

## 과제
1. **sequences** — 영상을 이야기의 큰 맥락 단위로 나눠라. 각 sequence 에 "누가 무엇을 해서 무슨 일이 있었다" 한 문장(content). 길이 제한 없음.
2. **chunks** — 10분(600초)을 넘는 sequence 만 의미 기준으로 10분 이하 chunk 들로 나눠라. 10분 이하 sequence 는 chunk 하나 = sequence 전체.
3. **exception_sector** — 본편이 아닌 구간: intro(타이틀 시퀀스)·recap(지난 화 요약)·teaser(다음 화 예고)·credit(엔딩 크레딧/스태프롤)·end(방송사 종료 화면 등 크레딧 이후 꼬리). 없는 항목은 null.
   **teaser 판별 신호** (M7 — 가왕쇼 6화 실사고 실측: 예고 시작을 50초 늦게 잡아 쇼츠 엔딩이 예고로 오염):
   - 화면에 콜라주/장식 프레임 테두리, 스태프롤·제작진 자막 병행, "다음 이야기/다음 화" 문구, 본편 흐름과 단절된 빠른 몽타주(장소·의상이 컷마다 바뀜)가 보이면 예고다.
   - ⚠ 예고의 **시작은 장식 프레임이 뜨는 순간이 아니다** — 본편 서사가 끝난 뒤 예고 소재(다른 날/다른 장소 장면의 나열)가 시작되는 **첫 컷**이다. 예고 몽타주는 종종 본편처럼 보이는 하이라이트 컷으로 문을 연다.
   - 말미 3분 안에서 위 신호가 보이면 그 몽타주 **전체**를 teaser 로 잡아라. 경계가 불확실하면 **이른 쪽**(본편을 덜 남기는 쪽)을 골라라 — 본편에 예고가 새어 들어가는 것이 예고를 조금 잘라내는 것보다 훨씬 나쁘다.

## 규칙 (위반하면 반려된다)
- 모든 시각은 "HH:MM:SS.mmm". 경계는 격자 요약의 장면 전환 시각 근처(±2초)로만.
- sequences 와 exception_sector 의 구간을 **전부 합치면 러닝타임 전체**가 되어야 한다 — 빈틈 0, 겹침 0. exception 구간은 sequence 에 넣지 마라.
- 인접 구간의 끝과 시작은 **같은 시각**으로 써라.
- chunks 는 자기 sequence 구간을 정확히 타일링한다(첫 chunk 시작 = sequence 시작, 마지막 chunk 끝 = sequence 끝).
- 시각을 지어내지 마라 — 확신 없으면 가장 가까운 장면 전환 시각을 써라.
{reject_block}
## 출력 (JSON 만)
{{
  "sequences": [
    {{"number": 0, "time": {{"start": "00:00:00.000", "end": "00:12:12.000"}},
      "content": "…", "chunks": [{{"number": 0, "time": {{"start": "00:00:00.000", "end": "00:12:12.000"}}}}]}}
  ],
  "exception_sector": {{"intro": null, "recap": null,
    "teaser": {{"start": "…", "end": "…"}}, "credit": {{"start": "…", "end": "…"}}, "end": null}}
}}"""


def build_prompt(grid: dict, *, research_context: str = "",
                 hints: dict | None = None, reject_note: str = "") -> str:
    research_block = ""
    if research_context:
        research_block = f"## 작품 배경 (리서치)\n{research_context.strip()[:2000]}\n\n"
    hints = hints or {}
    hint_lines = []
    for k in ("intro", "credit"):
        if k in hints:
            hint_lines.append(f"- {k} 후보: {schemas.format_ts(hints[k]['start'])} ~ "
                              f"{schemas.format_ts(hints[k]['end'])}")
    hints_block = "\n".join(hint_lines) if hint_lines else "- (휴리스틱 후보 없음)"
    reject_block = ""
    if reject_note:
        reject_block = f"\n## ⚠ 직전 제안 반려 사유 — 전부 고쳐서 다시 내라\n{reject_note}\n"
    return PROMPT_TEMPLATE.format(research_block=research_block,
                                  grid_summary=summarize_grid(grid),
                                  hints_block=hints_block,
                                  reject_block=reject_block)


# ── 스냅·정준화 ─────────────────────────────────────────────────────────────

def snap_stage1(norm: dict, grid_times: list[float], duration_sec: float,
                tolerance: float = schemas.SNAP_TOLERANCE_SEC) -> tuple[dict, list[dict]]:
    """정규화된 제안의 **모든 경계**를 격자에 스냅 → (스냅본, 실패 목록).

    인접 구간이 1초 안에서 어긋나게 낸 경계는 한 점으로 정준화한 뒤 스냅한다 —
    구간별 각자 스냅이 만들어내는 인공 빈틈을 없앤다. 0·러닝타임 끝은 항상
    자기 자신으로 스냅(격자에 이미 있다)."""
    endpoints: list[float] = []
    for sq in norm["sequences"]:
        endpoints += [sq["start"], sq["end"]]
        for c in sq["chunks"]:
            endpoints += [c["start"], c["end"]]
    for v in norm["exception_sector"].values():
        if v is not None:
            endpoints += [v["start"], v["end"]]

    uniq = sorted(set(round(e, 3) for e in endpoints))
    clusters: list[list[float]] = []
    for e in uniq:
        if clusters and e - clusters[-1][0] <= BOUNDARY_CLUSTER_EPS:
            clusters[-1].append(e)
        else:
            clusters.append([e])
    rep_of: dict[float, float] = {}
    for cl in clusters:
        rep = cl[len(cl) // 2]                     # 중앙값(결정적)
        for e in cl:
            rep_of[e] = rep

    snapped_of: dict[float, float] = {}
    failures: list[dict] = []
    for rep in sorted(set(rep_of.values())):
        s, err = schemas.snap_time(rep, grid_times, tolerance)
        if s is None:
            failures.append({"boundary_sec": rep, "boundary_ts": schemas.format_ts(rep),
                             "nearest_err": round(err, 3)})
            snapped_of[rep] = rep
        else:
            snapped_of[rep] = s

    def fix(v: float) -> float:
        return snapped_of[rep_of[round(v, 3)]]

    out = {"sequences": [], "exception_sector": {}}
    for sq in norm["sequences"]:
        s, e = fix(sq["start"]), fix(sq["end"])
        chunks = [{"start": fix(c["start"]), "end": fix(c["end"])} for c in sq["chunks"]]
        if chunks:                                  # chunk 겉경계는 부모와 강제 일치
            chunks[0]["start"] = s
            chunks[-1]["end"] = e
        out["sequences"].append({"content": sq["content"], "start": s, "end": e,
                                 "chunks": [c for c in chunks if c["end"] > c["start"]]})
    for k, v in norm["exception_sector"].items():
        out["exception_sector"][k] = None if v is None else {
            "start": fix(v["start"]), "end": fix(v["end"])}
    # 스냅·정준화 후 0길이로 붕괴한 구간은 **전부** 실패로 기록한다 — sequence 만 보면
    # 1초 미만 exception(정준화 클러스터 1.0s 가 반드시 붕괴시킨다)이 무기록으로
    # stage1.json 에 start==end 로 실리고, 중간 붕괴는 커버리지 오진을 만든다(리뷰 재현).
    def _collapsed(name: str, s: float, e: float) -> None:
        if e <= s:
            failures.append({"boundary_sec": s, "boundary_ts": schemas.format_ts(s),
                             "nearest_err": 0.0,
                             "reason": f"{name} 스냅/정준화 후 구간 소멸 — 1초 미만 "
                                       "구간은 넓히거나 빼고 다시 제안하라"})
    for i, sq in enumerate(out["sequences"]):
        _collapsed(f"sequence[{i}]", sq["start"], sq["end"])
        for j, c in enumerate(sq["chunks"]):
            _collapsed(f"sequence[{i}].chunk[{j}]", c["start"], c["end"])
    for k, v in out["exception_sector"].items():
        if v is not None:
            _collapsed(f"exception.{k}", v["start"], v["end"])
    return out, failures


def normalize_chunks(norm: dict, grid_times: list[float], *,
                     allow_fallback: bool) -> tuple[list[str], list[str]]:
    """chunk 보장(코드 몫) → (보정 내역, 반려 사유).

    - chunk 없는 ≤10분 sequence: chunk 1개 = sequence 전체로 조용히 채운다(기획서
      §3 명문 — 모델에게 다시 물을 일이 아니다).
    - >10분 sequence 의 chunk 부재/상한 위반: **의미 기준 분할은 모델 몫**이라
      반려 사유로 돌려보낸다. 재질의 소진(allow_fallback=True) 시에만 등분점에서
      가장 가까운 격자 눈금으로 코드 분할(결정적 폴백 — run_log 에 남는다)."""
    notes: list[str] = []
    problems: list[str] = []
    for i, sq in enumerate(norm["sequences"]):
        dur = sq["end"] - sq["start"]
        ok = bool(sq["chunks"]) and all(
            c["end"] - c["start"] <= schemas.CHUNK_MAX_SEC + schemas.COVERAGE_EPS_SEC
            for c in sq["chunks"])
        if ok:
            continue
        if dur <= schemas.CHUNK_MAX_SEC:
            sq["chunks"] = [{"start": sq["start"], "end": sq["end"]}]
            notes.append(f"sequence[{i}] chunk 1개 = 전체로 자동 채움")
            continue
        if not allow_fallback:
            problems.append(
                f"sequence[{i}] ({dur:.0f}s > 600s)의 chunk 분할이 없거나 10분 상한 위반 — "
                "의미 기준으로 10분 이하 chunk 들을 제안하라")
            continue
        # 등분점을 격자 눈금으로 당기되 **단조·10분 상한을 지키는 후보만** 고른다 —
        # 무제약 최근접은 성긴 격자에서 600s 초과·겹침 chunk 를 만들고, 그 위반이
        # 마지막 시도의 커버리지 검증에 걸려 폴백 자신이 Stage 1 을 죽인다(리뷰 재현).
        # 조건에 맞는 눈금이 없으면 등분 원값을 쓴다(수학적으로 항상 상한 안 —
        # dur/n < 600) — 그 경계는 격자 밖이므로 노트로 크게 남긴다.
        n = int(dur // schemas.CHUNK_MAX_SEC) + 1
        bounds = [sq["start"]]
        inner = [t for t in grid_times if sq["start"] < t < sq["end"]]
        off_grid = 0
        for j in range(1, n):
            target = sq["start"] + dur * j / n
            remaining = n - j                      # 이 경계 뒤에 남을 chunk 수
            feasible = [
                t for t in inner
                if bounds[-1] < t < sq["end"]
                and t - bounds[-1] <= schemas.CHUNK_MAX_SEC
                and sq["end"] - t <= remaining * schemas.CHUNK_MAX_SEC]
            if feasible:
                cand = min(feasible, key=lambda t: (abs(t - target), t))
            else:
                cand = target
                off_grid += 1
            bounds.append(cand)
        bounds.append(sq["end"])
        if any(b <= a for a, b in zip(bounds, bounds[1:])):
            # 혼합 선택이 단조를 깨는 극단 케이스 — 전부 등분 원값으로(항상 유효)
            bounds = [sq["start"] + dur * j / n for j in range(n)] + [sq["end"]]
            off_grid = n - 1
        sq["chunks"] = [{"start": a, "end": b} for a, b in zip(bounds, bounds[1:])
                        if b > a]
        note = (f"sequence[{i}] {dur:.0f}s — 재질의 소진 → 격자 눈금 등분 "
                f"{len(sq['chunks'])}개 코드 분할(폴백)")
        if off_grid:
            note += f" · ⚠ 조건 맞는 눈금 부족으로 등분 원값 경계 {off_grid}개(격자 밖)"
        notes.append(note)
    return notes, problems


def hint_mismatch(hints: dict, final_exc: dict) -> list[dict]:
    """휴리스틱 후보 vs 모델 확정의 불일치 목록(검수 신호 — run_log 용)."""
    out = []
    for k in ("intro", "credit"):
        h = hints.get(k)
        f = final_exc.get(k)
        if h is None and f is None:
            continue
        if h is None or f is None:
            out.append({"key": k, "heuristic": h, "final": f, "kind": "존재 불일치"})
            continue
        ds = abs(h["start"] - f["start"])
        de = abs(h["end"] - f["end"])
        if max(ds, de) > schemas.SNAP_TOLERANCE_SEC:
            out.append({"key": k, "heuristic": h, "final": f, "kind": "경계 불일치",
                        "delta": round(max(ds, de), 3)})
    return out


# ── Gemini 호출 ─────────────────────────────────────────────────────────────

def _upload_video(gemini, video_path: Path, *, log=print):
    """Files API 업로드 + PROCESSING 폴링 — analyze_chunk 인라인 패턴의 추출판."""
    safe_path, is_temp = _safe_upload_path(Path(video_path))
    try:
        last_err: Exception | None = None
        for attempt in range(gemini.config.max_retries):
            try:
                uploaded = gemini.client.files.upload(file=str(safe_path))
                while uploaded.state.name == "PROCESSING":
                    time.sleep(2)
                    uploaded = gemini.client.files.get(name=uploaded.name)
                if uploaded.state.name == "FAILED":
                    raise RuntimeError("Files API 처리 실패(FAILED)")
                return uploaded
            except Exception as e:  # noqa: BLE001
                last_err = e
                log(f"  [v3/stage1] 업로드 재시도 {attempt + 1}: {e}")
                time.sleep(2 ** attempt)
        raise RuntimeError(f"스캔 프록시 업로드 실패: {last_err}")
    finally:
        if is_temp:
            safe_path.unlink(missing_ok=True)


def _call_model(gemini, uploaded, prompt: str) -> dict:
    types = gemini.types
    part = types.Part(
        file_data=types.FileData(file_uri=uploaded.uri, mime_type="video/mp4"),
        video_metadata=types.VideoMetadata(fps=SCAN_SAMPLE_FPS))
    response = gemini.client.models.generate_content(
        model=gemini.config.model_name,          # Pro — 영상을 실제로 보는 호출
        contents=[part, prompt],
        config=types.GenerateContentConfig(
            temperature=0.0,
            response_mime_type="application/json",
            max_output_tokens=65536,
            media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
            thinking_config=types.ThinkingConfig(
                thinking_level=gemini.config.analysis_thinking_level),
        ))
    truncated = _max_tokens_usage(response)
    text = _extract_json_from_markdown(response.text or "")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        try:
            obj, _rest = _loads_first_json(text)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            return obj
        # 파싱 실패는 이 레포 실측의 상시 모드(analyze_chunk 22회 중 12회) — 크래시가
        # 아니라 ValueError 로 올려 재질의 루프의 반려 재료가 되게 한다(리뷰 재현 수정).
        raise ValueError(
            "응답 JSON 파싱 실패"
            + (f" (MAX_TOKENS 절단: {truncated})" if truncated else "")
            + f": {e} — 앞 200자: {text[:200]!r}") from e


def run_seq_analyze(gemini, scan_proxy: Path, grid: dict, *,
                    research_context: str = "", log=print) -> tuple[dict, dict]:
    """Stage 1 실행 → (stage1 doc, 감사 기록). 재질의 소진 시 RuntimeError(조용한
    통과 금지 — 933 방어가 이 관문이다)."""
    duration = float(grid["source"]["duration_sec"])
    grid_times = grid_snap_times(grid)
    hints = heuristic_hints(grid)
    audit: dict[str, Any] = {"attempts": [], "heuristic_hints": hints}

    uploaded = _upload_video(gemini, scan_proxy, log=log)
    try:
        reject_note = ""
        for attempt in range(1 + MAX_REASKS):
            prompt = build_prompt(grid, research_context=research_context,
                                  hints=hints, reject_note=reject_note)
            log(f"  [v3/stage1] Pro 제안 요청 (시도 {attempt + 1}/{1 + MAX_REASKS})")
            problems: list[str] = []
            try:
                raw = _call_model(gemini, uploaded, prompt)
                norm = schemas.normalize_stage1_response(raw)
            except ValueError as e:
                # 파싱 실패·구조 오류 둘 다 반려 재료다 — 루프 밖으로 새면 Pro 비용을
                # 치른 뒤 재질의 0회로 즉사한다(리뷰 재현).
                problems.append(f"응답 오류: {e}")
                audit["attempts"].append({"attempt": attempt + 1, "problems": problems})
                reject_note = ("- 직전 응답이 유효한 JSON/스키마가 아니었다. 지시한 "
                               "JSON 형식만, 다른 텍스트 없이 출력하라.\n"
                               + "\n".join(f"- {p}" for p in problems))
                continue

            snapped, snap_failures = snap_stage1(norm, grid_times, duration)
            fill_notes, chunk_problems = normalize_chunks(
                snapped, grid_times, allow_fallback=(attempt == MAX_REASKS))
            coverage = schemas.validate_coverage(snapped, duration)
            problems += [f"격자 스냅 실패(±{schemas.SNAP_TOLERANCE_SEC}s 밖): "
                         f"{f['boundary_ts']} (가장 가까운 눈금과 {f['nearest_err']}s)"
                         + (f" — {f['reason']}" if f.get("reason") else "")
                         for f in snap_failures]
            problems += chunk_problems
            problems += coverage
            audit["attempts"].append({
                "attempt": attempt + 1,
                "sequences": len(snapped["sequences"]),
                "snap_failures": snap_failures,
                "coverage_problems": coverage,
                "chunk_fill_notes": fill_notes,
                "problems": problems,
            })
            if not problems:
                doc = schemas.to_stage1_doc(snapped)
                audit["heuristic_mismatch"] = hint_mismatch(
                    hints, snapped["exception_sector"])
                return doc, audit
            log(f"  [v3/stage1] 반려 — 사유 {len(problems)}건")
            reject_note = "\n".join(f"- {p}" for p in problems[:20])
        raise RuntimeError(
            f"Stage 1 반려 {1 + MAX_REASKS}회 소진 — 마지막 사유: "
            + "; ".join(audit["attempts"][-1]["problems"][:5]))
    finally:
        try:
            gemini.client.files.delete(name=uploaded.name)
        except Exception as e:  # noqa: BLE001
            log(f"  [v3/stage1] WARN 서버 파일 삭제 실패: {e}")
